import json
import csv
import time
import math
import os
import shutil
import sys
from ultralytics import YOLO
import cv2

# ROS2
import rclpy
from rclpy.node import Node
from std_msgs.msg import String
from rclpy.qos import QoSProfile, DurabilityPolicy


class YOLOWebcamProcessor(Node):
    def __init__(self, model, output_dir):
        super().__init__('yolo_webcam_processor')

        self.model = model
        self.output_dir = output_dir
        self.csv_output = []
        self.confidences = []
        self.max_object_count = 0
        self.classNames = model.names
        self.should_shutdown = False

        # ✅ QoS 설정 (퍼블리시 유지 가능)
        qos = QoSProfile(depth=1)
        qos.durability = DurabilityPolicy.TRANSIENT_LOCAL

        self.pub = self.create_publisher(String, '/detected_object', qos)


        # car publish 한번만
        self.car_published = False

    def publish_car_detected(self, label):
        msg = String()
        msg.data = label
        self.pub.publish(msg)
        self.get_logger().info(f'Published detected object: {label}')

    def run(self):
        cap = cv2.VideoCapture(2)   # 카메라 번호

        if not cap.isOpened():
            print("Failed to open webcam.")
            return

        start_time = time.time()   # ✅ OpenCV 시작 시간

        while not self.should_shutdown:
            ret, img = cap.read()
            if not ret:
                continue

            # ✅ 5초 후 자동 종료
            if time.time() - start_time > 5:
                print("5 seconds passed → Auto shutdown")
                break

            results = self.model(img, stream=True)
            object_count = 0

            for r in results:
                for box in r.boxes:
                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 255), 2)

                    confidence = math.ceil((box.conf[0] * 100)) / 100
                    cls = int(box.cls[0])
                    label = self.classNames.get(cls, f"class_{cls}")

                    print(f"Detected: {label}")

                    # ✅ car 감지 시 publish (1회)
                    if label == "car" and not self.car_published:
                        self.car_published = True
                        print("car detected → publish")
                        self.publish_car_detected(label)

                    cv2.putText(img, f"{label}: {confidence}",
                                (x1, y1),
                                cv2.FONT_HERSHEY_SIMPLEX, 1,
                                (255, 0, 0), 2)

                    self.csv_output.append([x1, y1, x2, y2, confidence, label])
                    object_count += 1

            # 화면 출력
            cv2.imshow("Detection", img)
            if cv2.waitKey(1) == ord('q'):
                break

        cap.release()
        cv2.destroyAllWindows()

    def save_output(self):
        with open(os.path.join(self.output_dir, 'output.csv'), 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['X1', 'Y1', 'X2', 'Y2', 'Confidence', 'Class'])
            writer.writerows(self.csv_output)

        with open(os.path.join(self.output_dir, 'output.json'), 'w') as f:
            json.dump(self.csv_output, f, indent=4)


def main():
    rclpy.init()

    # ✅ 경로 하드코딩
    model_path = "/home/rokey/rokey_ws/src/web_cam_detect/best.pt"
    output_dir = "/home/rokey/yolo_output"

    if not os.path.exists(model_path):
        print(f"Model not found: {model_path}")
        return

    model = YOLO(model_path)

    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir)

    processor = YOLOWebcamProcessor(model, output_dir)
    processor.run() 
    processor.save_output()

    processor.destroy_node()
    rclpy.shutdown()
    print("Shutdown complete")


if __name__ == "__main__":
    main()
