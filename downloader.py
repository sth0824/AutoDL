"""AutoDL 다운로드 엔진.

yt-dlp를 감싸 '가능한 최고화질 MP4'로 영상을 받는 핵심 로직.
GUI(autodl.py)와 CLI 양쪽에서 공유한다.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Callable, Optional

import yt_dlp


def find_ffmpeg() -> Optional[str]:
    """ffmpeg 실행 파일 경로를 찾는다.

    1) 시스템 PATH에 ffmpeg가 있으면 그걸 사용한다.
    2) 없으면 pip로 설치되는 imageio-ffmpeg가 들고 있는 정적 빌드를 사용한다.

    둘 다 없으면 None. (이 경우 yt-dlp는 영상/음성을 합치지 못해
    화질이 떨어질 수 있다.)
    """
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


@dataclass
class DownloadResult:
    success: bool
    title: str = ""
    filepath: str = ""
    error: str = ""


# 진행 상황 콜백: dict(yt-dlp progress hook 형식)을 받는다.
ProgressCallback = Callable[[dict], None]
# 일반 로그 콜백: 문자열 한 줄을 받는다.
LogCallback = Callable[[str], None]


class _LoggerBridge:
    """yt-dlp 내부 로그를 우리 로그 콜백으로 흘려보낸다."""

    def __init__(self, log: LogCallback):
        self._log = log

    def debug(self, msg: str) -> None:
        # yt-dlp는 일반 정보도 debug로 보낸다. '[debug]' 접두사는 건너뛴다.
        if msg.startswith("[debug] "):
            return
        self._log(msg)

    def info(self, msg: str) -> None:
        self._log(msg)

    def warning(self, msg: str) -> None:
        self._log(f"⚠ {msg}")

    def error(self, msg: str) -> None:
        self._log(f"✖ {msg}")


def build_options(
    output_dir: str,
    *,
    remux_mp4: bool = True,
    progress_hook: Optional[ProgressCallback] = None,
    log: Optional[LogCallback] = None,
) -> dict:
    """yt-dlp 옵션 dict를 만든다.

    remux_mp4=True 이면 최고화질 영상+음성을 받아 MP4로 합친다.
    """
    ffmpeg = find_ffmpeg()

    opts: dict = {
        # 최고 해상도 영상 + 최고 음질 오디오를 따로 받아 합친다.
        # ffmpeg가 없으면 단일 파일 중 최선을 받는다.
        "format": "bv*+ba/b" if ffmpeg else "best",
        "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
        "noprogress": True,  # 콘솔 진행바 대신 progress_hook 사용
        "noplaylist": True,  # 기본은 단일 영상만 (재생목록 URL이어도)
        "ignoreerrors": False,
        "retries": 5,
        "fragment_retries": 5,
        "restrictfilenames": False,
    }

    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
        if remux_mp4:
            # 컨테이너만 MP4로 바꾼다(재인코딩 X → 화질 손실 없음).
            # mp4로 못 담는 코덱이면 yt-dlp가 알아서 원본 컨테이너 유지.
            opts["merge_output_format"] = "mp4"
            opts["postprocessors"] = [
                {
                    "key": "FFmpegVideoRemuxer",
                    "preferedformat": "mp4",
                }
            ]

    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    if log:
        opts["logger"] = _LoggerBridge(log)

    return opts


def download(
    url: str,
    output_dir: str,
    *,
    remux_mp4: bool = True,
    progress_hook: Optional[ProgressCallback] = None,
    log: Optional[LogCallback] = None,
) -> DownloadResult:
    """단일 URL을 다운로드한다."""
    os.makedirs(output_dir, exist_ok=True)
    opts = build_options(
        output_dir,
        remux_mp4=remux_mp4,
        progress_hook=progress_hook,
        log=log,
    )

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "") if info else ""
            filepath = ""
            if info:
                # 후처리 후 실제 경로를 최대한 정확히 얻는다.
                reqs = info.get("requested_downloads")
                if reqs:
                    filepath = reqs[0].get("filepath", "")
                if not filepath:
                    filepath = ydl.prepare_filename(info)
            return DownloadResult(success=True, title=title, filepath=filepath)
    except yt_dlp.utils.DownloadError as e:
        return DownloadResult(success=False, error=str(e))
    except Exception as e:  # noqa: BLE001 - GUI에 그대로 표시
        return DownloadResult(success=False, error=f"{type(e).__name__}: {e}")
