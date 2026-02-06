# ros_tb4_bridge.py
# ==========================================================
# TurtleBot4 Multi-Robot Bridge
# - 여러 로봇 namespace(/robot2, /robot6, ...)를 동시에 subscribe
# - 상태를 shared_state에 로봇별로 누적
# - (선택) SSE(EventBus)로 UI에 push
#
# 추가 기능(🔥화재 트리거):
# - std_msgs/String JSON 토픽을 받아 dict 값(list) 중
#   하나라도 길이>0이면 fire_active=True
#   모두 빈 리스트면 fire_active=False
# ==========================================================

import json
import math
import threading
import time
from typing import Dict, List, Optional, Set

import rclpy
from rclpy.node import Node
from rclpy.executors import MultiThreadedExecutor

from sensor_msgs.msg import BatteryState
from nav_msgs.msg import Odometry
from geometry_msgs.msg import PoseWithCovarianceStamped
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String  # ✅ 화재 토픽(JSON)

import base64


# ==========================================================
# Shared state
# ==========================================================
_state_lock = threading.Lock()

shared_state: Dict = {
    "robots": {},
    "bridge_running": False,
    "active_namespaces": [],

    # 🔥 fire trigger (JSON-based)
    "fire_active": False,
    "fire_last_ts": 0.0,
    "fire_payload": None,   # 마지막 파싱 결과(또는 raw)
}


def _normalize_ns(ns: str) -> str:
    ns = (ns or "").strip()
    if not ns:
        return ""
    if not ns.startswith("/"):
        ns = "/" + ns
    return ns.rstrip("/")


def _init_robot_state(ns: str) -> Dict:
    return {
        "robot_ns": ns,
        "connected": False,
        "last_seen_ts": 0.0,

        "battery_percent": 0,
        "battery_voltage": None,
        "battery_current": None,

        "pose_frame": "map",
        "x": 0.0,
        "y": 0.0,
        "yaw_deg": 0.0,

        "lin_vel": 0.0,
        "ang_vel": 0.0,

        "camera_topic": None,
        "camera_last_ts": 0.0,
        "camera_format": None,
        "camera_size": 0,
        "camera_b64": None,
        "camera_bytes": None,
    }


def _get_robot_state(ns: str) -> Dict:
    ns = _normalize_ns(ns)
    with _state_lock:
        robots = shared_state["robots"]
        if ns not in robots:
            robots[ns] = _init_robot_state(ns)
        return robots[ns]


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


def _quat_to_yaw_deg(q) -> float:
    siny_cosp = 2.0 * (q.w * q.z + q.x * q.y)
    cosy_cosp = 1.0 - 2.0 * (q.y * q.y + q.z * q.z)
    yaw = math.atan2(siny_cosp, cosy_cosp)
    return float(yaw * 180.0 / math.pi)


def _touch_connected(ns: str):
    now = time.time()
    st = _get_robot_state(ns)
    with _state_lock:
        st["connected"] = True
        st["last_seen_ts"] = now


# ==========================================================
# SSE EventBus
# ==========================================================
class EventBus:
    def __init__(self, max_events: int = 300):
        self._cond = threading.Condition()
        self._events: List[Dict] = []
        self._max = max_events

    def publish(self, ev: Dict):
        with self._cond:
            self._events.append(ev)
            if len(self._events) > self._max:
                self._events = self._events[-self._max:]
            self._cond.notify_all()

    def stream(self, last_idx: int = 0):
        idx = last_idx
        while True:
            with self._cond:
                while idx >= len(self._events):
                    self._cond.wait(timeout=10.0)
                    if idx >= len(self._events):
                        yield "event: ping\ndata: {}\n\n"

                ev = self._events[idx]
                idx += 1

            yield f"event: tb4\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"


event_bus = EventBus()


# ==========================================================
# 🔥 Fire Trigger Node (GLOBAL, only one)
# ==========================================================
class FireTriggerBridge(Node):
    """
    JSON 문자열 토픽 예:
    {
        "class_a_detection": ["stand", "fire"],
        "class_b_detection": ["fire"],
        "class_c_detection": ["down"]
    }

    규칙:
    - dict 안의 값이 list이고, list 길이가 1 이상인 항목이 하나라도 있으면 fire_active=True
    - 전부 빈 리스트면 fire_active=False
    """

    def __init__(self, topic: str):
        super().__init__("fire_trigger_bridge")
        self.topic = topic
        self.create_subscription(String, self.topic, self._on_msg, 10)
        self.get_logger().info(f"[FIRE] Subscribing: {self.topic}")

    @staticmethod
    def _calc_fire_active(obj) -> bool:
        if not isinstance(obj, dict):
            return False
        for _, v in obj.items():
            if isinstance(v, list) and len(v) > 0:
                return True
        return False

    def _on_msg(self, msg: String):
        now = time.time()
        raw = msg.data

        parsed = None
        fire = False

        try:
            parsed = json.loads(raw)
            fire = self._calc_fire_active(parsed)
        except Exception:
            # JSON 파싱 실패면 안전하게 false
            fire = False

        with _state_lock:
            shared_state["fire_active"] = bool(fire)
            shared_state["fire_last_ts"] = now
            shared_state["fire_payload"] = parsed if parsed is not None else raw

        # (선택) SSE push
        event_bus.publish({
            "ts": now,
            "type": "fire",
            "fire_active": bool(fire),
        })


# ==========================================================
# ROS Node (로봇 1대당 1개)
# ==========================================================
class Turtlebot4Bridge(Node):
    def __init__(self, ns: str):
        safe_name = ns.strip("/").replace("/", "_")
        super().__init__(f"tb4_ui_bridge_{safe_name}")

        self.ns = _normalize_ns(ns)

        self.topic_battery = f"{self.ns}/battery_state"
        self.topic_amcl    = f"{self.ns}/amcl_pose"
        self.topic_odom    = f"{self.ns}/odom"
        self.topic_cam     = f"{self.ns}/oakd/rgb/image_raw/compressed"

        self.create_subscription(BatteryState, self.topic_battery, self._on_battery, 10)
        self.create_subscription(PoseWithCovarianceStamped, self.topic_amcl, self._on_amcl, 10)
        self.create_subscription(Odometry, self.topic_odom, self._on_odom, 10)
        self.create_subscription(CompressedImage, self.topic_cam, self._on_cam, 10)

        self._cam_min_period = 0.2  # 5Hz
        self._cam_last_pub_ts = 0.0

        self.get_logger().info(
            f"[TB4:{self.ns}] Subscribing: {self.topic_battery}, {self.topic_amcl}, {self.topic_odom}, {self.topic_cam}"
        )

        st = _get_robot_state(self.ns)
        with _state_lock:
            st["camera_topic"] = self.topic_cam

        self.create_timer(1.0, self._watchdog)
        _get_robot_state(self.ns)

    def _on_battery(self, msg: BatteryState):
        _touch_connected(self.ns)
        st = _get_robot_state(self.ns)

        p = msg.percentage
        percent = 0
        try:
            if p is not None and p == p and p >= 0.0:
                percent = int(round(_clamp01(float(p)) * 100.0))
        except Exception:
            percent = 0

        with _state_lock:
            st["battery_percent"] = percent
            st["battery_voltage"] = float(msg.voltage) if msg.voltage == msg.voltage else None
            st["battery_current"] = float(msg.current) if msg.current == msg.current else None

        event_bus.publish({
            "ts": time.time(),
            "ns": self.ns,
            "type": "battery",
            "battery_percent": percent,
        })

    def _on_amcl(self, msg: PoseWithCovarianceStamped):
        _touch_connected(self.ns)
        st = _get_robot_state(self.ns)

        pose = msg.pose.pose
        yaw_deg = _quat_to_yaw_deg(pose.orientation)

        with _state_lock:
            st["pose_frame"] = "map"
            st["x"] = float(pose.position.x)
            st["y"] = float(pose.position.y)
            st["yaw_deg"] = yaw_deg

        event_bus.publish({
            "ts": time.time(),
            "ns": self.ns,
            "type": "pose",
            "frame": "map",
            "x": st["x"],
            "y": st["y"],
            "yaw_deg": yaw_deg,
        })

    def _on_odom(self, msg: Odometry):
        _touch_connected(self.ns)
        st = _get_robot_state(self.ns)

        lin = float(msg.twist.twist.linear.x)
        ang = float(msg.twist.twist.angular.z)

        with _state_lock:
            st["lin_vel"] = lin
            st["ang_vel"] = ang

    def _watchdog(self):
        st = _get_robot_state(self.ns)
        now = time.time()
        with _state_lock:
            last = float(st["last_seen_ts"])
            if last > 0.0 and (now - last) > 3.0:
                st["connected"] = False

    def _on_cam(self, msg: CompressedImage):
        _touch_connected(self.ns)
        now = time.time()

        if (now - self._cam_last_pub_ts) < self._cam_min_period:
            return
        self._cam_last_pub_ts = now

        st = _get_robot_state(self.ns)

        fmt = (msg.format or "").lower()
        if "jpeg" in fmt:
            fmt_norm = "jpeg"
        elif "png" in fmt:
            fmt_norm = "png"
        else:
            fmt_norm = fmt[:20] if fmt else None

        data_bytes = bytes(msg.data)
        size = len(data_bytes)

        with _state_lock:
            st["camera_last_ts"] = now
            st["camera_format"] = fmt_norm
            st["camera_size"] = size
            st["camera_bytes"] = data_bytes

        PUSH_IMAGE_B64 = False
        if PUSH_IMAGE_B64:
            b64 = base64.b64encode(data_bytes).decode("ascii")
            with _state_lock:
                st["camera_b64"] = b64

        event_bus.publish({
            "ts": now,
            "ns": self.ns,
            "type": "camera",
            "format": fmt_norm,
            "size": size,
            "has_b64": bool(PUSH_IMAGE_B64),
        })


# ==========================================================
# Executor/Thread 관리
# ==========================================================
_executor: Optional[MultiThreadedExecutor] = None
_spin_thread: Optional[threading.Thread] = None
_spin_stop_evt = threading.Event()

_nodes: Dict[str, Turtlebot4Bridge] = {}
_nodes_lock = threading.Lock()

# 🔥 fire node holder
_fire_node: Optional[FireTriggerBridge] = None

# 🔥 화재 토픽명 (네 실제 토픽명으로 바꿔)
FIRE_TOPIC = "/fire_detection"


def _spin_loop():
    global _executor
    assert _executor is not None

    with _state_lock:
        shared_state["bridge_running"] = True

    try:
        while rclpy.ok() and not _spin_stop_evt.is_set():
            _executor.spin_once(timeout_sec=0.1)
    finally:
        with _state_lock:
            shared_state["bridge_running"] = False


def start_bridge_system(initial_namespaces: Optional[List[str]] = None):
    global _executor, _spin_thread, _fire_node

    if _spin_thread and _spin_thread.is_alive():
        return

    if not rclpy.ok():
        rclpy.init()

    _executor = MultiThreadedExecutor(num_threads=4)
    _spin_stop_evt.clear()

    # ✅ 전역 fire trigger 노드 1개 등록
    _fire_node = FireTriggerBridge(topic=FIRE_TOPIC)
    _executor.add_node(_fire_node)

    if initial_namespaces:
        set_robot_namespaces(initial_namespaces)

    _spin_thread = threading.Thread(target=_spin_loop, daemon=True)
    _spin_thread.start()


def stop_bridge_system(join_timeout: float = 2.0):
    global _executor, _spin_thread, _fire_node

    _spin_stop_evt.set()

    t = _spin_thread
    if t and t.is_alive():
        t.join(timeout=join_timeout)

    with _nodes_lock:
        nss = list(_nodes.keys())

    for ns in nss:
        _remove_node(ns)

    # ✅ fire node 정리
    if _executor is not None and _fire_node is not None:
        try:
            _executor.remove_node(_fire_node)
        except Exception:
            pass
        try:
            _fire_node.destroy_node()
        except Exception:
            pass
    _fire_node = None

    _executor = None
    _spin_thread = None

    try:
        if rclpy.ok():
            rclpy.shutdown()
    except Exception:
        pass

    with _state_lock:
        shared_state["bridge_running"] = False
        shared_state["active_namespaces"] = []


def _add_node(ns: str):
    global _executor
    ns = _normalize_ns(ns)
    if not ns:
        return
    if _executor is None:
        raise RuntimeError("Bridge system not started. Call start_bridge_system() first.")

    with _nodes_lock:
        if ns in _nodes:
            return
        node = Turtlebot4Bridge(ns)
        _nodes[ns] = node
        _executor.add_node(node)

    with _state_lock:
        active = set(shared_state.get("active_namespaces", []))
        active.add(ns)
        shared_state["active_namespaces"] = sorted(active)


def _remove_node(ns: str):
    global _executor
    ns = _normalize_ns(ns)
    if not ns:
        return
    if _executor is None:
        return

    node = None
    with _nodes_lock:
        node = _nodes.pop(ns, None)

    if node is not None:
        try:
            _executor.remove_node(node)
        except Exception:
            pass
        try:
            node.destroy_node()
        except Exception:
            pass

    with _state_lock:
        active = set(shared_state.get("active_namespaces", []))
        if ns in active:
            active.remove(ns)
        shared_state["active_namespaces"] = sorted(active)


def set_robot_namespaces(namespaces: List[str]):
    desired: Set[str] = set(filter(None, (_normalize_ns(x) for x in (namespaces or []))))

    with _nodes_lock:
        current: Set[str] = set(_nodes.keys())

    for ns in sorted(desired - current):
        _add_node(ns)

    for ns in sorted(current - desired):
        _remove_node(ns)

    with _state_lock:
        shared_state["active_namespaces"] = sorted(desired)


def get_state_snapshot() -> Dict:
    with _state_lock:
        robots_copy = {ns: dict(st) for ns, st in shared_state["robots"].items()}
        snap = {
            "bridge_running": shared_state.get("bridge_running", False),
            "active_namespaces": list(shared_state.get("active_namespaces", [])),
            "robots": robots_copy,

            # 🔥 fire fields
            "fire_active": bool(shared_state.get("fire_active", False)),
            "fire_last_ts": float(shared_state.get("fire_last_ts", 0.0)),
            "fire_payload": shared_state.get("fire_payload", None),
        }
    return snap


if __name__ == "__main__":
    start_bridge_system(initial_namespaces=["/robot2", "/robot6"])
    try:
        while True:
            time.sleep(2.0)
            snap = get_state_snapshot()
            print(json.dumps(snap, ensure_ascii=False, indent=2))
    except KeyboardInterrupt:
        pass
    finally:
        stop_bridge_system()
