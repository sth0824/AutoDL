"""시스템 소리(스피커 출력) 루프백 녹음.

WASAPI loopback으로 '지금 스피커로 나오는 소리'를 그대로 WAV에 담는다.
녹화 영상과 동시에 녹음한 뒤 ffmpeg로 합친다(muxing).
"""

from __future__ import annotations

import threading
import wave


class LoopbackRecorder:
    """기본 출력 장치(스피커)의 소리를 WAV 파일로 녹음."""

    def __init__(self, wav_path: str):
        self.wav_path = wav_path
        self.error: str | None = None
        self._stop = False
        self._thread: threading.Thread | None = None
        self._started = threading.Event()

    @staticmethod
    def available() -> bool:
        try:
            import pyaudiowpatch  # noqa: F401

            return True
        except Exception:
            return False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # 첫 read까지 잠깐 기다려 영상과 시작 시점을 맞춘다.
        self._started.wait(timeout=2.0)

    def _run(self) -> None:
        try:
            import pyaudiowpatch as pa
        except Exception as e:  # noqa: BLE001
            self.error = f"오디오 모듈 없음: {e}"
            self._started.set()
            return

        p = pa.PyAudio()
        stream = None
        wf = None
        try:
            wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
            out = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
            # 기본 스피커에 대응하는 loopback 장치를 찾는다.
            lb = None
            for d in p.get_loopback_device_info_generator():
                if out["name"] in d["name"]:
                    lb = d
                    break
            if lb is None:
                for d in p.get_loopback_device_info_generator():
                    lb = d
                    break
            if lb is None:
                self.error = "루프백 오디오 장치를 찾을 수 없습니다."
                return

            channels = int(lb["maxInputChannels"]) or 2
            rate = int(lb["defaultSampleRate"])

            wf = wave.open(self.wav_path, "wb")
            wf.setnchannels(channels)
            wf.setsampwidth(p.get_sample_size(pa.paInt16))
            wf.setframerate(rate)

            stream = p.open(
                format=pa.paInt16,
                channels=channels,
                rate=rate,
                frames_per_buffer=1024,
                input=True,
                input_device_index=lb["index"],
            )
            self._started.set()
            while not self._stop:
                wf.writeframes(stream.read(1024, exception_on_overflow=False))
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self._started.set()
            try:
                if stream is not None:
                    stream.stop_stream()
                    stream.close()
            except Exception:
                pass
            try:
                if wf is not None:
                    wf.close()
            except Exception:
                pass
            p.terminate()

    def stop(self) -> None:
        self._stop = True
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
