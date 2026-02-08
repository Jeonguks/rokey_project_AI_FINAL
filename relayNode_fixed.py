#!/usr/bin/env python3
import rclpy
from rclpy.node import Node

from std_msgs.msg import Bool
from geometry_msgs.msg import Point

class RelayNode(Node):
    def __init__(self):
        super().__init__('relay_node')
        
        ##########################################################
        # robot2 -> robot6
        self.help_signal_sub_2 = self.create_subscription(
            Bool,
            '/robot2/signal/help',
            self.cb_signal_2,
            10
        )
        self.help_signal_pub6 = self.create_publisher(
            Bool, '/robot6/signal/help', 10
        )

        self.help_coordinate_sub2 = self.create_subscription(
            Point,
            '/robot2/signal/coordinate',
            self.cb_coordinate_2,
            10
        )
        self.help_coordinate_pub6 = self.create_publisher(
            Point, '/robot6/signal/coordinate', 10
        )

        ###########################################################
        # robot6 -> robot2
        self.help_signal_sub_6 = self.create_subscription(
            Bool,
            '/robot6/signal/help',
            self.cb_signal_6,
            10
        )
        self.help_signal_pub2 = self.create_publisher(
            Bool, '/robot2/signal/help', 10
        )

        self.help_coordinate_sub6 = self.create_subscription(
            Point,
            '/robot6/signal/coordinate',
            self.cb_coordinate_6,
            10
        )
        self.help_coordinate_pub2 = self.create_publisher(
            Point, '/robot2/signal/coordinate', 10
        )

        self.get_logger().info("Relay Node started: /robot2 <-> /robot6")

    #Callback Functions###################################################
    def cb_signal_2(self, msg: Bool):
        self.help_signal_pub6.publish(msg)

    def cb_coordinate_2(self, msg: Point):
        self.help_coordinate_pub6.publish(msg)

    def cb_signal_6(self, msg: Bool):
        self.help_signal_pub2.publish(msg)

    def cb_coordinate_6(self, msg: Point):
        self.help_coordinate_pub2.publish(msg)
    #######################################################################


def main():
    rclpy.init()
    node = RelayNode()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
