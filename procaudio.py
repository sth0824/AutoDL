"""특정 프로세스(앱)의 오디오만 녹음 — WASAPI 프로세스 루프백.

Windows 10 2004(빌드 19041)+ 에서만 동작. 지정한 PID와 그 자식
프로세스들이 내는 소리만 캡처하고, 다른 앱·시스템 알림 소리는 제외한다.
(브라우저는 모든 탭이 한 프로세스 트리라 탭별 분리는 불가 — 그 브라우저
전체 소리가 잡힌다.)

핵심 주의점:
- ActivateAudioInterfaceAsync는 MTA 스레드에서 호출해야 하므로 comtypes를
  MTA로 초기화한다(sys.coinit_flags=0).
- 완료 핸들러 COM 객체는 IAgileObject를 구현해야 한다. 안 그러면
  E_ILLEGAL_METHOD_CALL로 거부된다.
"""

from __future__ import annotations

import sys

# comtypes import 전에 MTA로 지정해야 한다.
sys.coinit_flags = 0  # COINIT_MULTITHREADED

import ctypes  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import wave  # noqa: E402
from ctypes import (  # noqa: E402
    POINTER, byref, c_byte, c_longlong, c_uint, c_ulonglong, c_void_p, cast,
    wintypes,
)

import comtypes  # noqa: E402
from comtypes import GUID, COMMETHOD, COMObject, IUnknown  # noqa: E402

HRESULT = ctypes.c_long
DWORD = wintypes.DWORD

_VIRTUAL = "VAD\\Process_Loopback"
_SHARED = 0
_FLAG_LOOPBACK = 0x00020000
_FLAG_EVENTCALLBACK = 0x00040000
_VT_BLOB = 65
_SILENT = 0x2


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", DWORD), ("nAvgBytesPerSec", DWORD),
        ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", wintypes.USHORT), ("r1", wintypes.USHORT),
        ("r2", wintypes.USHORT), ("r3", wintypes.USHORT),
        ("cbSize", wintypes.ULONG), ("pad", wintypes.ULONG),
        ("pBlobData", c_void_p),
    ]


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ActivationType", DWORD),       # 1 = PROCESS_LOOPBACK
        ("TargetProcessId", DWORD),
        ("ProcessLoopbackMode", DWORD),  # 0 = INCLUDE_TARGET_PROCESS_TREE
    ]


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetActivateResult",
                  (["out"], POINTER(HRESULT), "activateResult"),
                  (["out"], POINTER(POINTER(IUnknown)), "activatedInterface")),
    ]


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = [
        COMMETHOD([], HRESULT, "ActivateCompleted",
                  (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "op")),
    ]


class IAgileObject(IUnknown):
    _iid_ = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
    _methods_ = []


class IAudioClient(IUnknown):
    _iid_ = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Initialize",
                  (["in"], c_uint, "ShareMode"), (["in"], DWORD, "StreamFlags"),
                  (["in"], c_longlong, "hnsBufferDuration"),
                  (["in"], c_longlong, "hnsPeriodicity"),
                  (["in"], POINTER(WAVEFORMATEX), "pFormat"),
                  (["in"], POINTER(GUID), "AudioSessionGuid")),
        COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(c_uint), "p")),
        COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(c_longlong), "p")),
        COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], POINTER(c_uint), "p")),
        COMMETHOD([], HRESULT, "IsFormatSupported",
                  (["in"], c_uint, "sm"), (["in"], POINTER(WAVEFORMATEX), "pf"),
                  (["out"], POINTER(POINTER(WAVEFORMATEX)), "pp")),
        COMMETHOD([], HRESULT, "GetMixFormat", (["out"], POINTER(POINTER(WAVEFORMATEX)), "pp")),
        COMMETHOD([], HRESULT, "GetDevicePeriod",
                  (["out"], POINTER(c_longlong), "a"), (["out"], POINTER(c_longlong), "b")),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle", (["in"], c_void_p, "h")),
        COMMETHOD([], HRESULT, "GetService",
                  (["in"], POINTER(GUID), "riid"), (["out"], POINTER(c_void_p), "ppv")),
    ]


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetBuffer",
                  (["out"], POINTER(POINTER(c_byte)), "ppData"),
                  (["out"], POINTER(c_uint), "pNumFrames"),
                  (["out"], POINTER(DWORD), "pdwFlags"),
                  (["out"], POINTER(c_ulonglong), "pDevPos"),
                  (["out"], POINTER(c_ulonglong), "pQPCPos")),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint, "n")),
        COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint), "p")),
    ]


class _Handler(COMObject):
    # IAgileObject가 반드시 있어야 ActivateAudioInterfaceAsync가 받아준다.
    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

    def __init__(self):
        super().__init__()
        self.done = threading.Event()

    def ActivateCompleted(self, this, op):  # noqa: N802
        self.done.set()
        return 0


class ProcessAudioRecorder:
    """지정 PID(및 자식)의 오디오만 WAV로 녹음. LoopbackRecorder와 동일 인터페이스."""

    RATE = 48000
    CHANNELS = 2

    def __init__(self, pid: int, wav_path: str):
        self.pid = pid
        self.wav_path = wav_path
        self.error: str | None = None
        self._stop = False
        self._thread: threading.Thread | None = None
        self._init_done = threading.Event()
        self._init_ok = False

    @staticmethod
    def available() -> bool:
        try:
            import comtypes  # noqa: F401

            return sys.getwindowsversion().build >= 19041
        except Exception:
            return False

    def start(self) -> None:
        """녹음 스레드를 띄우고 초기화 성공/실패가 결정될 때까지 잠깐 기다린다."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._init_done.wait(timeout=4.0)
        if not self._init_ok and self.error is None:
            self.error = "프로세스 오디오 초기화 시간 초과"

    def _run(self) -> None:
        try:
            comtypes.CoInitializeEx()  # sys.coinit_flags=0 → MTA
        except OSError:
            pass
        try:
            self._capture()
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
            self._init_ok = False
            self._init_done.set()

    def _capture(self) -> None:
        mmdev = ctypes.WinDLL("Mmdevapi.dll")
        activate = mmdev.ActivateAudioInterfaceAsync
        activate.restype = HRESULT
        activate.argtypes = [wintypes.LPCWSTR, POINTER(GUID),
                             POINTER(PROPVARIANT), c_void_p, POINTER(c_void_p)]

        params = AUDIOCLIENT_ACTIVATION_PARAMS(1, self.pid, 0)
        pv = PROPVARIANT()
        pv.vt = _VT_BLOB
        pv.cbSize = ctypes.sizeof(params)
        pv.pBlobData = cast(byref(params), c_void_p)

        handler = _Handler()
        php = handler.QueryInterface(IActivateAudioInterfaceCompletionHandler)
        op_ptr = c_void_p()
        hr = activate(_VIRTUAL, byref(IAudioClient._iid_), byref(pv),
                      cast(php, c_void_p), byref(op_ptr))
        if hr != 0:
            raise OSError(f"ActivateAudioInterfaceAsync 0x{hr & 0xffffffff:08x}")
        handler.done.wait(3)

        op = cast(op_ptr, POINTER(IActivateAudioInterfaceAsyncOperation))
        act_hr, unk = op.GetActivateResult()
        if act_hr != 0:
            raise OSError(f"GetActivateResult 0x{act_hr & 0xffffffff:08x}")
        client = unk.QueryInterface(IAudioClient)

        fmt = WAVEFORMATEX(1, self.CHANNELS, self.RATE,
                           self.RATE * self.CHANNELS * 2, self.CHANNELS * 2, 16, 0)
        flags = _FLAG_LOOPBACK | _FLAG_EVENTCALLBACK
        hr = client.Initialize(_SHARED, flags, 2000000, 0, byref(fmt), None)
        if hr != 0:
            raise OSError(f"Initialize 0x{ctypes.c_uint(hr).value:08x}")

        k32 = ctypes.windll.kernel32
        h_event = k32.CreateEventW(None, False, False, None)
        client.SetEventHandle(h_event)
        cap_ptr = client.GetService(byref(IAudioCaptureClient._iid_))
        capcli = cast(cap_ptr, POINTER(IAudioCaptureClient))

        block = self.CHANNELS * 2
        wf = wave.open(self.wav_path, "wb")
        wf.setnchannels(self.CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(self.RATE)

        client.Start()
        # 여기까지 왔으면 초기화 성공 — 호출자에게 알림(폴백 안 함)
        self._init_ok = True
        self._init_done.set()
        try:
            while not self._stop:
                k32.WaitForSingleObject(h_event, 200)
                while True:
                    n = capcli.GetNextPacketSize()
                    if n == 0:
                        break
                    ppData, num, dflags, _dp, _qp = capcli.GetBuffer()
                    nbytes = num * block
                    if dflags & _SILENT:
                        wf.writeframes(b"\x00" * nbytes)
                    else:
                        wf.writeframes(ctypes.string_at(ppData, nbytes))
                    capcli.ReleaseBuffer(num)
        finally:
            try:
                client.Stop()
            except Exception:
                pass
            wf.close()

    def stop(self) -> None:
        self._stop = True
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
