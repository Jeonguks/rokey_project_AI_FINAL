import time
import threading
from typing import Optional

class FireLoopPlayer:
    """
    - fire 이벤트가 '계속' 들어오는 동안 mp3를 반복 재생
    - mp3 재생 시간 동안 재시작 금지(= 자연스러운 루프)
    - fire가 끊기면 반복 중단
    """

    def __init__(
        self,
        audio_player,                 # 기존 AudioPlayer 인스턴스 (유지)
        mp3_path: str,
        duration_sec: float,
        fire_hold_sec: float = 1.5,   # 마지막 fire 이벤트 이후 이 시간 안이면 "fire 지속"으로 간주
        poll_sec: float = 0.1,
    ):
        self.audio_player = audio_player
        self.mp3_path = mp3_path
        self.duration_sec = duration_sec
        self.fire_hold_sec = fire_hold_sec
        self.poll_sec = poll_sec

        self._lock = threading.Lock()
        self._last_fire_seen: float = 0.0
        self._next_allowed_play: float = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def shutdown(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    def notify_fire(self):
        """on_detect에서 fire 감지될 때마다 호출"""
        with self._lock:
            self._last_fire_seen = time.time()

    def _fire_is_active(self, now: float) -> bool:
        with self._lock:
            return (now - self._last_fire_seen) <= self.fire_hold_sec

    def _can_play_now(self, now: float) -> bool:
        with self._lock:
            return now >= self._next_allowed_play

    def _mark_play_started(self, now: float):
        # mp3 재생 시간만큼 다시 시작 못 하게 막기
        with self._lock:
            self._next_allowed_play = now + self.duration_sec

    def _play_async(self):
        # AudioPlayer가 blocking일 수도 있어서 별도 스레드로 실행
        def _run():
            try:
                self.audio_player.play(self.mp3_path)
            except Exception as e:
                print(f"[FireLoopPlayer] play error: {e}")

        threading.Thread(target=_run, daemon=True).start()

    def stop_alarm(self, silence_sec: Optional[float] = None):
        """
        수동 경보 종료(ack).
        - 즉시 재생 중지
        - silence_sec 동안 fire가 와도 경보 재시작 안 함
        """
        now = time.time()
        if silence_sec is None:
            silence_sec = self.ack_silence_sec

        with self._lock:
            self._ack_until = now + float(silence_sec)
            # fire 활성 상태도 끊어버림
            self._last_fire_seen = 0.0

        # 즉시 재생 중지
        try:
            self.audio_player.stop()
        except Exception:
            pass

    def _loop(self):
        while not self._stop.is_set():
            now = time.time()

            if self._fire_is_active(now) and self._can_play_now(now):
                self._mark_play_started(now)
                self._play_async()

            time.sleep(self.poll_sec)
