# ros_tb4_bridge.py
import json
import math
import threading
import time

import rclpy
from rclpy.node import Node

from sensor_msgs.msg import BatteryState
from nav_msgs.msg import Odometry
from geometry_msgs.msg import PoseWithCovarianceStamped


# -------------------------
# Shared state (Flask가 읽음)
# -------------------------
_state_lock = threading.Lock()
shared_state = {
    "robot_ns": "/robot6",
    "connected": False,
    "last_seen_ts": 0.0,

    # Battery
    "battery_percent": 0,      # 0~100
    "battery_voltage": None,   # float
    "battery_current": None,   # float

    # Pose (map / odom 기반)
    "pose_frame": "map",       # "map" or "odom"
    "x": 0.0,
    "y": 0.0,
    "yaw_deg": 0.0,

    # Velocity
    "lin_vel": 0.0,
    "ang_vel": 0.0,
}

def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v

def _quat_to_yaw_deg(q) -> float:
    # geometry_msgs/Quaternion: x,y,z,w
    # yaw = atan2(2(wz + xy), 1 - 2(y^2 + z^2))
    siny_cosp = 2.0 * (q.w * q.z + q.x * q.y)
    cosy_cosp = 1.0 - 2.0 * (q.y * q.y + q.z * q.z)
    yaw = math.atan2(siny_cosp, cosy_cosp)
    return float(yaw * 180.0 / math.pi)

def _touch_connected():
    now = time.time()
    with _state_lock:
        shared_state["connected"] = True
        shared_state["last_seen_ts"] = now


# -------------------------
# SSE EventBus (원하면 UI 실시간 갱신용)
# -------------------------
class EventBus:
    def __init__(self):
        self._cond = threading.Condition()
        self._events = []
        self._max = 300

    def publish(self, ev: dict):
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


# -------------------------
# ROS Node
# -------------------------
class Turtlebot4Bridge(Node):
    def __init__(self, ns: str):
        super().__init__("tb4_ui_bridge")
        self.ns = ns.rstrip("/")

        # 토픽 이름(환경마다 조금씩 다를 수 있어서 여기만 수정하면 됨)
        self.topic_battery = f"{self.ns}/battery_state"   # 많이 쓰는 이름
        self.topic_amcl    = f"{self.ns}/amcl_pose"
        self.topic_odom    = f"{self.ns}/odom"

        self.create_subscription(BatteryState, self.topic_battery, self._on_battery, 10)
        self.create_subscription(PoseWithCovarianceStamped, self.topic_amcl, self._on_amcl, 10)
        self.create_subscription(Odometry, self.topic_odom, self._on_odom, 10)

        # 연결상태 감시(최근 N초 안에 메시지 없으면 disconnected 처리)
        self.create_timer(1.0, self._watchdog)

        self.get_logger().info(f"[TB4] Subscribing: {self.topic_battery}, {self.topic_amcl}, {self.topic_odom}")

    def _on_battery(self, msg: BatteryState):
        _touch_connected()

        # BatteryState.percentage는 보통 0.0~1.0 (가끔 -1/NaN 가능)
        p = msg.percentage
        percent = 0
        try:
            if p is not None and p == p and p >= 0.0:  # NaN 체크(p==p)
                percent = int(round(_clamp01(float(p)) * 100.0))
        except Exception:
            percent = 0

        with _state_lock:
            shared_state["battery_percent"] = percent
            shared_state["battery_voltage"] = float(msg.voltage) if msg.voltage == msg.voltage else None
            shared_state["battery_current"] = float(msg.current) if msg.current == msg.current else None

        event_bus.publish({
            "ts": time.time(),
            "type": "battery",
            "battery_percent": percent,
        })

    def _on_amcl(self, msg: PoseWithCovarianceStamped):
        _touch_connected()

        pose = msg.pose.pose
        yaw_deg = _quat_to_yaw_deg(pose.orientation)

        with _state_lock:
            shared_state["pose_frame"] = "map"
            shared_state["x"] = float(pose.position.x)
            shared_state["y"] = float(pose.position.y)
            shared_state["yaw_deg"] = yaw_deg

        event_bus.publish({
            "ts": time.time(),
            "type": "pose",
            "frame": "map",
            "x": shared_state["x"],
            "y": shared_state["y"],
            "yaw_deg": yaw_deg,
        })

    def _on_odom(self, msg: Odometry):
        _touch_connected()

        # 속도는 odom이 가장 흔하게 안정적으로 줌
        lin = float(msg.twist.twist.linear.x)
        ang = float(msg.twist.twist.angular.z)

        with _state_lock:
            shared_state["lin_vel"] = lin
            shared_state["ang_vel"] = ang

        # pose는 amcl이 없을 수도 있어서, odom pose도 fallback으로 쓸 수 있음(원하면)
        # 여기서는 velocity 위주로만 반영

    def _watchdog(self):
        now = time.time()
        with _state_lock:
            last = float(shared_state["last_seen_ts"])
            # 3초 이상 메시지 없으면 끊긴 것으로 처리
            if last > 0 and (now - last) > 3.0:
                shared_state["connected"] = False


# -------------------------
# Thread control
# -------------------------
_ros_thread = None
_stop_evt = threading.Event()

def start_ros_thread(robot_ns: str = "/robot6"):
    global _ros_thread
    if _ros_thread and _ros_thread.is_alive():
        return

    with _state_lock:
        shared_state["robot_ns"] = robot_ns

    _stop_evt.clear()

    def _spin():
        rclpy.init()
        node = Turtlebot4Bridge(robot_ns)
        try:
            while rclpy.ok() and not _stop_evt.is_set():
                rclpy.spin_once(node, timeout_sec=0.1)
        finally:
            node.destroy_node()
            rclpy.shutdown()

    _ros_thread = threading.Thread(target=_spin, daemon=True)
    _ros_thread.start()

def stop_ros_thread():
    _stop_evt.set()
