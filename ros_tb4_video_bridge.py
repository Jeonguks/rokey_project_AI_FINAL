# ros_tb4_video_bridge.py
import threading
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy

from sensor_msgs.msg import CompressedImage, Image
from cv_bridge import CvBridge
import cv2


_lock = threading.RLock()

def _norm_ns(ns: str) -> str:
    ns = (ns or "").strip().rstrip("/")
    if not ns.startswith("/"):
        ns = "/" + ns
    return ns


_tb4_latest_jpeg = {}   # ns -> jpeg bytes
_tb4_latest_ts = {}     # ns -> timestamp

def get_tb4_latest_jpeg(ns: str):
    ns = _norm_ns(ns)
    with _lock:
        return _tb4_latest_jpeg.get(ns), _tb4_latest_ts.get(ns, 0.0)


class Turtlebot4VideoBridge(Node):
    """
    기본은 compressed 토픽을 구독 (이미 JPEG라 그대로 전송 가능)
    필요 시 raw(Image)도 받아서 JPEG로 인코딩 가능
    """
    def __init__(self, ns: str, use_compressed: bool = True, topic: str | None = None):
        self.ns = _norm_ns(ns)
        node_name = f"tb4_video_bridge_{self.ns.strip('/').replace('/', '_')}"
        super().__init__(node_name)

        self.bridge = CvBridge()
        self.use_compressed = use_compressed

        # SensorData QoS 성격(유실 허용, 최신 우선)
        qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
        )

        if topic:
            self.topic = topic
        else:
            # ✅ 기본값 (너 환경에 맞게 바꿔도 됨)
            self.topic = f"{self.ns}/oakd/rgb/image_raw/compressed" if use_compressed \
                         else f"{self.ns}/oakd/rgb/image_raw"

        if self.use_compressed:
            self.create_subscription(CompressedImage, self.topic, self._on_compressed, qos)
        else:
            self.create_subscription(Image, self.topic, self._on_raw, qos)

        self.get_logger().info(f"[TB4-VIDEO:{self.ns}] Subscribing: {self.topic}")

    def _on_compressed(self, msg: CompressedImage):
        # msg.data 자체가 jpeg bytes
        jpeg_bytes = bytes(msg.data)
        with _lock:
            _tb4_latest_jpeg[self.ns] = jpeg_bytes
            _tb4_latest_ts[self.ns] = time.time()


    def _on_raw(self, msg: Image):
        # raw -> cv2 -> jpeg 인코딩
        try:
            frame = self.bridge.imgmsg_to_cv2(msg, desired_encoding="bgr8")
            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if not ok:
                return
            jpeg_bytes = buf.tobytes()
            with _lock:
                _latest_jpeg[self.ns] = jpeg_bytes
                _latest_ts[self.ns] = time.time()
        except Exception:
            return
