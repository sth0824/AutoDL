"""Windows 창 열거 / 좌표 / DPI 헬퍼 (ctypes).

창 단위 녹화(WGC)에서 '어떤 창'을 고르고, 그 창의 화면상 위치를 알아내
사용자가 선택한 부분(crop)을 창 기준 좌표로 변환하는 데 쓴다.
"""

from __future__ import annotations

import ctypes
import glob
import os
import subprocess
from ctypes import wintypes
from dataclasses import dataclass

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi

# WS_EX_TOOLWINDOW: 작업표시줄에 안 뜨는 보조 창 → 목록에서 제외
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14


@dataclass
class WindowInfo:
    hwnd: int
    title: str


def set_dpi_awareness() -> None:
    """프로세스를 Per-Monitor DPI Aware로 설정(좌표를 물리 픽셀로 통일).

    Tk 창보다 먼저 호출해야 한다. 그래야 Tk 좌표·GetWindowRect·WGC 프레임이
    같은 픽셀 기준이 되어 crop이 정확해진다.
    """
    try:
        # PER_MONITOR_AWARE_V2 = -4
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def get_dpi_scale(hwnd: int = 0) -> float:
    """해당 창(또는 시스템)의 DPI 배율(1.0 = 100%)."""
    try:
        dpi = ctypes.windll.user32.GetDpiForWindow(hwnd) if hwnd else \
            ctypes.windll.user32.GetDpiForSystem()
        if dpi:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0


def _is_cloaked(hwnd: int) -> bool:
    val = ctypes.c_int(0)
    res = dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd), DWMWA_CLOAKED,
        ctypes.byref(val), ctypes.sizeof(val),
    )
    return res == 0 and val.value != 0


def list_windows() -> list[WindowInfo]:
    """제목이 있는, 보이는 최상위 창 목록(작업표시줄에 뜨는 것 위주)."""
    results: list[WindowInfo] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if ex & WS_EX_TOOLWINDOW:
            return True
        if _is_cloaked(hwnd):  # 보이지 않는 UWP 유령 창 제외
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value.strip()
        if title:
            results.append(WindowInfo(hwnd=int(hwnd), title=title))
        return True

    # 콜백 참조를 살려두기 위해 지역 변수로 보관 후 호출
    user32.EnumWindows(_cb, 0)
    return results


def window_frame_rect(hwnd: int) -> tuple[int, int, int, int]:
    """창의 화면상 사각형 (left, top, width, height), 물리 픽셀.

    WGC가 캡처하는 영역과 가장 잘 맞는 DWM 확장 프레임 경계를 우선 사용한다.
    """
    rect = wintypes.RECT()
    res = dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS,
        ctypes.byref(rect), ctypes.sizeof(rect),
    )
    if res != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def pid_of_window(hwnd: int) -> int:
    """창(hwnd)을 소유한 프로세스 ID."""
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def is_window(hwnd: int) -> bool:
    """아직 살아 있는(유효한) 창 핸들인지."""
    return bool(user32.IsWindow(wintypes.HWND(hwnd)))


def is_minimized(hwnd: int) -> bool:
    """최소화(아이콘화)된 창인지."""
    return bool(user32.IsIconic(wintypes.HWND(hwnd)))


def bring_to_front(hwnd: int) -> None:
    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)


def find_whale() -> str | None:
    """Naver Whale 실행 파일 경로를 찾는다."""
    for base in (
        os.environ.get("LOCALAPPDATA", ""),
        os.environ.get("PROGRAMFILES", ""),
        os.environ.get("PROGRAMFILES(X86)", ""),
    ):
        if not base:
            continue
        hits = glob.glob(
            os.path.join(base, "Naver", "Naver Whale", "Application", "whale.exe")
        )
        if hits:
            return hits[0]
    return None


def launch_no_occlusion(exe: str) -> None:
    """크로미움 기반 브라우저를 '가려도 계속 그리도록' 실행.

    CalculateNativeWinOcclusion 기능을 끄면 창이 가려져도 렌더링을 멈추지
    않아, 창 녹화(WGC)에서 흰 화면이 되는 문제가 사라진다.
    (이미 같은 브라우저가 실행 중이면 플래그가 무시되므로 먼저 닫아야 함)
    """
    subprocess.Popen([
        exe,
        "--disable-features=CalculateNativeWinOcclusion",
        "--disable-backgrounding-occluded-windows",
    ])
