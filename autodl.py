"""AutoDL - 영상 다운로드 + 화면 영역 녹화 데스크톱 앱.

실행:  python autodl.py
"""

from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

import downloader
import recorder
import winutil


def default_download_dir() -> str:
    d = os.path.join(os.path.expanduser("~"), "Downloads")
    return d if os.path.isdir(d) else os.path.expanduser("~")


def open_folder(path: str) -> None:
    try:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            webbrowser.open("file://" + os.path.abspath(path))
    except Exception:
        pass


# ===================== 영역 선택 오버레이 =====================
class RegionSelector:
    """전체화면 반투명 오버레이에서 드래그로 영역을 고른다(주 모니터 기준)."""

    def __init__(self, parent: tk.Tk):
        self.parent = parent
        self.result: recorder.Region | None = None

    def select(self) -> recorder.Region | None:
        top = tk.Toplevel(self.parent)
        top.attributes("-fullscreen", True)
        top.attributes("-alpha", 0.3)
        top.attributes("-topmost", True)
        top.config(cursor="cross", bg="black")

        canvas = tk.Canvas(top, bg="black", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        canvas.create_text(
            top.winfo_screenwidth() // 2,
            40,
            text="녹화할 영역을 드래그하세요  ·  취소: Esc",
            fill="white",
            font=("Segoe UI", 16),
        )

        start = {"x": 0, "y": 0}
        rect = {"id": None}

        def on_press(e):
            start["x"], start["y"] = e.x, e.y
            if rect["id"]:
                canvas.delete(rect["id"])
            rect["id"] = canvas.create_rectangle(
                e.x, e.y, e.x, e.y, outline="#00d1ff", width=2
            )

        def on_drag(e):
            if rect["id"]:
                canvas.coords(rect["id"], start["x"], start["y"], e.x, e.y)

        def on_release(e):
            self.result = recorder.Region(
                start["x"], start["y"], e.x - start["x"], e.y - start["y"]
            ).normalized()
            top.destroy()

        def on_cancel(_e):
            self.result = None
            top.destroy()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        top.bind("<Escape>", on_cancel)

        top.grab_set()
        top.focus_force()
        self.parent.wait_window(top)
        return self.result


# ===================== 앱 본체 =====================
class AutoDLApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("AutoDL — 다운로드 & 화면 녹화")
        root.geometry("700x560")
        root.minsize(580, 500)

        self._events: "queue.Queue[tuple]" = queue.Queue()
        self._downloading = False

        # 영역 녹화 상태
        self._recorder: recorder.RegionRecorder | None = None
        self._region: recorder.Region | None = None

        # 창 녹화 상태
        self._win_recorder: recorder.WindowRecorder | None = None
        self._win_list: list[winutil.WindowInfo] = []
        self._win_hwnd: int | None = None
        self._win_crop: tuple[int, int, int, int] | None = None
        self._win_out: str = ""

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self._dl_tab = ttk.Frame(nb)
        self._rec_tab = ttk.Frame(nb)
        self._win_tab = ttk.Frame(nb)
        nb.add(self._dl_tab, text="  ⬇ 다운로드  ")
        nb.add(self._rec_tab, text="  ● 화면 영역  ")
        nb.add(self._win_tab, text="  ● 창 녹화  ")

        self._build_download_tab(self._dl_tab)
        self._build_record_tab(self._rec_tab)
        self._build_window_tab(self._win_tab)
        self._poll_events()

        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 다운로드 탭 ----------
    def _build_download_tab(self, frm: ttk.Frame) -> None:
        pad = {"padx": 12, "pady": 6}

        ttk.Label(frm, text="영상 URL").pack(anchor="w", **pad)
        url_row = ttk.Frame(frm)
        url_row.pack(fill="x", padx=12)
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(url_row, textvariable=self.url_var)
        self.url_entry.pack(side="left", fill="x", expand=True)
        self.url_entry.bind("<Return>", lambda _e: self.start_download())
        ttk.Button(url_row, text="붙여넣기", command=self._paste).pack(
            side="left", padx=(6, 0)
        )

        ttk.Label(frm, text="저장 폴더").pack(anchor="w", **pad)
        dir_row = ttk.Frame(frm)
        dir_row.pack(fill="x", padx=12)
        self.dl_dir_var = tk.StringVar(value=default_download_dir())
        ttk.Entry(dir_row, textvariable=self.dl_dir_var).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(
            dir_row, text="찾아보기",
            command=lambda: self._choose_dir(self.dl_dir_var),
        ).pack(side="left", padx=(6, 0))

        opt_row = ttk.Frame(frm)
        opt_row.pack(fill="x", padx=12, pady=(10, 0))
        self.mp4_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opt_row, text="MP4로 합치기 (호환성 ↑)", variable=self.mp4_var
        ).pack(side="left")

        self.download_btn = ttk.Button(
            frm, text="⬇  최고화질로 다운로드", command=self.start_download
        )
        self.download_btn.pack(fill="x", padx=12, pady=(12, 4))

        self.progress = ttk.Progressbar(frm, mode="determinate", maximum=100)
        self.progress.pack(fill="x", padx=12, pady=(6, 2))
        self.dl_status_var = tk.StringVar(value="대기 중")
        ttk.Label(frm, textvariable=self.dl_status_var).pack(anchor="w", padx=12)

        ttk.Label(frm, text="로그").pack(anchor="w", padx=12, pady=(8, 0))
        log_frame = ttk.Frame(frm)
        log_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.log = tk.Text(log_frame, height=7, state="disabled", wrap="word")
        self.log.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(log_frame, command=self.log.yview)
        sb.pack(side="right", fill="y")
        self.log.config(yscrollcommand=sb.set)

        if downloader.find_ffmpeg() is None:
            self._append_log(
                "⚠ ffmpeg를 찾지 못했습니다. 최고화질 합치기가 제한될 수 있어요."
            )

    # ---------- 녹화 탭 ----------
    def _build_record_tab(self, frm: ttk.Frame) -> None:
        pad = {"padx": 12, "pady": 6}

        info = (
            "화면에서 원하는 영역만 드래그로 선택해 MP4로 녹화합니다.\n"
            "(시스템 소리 포함 · 그 영역 위로 다른 창이 오면 그 창이 찍힙니다)"
        )
        ttk.Label(frm, text=info, justify="left").pack(anchor="w", **pad)

        ttk.Label(frm, text="저장 폴더").pack(anchor="w", **pad)
        dir_row = ttk.Frame(frm)
        dir_row.pack(fill="x", padx=12)
        self.rec_dir_var = tk.StringVar(value=default_download_dir())
        ttk.Entry(dir_row, textvariable=self.rec_dir_var).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(
            dir_row, text="찾아보기",
            command=lambda: self._choose_dir(self.rec_dir_var),
        ).pack(side="left", padx=(6, 0))

        ctl_row = ttk.Frame(frm)
        ctl_row.pack(fill="x", padx=12, pady=(12, 0))
        ttk.Label(ctl_row, text="FPS").pack(side="left")
        self.fps_var = tk.StringVar(value="30")
        ttk.Combobox(
            ctl_row, textvariable=self.fps_var, width=5, state="readonly",
            values=["15", "30", "60"],
        ).pack(side="left", padx=(6, 16))
        self.rec_audio_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            ctl_row, text="소리 포함", variable=self.rec_audio_var
        ).pack(side="left", padx=(0, 16))
        ttk.Button(ctl_row, text="영역 선택", command=self._select_region).pack(
            side="left"
        )

        self.region_var = tk.StringVar(value="선택된 영역 없음")
        ttk.Label(frm, textvariable=self.region_var).pack(anchor="w", padx=12, pady=(8, 0))

        self.record_btn = ttk.Button(
            frm, text="●  녹화 시작", command=self._toggle_record, state="disabled"
        )
        self.record_btn.pack(fill="x", padx=12, pady=(12, 4))

        self.rec_status_var = tk.StringVar(value="대기 중")
        ttk.Label(frm, textvariable=self.rec_status_var).pack(anchor="w", padx=12)

        if downloader.find_ffmpeg() is None:
            messagebox.showwarning(
                "ffmpeg 필요", "화면 녹화에는 ffmpeg가 필요합니다."
            )

    # ---------- 창 녹화 탭 ----------
    def _build_window_tab(self, frm: ttk.Frame) -> None:
        pad = {"padx": 12, "pady": 6}

        info = (
            "특정 창을 녹화합니다. 다른 창이 위를 덮거나 다른 앱으로 전환해도\n"
            "그 창의 내용만 계속 녹화됩니다. (최소화하면 멈춤 · 영상만, 소리 없음)"
        )
        ttk.Label(frm, text=info, justify="left").pack(anchor="w", **pad)

        ttk.Label(frm, text="녹화할 창").pack(anchor="w", **pad)
        win_row = ttk.Frame(frm)
        win_row.pack(fill="x", padx=12)
        self.win_var = tk.StringVar()
        self.win_combo = ttk.Combobox(
            win_row, textvariable=self.win_var, state="readonly"
        )
        self.win_combo.pack(side="left", fill="x", expand=True)
        self.win_combo.bind("<<ComboboxSelected>>", self._on_win_selected)
        ttk.Button(win_row, text="새로고침", command=self._refresh_windows).pack(
            side="left", padx=(6, 0)
        )

        crop_row = ttk.Frame(frm)
        crop_row.pack(fill="x", padx=12, pady=(10, 0))
        self.win_crop_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            crop_row, text="창의 일부만 녹화", variable=self.win_crop_var,
            command=self._on_crop_toggle,
        ).pack(side="left")
        self.win_region_btn = ttk.Button(
            crop_row, text="영역 선택", command=self._select_win_region,
            state="disabled",
        )
        self.win_region_btn.pack(side="left", padx=(8, 0))
        self.win_region_var = tk.StringVar(value="(전체 창)")
        ttk.Label(crop_row, textvariable=self.win_region_var).pack(
            side="left", padx=(10, 0)
        )

        dir_row = ttk.Frame(frm)
        dir_row.pack(fill="x", padx=12, pady=(12, 0))
        ttk.Label(dir_row, text="FPS").pack(side="left")
        self.win_fps_var = tk.StringVar(value="30")
        ttk.Combobox(
            dir_row, textvariable=self.win_fps_var, width=5, state="readonly",
            values=["15", "30", "60"],
        ).pack(side="left", padx=(6, 16))
        self.win_dir_var = tk.StringVar(value=default_download_dir())
        ttk.Entry(dir_row, textvariable=self.win_dir_var).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(
            dir_row, text="찾아보기",
            command=lambda: self._choose_dir(self.win_dir_var),
        ).pack(side="left", padx=(6, 0))

        opt_row = ttk.Frame(frm)
        opt_row.pack(fill="x", padx=12, pady=(8, 0))
        self.win_audio_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opt_row, text="소리 포함", variable=self.win_audio_var
        ).pack(side="left")
        self.whale_btn = ttk.Button(
            opt_row, text="가려도 녹화되게 Whale 실행",
            command=self._launch_whale_for_capture,
        )
        self.whale_btn.pack(side="left", padx=(16, 0))

        tip = (
            "※ 가린 상태에서 흰 화면이 나오면 브라우저가 절전으로 그리기를 멈춘 것.\n"
            "   위 버튼으로 Whale을 실행하면 가려도 계속 녹화됩니다 (기존 Whale은 먼저 닫기)."
        )
        ttk.Label(frm, text=tip, justify="left", foreground="#888").pack(
            anchor="w", padx=12, pady=(6, 0)
        )

        self.win_record_btn = ttk.Button(
            frm, text="●  녹화 시작", command=self._toggle_win_record,
            state="disabled",
        )
        self.win_record_btn.pack(fill="x", padx=12, pady=(12, 4))

        self.win_status_var = tk.StringVar(value="창을 선택하세요")
        ttk.Label(frm, textvariable=self.win_status_var).pack(anchor="w", padx=12)

        self._refresh_windows()

        if downloader.find_ffmpeg() is None:
            self.win_status_var.set("⚠ 화면 녹화에는 ffmpeg가 필요합니다.")

    def _launch_whale_for_capture(self) -> None:
        """가려져도 계속 렌더링하도록 Whale을 anti-occlusion 플래그로 실행."""
        exe = winutil.find_whale()
        if not exe:
            messagebox.showwarning(
                "Whale 없음",
                "Whale 실행 파일을 찾지 못했습니다.\n"
                "직접 실행 시 다음 플래그를 추가하세요:\n"
                "--disable-features=CalculateNativeWinOcclusion",
            )
            return
        if not messagebox.askyesno(
            "Whale 실행",
            "기존에 열려 있는 Whale 창을 모두 닫은 뒤 진행하세요.\n"
            "(이미 실행 중이면 플래그가 적용되지 않습니다)\n\n"
            "지금 실행할까요?",
        ):
            return
        try:
            winutil.launch_no_occlusion(exe)
            self.win_status_var.set(
                "Whale 실행됨 — 잠시 후 '새로고침'으로 창을 다시 선택하세요."
            )
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("실행 실패", str(e))

    def _refresh_windows(self) -> None:
        self._win_list = [
            w for w in winutil.list_windows()
            if w.title != self.root.title()  # 우리 앱 창 제외
        ]
        labels = [self._win_label(w) for w in self._win_list]
        self.win_combo.config(values=labels)
        if labels:
            self.win_status_var.set("창을 선택하세요")
        else:
            self.win_status_var.set("녹화할 창을 찾지 못했습니다")

    @staticmethod
    def _win_label(w: winutil.WindowInfo) -> str:
        t = w.title if len(w.title) <= 60 else w.title[:57] + "…"
        return f"{t}   [{w.hwnd}]"

    def _on_win_selected(self, _e=None) -> None:
        idx = self.win_combo.current()
        if idx < 0 or idx >= len(self._win_list):
            return
        self._win_hwnd = self._win_list[idx].hwnd
        self._win_crop = None
        self.win_region_var.set("(전체 창)")
        self.win_record_btn.config(state="normal")
        self.win_status_var.set("준비됨 — 녹화 시작 가능")

    def _on_crop_toggle(self) -> None:
        on = self.win_crop_var.get()
        self.win_region_btn.config(state="normal" if on else "disabled")
        if not on:
            self._win_crop = None
            self.win_region_var.set("(전체 창)")

    def _select_win_region(self) -> None:
        if self._win_hwnd is None:
            messagebox.showinfo("창 선택", "먼저 녹화할 창을 고르세요.")
            return
        winutil.bring_to_front(self._win_hwnd)
        self.root.withdraw()
        self.root.update()
        time.sleep(0.35)
        rect = winutil.window_frame_rect(self._win_hwnd)  # (l, t, w, h)
        sel = RegionSelector(self.root).select()
        self.root.deiconify()
        if not sel or sel.width < 2 or sel.height < 2:
            return
        # 화면 좌표 → 창 기준 좌표
        cl = max(0, sel.x - rect[0])
        ct = max(0, sel.y - rect[1])
        self._win_crop = (cl, ct, sel.width, sel.height)
        self.win_region_var.set(f"부분 {sel.width}×{sel.height} @창({cl},{ct})")

    def _toggle_win_record(self) -> None:
        if self._win_recorder and self._win_recorder.is_recording:
            self._stop_win_record()
        else:
            self._start_win_record()

    def _start_win_record(self) -> None:
        if self._win_hwnd is None:
            return
        out_dir = self.win_dir_var.get().strip() or default_download_dir()
        fname = "window_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".mp4"
        self._win_out = os.path.join(out_dir, fname)
        try:
            fps = int(self.win_fps_var.get())
        except ValueError:
            fps = 30
        crop = self._win_crop if self.win_crop_var.get() else None

        self._win_recorder = recorder.WindowRecorder(
            self._win_hwnd, self._win_out, fps=fps, crop=crop,
            record_audio=self.win_audio_var.get(),
        )
        self.win_record_btn.config(state="disabled", text="준비 중…")
        self.win_status_var.set("준비 중… (첫 프레임 대기)")

        rec = self._win_recorder

        def worker():
            try:
                rec.start()
                self._events.put(("winrec_started", os.path.basename(self._win_out)))
            except Exception as e:  # noqa: BLE001
                self._events.put(("winrec_error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _stop_win_record(self) -> None:
        if not self._win_recorder:
            return
        self.win_record_btn.config(state="disabled", text="저장 중…")
        self.win_status_var.set("저장 중… (영상 마무리)")
        rec = self._win_recorder
        out = self._win_out

        def worker():
            rec.stop()
            self._events.put(("winrec_done", out))

        threading.Thread(target=worker, daemon=True).start()

    # ---------- 공용 ----------
    def _paste(self) -> None:
        try:
            self.url_var.set(self.root.clipboard_get().strip())
        except tk.TclError:
            pass

    def _choose_dir(self, var: tk.StringVar) -> None:
        d = filedialog.askdirectory(
            initialdir=var.get() or os.path.expanduser("~")
        )
        if d:
            var.set(d)

    # ---------- 다운로드 동작 ----------
    def start_download(self) -> None:
        if self._downloading:
            return
        url = self.url_var.get().strip()
        out = self.dl_dir_var.get().strip()
        if not url:
            messagebox.showwarning("URL 필요", "다운로드할 영상 URL을 입력하세요.")
            return
        if not out:
            messagebox.showwarning("폴더 필요", "저장 폴더를 선택하세요.")
            return

        self._downloading = True
        self.download_btn.config(state="disabled", text="다운로드 중…")
        self.progress.config(value=0)
        self.dl_status_var.set("준비 중…")
        self._append_log(f"▶ 시작: {url}")

        threading.Thread(
            target=self._dl_worker,
            args=(url, out, self.mp4_var.get()),
            daemon=True,
        ).start()

    def _dl_worker(self, url: str, out: str, remux: bool) -> None:
        result = downloader.download(
            url, out, remux_mp4=remux,
            progress_hook=lambda d: self._events.put(("progress", d)),
            log=lambda m: self._events.put(("log", m)),
        )
        self._events.put(("done", result))

    # ---------- 녹화 동작 ----------
    def _select_region(self) -> None:
        if self._recorder and self._recorder.is_recording:
            return
        self.root.withdraw()  # 선택 중에는 앱 창을 숨김
        self.root.update()
        time.sleep(0.2)
        region = RegionSelector(self.root).select()
        self.root.deiconify()
        if region and region.width >= 2 and region.height >= 2:
            self._region = region
            self.region_var.set(
                f"선택됨: {region.width}×{region.height}  @ ({region.x}, {region.y})"
            )
            self.record_btn.config(state="normal")
        elif region is not None:
            messagebox.showinfo("영역 너무 작음", "조금 더 크게 드래그해 주세요.")

    def _toggle_record(self) -> None:
        if self._recorder and self._recorder.is_recording:
            self._stop_record()
        else:
            self._start_record()

    def _start_record(self) -> None:
        if not self._region:
            return
        out_dir = self.rec_dir_var.get().strip() or default_download_dir()
        fname = "recording_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".mp4"
        out_path = os.path.join(out_dir, fname)
        try:
            fps = int(self.fps_var.get())
        except ValueError:
            fps = 30

        self._recorder = recorder.RegionRecorder(
            self._region, out_path, fps=fps, record_audio=self.rec_audio_var.get()
        )
        try:
            self._recorder.start()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("녹화 시작 실패", str(e))
            self._recorder = None
            return

        # 시작 직후 ffmpeg가 바로 죽지 않았는지 잠깐 확인
        self.root.after(600, self._check_record_started)
        self._rec_out = out_path
        self.record_btn.config(text="■  녹화 중지")
        self.rec_status_var.set(f"● 녹화 중 → {fname}")

    def _check_record_started(self) -> None:
        if self._recorder and self._recorder.failed_early():
            self.rec_status_var.set("녹화 실패 ✖ (영역/권한을 확인하세요)")
            self.record_btn.config(text="●  녹화 시작")
            self._recorder = None

    def _stop_record(self) -> None:
        if not self._recorder:
            return
        self.record_btn.config(state="disabled", text="저장 중…")
        self.rec_status_var.set("저장 중… (영상 마무리)")
        rec = self._recorder
        out = getattr(self, "_rec_out", "")

        def worker():
            rec.stop()
            self._events.put(("rec_done", out))

        threading.Thread(target=worker, daemon=True).start()

    # ---------- 이벤트 루프 ----------
    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "progress":
                    self._on_progress(payload)
                elif kind == "log":
                    self._append_log(payload)
                elif kind == "done":
                    self._on_dl_done(payload)
                elif kind == "rec_done":
                    self._on_rec_done(payload)
                elif kind == "winrec_started":
                    self._on_winrec_started(payload)
                elif kind == "winrec_error":
                    self._on_winrec_error(payload)
                elif kind == "winrec_done":
                    self._on_winrec_done(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _on_progress(self, d: dict) -> None:
        st = d.get("status")
        if st == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes", 0)
            if total:
                pct = done / total * 100
                self.progress.config(value=pct)
                speed = d.get("speed") or 0
                eta = d.get("eta")
                mb = total / 1024 / 1024
                self.dl_status_var.set(
                    f"{pct:5.1f}%  /  {mb:.1f} MB  /  "
                    f"{(speed / 1024 / 1024):.1f} MB/s"
                    + (f"  /  남은시간 {eta}s" if eta else "")
                )
        elif st == "finished":
            self.progress.config(value=100)
            self.dl_status_var.set("합치는 중… (후처리)")

    def _on_dl_done(self, result: downloader.DownloadResult) -> None:
        self._downloading = False
        self.download_btn.config(state="normal", text="⬇  최고화질로 다운로드")
        if result.success:
            self.progress.config(value=100)
            self.dl_status_var.set("완료 ✓")
            self._append_log(f"✓ 완료: {result.title}")
            if result.filepath:
                self._append_log(f"  저장 위치: {result.filepath}")
            if messagebox.askyesno("완료", "다운로드 완료!\n저장 폴더를 열까요?"):
                open_folder(self.dl_dir_var.get())
        else:
            self.dl_status_var.set("실패 ✖")
            self._append_log(f"✖ 실패: {result.error}")
            messagebox.showerror("실패", result.error[:1000])

    def _on_rec_done(self, out_path: str) -> None:
        self._recorder = None
        self.record_btn.config(state="normal", text="●  녹화 시작")
        if out_path and os.path.exists(out_path):
            size_mb = os.path.getsize(out_path) / 1024 / 1024
            self.rec_status_var.set(f"저장 완료 ✓  ({size_mb:.1f} MB)")
            if messagebox.askyesno("녹화 완료", "녹화를 저장했습니다.\n폴더를 열까요?"):
                open_folder(os.path.dirname(out_path))
        else:
            self.rec_status_var.set("저장 실패 ✖")

    def _on_winrec_started(self, fname: str) -> None:
        self.win_record_btn.config(state="normal", text="■  녹화 중지")
        self.win_status_var.set(f"● 녹화 중 → {fname}")

    def _on_winrec_error(self, msg: str) -> None:
        self.win_record_btn.config(state="normal", text="●  녹화 시작")
        self.win_status_var.set("녹화 시작 실패 ✖")
        self._win_recorder = None
        messagebox.showerror("녹화 시작 실패", msg)

    def _on_winrec_done(self, out_path: str) -> None:
        self._win_recorder = None
        self.win_record_btn.config(state="normal", text="●  녹화 시작")
        if out_path and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            size_mb = os.path.getsize(out_path) / 1024 / 1024
            self.win_status_var.set(f"저장 완료 ✓  ({size_mb:.1f} MB)")
            if messagebox.askyesno("녹화 완료", "녹화를 저장했습니다.\n폴더를 열까요?"):
                open_folder(os.path.dirname(out_path))
        else:
            self.win_status_var.set("저장 실패 ✖")

    # ---------- 보조 ----------
    def _append_log(self, msg: str) -> None:
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _on_close(self) -> None:
        recording = (self._recorder and self._recorder.is_recording) or (
            self._win_recorder and self._win_recorder.is_recording
        )
        if recording:
            if not messagebox.askyesno(
                "녹화 중", "녹화가 진행 중입니다. 중지하고 종료할까요?"
            ):
                return
            if self._recorder:
                self._recorder.stop()
            if self._win_recorder:
                self._win_recorder.stop()
        self.root.destroy()


def main() -> None:
    # Tk 생성 전에 DPI 인식을 켜야 좌표가 물리 픽셀로 통일된다.
    winutil.set_dpi_awareness()
    root = tk.Tk()
    # DPI 배율만큼 UI를 키워 작게 보이지 않게 한다.
    try:
        scale = winutil.get_dpi_scale()
        if scale and scale != 1.0:
            root.tk.call("tk", "scaling", scale * 1.0)
    except Exception:
        pass
    AutoDLApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
