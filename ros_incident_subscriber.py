import json
import threading
import time

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

_inc_lock = threading.Lock()
incident_state = {
    "status": "N/A",
}

class RosIncidentSubscriber(Node):
    def __init__(self, topic: str = "/incident_status"):
        super().__init__("incident_status_subscriber")
        self.topic = topic

        self.sub = self.create_subscription(
            String,
            self.topic,
            self._on_msg,
            10
        )
        self.get_logger().info(f"Subscribed to {self.topic}")

    def _on_msg(self, msg: String):
        try:
            data = json.loads(msg.data)
        except Exception:
            self.get_logger().warn(f"Invalid JSON: {msg.data}")
            return

        # 필드명은 필요하면 여기서 맞춰주면 됨
        ts = float(data.get("ts") or time.time())
        coord = str(data.get("coord") or "N/A")
        status = str(data.get("status") or "N/A")
        detail = str(data.get("detail") or "N/A")

        with _inc_lock:
            incident_state["status"] = status
