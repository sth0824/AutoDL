"""AutoDL 화면 녹화 엔진.

두 가지 방식:
- RegionRecorder: ffmpeg gdigrab으로 화면의 고정 영역 녹화.
- WindowRecorder: Windows Graphics Capture(WGC)로 특정 창 녹화(가려져도 OK).

두 방식 모두 시스템 소리(스피커 루프백)를 함께 녹음해 ffmpeg로 합친다.
ffmpeg 바이너리는 downloader와 동일하게 시스템 PATH → imageio-ffmpeg 순으로 찾는다.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

import audiocap
import downloader  # find_ffmpeg 재사용

# Windows에서 콘솔 창이 깜빡이지 않게.
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _safe_remove(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _mux_av(video_tmp: str, audio: Optional[audiocap.LoopbackRecorder],
            output_path: str) -> None:
    """녹화된 (무음)영상과 녹음된 오디오를 합쳐 output_path로 만든다.

    오디오가 없거나 실패하면 영상만 그대로 결과로 쓴다(무음 fallback).
    """
    audio_wav = None
    if audio is not None:
        audio.stop()
        audio_wav = audio.wav_path
        ok = (
            audio.error is None
            and os.path.exists(audio_wav)
            and os.path.getsize(audio_wav) > 1024
        )
        if ok:
            ff = downloader.find_ffmpeg()
            cmd = [
                ff, "-hide_banner", "-y",
                "-i", video_tmp,
                "-i", audio_wav,
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                output_path,
            ]
            r = subprocess.run(
                cmd, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=_CREATE_NO_WINDOW,
            )
            if r.returncode == 0 and os.path.exists(output_path):
                _safe_remove(video_tmp)
                _safe_remove(audio_wav)
                return

    # fallback: 무음 영상만
    _safe_remove(output_path)
    try:
        os.replace(video_tmp, output_path)
    except OSError:
        pass
    _safe_remove(audio_wav)


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

    def __init__(self, region: Region, output_path: str, fps: int = 30,
                 record_audio: bool = True, crf: int = 18,
                 show_cursor: bool = False):
        self.region = region.normalized()
        self.output_path = output_path
        self.fps = fps
        self.record_audio = record_audio
        self.crf = crf
        self.show_cursor = show_cursor
        self._proc: subprocess.Popen | None = None
        self._video_tmp = output_path + ".video.mp4"
        self._audio: audiocap.LoopbackRecorder | None = None

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
            "-draw_mouse", "1" if self.show_cursor else "0",
            "-i", "desktop",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", str(self.crf),
            "-pix_fmt", "yuv420p",
            self._video_tmp,
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

        if self.record_audio and audiocap.LoopbackRecorder.available():
            self._audio = audiocap.LoopbackRecorder(output_path + ".audio.wav")
            self._audio.start()

    def stop(self, timeout: float = 8.0) -> None:
        """녹화를 정상 종료하고 오디오와 합친다."""
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
        _mux_av(self._video_tmp, self._audio, self.output_path)
        self._audio = None

    def failed_early(self) -> bool:
        """시작 직후 ffmpeg가 0이 아닌 코드로 죽었는지(영역 오류 등)."""
        return self._proc is not None and (self._proc.poll() or 0) != 0


def _start_ffmpeg_rawvideo(ff: str, width: int, height: int, fps: int, out: str,
                           crf: int = 18):
    """BGRA 원시 프레임을 stdin으로 받아 H.264 MP4로 인코딩하는 ffmpeg 시작."""
    cmd = [
        ff, "-hide_banner", "-y",
        "-f", "rawvideo",
        "-pix_fmt", "bgra",
        "-s", f"{width}x{height}",
        "-r", str(fps),
        "-i", "-",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        out,
    ]
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_CREATE_NO_WINDOW,
    )


def _start_window_audio(hwnd: int, wav: str, mode: str):
    """창의 오디오 녹음기를 시작한다.

    mode="app": 그 창의 앱(프로세스) 소리만 녹음(프로세스 루프백). 실패하면
    전체 시스템 소리로 자동 폴백한다. mode="system": 처음부터 전체 소리.
    반환: (recorder_or_None, 실제_사용_모드)
    """
    if mode == "app":
        try:
            import procaudio
            import winutil

            if procaudio.ProcessAudioRecorder.available():
                pid = winutil.pid_of_window(hwnd)
                if pid:
                    rec = procaudio.ProcessAudioRecorder(pid, wav)
                    rec.start()
                    if rec.error is None:
                        return rec, "app"
                    rec.stop()  # 초기화 실패 → 폴백
        except Exception:
            pass  # 아래 시스템 폴백

    if audiocap.LoopbackRecorder.available():
        rec = audiocap.LoopbackRecorder(wav)
        rec.start()
        return rec, "system"
    return None, ""


class WindowRecorder:
    """특정 창을 Windows Graphics Capture(WGC)로 녹화한다.

    다른 창이 위를 덮거나 다른 앱으로 전환해도 그 창의 내용만 캡처된다.
    crop이 주어지면 창 기준 (left, top, width, height) 영역만 잘라 녹화한다.

    프레임은 변화가 있을 때만 도착하므로, 별도 writer 스레드가 '가장 최근
    프레임'을 고정 FPS로 ffmpeg에 흘려보내 재생 길이를 정확히 맞춘다.
    """

    def __init__(
        self,
        hwnd: int,
        output_path: str,
        fps: int = 30,
        crop: Optional[tuple[int, int, int, int]] = None,
        record_audio: bool = True,
        crf: int = 18,
        show_cursor: bool = False,
        audio_mode: str = "app",
    ):
        self.hwnd = hwnd
        self.output_path = output_path
        self.fps = fps
        self.crop = crop
        self.record_audio = record_audio
        self.crf = crf
        self.show_cursor = show_cursor
        self.audio_mode = audio_mode  # "app" = 이 창의 앱 소리만, "system" = 전체
        self.audio_mode_used = ""     # 실제 사용된 모드(폴백 결과 보고용)

        self._cap = None
        self._control = None
        self._proc: subprocess.Popen | None = None
        self._writer: threading.Thread | None = None
        self._lock = threading.Lock()
        self._latest: bytes | None = None
        self._out_size: tuple[int, int] | None = None
        self._stop = False
        self.error: str | None = None
        self._video_tmp = output_path + ".video.mp4"
        self._audio: audiocap.LoopbackRecorder | None = None

    @property
    def is_recording(self) -> bool:
        return self._control is not None and not self._stop

    # ---- WGC 콜백 ----
    def _on_frame(self, frame, _ctrl) -> None:
        try:
            import cv2  # 크기 변화 시 리사이즈용
            buf = frame.frame_buffer  # (h, w, 4) BGRA
            fh, fw = buf.shape[0], buf.shape[1]

            if self.crop:
                l, t, w, h = self.crop
                l = max(0, min(l, fw - 2))
                t = max(0, min(t, fh - 2))
                w = min(w, fw - l)
                h = min(h, fh - t)
                w -= w % 2
                h -= h % 2
                buf = buf[t:t + h, l:l + w]
            else:
                h = fh - fh % 2
                w = fw - fw % 2
                buf = buf[:h, :w]

            if w < 2 or h < 2:
                return

            with self._lock:
                if self._out_size is None:
                    self._out_size = (w, h)
                ow, oh = self._out_size
                if (w, h) != (ow, oh):
                    # 창 크기가 바뀌면 첫 크기에 맞춰 리사이즈(인코더 입력 고정).
                    buf = cv2.resize(buf, (ow, oh))
                self._latest = bytes(buf.tobytes())
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"

    def _on_closed(self) -> None:
        pass

    # ---- writer 스레드 ----
    def _writer_loop(self) -> None:
        interval = 1.0 / self.fps
        next_t = time.perf_counter()
        while not self._stop:
            with self._lock:
                data = self._latest
            if data and self._proc and self._proc.stdin:
                try:
                    self._proc.stdin.write(data)
                except (OSError, ValueError):
                    break
            next_t += interval
            delay = next_t - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.perf_counter()

    # ---- 제어 ----
    def start(self) -> None:
        ff = downloader.find_ffmpeg()
        if not ff:
            raise RuntimeError("ffmpeg를 찾을 수 없습니다.")
        import windows_capture as wc

        self._cap = wc.WindowsCapture(
            cursor_capture=self.show_cursor,
            draw_border=False,
            window_hwnd=self.hwnd,
        )
        # event()는 함수 __name__으로 분기하므로 핸들러를 직접 지정한다.
        self._cap.frame_handler = self._on_frame
        self._cap.closed_handler = self._on_closed
        self._control = self._cap.start_free_threaded()

        # 첫 프레임으로 출력 크기를 확정할 때까지 잠깐 대기
        t0 = time.perf_counter()
        while self._out_size is None and time.perf_counter() - t0 < 4.0:
            if self.error:
                break
            time.sleep(0.03)
        if self._out_size is None:
            self.stop()
            raise RuntimeError(
                "창 프레임을 받지 못했습니다. (최소화되어 있거나 캡처 불가한 창)"
            )

        os.makedirs(os.path.dirname(os.path.abspath(self.output_path)), exist_ok=True)
        w, h = self._out_size
        self._proc = _start_ffmpeg_rawvideo(
            ff, w, h, self.fps, self._video_tmp, crf=self.crf
        )
        self._writer = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer.start()

        if self.record_audio:
            self._audio, self.audio_mode_used = _start_window_audio(
                self.hwnd, self.output_path + ".audio.wav", self.audio_mode
            )

    def stop(self, timeout: float = 10.0) -> None:
        if self._stop:
            return
        self._stop = True
        if self._control is not None:
            try:
                self._control.stop()
            except Exception:
                pass
            self._control = None
        if self._writer:
            self._writer.join(timeout=2)
            self._writer = None
        if self._proc:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._proc.terminate()
            self._proc = None
        _mux_av(self._video_tmp, self._audio, self.output_path)
        self._audio = None
