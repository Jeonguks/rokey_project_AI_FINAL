# Flask
SECRET_KEY = "your_secret_key"

# Camera paths
# CAM0 = "/dev/v4l/by-path/pci-0000:00:14.0-usb-0:6:1.0-video-index0" #-> ../../video0 내장 카메라
CAM1 = "/dev/v4l/by-path/pci-0000:00:14.0-usb-0:1:1.0-video-index0" #-> ../../video7 1번포트
CAM2 = "/dev/v4l/by-path/pci-0000:00:14.0-usb-0:3:1.0-video-index0" #-> ../../video3 2번 포트
CAM3 = "/dev/v4l/by-path/pci-0000:00:14.0-usb-0:2:1.0-video-index0" #-> ../../video5 3번 포트


# YOLO
# YOLO_MODEL_PATH = "/home/rokey/rokey_ws/src/web_cam_detect/best.pt"
YOLO_MODEL_PATH = "res/best_webcam_v8n.pt"
YOLO_CONF_THRES = 0.3
YOLO_IMG_SZ = 640

# fps 조정 목적
FRAME_SLEEP = 3

# audio
FIRE_ALARM_PATH = "res/fire_alarm.mp3"


ROS_ENABLED = False
_ros_fire = None


TB4_VIDEO_SLEEP_SEC = 0.05  # TB4 카메라 프레임 딜레이 조정용

