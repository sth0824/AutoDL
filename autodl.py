"""AutoDL - URL만 붙여넣으면 최고화질로 영상을 받는 데스크톱 앱.

실행:  python autodl.py
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

import downloader


def default_download_dir() -> str:
    d = os.path.join(os.path.expanduser("~"), "Downloads")
    return d if os.path.isdir(d) else os.path.expanduser("~")


class AutoDLApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("AutoDL — 영상 최고화질 다운로더")
        root.geometry("680x520")
        root.minsize(560, 460)

        # 백그라운드 스레드 → UI 스레드로 메시지를 넘기는 큐
        self._events: "queue.Queue[tuple]" = queue.Queue()
        self._downloading = False

        self._build_ui()
        self._poll_events()

    # ---------- UI 구성 ----------
    def _build_ui(self) -> None:
        pad = {"padx": 12, "pady": 6}
        frm = ttk.Frame(self.root)
        frm.pack(fill="both", expand=True)

        # URL 입력
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

        # 저장 폴더
        ttk.Label(frm, text="저장 폴더").pack(anchor="w", **pad)
        dir_row = ttk.Frame(frm)
        dir_row.pack(fill="x", padx=12)
        self.dir_var = tk.StringVar(value=default_download_dir())
        ttk.Entry(dir_row, textvariable=self.dir_var).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(dir_row, text="찾아보기", command=self._choose_dir).pack(
            side="left", padx=(6, 0)
        )

        # 옵션
        opt_row = ttk.Frame(frm)
        opt_row.pack(fill="x", padx=12, pady=(10, 0))
        self.mp4_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opt_row, text="MP4로 합치기 (호환성 ↑)", variable=self.mp4_var
        ).pack(side="left")

        # 다운로드 버튼
        self.download_btn = ttk.Button(
            frm, text="⬇  최고화질로 다운로드", command=self.start_download
        )
        self.download_btn.pack(fill="x", padx=12, pady=(12, 4))

        # 진행바 + 상태
        self.progress = ttk.Progressbar(frm, mode="determinate", maximum=100)
        self.progress.pack(fill="x", padx=12, pady=(6, 2))
        self.status_var = tk.StringVar(value="대기 중")
        ttk.Label(frm, textvariable=self.status_var).pack(anchor="w", padx=12)

        # 로그
        ttk.Label(frm, text="로그").pack(anchor="w", padx=12, pady=(8, 0))
        log_frame = ttk.Frame(frm)
        log_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.log = tk.Text(log_frame, height=8, state="disabled", wrap="word")
        self.log.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(log_frame, command=self.log.yview)
        sb.pack(side="right", fill="y")
        self.log.config(yscrollcommand=sb.set)

        # ffmpeg 상태 안내
        if downloader.find_ffmpeg() is None:
            self._append_log(
                "⚠ ffmpeg를 찾지 못했습니다. 일부 사이트는 최고화질 합치기가 "
                "제한될 수 있어요. (pip install imageio-ffmpeg 권장)"
            )

    # ---------- 동작 ----------
    def _paste(self) -> None:
        try:
            self.url_var.set(self.root.clipboard_get().strip())
        except tk.TclError:
            pass

    def _choose_dir(self) -> None:
        d = filedialog.askdirectory(initialdir=self.dir_var.get() or os.path.expanduser("~"))
        if d:
            self.dir_var.set(d)

    def start_download(self) -> None:
        if self._downloading:
            return
        url = self.url_var.get().strip()
        out = self.dir_var.get().strip()
        if not url:
            messagebox.showwarning("URL 필요", "다운로드할 영상 URL을 입력하세요.")
            return
        if not out:
            messagebox.showwarning("폴더 필요", "저장 폴더를 선택하세요.")
            return

        self._downloading = True
        self.download_btn.config(state="disabled", text="다운로드 중…")
        self.progress.config(value=0)
        self.status_var.set("준비 중…")
        self._append_log(f"▶ 시작: {url}")

        t = threading.Thread(
            target=self._worker,
            args=(url, out, self.mp4_var.get()),
            daemon=True,
        )
        t.start()

    def _worker(self, url: str, out: str, remux: bool) -> None:
        def hook(d: dict) -> None:
            self._events.put(("progress", d))

        def log(msg: str) -> None:
            self._events.put(("log", msg))

        result = downloader.download(
            url, out, remux_mp4=remux, progress_hook=hook, log=log
        )
        self._events.put(("done", result))

    # ---------- UI 스레드: 큐 처리 ----------
    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "progress":
                    self._on_progress(payload)
                elif kind == "log":
                    self._append_log(payload)
                elif kind == "done":
                    self._on_done(payload)
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
                self.status_var.set(
                    f"{pct:5.1f}%  /  {mb:.1f} MB  /  "
                    f"{(speed / 1024 / 1024):.1f} MB/s"
                    + (f"  /  남은시간 {eta}s" if eta else "")
                )
        elif st == "finished":
            self.progress.config(value=100)
            self.status_var.set("합치는 중… (후처리)")

    def _on_done(self, result: downloader.DownloadResult) -> None:
        self._downloading = False
        self.download_btn.config(state="normal", text="⬇  최고화질로 다운로드")
        if result.success:
            self.progress.config(value=100)
            self.status_var.set("완료 ✓")
            self._append_log(f"✓ 완료: {result.title}")
            if result.filepath:
                self._append_log(f"  저장 위치: {result.filepath}")
            if messagebox.askyesno("완료", "다운로드 완료!\n저장 폴더를 열까요?"):
                self._open_folder(self.dir_var.get())
        else:
            self.status_var.set("실패 ✖")
            self._append_log(f"✖ 실패: {result.error}")
            messagebox.showerror("실패", result.error[:1000])

    # ---------- 보조 ----------
    def _append_log(self, msg: str) -> None:
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _open_folder(self, path: str) -> None:
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                webbrowser.open("file://" + os.path.abspath(path))
        except Exception:
            pass


def main() -> None:
    root = tk.Tk()
    AutoDLApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
