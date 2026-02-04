# audio_player.py
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from typing import Optional

from mutagen.mp3 import MP3



# mp3 초 구하는 유틸
def get_mp3_duration_sec(path: str) -> float:
    return float(MP3(path).info.length)

class AudioPlayer:
    """
    간단/안정 MP3 재생기

    - 기본 정책: 재생 중이면 play()가 와도 무시(겹침 방지)
    - restart=True: 재생 중이어도 강제 재시작
    - stop(): 재생 중지
    - is_playing(): 재생 상태
    """

    def __init__(self, backend: str = "auto", volume: Optional[float] = None):
        """
        backend:
          - "auto"  : ffplay -> mpg123 순으로 탐색
          - "ffplay": ffplay 강제
          - "mpg123": mpg123 강제

        volume:
          - ffplay일 때만 사용(0.0~1.0 권장). None이면 기본값.
        """
        self.backend = backend
        self.volume = volume

        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._current_path: Optional[str] = None

        # 백엔드 탐색
        self._cmd_kind = self._select_backend(backend)

    def _select_backend(self, backend: str) -> str:
        if backend == "ffplay":
            if not shutil.which("ffplay"):
                raise RuntimeError("ffplay not found. Install: sudo apt install ffmpeg")
            return "ffplay"

        if backend == "mpg123":
            if not shutil.which("mpg123"):
                raise RuntimeError("mpg123 not found. Install: sudo apt install mpg123")
            return "mpg123"

        # auto
        if shutil.which("ffplay"):
            return "ffplay"
        if shutil.which("mpg123"):
            return "mpg123"

        raise RuntimeError(
            "No audio backend found. Install one:\n"
            "- sudo apt install ffmpeg   (for ffplay)\n"
            "- sudo apt install mpg123"
        )

    def is_playing(self) -> bool:
        with self._lock:
            return self._proc is not None and (self._proc.poll() is None)

    def current(self) -> Optional[str]:
        with self._lock:
            return self._current_path

    def stop(self, timeout: float = 0.5) -> None:
        """현재 재생 중인 프로세스를 종료."""
        proc = None
        with self._lock:
            proc = self._proc
        
        if not proc:
            return

        try:
            proc.terminate()
            proc.wait(timeout=timeout)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        finally:
            with self._lock:
                if self._proc is proc:
                    self._proc = None
                    self._current_path = None

    def play(self, path: str, restart: bool = False) -> bool:
        """
        path 재생.
        - restart=False(기본): 재생 중이면 무시하고 False 리턴
        - restart=True: 재생 중이어도 stop 후 재생, True 리턴
        """
        if not path or not os.path.exists(path):
            raise FileNotFoundError(path)

        with self._lock:
            # 재생 중이면 기본적으로 무시
            if self._proc is not None and self._proc.poll() is None:
                if not restart:
                    return False

        if restart:
            self.stop()

        cmd = self._build_cmd(path)

        # subprocess 실행 (비동기)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )

        with self._lock:
            self._proc = proc
            self._current_path = path

        # 종료 감시(프로세스 끝나면 상태 정리)
        threading.Thread(target=self._watch_proc, args=(proc,), daemon=True).start()
        return True

    def _watch_proc(self, proc: subprocess.Popen) -> None:
        try:
            proc.wait()
        finally:
            with self._lock:
                if self._proc is proc:
                    self._proc = None
                    self._current_path = None

    def _build_cmd(self, path: str) -> list[str]:
        if self._cmd_kind == "ffplay":
            cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error"]
            # volume(옵션): ffplay는 -volume 0~100
            if self.volume is not None:
                v = int(max(0.0, min(1.0, float(self.volume))) * 100)
                cmd += ["-volume", str(v)]
            cmd += [path]
            return cmd

        if self._cmd_kind == "mpg123":
            # -q : quiet
            return ["mpg123", "-q", path]

        raise RuntimeError(f"Unknown backend: {self._cmd_kind}")
