import json
import threading
import time
import cv2
import numpy as np

class EventBus:
    def __init__(self):
        self._cond = threading.Condition()
        self._events = []  # list[dict]
        self._max = 200

    def publish(self, ev: dict):
        with self._cond:
            self._events.append(ev)
            if len(self._events) > self._max:
                self._events = self._events[-self._max:]
            self._cond.notify_all()

    def sse_stream(self, last_idx: int = 0):
        """
        SSE generator. client마다 last_idx를 따로 유지할 수도 있는데,
        간단히 '현재 시점부터' 받게 하려면 last_idx = len(self._events)로 시작하면 됨.
        """
        idx = last_idx
        while True:
            with self._cond:
                while idx >= len(self._events):
                    self._cond.wait(timeout=10.0)
                    # heartbeat (프록시 타임아웃 방지)
                    if idx >= len(self._events):
                        yield "event: ping\ndata: {}\n\n"
                ev = self._events[idx]
                idx += 1
            yield f"event: detection\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"

event_bus = EventBus()

class CameraWorker:
    def __init__(self, camera_key: str, camera_path: str, model, conf_thres=0.25, imgsz=640):
        self.camera_key = camera_key
        self.camera_path = camera_path
        self.model = model
        self.conf_thres = conf_thres
        self.imgsz = imgsz

        self._lock = threading.Lock()
        self._latest_jpeg = None
        self._latest_meta = {"ts": 0, "objects": []}

        self._callbacks = []  # list[callable]
        self._stop = threading.Event()
        self._thread = None

        # 이벤트 중복 방지용 (예: car 검출 연속 알림 스팸 방지)
        self._last_fire_ts = 0.0

    def register_callback(self, fn):
        """fn(ev_dict) 형태 콜백 등록"""
        self._callbacks.append(fn)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def get_latest_jpeg(self):
        with self._lock:
            return self._latest_jpeg

    def get_latest_meta(self):
        with self._lock:
            return dict(self._latest_meta)

    def _emit(self, ev: dict):
        # 1) 파이썬 콜백
        for cb in self._callbacks:
            try:
                cb(ev)
            except Exception as e:
                # 콜백 에러가 워커를 죽이지 않게
                print(f"[callback error] {e}")

        # 2) SSE publish
        event_bus.publish(ev)

    def _run_yolo(self, frame):
        results = self.model.predict(frame, imgsz=self.imgsz, conf=self.conf_thres, verbose=False)
        r = results[0]
        dets = []
        if r.boxes is None:
            return dets

        names = self.model.names
        for b in r.boxes:
            x1, y1, x2, y2 = map(int, b.xyxy[0].tolist())
            conf = float(b.conf[0].item())
            cls = int(b.cls[0].item())
            label = names.get(cls, f"class_{cls}") if isinstance(names, dict) else (names[cls] if cls < len(names) else f"class_{cls}")
            dets.append({"label": label, "conf": conf, "bbox": [x1, y1, x2, y2]})
        return dets

    def _draw(self, frame, dets):
        for d in dets:
            x1, y1, x2, y2 = d["bbox"]
            label = d["label"]
            conf = d["conf"]
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 2)
            cv2.putText(frame, f"{label}: {conf:.2f}", (x1, max(20, y1 - 5)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 0, 0), 2)

        cv2.putText(frame, f"Objects: {len(dets)}", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)

    def _loop(self):
        cap = cv2.VideoCapture(self.camera_path, cv2.CAP_V4L2)
        if not cap.isOpened():
            print(f"[{self.camera_key}] camera open failed: {self.camera_path}")
            return

        while not self._stop.is_set():
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.05)
                continue

            dets = self._run_yolo(frame)
            self._draw(frame, dets)

            # 최신 메타 업데이트
            meta = {"ts": time.time(), "objects": dets}
            ret, buf = cv2.imencode(".jpg", frame)
            if ret:
                with self._lock:
                    self._latest_jpeg = buf.tobytes()
                    self._latest_meta = meta

            # ---- 여기서 "콜백 트리거 조건"을 정하면 됨 ----
            # 예: car가 감지되면 1초 디바운스 후 이벤트 발행
            now = time.time()
            has_car = any(d["label"] == "car" for d in dets)
            if has_car and (now - self._last_fire_ts) >= 1.0:
                self._last_fire_ts = now
                ev = {
                    "ts": now,
                    "camera": self.camera_key,
                    "type": "detected",
                    "label": "car",
                    "objects": dets,  # 필요하면 전체 넣고, 아니면 car만 필터링
                }
                self._emit(ev)

        cap.release()
