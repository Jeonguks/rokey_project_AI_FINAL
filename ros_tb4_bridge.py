# ros_tb4_bridge.py
# ==========================================================
# TurtleBot4 Multi-Robot Bridge
# - 여러 로봇 namespace(/robot2, /robot6, ...)를 동시에 subscribe
# - 상태를 shared_state에 로봇별로 누적
# - (선택) SSE(EventBus)로 UI에 push
#
# 설계 포인트:
# 1) rclpy.init()은 프로세스에서 1회만 호출 (안정성)
# 2) MultiThreadedExecutor로 여러 노드 동시 spin
# 3) Flask thread와 ROS thread 간 데이터 공유는 lock으로 보호
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
from sensor_msgs.msg import CompressedImage  # 확장 대비 (현재 예제에서는 미사용)

import base64


# ==========================================================
# Shared state (Flask가 읽는 전역 상태)
# - robots: { "/robot2": {...}, "/robot6": {...} }
# ==========================================================
_state_lock = threading.Lock()

shared_state: Dict = {
    "robots": {},            # 로봇별 상태 저장
    "bridge_running": False, # ROS executor spin thread 동작 여부
    "active_namespaces": [], # 현재 브릿징 중인 ns 목록(문자열 리스트)
}


def _normalize_ns(ns: str) -> str:
    """
    입력 namespace를 '/robot2' 형태로 정규화
    - 'robot2' -> '/robot2'
    - '/robot2/' -> '/robot2'
    """
    ns = (ns or "").strip()
    if not ns:
        return ""
    if not ns.startswith("/"):
        ns = "/" + ns
    return ns.rstrip("/")


def _init_robot_state(ns: str) -> Dict:
    """로봇 1대에 대한 상태 기본값 생성"""
    return {
        "robot_ns": ns,
        "connected": False,
        "last_seen_ts": 0.0,

        # Battery
        "battery_percent": 0,
        "battery_voltage": None,
        "battery_current": None,

        # Pose
        "pose_frame": "map",   # "map" 또는 "odom"
        "x": 0.0,
        "y": 0.0,
        "yaw_deg": 0.0,

        # Velocity
        "lin_vel": 0.0,
        "ang_vel": 0.0,

        # Camera (Compressed)
        "camera_topic": None,
        "camera_last_ts": 0.0,
        "camera_format": None,     # "jpeg" / "png"
        "camera_size": 0,          # bytes
        "camera_b64": None,        # (옵션) base64 string
        "camera_bytes": None,   # 최신 프레임 bytes (1장)
    }


def _get_robot_state(ns: str) -> Dict:
    """
    로봇별 상태 dict를 반환.
    없으면 생성해서 shared_state["robots"]에 등록.
    """
    ns = _normalize_ns(ns)
    with _state_lock:
        robots = shared_state["robots"]
        if ns not in robots:
            robots[ns] = _init_robot_state(ns)
        return robots[ns]


def _clamp01(v: float) -> float:
    """값을 0~1 범위로 제한"""
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


def _quat_to_yaw_deg(q) -> float:
    """
    Quaternion(x,y,z,w) → yaw(deg)
    yaw = atan2(2(wz + xy), 1 - 2(y^2 + z^2))
    """
    siny_cosp = 2.0 * (q.w * q.z + q.x * q.y)
    cosy_cosp = 1.0 - 2.0 * (q.y * q.y + q.z * q.z)
    yaw = math.atan2(siny_cosp, cosy_cosp)
    return float(yaw * 180.0 / math.pi)


def _touch_connected(ns: str):
    """해당 로봇(ns)이 메시지를 수신했음을 기록"""
    now = time.time()
    st = _get_robot_state(ns)
    with _state_lock:
        st["connected"] = True
        st["last_seen_ts"] = now


# ==========================================================
# SSE EventBus (Flask UI 실시간 갱신용)
# - 하나의 스트림으로도 충분: 이벤트에 ns 태그를 넣어 구분
# ==========================================================
class EventBus:
    """
    Flask에서 Server-Sent Events로 UI에 데이터를 push하기 위한 이벤트 큐
    - publish(ev): ROS 콜백에서 이벤트 추가
    - stream(): Flask endpoint에서 yield
    """

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
        """
        Flask SSE endpoint에서 사용:
        예) return Response(event_bus.stream(), mimetype="text/event-stream")
        """
        idx = last_idx
        while True:
            with self._cond:
                while idx >= len(self._events):
                    self._cond.wait(timeout=10.0)
                    if idx >= len(self._events):
                        # keep-alive ping
                        yield "event: ping\ndata: {}\n\n"

                ev = self._events[idx]
                idx += 1

            yield f"event: tb4\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"


event_bus = EventBus()


# ==========================================================
# ROS Node (로봇 1대당 1개 인스턴스)
# ==========================================================
class Turtlebot4Bridge(Node):
    """
    역할:
    - 해당 ns의 토픽을 subscribe
    - shared_state["robots"][ns] 업데이트
    - (선택) event_bus로 이벤트 발행

    생성 단위:
    - 로봇 1대당 1개 노드
    """

    def __init__(self, ns: str):
        # 노드 이름은 충돌 방지를 위해 ns를 포함
        safe_name = ns.strip("/").replace("/", "_")
        super().__init__(f"tb4_ui_bridge_{safe_name}")

        self.ns = _normalize_ns(ns)

        # 토픽 이름(환경에 따라 바뀌면 여기만 수정)
        self.topic_battery = f"{self.ns}/battery_state"
        self.topic_amcl    = f"{self.ns}/amcl_pose"
        self.topic_odom    = f"{self.ns}/odom"
        self.topic_cam = f"{self.ns}/oakd/rgb/image_raw/compressed"

        # Subscriber
        self.create_subscription(BatteryState, self.topic_battery, self._on_battery, 10)
        self.create_subscription(PoseWithCovarianceStamped, self.topic_amcl, self._on_amcl, 10)
        self.create_subscription(Odometry, self.topic_odom, self._on_odom, 10)
        self.create_subscription(CompressedImage, self.topic_cam, self._on_cam, 10)
        # camera publish throttle (Hz)
        self._cam_min_period = 0.2  # 5Hz
        self._cam_last_pub_ts = 0.0
        self.get_logger().info(
            f"[TB4:{self.ns}] Subscribing: {self.topic_battery}, {self.topic_amcl}, {self.topic_odom}, {self.topic_cam}"
        )
        st = _get_robot_state(self.ns)
        with _state_lock:
            st["camera_topic"] = self.topic_cam


        # watchdog timer: N초 메시지 없으면 disconnected
        self.create_timer(1.0, self._watchdog)

        self.get_logger().info(
            f"[TB4:{self.ns}] Subscribing: {self.topic_battery}, {self.topic_amcl}, {self.topic_odom}"
        )

        # state 미리 생성
        _get_robot_state(self.ns)

    # -------------------------
    # Battery callback
    # -------------------------
    def _on_battery(self, msg: BatteryState):
        _touch_connected(self.ns)
        st = _get_robot_state(self.ns)

        p = msg.percentage
        percent = 0
        try:
            # NaN 체크는 (p == p)
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

    # -------------------------
    # AMCL pose callback
    # -------------------------
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

    # -------------------------
    # Odometry callback (velocity)
    # -------------------------
    def _on_odom(self, msg: Odometry):
        _touch_connected(self.ns)
        st = _get_robot_state(self.ns)

        lin = float(msg.twist.twist.linear.x)
        ang = float(msg.twist.twist.angular.z)

        with _state_lock:
            st["lin_vel"] = lin
            st["ang_vel"] = ang

    # -------------------------
    # Watchdog: disconnected 처리
    # -------------------------
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

        # throttle (SSE/공유상태 업데이트 과부하 방지)
        if (now - self._cam_last_pub_ts) < self._cam_min_period:
            return
        self._cam_last_pub_ts = now

        st = _get_robot_state(self.ns)

        fmt = (msg.format or "").lower()  # e.g. "jpeg", "png", "jpeg compressed"
        # 보통 "jpeg" / "png" 또는 "jpeg compressed"처럼 들어올 수 있음
        if "jpeg" in fmt:
            fmt_norm = "jpeg"
        elif "png" in fmt:
            fmt_norm = "png"
        else:
            fmt_norm = fmt[:20] if fmt else None

        data_bytes = bytes(msg.data)
        size = len(data_bytes)

        # ✅ 기본: 메타만 저장 (가볍게)
        with _state_lock:
            st["camera_last_ts"] = now
            st["camera_format"] = fmt_norm
            st["camera_size"] = size
            st["camera_bytes"] = data_bytes  # ✅ 여기

        # ✅ 옵션: UI에서 바로 <img src="data:image/jpeg;base64,...">로 쓰고 싶으면 켜기
        # (주의: 트래픽/메모리 증가)
        PUSH_IMAGE_B64 = False
        if PUSH_IMAGE_B64:
            b64 = base64.b64encode(data_bytes).decode("ascii")
            with _state_lock:
                st["camera_b64"] = b64

        # SSE 이벤트 (메타만 push)
        event_bus.publish({
            "ts": now,
            "ns": self.ns,
            "type": "camera",
            "format": fmt_norm,
            "size": size,
            "has_b64": bool(PUSH_IMAGE_B64),
        })
        #print(f"[DEBUG] camera msg received: {self.ns} size={(msg.data)}")

# ==========================================================
# Executor/Thread 관리 (여러 노드를 1개 executor로)
# ==========================================================
_executor: Optional[MultiThreadedExecutor] = None
_spin_thread: Optional[threading.Thread] = None
_spin_stop_evt = threading.Event()

# ns -> node
_nodes: Dict[str, Turtlebot4Bridge] = {}
_nodes_lock = threading.Lock()


def _spin_loop():
    """
    별도 thread에서 executor.spin_once를 반복 수행.
    (Flask가 메인 thread를 쓰는 경우를 대비)
    """
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
    """
    ROS 브릿지 시스템 시작:
    - rclpy.init() 1회
    - executor 생성
    - (옵션) 초기 ns 목록을 등록/노드 생성
    - spin thread 시작
    """
    global _executor, _spin_thread

    if _spin_thread and _spin_thread.is_alive():
        return  # 이미 실행 중

    # rclpy는 프로세스에서 1회 init이 정석
    if not rclpy.ok():
        rclpy.init()

    _executor = MultiThreadedExecutor(num_threads=4)  # 필요하면 늘려도 됨
    _spin_stop_evt.clear()

    # 초기 로봇 등록
    if initial_namespaces:
        set_robot_namespaces(initial_namespaces)

    _spin_thread = threading.Thread(target=_spin_loop, daemon=True)
    _spin_thread.start()


def stop_bridge_system(join_timeout: float = 2.0):
    """
    ROS 브릿지 시스템 전체 종료:
    - 모든 노드 제거/파괴
    - spin thread 종료
    - rclpy.shutdown()
    """
    global _executor, _spin_thread

    _spin_stop_evt.set()

    t = _spin_thread
    if t and t.is_alive():
        t.join(timeout=join_timeout)

    # 노드 모두 정리
    with _nodes_lock:
        nss = list(_nodes.keys())

    for ns in nss:
        _remove_node(ns)

    # executor 정리
    _executor = None
    _spin_thread = None

    # rclpy 종료
    try:
        if rclpy.ok():
            rclpy.shutdown()
    except Exception:
        pass

    with _state_lock:
        shared_state["bridge_running"] = False
        shared_state["active_namespaces"] = []


def _add_node(ns: str):
    """
    내부용: 특정 ns에 대한 브릿지 노드를 executor에 추가
    """
    global _executor
    ns = _normalize_ns(ns)
    if not ns:
        return
    if _executor is None:
        raise RuntimeError("Bridge system not started. Call start_bridge_system() first.")

    with _nodes_lock:
        if ns in _nodes:
            return  # 이미 존재
        node = Turtlebot4Bridge(ns)
        _nodes[ns] = node
        _executor.add_node(node)

    # shared_state에 active ns 반영
    with _state_lock:
        active = set(shared_state.get("active_namespaces", []))
        active.add(ns)
        shared_state["active_namespaces"] = sorted(active)


def _remove_node(ns: str):
    """
    내부용: 특정 ns의 노드를 executor에서 제거 후 파괴
    """
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
            # executor 상태에 따라 remove가 실패할 수 있으니 방어
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
    """
    ✅ 핵심 API: 브릿징 대상 로봇 목록을 "동기화"한다.
    - 입력 목록에 있는 ns는 추가
    - 입력 목록에 없는 기존 ns는 제거

    예:
      set_robot_namespaces(["/robot2", "/robot6"])
      set_robot_namespaces(["/robot2"])  # robot6 제거
    """
    # 정규화 + 중복 제거
    desired: Set[str] = set(filter(None, (_normalize_ns(x) for x in (namespaces or []))))

    with _nodes_lock:
        current: Set[str] = set(_nodes.keys())

    # add
    for ns in sorted(desired - current):
        _add_node(ns)

    # remove
    for ns in sorted(current - desired):
        _remove_node(ns)

    # shared_state에 desired를 반영(정렬해서)
    with _state_lock:
        shared_state["active_namespaces"] = sorted(desired)


# ==========================================================
# Flask가 읽기 좋게 상태 스냅샷 제공
# ==========================================================
def get_state_snapshot() -> Dict:
    """
    Flask에서 JSON 응답으로 내리기 좋게 복사본 스냅샷 제공
    """
    with _state_lock:
        # 깊은 복사가 필요하면 copy.deepcopy 사용 가능하지만,
        # 여기선 dict/primitive만이라 얕은 복사 + 내부 dict 복사로 충분
        robots_copy = {ns: dict(st) for ns, st in shared_state["robots"].items()}
        snap = {
            "bridge_running": shared_state.get("bridge_running", False),
            "active_namespaces": list(shared_state.get("active_namespaces", [])),
            "robots": robots_copy,
        }
    return snap


# ==========================================================
# (선택) 빠른 사용 예시
# - 이 파일을 직접 실행할 경우, 로봇2/로봇6 브릿징을 시작
# ==========================================================
if __name__ == "__main__":
    # 예: python3 ros_tb4_bridge.py
    # 로봇2, 로봇6 동시에 브릿징 시작
    start_bridge_system(initial_namespaces=["/robot2", "/robot6"])

    try:
        while True:
            # 2초마다 상태 확인 출력
            time.sleep(2.0)
            snap = get_state_snapshot()
            print(json.dumps(snap, ensure_ascii=False, indent=2))
    except KeyboardInterrupt:
        pass
    finally:
        stop_bridge_system()
