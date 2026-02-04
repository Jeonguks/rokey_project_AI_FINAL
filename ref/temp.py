

from flask import Flask, render_template, request, redirect, url_for, session, Response, jsonify
import cv2
import numpy as np
import sqlite3
import time
import threading
import os
from ultralytics import YOLO

app = Flask(__name__)
app.secret_key = "your_secret_key"

# Hardcoded user credentials for demonstration
USERNAME = "user"
PASSWORD = "password"

# 카메라 디바이스 경로
CAM1 = "/dev/v4l/by-path/pci-0000:00:14.0-usb-0:1:1.0-video-index0"
CAM2 = "/dev/v4l/by-path/pci-0000:00:14.0-usb-0:3:1.0-video-index0"
CAM3 = "/dev/v4l/by-path/pci-0000:00:14.0-usb-0:2:1.0-video-index0"

# ----------------------------
# YOLO 설정
# ----------------------------
YOLO_MODEL_PATH = "/home/rokey/rokey_ws/src/web_cam_detect/best.pt"   # <-- 여기만 바꾸세요
YOLO_CONF_THRES = 0.25                     # confidence threshold
YOLO_IMG_SZ = 640                          # inference imgsz (성능/정확도 trade-off)

# 모델은 1회 로드
if not os.path.exists(YOLO_MODEL_PATH):
    raise FileNotFoundError(f"YOLO model not found: {YOLO_MODEL_PATH}")

model = YOLO(YOLO_MODEL_PATH)
class_names = model.names  # dict or list (ultralytics가 제공)

# 최근 디텍션 결과 공유 (API로 조회)
last_detections = {
    "cam1": {"ts": 0, "objects": []},
    "cam2": {"ts": 0, "objects": []},
    "cam3": {"ts": 0, "objects": []},
}
last_lock = threading.Lock()

# ----------------------------
# DB 설정 (옵션)
# ----------------------------
DB_PATH = "mydatabase.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS detection_table (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            camera TEXT NOT NULL,
            label TEXT NOT NULL,
            conf REAL NOT NULL,
            x1 INTEGER NOT NULL,
            y1 INTEGER NOT NULL,
            x2 INTEGER NOT NULL,
            y2 INTEGER NOT NULL
        );
    """)
    conn.commit()
    conn.close()

init_db()

def insert_detections(camera_key: str, detections: list):
    """
    detections: [{"label": str, "conf": float, "bbox": [x1,y1,x2,y2]}, ...]
    """
    if not detections:
        return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    ts = time.time()
    rows = []
    for d in detections:
        x1, y1, x2, y2 = d["bbox"]
        rows.append((ts, camera_key, d["label"], float(d["conf"]), int(x1), int(y1), int(x2), int(y2)))
    cur.executemany(
        "INSERT INTO detection_table(ts, camera, label, conf, x1, y1, x2, y2) VALUES(?,?,?,?,?,?,?,?)",
        rows
    )
    conn.commit()
    conn.close()

# ----------------------------
# Helpers
# ----------------------------
def safe_int(v, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return default

def clamp_percent(v, default: int) -> int:
    n = safe_int(v, default)
    return max(0, min(100, n))

# ----------------------------
# Routes (Auth / Dashboard)
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
        return redirect(url_for("login"))
    return render_template("login.html")

@app.route("/dashboard")
def dashboard():
    if "username" not in session:
        return redirect(url_for("login"))

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
# YOLO + Stream
# ----------------------------
# (예시) 화면에 사각형 영역 표시하고 싶으면 사용
pt_1 = (460, 0)
pt_2 = (640, 0)
pt_3 = (640, 120)
pt_4 = (460, 120)
coordinates = [pt_1, pt_2, pt_3, pt_4]

def run_yolo_on_frame(frame):
    """
    return: detections list
      [{"label": str, "conf": float, "bbox": [x1,y1,x2,y2]}, ...]
    """
    # Ultralytics YOLOv8: results = model(frame)
    # stream=False 기본. 여기서는 1장이라 결과 1개.
    results = model.predict(frame, imgsz=YOLO_IMG_SZ, conf=YOLO_CONF_THRES, verbose=False)
    r = results[0]

    dets = []
    if r.boxes is None:
        return dets

    for b in r.boxes:
        # xyxy: tensor([x1,y1,x2,y2])
        x1, y1, x2, y2 = map(int, b.xyxy[0].tolist())
        conf = float(b.conf[0].item())
        cls = int(b.cls[0].item())

        # model.names가 dict일 수도, list일 수도 있음
        if isinstance(class_names, dict):
            label = class_names.get(cls, f"class_{cls}")
        else:
            label = class_names[cls] if 0 <= cls < len(class_names) else f"class_{cls}"

        dets.append({"label": label, "conf": conf, "bbox": [x1, y1, x2, y2]})
    return dets

def draw_overlays(frame, dets):
    # (옵션) ROI 박스 표시
    for (x, y) in coordinates:
        cv2.circle(frame, (x, y), 5, (0, 0, 255), -1)
    pts = np.array(coordinates, np.int32).reshape((-1, 1, 2))
    cv2.polylines(frame, [pts], isClosed=True, color=(0, 255, 0), thickness=2)

    # YOLO 박스 표시
    for d in dets:
        x1, y1, x2, y2 = d["bbox"]
        label = d["label"]
        conf = d["conf"]

        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 2)
        text = f"{label}: {conf:.2f}"
        cv2.putText(frame, text, (x1, max(20, y1 - 5)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 0, 0), 2)

    cv2.putText(frame, f"Objects: {len(dets)}", (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)

def generate_frames_yolo(camera_path: str, camera_key: str, save_to_db: bool = True):
    cap = cv2.VideoCapture(camera_path, cv2.CAP_V4L2)
    if not cap.isOpened():
        return

    # (옵션) 해상도 강제
    # cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    # cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

    while True:
        ok, frame = cap.read()
        if not ok:
            break

        dets = run_yolo_on_frame(frame)
        draw_overlays(frame, dets)

        # 최근 결과 업데이트
        with last_lock:
            last_detections[camera_key] = {"ts": time.time(), "objects": dets}

        # DB 저장 (원하면 끄기)
        if save_to_db:
            insert_detections(camera_key, dets)

        ret, buffer = cv2.imencode(".jpg", frame)
        if not ret:
            continue

        frame_bytes = buffer.tobytes()
        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n")

    cap.release()

@app.route("/video_feed1")
def video_feed1():
    return Response(generate_frames_yolo(CAM1, "cam1"), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/video_feed2")
def video_feed2():
    return Response(generate_frames_yolo(CAM2, "cam2"), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/video_feed3")
def video_feed3():
    return Response(generate_frames_yolo(CAM3, "cam3"), mimetype="multipart/x-mixed-replace; boundary=frame")

# ----------------------------
# Status / Detections API
# ----------------------------
@app.route("/api/status")
def api_status():
    # TODO: random 대신 실제 배터리 수신 값으로 바꾸면 끝
    import random
    return jsonify({
        "robotA_battery": random.randint(0, 100),
        "robotB_battery": random.randint(0, 100)
    })

@app.route("/api/detections")
def api_detections():
    # 최근 디텍션 결과 (cam1/2/3)
    with last_lock:
        return jsonify(last_detections)

# ----------------------------
# DB 조회 (원하면 그대로 사용)
# ----------------------------
@app.route("/api/db_detections")
def api_db_detections():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT ts, camera, label, conf, x1, y1, x2, y2 FROM detection_table ORDER BY id DESC LIMIT 200;")
    rows = cur.fetchall()
    conn.close()

    data = []
    for r in rows:
        data.append({
            "ts": r[0], "camera": r[1], "label": r[2], "conf": r[3],
            "bbox": [r[4], r[5], r[6], r[7]]
        })
    return jsonify(data)

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
