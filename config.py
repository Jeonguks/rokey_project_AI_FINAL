# Flask
SECRET_KEY = "your_secret_key"

# Hardcoded user credentials for demonstration
USERNAME = "user"
PASSWORD = "password"


# Camera paths
# CAM0 = "/dev/v4l/by-path/pci-0000:00:14.0-usb-0:6:1.0-video-index0" #-> ../../video0 내장 카메라
CAM1 = "/dev/v4l/by-path/pci-0000:00:14.0-usb-0:1:1.0-video-index0" #-> ../../video7 1번포트
CAM2 = "/dev/v4l/by-path/pci-0000:00:14.0-usb-0:3:1.0-video-index0" #-> ../../video3 2번 포트
CAM3 = "/dev/v4l/by-path/pci-0000:00:14.0-usb-0:2:1.0-video-index0" #-> ../../video5 3번 포트


# YOLO
# YOLO_MODEL_PATH = "/home/rokey/rokey_ws/src/web_cam_detect/best.pt"
YOLO_MODEL_PATH = "res/best_web_v8n_param_add.pt"
YOLO_CONF_THRES = 0.7
YOLO_IMG_SZ = 640

# fps 조정 목적
FRAME_SLEEP = 0.1

# audio
FIRE_ALARM_PATH = "res/fire_alarm.mp3"


ROS_ENABLED = False
_ros_fire = None


TB4_VIDEO_SLEEP_SEC = 0.1  # TB4 카메라 프레임 딜레이 조정용

# 웹캠 디텍션 토픽명
ROS_WEBCAM_DETECTED_TOPIC = "/webcam_detected"

'''
{
    "class_a_detection": ["stand", "fire"],
    "class_b_detection": ["fire"],
    "class_c_detection": ["down"]
}
'''


# 로봇 귀환 요청 토픽명
ROS_RETURN_HOME_TOPIC = "/return_home_request" # 토픽 없음



# 로봇 상황 보고 토픽명
ROS_AMR_SITUATION_TOPIC = "/amr_situation"
''' 
{
    "status": "화진 진압중"
}
''' 


# <추가로 할것>
# 웹에서 현재 상황 robot2,6 표시 되는지 확인
# 복귀 명령 로봇에서 받기
# 아이패드에 api 요청 보내기

