# ros_fire_publisher.py
import time
import json
import threading

ROS_ENABLED = False
try:
    import rclpy
    from rclpy.node import Node
    from rclpy.executors import SingleThreadedExecutor
    from std_msgs.msg import String
    from rclpy.qos import (
        QoSProfile,
        ReliabilityPolicy,
        DurabilityPolicy,
        HistoryPolicy,
    )
    ROS_ENABLED = True
except Exception as e:
    print(f"[ROS] disabled: {e}")
    ROS_ENABLED = False


def _parse_label(ev: dict) -> str | None:
    """ev에서 단일 label 추출 (fire/stand/down)"""
    if not ev:
        return None
    v = ev.get("label")
    if isinstance(v, str):
        v = v.strip()
        return v if v else None
    return None


def _camera_to_key(camera: str | None) -> str | None:
    """cam1/cam2/cam3 -> class_a/b/c_detection"""
    if camera == "cam1":
        return "class_a_detection"
    if camera == "cam2":
        return "class_b_detection"
    if camera == "cam3":
        return "class_c_detection"
    return None


class RosFirePublisher(Node):
    def __init__(self):
        super().__init__("fire_event_publisher")

        fire_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )

        self.pub = self.create_publisher(String, "/webcam_detected", fire_qos)

        self._last_pub_ts = 0.0
        self._debounce_sec = 1.0

        # ✅ 누적 상태 (항상 이 구조 유지)
        self._payload = {
            "class_a_detection": [],
            "class_b_detection": [],
            "class_c_detection": [],
        }

        # 멀티스레드 접근 가능성 대비(Runner에서 publish가 다른 스레드로 올 수 있음)
        self._lock = threading.Lock()

    def publish_fire(self, ev: dict):
        # (옵션) 이벤트 타입 필터: detected 만 누적
        if ev.get("type") not in (None, "detected"):
            return

        label = _parse_label(ev)
        key = _camera_to_key(ev.get("camera"))

        if not label or not key:
            return

        with self._lock:
            # ✅ 중복 방지(원하면 제거 가능)
            if label not in self._payload[key]:
                self._payload[key].append(label)

            # ✅ 최종 payload는 항상 전체 구조로 유지
            payload = dict(self._payload)

        # debounce는 publish 빈도만 제한 (누적은 계속 됨)
        now = time.time()
        if (now - self._last_pub_ts) < self._debounce_sec:
            return
        self._last_pub_ts = now

        msg = String()
        msg.data = json.dumps(payload, ensure_ascii=False)
        self.pub.publish(msg)

        self.get_logger().info(
            f"fire published | camera={ev.get('camera')} | label={label} | payload={msg.data}"
        )
