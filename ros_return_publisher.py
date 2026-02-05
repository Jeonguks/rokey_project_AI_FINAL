# ros_return_publisher.py
import threading

ROS_ENABLED = False

try:
    import rclpy
    from rclpy.node import Node
    from rclpy.executors import SingleThreadedExecutor
    from std_msgs.msg import Empty
    from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
    ROS_ENABLED = True
except Exception as e:
    print(f"[ROS] disabled: {e}")
    ROS_ENABLED = False


class RosReturnPublisher(Node):
    def __init__(self):
        super().__init__("return_home_publisher")

        qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,  # 버튼 클릭 이벤트는 보통 volatile로 충분
        )

        self.pub = self.create_publisher(Empty, "/return_home_request", qos)

    def publish_request(self):
        self.pub.publish(Empty())
        self.get_logger().info("Published /return_home_request")

