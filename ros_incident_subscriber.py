# ros_incident_subscriber.py
import json
import threading
import time

import rclpy
from rclpy.node import Node
from std_msgs.msg import String


_inc_lock = threading.RLock()

def _new_incident(ns: str):
    return {
        "robot_ns": ns,
        "status": "-",         # ✅ UI에 표시할 값(필수)
        "last_seen_ts": 0.0,
    }

incident_states = {
    "/robot2": _new_incident("/robot2"),
    "/robot6": _new_incident("/robot6"),
}

def norm_ns(ns: str) -> str:
    ns = (ns or "").strip().rstrip("/")
    if not ns.startswith("/"):
        ns = "/" + ns
    return ns

def get_incident(ns: str) -> dict:
    ns = norm_ns(ns)
    with _inc_lock:
        if ns not in incident_states:
            incident_states[ns] = _new_incident(ns)
        return incident_states[ns]


class RosIncidentSubscriber(Node):
    """
    기본 토픽: {ns}/incident_status   (예: /robot2/incident_status)
    메시지 포맷:
      1) JSON: {"status": "화재 진압중"}  -> status만 뽑음
      2) Plain text: "화재 진압중"        -> 그대로 status로 사용
    """
    def __init__(self, ns: str, topic_suffix: str = "incident_status"):
        self.ns = norm_ns(ns)
        node_name = f"incident_sub_{self.ns.strip('/').replace('/', '_')}"
        super().__init__(node_name)

        self.topic = f"{self.ns}/{topic_suffix}".replace("//", "/")
        self.create_subscription(String, self.topic, self._on_msg, 10)

        self.get_logger().info(f"[INCIDENT:{self.ns}] Subscribing: {self.topic}")

    def _on_msg(self, msg: String):
        raw = (msg.data or "").strip()
        if not raw:
            return

        status = None

        # JSON이면 status 키만 사용
        if raw.startswith("{") and raw.endswith("}"):
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    v = data.get("status")
                    if isinstance(v, str) and v.strip():
                        status = v.strip()
            except Exception:
                status = None

        # JSON 아니면 plain text로 처리
        if status is None:
            status = raw

        with _inc_lock:
            st = get_incident(self.ns)
            st["status"] = status
            st["last_seen_ts"] = time.time()
