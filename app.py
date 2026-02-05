from config import *

import time
import random
import sqlite3
import atexit
from threading import Lock

import cv2
import numpy as np
from ultralytics import YOLO

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, Response, jsonify
)

# YOLO(로컬 카메라) 파이프라인
from yolo.detector import CameraWorker, event_bus as yolo_event_bus

# 오디오
from audio.AudioPlayer import AudioPlayer, get_mp3_duration_sec
#from audio.FireLoopPlayer import FireloopPlayer

# ROS 브릿지
import ros_tb4_bridge


# ==========================================================
# Flask App
# ==========================================================
app = Flask(__name__)
app.secret_key = SECRET_KEY

USERNAME = "user"
PASSWORD = "password"


# ==========================================================
# YOLO Local Cameras (USB/V4L2) Control
# ==========================================================
workers_lock = Lock()
cameras_enabled = False

# YOLO 모델 로드
model = YOLO(YOLO_MODEL_PATH)

workers = {
    "cam1": CameraWorker(camera_key="cam1", camera_path=CAM1, model=model, conf_thres=YOLO_CONF_THRES),
    "cam2": CameraWorker(camera_key="cam2", camera_path=CAM2, model=model, conf_thres=YOLO_CONF_THRES),
    "cam3": CameraWorker(camera_key="cam3", camera_path=CAM3, model=model, conf_thres=YOLO_CONF_THRES),
}


def start_cameras():
    """YOLO 로컬 카메라 워커 시작"""
    global cameras_enabled
    with workers_lock:
        if cameras_enabled:
            return
        for w in workers.values():
            w.start()
        cameras_enabled = True
        print("[CAM] started")


def stop_cameras():
    """YOLO 로컬 카메라 워커 정지"""
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


@app.post("/api/cameras/start")
def api_cameras_start():
    start_cameras()
    return jsonify(ok=True, cameras_enabled=True)


@app.post("/api/cameras/stop")
def api_cameras_stop():
    stop_cameras()
    return jsonify(ok=True, cameras_enabled=False)


@app.get("/api/cameras/status")
def api_cameras_status():
    return jsonify(ok=True, cameras_enabled=cameras_enabled)


def mjpeg(worker: CameraWorker):
    """
    YOLO 로컬 카메라 worker의 latest jpeg를 브라우저로 MJPEG 스트림 전송
    카메라가 꺼지면 generator 종료(브라우저 연결 닫힘)
    """
    while True:
        if not cameras_enabled:
            return

        jpg = worker.get_latest_jpeg() if hasattr(worker, "get_latest_jpeg") else getattr(worker, "latest_jpeg", None)
        if jpg:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" +
                jpg +
                b"\r\n"
            )
        time.sleep(0.1)


@app.route("/video_feed1")
def video_feed1():
    if not cameras_enabled:
        return ("video_feed1 disabled", 404)
    return Response(mjpeg(workers["cam1"]), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/video_feed2")
def video_feed2():
    if not cameras_enabled:
        return ("video_feed2 disabled", 404)
    return Response(mjpeg(workers["cam2"]), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/video_feed3")
def video_feed3():
    if not cameras_enabled:
        return ("video_feed3 disabled", 404)
    return Response(mjpeg(workers["cam3"]), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/events")
def events():
    """YOLO detection SSE"""
    return Response(yolo_event_bus.stream(), mimetype="text/event-stream")


# ==========================================================
# Fire Alarm Loop
# ==========================================================
audio_player = AudioPlayer()
fire_duration = get_mp3_duration_sec(FIRE_ALARM_PATH)

#fire_loop = FireLoopPlayer(
    # mp3_path=FIRE_ALARM_PATH,
  #  duration_sec=fire_duration,
 #   fire_hold_sec=1.5,
#)
#fire_loop.start()


def on_detect(ev: dict):
    """YOLO detection callback"""
    print(f"[DETECTION] camera={ev.get('camera')} label={ev.get('label')}")
    if ev.get("label") == "fire":
       # fire_loop.notify_fire()


        for w in workers.values():
            w.register_callback(on_detect)


@app.route("/api/alarm/stop", methods=["POST"])
def api_alarm_stop():
    #fire_loop.stop_alarm(silence_sec=5)
    return jsonify(ok=True)


# ==========================================================
# ROS TB4 Status + Camera (ROS bridge)
# ==========================================================
@app.get("/api/tb4_status")
def api_tb4_status():
    """
    ns별 상태 조회:
      /api/tb4_status?ns=/robot6
      /api/tb4_status?ns=/robot2
    """
    ns = (request.args.get("ns", "/robot6") or "/robot6").rstrip("/")
    if not ns.startswith("/"):
        ns = "/" + ns

    snap = ros_tb4_bridge.get_state_snapshot()
    st = snap["robots"].get(ns)

    if not st:
        return jsonify(ok=False, reason="unknown ns", ns=ns), 404

    # -----------------------------
    # ✅ JSON 직렬화 안전화
    # - camera_bytes 같은 바이너리 제거
    # - bytes가 남아있으면 문자열로 변환
    # -----------------------------
    def _to_json_safe(v):
        # bytes/bytearray -> utf-8 문자열(깨지면 replace)
        if isinstance(v, (bytes, bytearray)):
            return v.decode("utf-8", errors="replace")
        # numpy 타입 방어(있을 수도)
        try:
            import numpy as np
            if isinstance(v, (np.integer,)):
                return int(v)
            if isinstance(v, (np.floating,)):
                return float(v)
            if isinstance(v, (np.ndarray,)):
                return v.tolist()
        except Exception:
            pass
        return v

    out = {}
    for k, v in dict(st).items():
        # ✅ MJPEG 바이너리 제거 (이게 지금 500의 주범)
        if k in ("camera_bytes", "cam_jpeg", "jpg", "jpeg", "image_bytes"):
            continue
        out[k] = _to_json_safe(v)

    out["ok"] = True
    out["ns"] = ns  # 프론트에서 확인용으로 있으면 편함
    return jsonify(out)


@app.route("/tb4_events")
def tb4_events():
    """ROS TB4 SSE"""
    return Response(ros_tb4_bridge.event_bus.stream(), mimetype="text/event-stream")

@app.get("/api/ros/cameras/status")
def ros_camera_status():
    """
    ROS 카메라 상태:
      /api/ros/cameras/status?ns=/robot2

    state:
      ON        : 최근 10초 내 프레임 수신
      OFF       : 로봇 없음 / 프레임 없음 / 오래됨
    """
    ns = (request.args.get("ns", "/robot2") or "/robot2").rstrip("/")
    if not ns.startswith("/"):
        ns = "/" + ns

    now = time.time()
    with ros_tb4_bridge._state_lock:
        st = ros_tb4_bridge.shared_state["robots"].get(ns)
        if not st:
            return jsonify(ok=False, state="OFF", reason="unknown ns", ns=ns), 404

        last = float(st.get("camera_last_ts") or 0.0)
        size = int(st.get("camera_size") or 0)
        fmt  = st.get("camera_format")

    # 10초 기준
    if last > 0 and (now - last) <= 10.0 and size > 0:
        return jsonify(ok=True, state="ON", ns=ns, last_ts=last, age_sec=now-last, size=size, fmt=fmt)
    else:
        return jsonify(ok=True, state="OFF", ns=ns, last_ts=last, age_sec=(now-last if last else None), size=size, fmt=fmt)


@app.get("/api/ros/cameras/mjpeg")
def ros_cameras_mjpeg():
    """
    ROS CompressedImage 기반 MJPEG:
      /api/ros/cameras/mjpeg?ns=/robot2
    """
    ns = (request.args.get("ns", "/robot2") or "/robot2").rstrip("/")
    if not ns.startswith("/"):
        ns = "/" + ns

    def gen():
        boundary = b"--frame"
        while True:
            snap = ros_tb4_bridge.get_state_snapshot()
            st = snap["robots"].get(ns)

            # ✅ 키 수정: cam_jpeg -> camera_bytes
            jpg = st.get("camera_bytes") if st else None
            if not jpg:
                time.sleep(0.1)
                continue

            yield boundary + b"\r\n"
            yield b"Content-Type: image/jpeg\r\n"
            yield f"Content-Length: {len(jpg)}\r\n\r\n".encode()
            yield jpg + b"\r\n"

            time.sleep(0.05)  # ~20fps 제한

    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")

# ==========================================================
# Auth + Pages
# ==========================================================
@app.route("/")
def home():
    if "username" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")  # ✅ 버그 수정
        password = request.form.get("password", "")

        if username == USERNAME and password == PASSWORD:
            session["username"] = username
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("username", None)
    return redirect(url_for("login"))


@app.route("/dashboard")
def dashboard():
    if "username" not in session:
        return redirect(url_for("login"))

    # Dashboard 들어오면 YOLO 카메라 ON
    start_cameras()

    robotA_battery = 0
    robotB_battery = 0
    incident_coord = "x=12.3, y=4.5"
    incident_status = "화재 진압중"
    incident_detail = "소화 작업 진행 중"

    return render_template(
        "dashboard.html",
        username=session["username"],
        robotA_battery=robotA_battery,
        robotB_battery=robotB_battery,
        incident_coord=incident_coord,
        incident_status=incident_status,
        incident_detail=incident_detail,
    )


@app.route("/robot_display")
def robot_display():
    if "username" not in session:
        return redirect(url_for("login"))

    # robot_display 들어오면 YOLO 카메라 OFF
    stop_cameras()
    return render_template("robot_display.html", username=session["username"])


@app.route("/tb4")
def tb4_page():
    if "username" not in session:
        return redirect(url_for("login"))
    # TB4 페이지는 로봇 상태 중심 → YOLO 카메라 OFF 유지하고 싶으면 여기서 stop_cameras()도 가능
    return render_template("tb4_monitor.html")


# ==========================================================
# Misc APIs
# ==========================================================
@app.route("/api/status")
def api_status():
    # TODO: random → 실제 값으로 교체
    return jsonify(robotA_battery=random.randint(0, 100),
                   robotB_battery=random.randint(0, 100))


@app.route("/api/dispatch_robot")
def dispatch_robot():
    # TODO: 출동 요청 구현
    return jsonify(ok=True, msg="dispatch requested (todo)")


def get_detection_entries():
    conn = sqlite3.connect("mydatabase.db")
    cur = conn.cursor()
    cur.execute("SELECT * FROM detection_table;")
    rows = cur.fetchall()
    conn.close()
    return rows


# ==========================================================
# Graceful shutdown hooks
# ==========================================================
@atexit.register
def _cleanup():
    try:
        stop_cameras()
    except Exception:
        pass
    try:
        ros_tb4_bridge.stop_bridge_system()
    except Exception:
        pass


# ==========================================================
# Main
# ==========================================================
if __name__ == "__main__":
    # Flask 3.x에서도 안전하게 여기서 시작
    ros_tb4_bridge.start_bridge_system(initial_namespaces=["/robot2", "/robot6"])
    app.run(host="0.0.0.0", port=5167, debug=True, use_reloader=False)
