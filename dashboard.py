from flask import Flask, render_template, request, redirect, url_for, session, flash, Response, jsonify, abort
import random
import cv2
import numpy as np
import atexit
import random
import sqlite3


app = Flask(__name__)
app.secret_key = "your_secret_key"

# Hardcoded user credentials for demonstration
USERNAME = "user"
PASSWORD = "password"


# CAM0 = "/dev/v4l/by-path/pci-0000:00:14.0-usb-0:6:1.0-video-index0" #-> ../../video0 내장 카메라
CAM1 = "/dev/v4l/by-path/pci-0000:00:14.0-usb-0:1:1.0-video-index0" #-> ../../video7 1번포트
CAM2 = "/dev/v4l/by-path/pci-0000:00:14.0-usb-0:3:1.0-video-index0" #-> ../../video3 2번 포트
CAM3 = "/dev/v4l/by-path/pci-0000:00:14.0-usb-0:2:1.0-video-index0" #-> ../../video5 3번 포트

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

    # todo 로봇에서 데이터 연동 필요
    robotA_battery = clamp_percent(0, 0)
    robotB_battery = clamp_percent(0, 0)
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
    return Response(generate_frames_box(CAM1), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/video_feed2")
def video_feed2():
    return Response(generate_frames_box(CAM2), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/video_feed3")
def video_feed3():
    return Response(generate_frames_box(CAM3), mimetype="multipart/x-mixed-replace; boundary=frame")


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
    app.run(debug=True, use_reloader=False, port=5167)


