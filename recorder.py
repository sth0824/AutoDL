"""AutoDL 화면 녹화 엔진.

ffmpeg의 gdigrab(Windows 화면 캡처)을 써서, 사용자가 고른 화면 영역만
동영상(MP4)으로 녹화한다. ffmpeg 바이너리는 downloader와 동일하게
시스템 PATH → imageio-ffmpeg 순으로 찾는다.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass

import downloader  # find_ffmpeg 재사용

# Windows에서 콘솔 창이 깜빡이지 않게.
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass
class Region:
    x: int
    y: int
    width: int
    height: int

    def normalized(self) -> "Region":
        """음수 폭/높이(역방향 드래그)를 정상화하고, 폭·높이를 짝수로 맞춘다.

        libx264 + yuv420p는 짝수 해상도를 요구한다.
        """
        x, y, w, h = self.x, self.y, self.width, self.height
        if w < 0:
            x, w = x + w, -w
        if h < 0:
            y, h = y + h, -h
        w -= w % 2
        h -= h % 2
        return Region(x, y, w, h)


class RegionRecorder:
    """gdigrab로 지정 영역을 녹화하는 ffmpeg 프로세스 래퍼."""

    def __init__(self, region: Region, output_path: str, fps: int = 30):
        self.region = region.normalized()
        self.output_path = output_path
        self.fps = fps
        self._proc: subprocess.Popen | None = None

    @property
    def is_recording(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.is_recording:
            return
        ff = downloader.find_ffmpeg()
        if not ff:
            raise RuntimeError("ffmpeg를 찾을 수 없습니다.")
        r = self.region
        if r.width < 2 or r.height < 2:
            raise ValueError("녹화 영역이 너무 작습니다.")

        os.makedirs(os.path.dirname(os.path.abspath(self.output_path)), exist_ok=True)

        cmd = [
            ff, "-hide_banner", "-y",
            "-f", "gdigrab",
            "-framerate", str(self.fps),
            "-offset_x", str(r.x),
            "-offset_y", str(r.y),
            "-video_size", f"{r.width}x{r.height}",
            "-draw_mouse", "1",
            "-i", "desktop",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            self.output_path,
        ]
        # stdin은 'q'로 정상 종료시키는 데 쓴다(MP4 moov atom 정상 기록).
        # stderr는 한글 콘솔 인코딩 문제를 피하려 버린다.
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )

    def stop(self, timeout: float = 8.0) -> None:
        """녹화를 정상 종료한다."""
        if not self._proc:
            return
        if self._proc.poll() is None:
            try:
                assert self._proc.stdin is not None
                self._proc.stdin.write(b"q")
                self._proc.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                self._proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        self._proc = None

    def failed_early(self) -> bool:
        """시작 직후 ffmpeg가 0이 아닌 코드로 죽었는지(영역 오류 등)."""
        return self._proc is not None and (self._proc.poll() or 0) != 0
