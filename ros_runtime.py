# ros_runtime.py
import threading
import rclpy
from rclpy.executors import MultiThreadedExecutor

from ros_tb4_bridge import Turtlebot4Bridge
from ros_fire_publisher import RosFirePublisher
from ros_return_publisher import RosReturnPublisher
from ros_incident_subscriber import RosIncidentSubscriber


class RosRuntime:
    def __init__(self, robot_ns="/robot6"):
        self.robot_ns = robot_ns
        self.executor = None
        self.thread = None
        self.tb4 = None
        self.fire = None
        self.ret = None
        self.incident = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return

        def _spin():
            print('ros_runtime_spin 실행 ')

            rclpy.init(args=None)   # ✅ 딱 1번
            self.executor = MultiThreadedExecutor()

            # self.tb4 = Turtlebot4Bridge(self.robot_ns)
            self.tb4_6 = Turtlebot4Bridge("/robot6")
            self.tb4_2 = Turtlebot4Bridge("/robot2")

            self.fire = RosFirePublisher()
            self.ret  = RosReturnPublisher()
            self.incident = RosIncidentSubscriber("/incident_status")

            # self.executor.add_node(self.tb4)
            self.executor.add_node(self.tb4_6)
            self.executor.add_node(self.tb4_2)
            self.executor.add_node(self.fire)
            self.executor.add_node(self.ret)
            self.executor.add_node(self.incident)


            try:
                self.executor.spin()
            finally:
                self.executor.shutdown()
                # self.tb4.destroy_node()
                self.tb4_6.destroy_node()
                self.tb4_2.destroy_node()
                self.fire.destroy_node()
                self.ret.destroy_node()
                rclpy.shutdown()

        self.thread = threading.Thread(target=_spin, daemon=True)
        self.thread.start()
