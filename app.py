from config import *
from yolo.detector import CameraWorker, event_bus
from audio.AudioPlayer  import AudioPlayer, get_mp3_duration_sec
from audio.FireLoopPlayer  import FireLoopPlayer 


from ultralytics import YOLO
from flask import Flask, render_template, request, redirect, url_for, session, flash, Response, jsonify, abort
import sqlite3
from threading import Lock

import random
import cv2
import numpy as np
import atexit
import random
import sqlite3
import time

import ros_tb4_bridge

from threading import Lock
import json

from ros_incident_subscriber import get_incident, _inc_lock
from flask import request, Response, jsonify


from ros_runtime import RosRuntime


from ros_tb4_video_bridge import get_tb4_latest_jpeg

try:
    import rclpy
    from rclpy.node import Node
    from rclpy.executors import SingleThreadedExecutor
    from std_msgs.msg import String
    ROS_ENABLED = True
except Exception as e:
    print(f"[ROS] disabled (import failed): {e}")
    ROS_ENABLED = False



import db_store


app = Flask(__name__)
app.secret_key = SECRET_KEY



rt = RosRuntime()
rt.start()

db_store.init_db()



@app.get("/api/db/robot_status")
def api_db_robot_status():
    ns = request.args.get("ns")  # optional
    limit = request.args.get("limit", "200")
    try:
        limit_n = int(limit)
    except Exception:
        limit_n = 200

    rows = db_store.fetch_robot_status(ns=ns, limit=limit_n)
    return jsonify({"ok": True, "rows": rows})


@app.post("/api/db/robot_status/clear")
def api_db_robot_status_clear():
    db_store.clear_robot_status()
    return jsonify({"ok": True})




@app.route("/tb4_video_feed")
def tb4_video_feed():
    ns = request.args.get("ns", "/robot2")

    def gen():
        last_ts = 0.0

        while True:
            jpeg, ts = get_tb4_latest_jpeg(ns)

            #  새 프레임일 때만 전송(중복 전송 방지)
            if jpeg and ts > last_ts:
                last_ts = ts
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n"
                       b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" +
                       jpeg + b"\r\n")
            time.sleep(0.05)

            #  busy-loop 방지(대략 30fps)
            time.sleep(TB4_VIDEO_SLEEP_SEC if 'TB4_VIDEO_SLEEP_SEC' in globals() else 0.03)

    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")




@app.route("/api/incident_status")
def api_incident_status():
    ns = request.args.get("ns", "/robot2")
    with _inc_lock:
        snapshot = dict(get_incident(ns))  #  복사
    return jsonify(snapshot)               #  락 밖



# ✅ 추가: 복귀 요청 API
@app.post("/api/return_home")
def api_return_home():
    # 1) ROS 런타임/노드 준비 체크

    if rt is None:
        return {"ok": False, "error": "ROS runtime not created"}, 503

    # 2) 짧게 준비 대기(최대 1초)
    for _ in range(10):
        if getattr(rt, "ret", None) is not None:
            break
        time.sleep(0.1)

    if rt.ret is None:
        return {"ok": False, "error": "ROS node not ready"}, 503

    # 3) publish
    rt.ret.publish_request()
    return {"ok": True}





# 페이지 전환시 카메라 안돌게 
workers_lock = Lock()
cameras_enabled = False


@app.post("/api/cameras/start")
def api_cameras_start():
    start_cameras()
    return jsonify({"ok": True, "cameras_enabled": True})

@app.post("/api/cameras/stop")
def api_cameras_stop():
    stop_cameras()
    return jsonify({"ok": True, "cameras_enabled": False})

@app.get("/api/cameras/status")
def api_cameras_status():
    return jsonify({"ok": True, "cameras_enabled": cameras_enabled})


def start_cameras():
    global cameras_enabled
    with workers_lock:
        if cameras_enabled:
            return
        for w in workers.values():
            w.start()
        cameras_enabled = True
        print("[CAM] started")

def stop_cameras():
    global cameras_enabled
    with workers_lock:
        if not cameras_enabled:
            return
        for w in workers.values():
            try:
                w.stop()
            except Exception as e:
                print("[CAM] stop error:", e)
        cameras_enabled = False
        print("[CAM] stopped")





# ros2 토픽 리시브

# ros_tb4_bridge.start_ros_thread("/robot6")  




@app.route("/api/tb4_status")
def api_tb4_status():
    ns = request.args.get("ns", "/robot6")

    # ✅ lock timeout: 무한 대기 방지 + 락 최소화
    if not ros_tb4_bridge._state_lock.acquire(timeout=0.3):
        return jsonify({"ok": False, "error": "tb4 state lock timeout"}), 503
    try:
        if hasattr(ros_tb4_bridge, "get_state"):
            snapshot = dict(ros_tb4_bridge.get_state(ns))   # ✅ 복사만
        else:
            snapshot = dict(ros_tb4_bridge.shared_state)    # legacy
    finally:
        ros_tb4_bridge._state_lock.release()


    db_store.save_status_snapshot(ns, st, inc)

    # ✅ 락 밖에서 처리(필요하면 여기서 계산/포맷/로그)
    return jsonify(snapshot)





@app.route("/tb4_events")
def tb4_events():
    return Response(ros_tb4_bridge.event_bus.stream(), mimetype="text/event-stream")

@app.route("/tb4")
def tb4_page():
    if "username" not in session:   # 로그인 유지하고 싶으면
        return redirect(url_for("login"))
    return render_template("robot_display.html")

@app.route("/robot_display")
def robot_display():
    if "username" not in session:
        return redirect(url_for("login"))
    
    stop_cameras()
    return render_template("robot_display.html", username=session["username"])



# 화재 경보음
audio_player = AudioPlayer()
fire_duration = get_mp3_duration_sec(FIRE_ALARM_PATH)

fire_loop = FireLoopPlayer(
    audio_player=audio_player,
    mp3_path=FIRE_ALARM_PATH,
    duration_sec=fire_duration,
    fire_hold_sec=1.5,   # fire 이벤트가 1.5초 이상 안 들어오면 "fire 종료"로 봄
)

fire_loop.start()




# YOLO 모델 로드
model = YOLO(YOLO_MODEL_PATH)

# Camera workers
workers = {
    "cam1": CameraWorker(camera_key="cam1", camera_path=CAM1, model=model, conf_thres = YOLO_CONF_THRES),
    "cam2": CameraWorker(camera_key="cam2", camera_path=CAM2, model=model, conf_thres = YOLO_CONF_THRES),
    "cam3": CameraWorker(camera_key="cam3", camera_path=CAM3, model=model, conf_thres = YOLO_CONF_THRES),
}


# 디텍션시 사용할 콜백 함수
# def on_detect(ev):
#     print(f"[DETECTION] camera={ev['camera']} label={ev['label']}")
"""
ev 예시:
{
    "ts": 1690000000.123,
    "camera": "cam2",
    "type": "detected",
    "label": "fire",   # fire / stand / down
    ...
}
"""


# 디텍션
def on_detect(ev):
    print(f"[DETECTION] camera={ev['camera']} label={ev['label']}")

    # ev['label'] = 'fire' # test code

    label = ev.get("label")
    rt.fire.publish_fire(ev)

    if label == "fire":
        fire_loop.notify_fire()
        # ros_fire_runner.publish_fire(ev)
  

# 캠 워커 활성화 > 
for w in workers.values():
    w.register_callback(on_detect)
    # w.start() # 제어형으로 바뀌면서 사용 안함

# 영상 전송 루프: Flask가 브라우저에게 MJPEG 스트림을 보내기 위해 쓰는 generator
# CameraWorker는 계속 latest_jpeg를 최신으로 갈아끼움
# mjpeg()는 그 latest_jpeg를 계속 읽어서 브라우저에 보내기만 함
def mjpeg(worker):
    # 카메라가 꺼지면 연결을 끊어버림(루프 종료)
    while True:
        if not cameras_enabled:
            # 204로 끝내는 대신 generator 종료 -> 브라우저 연결 닫힘
            return

        jpg = worker.get_latest_jpeg() if hasattr(worker, "get_latest_jpeg") else getattr(worker, "latest_jpeg", None)

        if jpg:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" +
                jpg +
                b"\r\n"
            )
        # 프레임 없을 때는 조용히 대기 (로그 X)
        time.sleep(FRAME_SLEEP)

@app.route("/events")
def events():
    return Response(event_bus.stream(), mimetype="text/event-stream")

# ----------------------------
# Helpers
# ----------------------------
def camera_available(idx: int) -> bool:
    cap = cv2.VideoCapture(idx)
    ok = cap.isOpened()
    cap.release()
    return ok

def safe_int(v, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return default

def clamp_percent(v, default: int) -> int:
    n = safe_int(v, default)
    return max(0, min(100, n))

# ----------------------------
# Routes
# ----------------------------
@app.route("/")
def home():
    if "username" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if username == USERNAME and password == PASSWORD:
            session["username"] = username
            return redirect(url_for("dashboard"))
        else:
            return redirect(url_for("login"))

    return render_template("login.html")

@app.route("/dashboard")
def dashboard():
    if "username" not in session:
        return redirect(url_for("login"))
    start_cameras() 

    return render_template(
        "dashboard.html",
        username=session["username"],
    )


# ----------------------------
# Camera streaming
# ----------------------------
# List of store coordinates
pt_1 = (460, 0)
pt_2 = (640, 0)
pt_3 = (640, 120)
pt_4 = (460, 120)
coordinates = [pt_1, pt_2, pt_3, pt_4]


# todo 분석 필요
# def generate_frames_box(camera_id: int):
def generate_frames_box(camera_path: int):
    # camera = cv2.VideoCapture(camera_id)
    camera = cv2.VideoCapture(camera_path, cv2.CAP_V4L2)

    if not camera.isOpened():
        return

    while True:
        success, frame = camera.read()
        if not success:
            break

        for (x, y) in coordinates:
            cv2.circle(frame, (x, y), 5, (0, 0, 255), -1)

        pts = np.array(coordinates, np.int32).reshape((-1, 1, 2))
        cv2.polylines(frame, [pts], isClosed=True, color=(0, 255, 0), thickness=2)

        ret, buffer = cv2.imencode(".jpg", frame)
        if not ret:
            continue

        frame_bytes = buffer.tobytes()
        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n")

    camera.release()

@app.route("/video_feed1")
def video_feed1():
    if not cameras_enabled:
        return ("video_feed1 에러", 404)  # 또는 abort(404)
    return Response(mjpeg(workers["cam1"]), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/video_feed2")
def video_feed2():
    if not cameras_enabled:
        return ("video_feed2 에러", 404)  # 또는 abort(404)
    return Response(mjpeg(workers["cam2"]), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/video_feed3")
def video_feed3():
    if not cameras_enabled:
        return ("video_feed3 에러", 404)  # 또는 abort(404)
    return Response(mjpeg(workers["cam3"]), mimetype="multipart/x-mixed-replace; boundary=frame")


# JS로 주기 호출 추가
@app.route("/api/status")
def api_status():
    # TODO: 여기 random 대신 실제 배터리 수신 값으로 바꾸면 끝
    robotA_battery = random.randint(0, 100)
    robotB_battery = random.randint(0, 100)
    return jsonify({
        "robotA_battery": robotA_battery,
        "robotB_battery": robotB_battery
    })


# ✅ 템플릿에 있는 form action 때문에 필요
@app.route("/api/dispatch_robot")
def dispatch_robot():
    # todo 출동 요청 구현
    return


@app.route("/api/alarm/stop", methods=["POST"])
def api_alarm_stop():
    # 10초동안 재감지 무시 (원하면 값 조절)
    fire_loop.stop_alarm(silence_sec=5)
    return jsonify({"ok": True})


# ----------------------------
# DB 데이터 조회
# ----------------------------
def get_detection_entries():

    # Connect to SQLite database (or create it if it doesn't exist)
    connection = sqlite3.connect('mydatabase.db')

    # Create a cursor object to interact with the database
    cursor = connection.cursor()

    # SQL command to select all data from the table
    select_query = "SELECT * FROM detection_table;"

    # Execute the command and fetch all results
    cursor.execute(select_query)
    rows = cursor.fetchall()

    # Print each row
    for row in rows:
        print(row)

    # Commit the changes and close the connection
    connection.commit()
    connection.close()
    return rows


# ----------------------------
# Logout
# ----------------------------
@app.route("/logout")
def logout():
    session.pop("username", None)
    return redirect(url_for("login"))

if __name__ == "__main__":
    # use_reloader=False는 카메라 핸들 이슈 줄이는데 도움
    app.run(debug=True, use_reloader=False, port=5167, threaded=True)

