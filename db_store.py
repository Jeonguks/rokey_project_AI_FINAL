# db_store.py
import sqlite3
import time
from threading import Lock
from typing import Optional, Dict, Any

DB_PATH = "mydatabase.db"

_db_write_lock = Lock()
_last_db_write_ts: Dict[str, float] = {}  # ns -> last write time
DB_WRITE_MIN_INTERVAL_SEC = 2.0  # 폴링 저장 폭주 방지


def db_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS robot_status_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,

            robot_ns TEXT NOT NULL,
            connected INTEGER NOT NULL,
            last_seen_ts REAL NOT NULL,

            battery_percent INTEGER NOT NULL,
            battery_voltage REAL,
            battery_current REAL,

            pose_frame TEXT,
            x REAL,
            y REAL,
            yaw_deg REAL,

            lin_vel REAL,
            ang_vel REAL,

            incident_status TEXT,
            incident_last_seen_ts REAL
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_robot_status_log_ts ON robot_status_log(ts);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_robot_status_log_ns ON robot_status_log(robot_ns);")
        conn.commit()
    finally:
        conn.close()


def save_status_snapshot(ns: str, st: Dict[str, Any], incident: Optional[Dict[str, Any]] = None):
    """상태 조회 시 스냅샷을 DB에 저장(과도한 저장은 throttle)."""
    now = time.time()

    last = _last_db_write_ts.get(ns, 0.0)
    if (now - last) < DB_WRITE_MIN_INTERVAL_SEC:
        return
    _last_db_write_ts[ns] = now

    inc_status = None
    inc_last_seen = None
    if isinstance(incident, dict):
        inc_status = incident.get("status")
        try:
            inc_last_seen = float(incident.get("last_seen_ts", 0.0) or 0.0)
        except Exception:
            inc_last_seen = 0.0

    def f(key, default=0.0):
        try:
            return float(st.get(key, default) or default)
        except Exception:
            return float(default)

    def i(key, default=0):
        try:
            return int(st.get(key, default) or default)
        except Exception:
            return int(default)

    row = (
        now,
        str(st.get("robot_ns", ns)),
        1 if bool(st.get("connected", False)) else 0,
        f("last_seen_ts", 0.0),

        i("battery_percent", 0),
        st.get("battery_voltage", None),
        st.get("battery_current", None),

        st.get("pose_frame", None),
        f("x", 0.0),
        f("y", 0.0),
        f("yaw_deg", 0.0),

        f("lin_vel", 0.0),
        f("ang_vel", 0.0),

        inc_status,
        inc_last_seen,
    )

    with _db_write_lock:
        conn = db_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
            INSERT INTO robot_status_log (
                ts,
                robot_ns, connected, last_seen_ts,
                battery_percent, battery_voltage, battery_current,
                pose_frame, x, y, yaw_deg,
                lin_vel, ang_vel,
                incident_status, incident_last_seen_ts
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, row)
            conn.commit()
        finally:
            conn.close()


def fetch_robot_status(ns: Optional[str] = None, limit: int = 200):
    limit = max(1, min(2000, int(limit)))
    conn = db_conn()
    try:
        cur = conn.cursor()
        if ns:
            cur.execute("""
                SELECT * FROM robot_status_log
                WHERE robot_ns = ?
                ORDER BY id DESC
                LIMIT ?
            """, (ns, limit))
        else:
            cur.execute("""
                SELECT * FROM robot_status_log
                ORDER BY id DESC
                LIMIT ?
            """, (limit,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def clear_robot_status():
    conn = db_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM robot_status_log;")
        conn.commit()
    finally:
        conn.close()
