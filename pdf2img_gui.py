#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf2img_gui.py —— PDF 转高清图片（图形界面）

选 PDF（或整个文件夹）→ 选格式与清晰度 → 点「开始转换」。
内部复用 pdf2img.py 的转换核心，与命令行完全一致。
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk
from tkinter import font as tkfont
from tkinter.scrolledtext import ScrolledText

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pdf2img import (  # noqa: E402
    ALPHA_FORMATS,
    Options,
    Pdf2ImgError,
    convert_many,
    parse_pages,
    pdf_info,
    resolve_inputs,
)

APP_TITLE = "PDF 转高清图片"
FORMATS = [
    ("PNG  无损 · 推荐", "png"),
    ("JPG  体积小", "jpg"),
    ("WebP 更小", "webp"),
    ("TIFF 印刷/存档", "tif"),
    ("BMP  不压缩", "bmp"),
]
SIZE_MODES = [
    ("按 DPI（清晰度）", "dpi"),
    ("按长边像素", "long"),
    ("按宽度像素", "width"),
]
DEFAULT_VALUE = {"dpi": "300", "long": "3000", "width": "2400"}


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_TITLE}  v1.0")
        self.geometry("880x700")
        self.minsize(780, 620)
        self._setup_style()

        self.queue: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.running = False
        self.worker: threading.Thread | None = None
        self.last_out = ""
        self._inputs: list[str] = []
        self._display = ""     # 输入框里由对话框填入的显示文本

        self.var_input = tk.StringVar()
        self.var_out = tk.StringVar()
        self.var_fmt = tk.StringVar(value=FORMATS[0][0])
        self.var_mode = tk.StringVar(value=SIZE_MODES[0][0])
        self.var_value = tk.StringVar(value="300")
        self.var_quality = tk.IntVar(value=92)
        self.var_pages = tk.StringVar(value="全部")
        self.var_gray = tk.BooleanVar(value=False)
        self.var_alpha = tk.BooleanVar(value=False)
        self.var_zip = tk.BooleanVar(value=False)
        self.var_open = tk.BooleanVar(value=True)
        self.var_skip = tk.BooleanVar(value=False)

        self._build()
        self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------ UI ----
    def _setup_style(self) -> None:
        try:
            # Windows 高 DPI 下字体不糊
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        style = ttk.Style(self)
        for theme in ("vista", "winnative", "clam", "default"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        base = tkfont.nametofont("TkDefaultFont")
        family = "Microsoft YaHei UI" if sys.platform == "win32" else base.cget("family")
        base.configure(family=family, size=10)
        tkfont.nametofont("TkTextFont").configure(family=family, size=10)
        style.configure("Go.TButton", font=(family, 11, "bold"), padding=(18, 8))
        style.configure("Hint.TLabel", foreground="#666")
        style.configure("TProgressbar", thickness=16)

    def _build(self) -> None:
        pad = {"padx": 10, "pady": 5}
        root = ttk.Frame(self, padding=(12, 10))
        root.pack(fill="both", expand=True)

        # ---- 输入 / 输出 ----
        box_io = ttk.LabelFrame(root, text=" 文件 ", padding=10)
        box_io.pack(fill="x")

        ttk.Label(box_io, text="PDF 文件").grid(row=0, column=0, sticky="w")
        ttk.Entry(box_io, textvariable=self.var_input).grid(
            row=0, column=1, sticky="ew", padx=6
        )
        ttk.Button(box_io, text="选择文件…", command=self._pick_files).grid(row=0, column=2)
        ttk.Button(box_io, text="选择文件夹…", command=self._pick_dir).grid(
            row=0, column=3, padx=(6, 0)
        )

        ttk.Label(box_io, text="输出到").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(box_io, textvariable=self.var_out).grid(
            row=1, column=1, sticky="ew", padx=6, pady=(8, 0)
        )
        ttk.Button(box_io, text="浏览…", command=self._pick_out).grid(
            row=1, column=2, pady=(8, 0)
        )
        ttk.Button(box_io, text="打开", command=self._open_out).grid(
            row=1, column=3, padx=(6, 0), pady=(8, 0)
        )
        box_io.columnconfigure(1, weight=1)

        # ---- 输出设置 ----
        box_set = ttk.LabelFrame(root, text=" 输出设置 ", padding=10)
        box_set.pack(fill="x", pady=(10, 0))

        ttk.Label(box_set, text="格式").grid(row=0, column=0, sticky="w")
        self.cb_fmt = ttk.Combobox(
            box_set, textvariable=self.var_fmt, state="readonly", width=20,
            values=[f[0] for f in FORMATS],
        )
        self.cb_fmt.grid(row=0, column=1, sticky="w", padx=6)
        self.cb_fmt.bind("<<ComboboxSelected>>", lambda e: self._sync_state())

        ttk.Label(box_set, text="清晰度").grid(row=0, column=2, sticky="w", padx=(18, 0))
        self.cb_mode = ttk.Combobox(
            box_set, textvariable=self.var_mode, state="readonly", width=18,
            values=[m[0] for m in SIZE_MODES],
        )
        self.cb_mode.grid(row=0, column=3, sticky="w", padx=6)
        self.cb_mode.bind("<<ComboboxSelected>>", lambda e: self._sync_mode())

        self.sp_value = ttk.Spinbox(
            box_set, textvariable=self.var_value, from_=1, to=20000, width=8
        )
        self.sp_value.grid(row=0, column=4, sticky="w")
        self.lb_unit = ttk.Label(box_set, text="DPI")
        self.lb_unit.grid(row=0, column=5, sticky="w", padx=(4, 0))

        ttk.Label(box_set, text="图片质量").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.sp_quality = ttk.Spinbox(
            box_set, textvariable=self.var_quality, from_=1, to=100, width=6
        )
        self.sp_quality.grid(row=1, column=1, sticky="w", padx=6, pady=(8, 0))
        self.lb_quality_hint = ttk.Label(
            box_set, text="仅 JPG / WebP 生效", style="Hint.TLabel"
        )
        self.lb_quality_hint.grid(row=1, column=2, columnspan=2, sticky="w", padx=(18, 0),
                                  pady=(8, 0))

        ttk.Label(box_set, text="页范围").grid(row=2, column=0, sticky="w", pady=(8, 0))
        e_pages = ttk.Entry(box_set, textvariable=self.var_pages, width=22)
        e_pages.grid(row=2, column=1, sticky="w", padx=6, pady=(8, 0))
        ttk.Label(box_set, text="如 1-3,5,8-（默认全部）", style="Hint.TLabel").grid(
            row=2, column=2, columnspan=4, sticky="w", padx=(18, 0), pady=(8, 0)
        )

        opts = ttk.Frame(box_set)
        opts.grid(row=3, column=0, columnspan=6, sticky="w", pady=(10, 0))
        ttk.Checkbutton(opts, text="灰度", variable=self.var_gray,
                        command=self._sync_state).pack(side="left")
        self.ck_alpha = ttk.Checkbutton(opts, text="透明背景（PNG/WebP/TIFF）",
                                        variable=self.var_alpha)
        self.ck_alpha.pack(side="left", padx=(16, 0))
        ttk.Checkbutton(opts, text="打包 ZIP", variable=self.var_zip).pack(
            side="left", padx=(16, 0))
        ttk.Checkbutton(opts, text="跳过已存在", variable=self.var_skip).pack(
            side="left", padx=(16, 0))
        ttk.Checkbutton(opts, text="完成后打开目录", variable=self.var_open).pack(
            side="left", padx=(16, 0))

        # ---- 操作 ----
        bar = ttk.Frame(root)
        bar.pack(fill="x", pady=(12, 0))
        self.btn_go = ttk.Button(bar, text="开始转换", style="Go.TButton", command=self._start)
        self.btn_go.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="停止", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(10, 0))

        self.pb = ttk.Progressbar(root, mode="determinate", maximum=100)
        self.pb.pack(fill="x", pady=(12, 4))
        self.var_status = tk.StringVar(value="就绪")
        ttk.Label(root, textvariable=self.var_status).pack(anchor="w")

        # ---- 日志 ----
        self.log = ScrolledText(root, height=12, wrap="word", state="disabled",
                                font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, pady=(8, 0))
        self.log.tag_configure("ok", foreground="#137333")
        self.log.tag_configure("err", foreground="#c5221f")
        self.log.tag_configure("dim", foreground="#888888")

        self._log_line(f"{APP_TITLE} v1.0  就绪。选择 PDF 后点「开始转换」。", "dim")

    # --------------------------------------------------------- 事件 ----
    def _pick_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择 PDF（可多选）",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")],
        )
        if not paths:
            return
        self._inputs = list(paths)
        text = paths[0] if len(paths) == 1 else f"{paths[0]} 等 {len(paths)} 个文件"
        self._display = text
        self.var_input.set(text)
        self._suggest_out(paths[0])

    def _pick_dir(self) -> None:
        d = filedialog.askdirectory(title="选择包含 PDF 的文件夹")
        if not d:
            return
        self._inputs = [d]
        self._display = d
        self.var_input.set(d)
        self.var_out.set(str(Path(d) / "pdf2img_out"))

    def _pick_out(self) -> None:
        d = filedialog.askdirectory(title="选择输出文件夹")
        if d:
            self.var_out.set(d)

    def _suggest_out(self, first: str) -> None:
        if self.var_out.get().strip():
            return
        self.var_out.set(str(Path(first).parent / "pdf2img_out"))

    def preload(self, paths: list[str]) -> None:
        """命令行参数 / 拖拽到 exe 上时：预填文件并自动开始转换。"""
        if not paths:
            return
        self._inputs = list(paths)
        text = paths[0] if len(paths) == 1 else f"{paths[0]} 等 {len(paths)} 个文件"
        self._display = text
        self.var_input.set(text)
        self._suggest_out(paths[0])
        self.after(400, self._start)

    def _open_out(self) -> None:
        target = self.last_out or self.var_out.get().strip()
        if not target:
            messagebox.showinfo(APP_TITLE, "还没有输出目录")
            return
        p = Path(target)
        if not p.exists():
            messagebox.showinfo(APP_TITLE, f"目录不存在：\n{p}")
            return
        try:
            os.startfile(str(p))  # type: ignore[attr-defined]
        except Exception:
            messagebox.showinfo(APP_TITLE, f"输出目录：\n{p}")

    def _fmt_key(self) -> str:
        for label, key in FORMATS:
            if label == self.var_fmt.get():
                return key
        return "png"

    def _mode_key(self) -> str:
        for label, key in SIZE_MODES:
            if label == self.var_mode.get():
                return key
        return "dpi"

    def _sync_mode(self) -> None:
        mode = self._mode_key()
        self.sp_value.configure(from_=1, to=20000)
        self.var_value.set(DEFAULT_VALUE[mode])
        self.lb_unit.configure(text={"dpi": "DPI", "long": "px", "width": "px"}[mode])

    def _sync_state(self) -> None:
        fmt = self._fmt_key()
        lossy = fmt in ("jpg", "webp")
        self.sp_quality.configure(state="normal" if lossy else "disabled")
        self.lb_quality_hint.configure(
            text="仅 JPG / WebP 生效" if lossy else "当前格式无损，无需质量"
        )
        alpha_ok = fmt in ALPHA_FORMATS and not self.var_gray.get()
        self.ck_alpha.configure(state="normal" if alpha_ok else "disabled")
        if not alpha_ok:
            self.var_alpha.set(False)

    # --------------------------------------------------------- 执行 ----
    def _start(self) -> None:
        if self.running:
            return
        text = self.var_input.get().strip()
        # 用户手动改过输入框时，以输入框内容为准
        if text != self._display:
            raw = [text] if text else []
            self._inputs = []
        else:
            raw = list(self._inputs) or ([text] if text else [])
        out_dir = self.var_out.get().strip()
        if not raw or not raw[0]:
            messagebox.showwarning(APP_TITLE, "请先选择 PDF 文件或文件夹")
            return
        if not out_dir:
            messagebox.showwarning(APP_TITLE, "请选择输出目录")
            return

        try:
            pdfs = resolve_inputs(raw)
        except Pdf2ImgError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        fmt = self._fmt_key()
        mode = self._mode_key()
        try:
            value = float(self.var_value.get())
            if value <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror(APP_TITLE, "清晰度数值无效")
            return

        pages = self.var_pages.get().strip()
        if pages in ("", "全部", "all", "*"):
            pages = "all"

        # 预扫描页数，用于总进度
        total_pages = 0
        for pdf in pdfs:
            try:
                info = pdf_info(pdf)
                total_pages += len(parse_pages(pages, info["pages"]))
            except Pdf2ImgError as exc:
                messagebox.showerror(APP_TITLE, str(exc))
                return

        opts = Options(
            out_dir=Path(out_dir),
            fmt=fmt,
            dpi=value if mode == "dpi" else None,
            long_side=int(value) if mode == "long" else None,
            width=int(value) if mode == "width" else None,
            pages=pages,
            quality=int(self.var_quality.get() or 92),
            gray=self.var_gray.get(),
            transparent=self.var_alpha.get(),
            overwrite=not self.var_skip.get(),
            make_zip=self.var_zip.get(),
            jobs=1,
            log=lambda m: self.queue.put(("log", str(m))),
            should_stop=self.cancel.is_set,
        )

        self.cancel.clear()
        self.running = True
        self.btn_go.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.pb.configure(maximum=max(1, total_pages), value=0)
        self.var_status.set(f"共 {len(pdfs)} 个文件 / {total_pages} 页，开始转换…")
        self._log_line("", None)
        self._log_line(f"→ 输出目录：{Path(out_dir).resolve()}", "dim")

        self.worker = threading.Thread(
            target=self._run, args=(pdfs, opts, total_pages), daemon=True
        )
        self.worker.start()

    def _run(self, pdfs, opts: Options, total_pages: int) -> None:
        done_before = 0
        last_i = 0

        def page_cb(i: int, n: int, res) -> None:
            nonlocal done_before, last_i
            if i == 1 and last_i:
                done_before += last_i
            last_i = i
            self.queue.put(("progress", done_before + i, total_pages,
                            f"{Path(res.path).name}  {res.width}×{res.height}"))

        try:
            results = convert_many(pdfs, opts, page_progress=page_cb)
            self.queue.put(("done", results, opts))
        except BaseException as exc:  # noqa: BLE001
            cancelled = type(exc).__name__ == "CancelledError"
            detail = "" if cancelled else f"\n{traceback.format_exc(limit=2)}"
            self.queue.put(("failed", exc, cancelled, detail))

    def _stop(self) -> None:
        if self.running:
            self.cancel.set()
            self.var_status.set("正在停止…")
            self.btn_stop.configure(state="disabled")

    def _on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno(APP_TITLE, "转换正在进行，确定退出？"):
                return
            self.cancel.set()
        self.destroy()

    # --------------------------------------------------------- 回调 ----
    def _log_line(self, text: str, tag: str | None = None) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _poll(self) -> None:
        try:
            while True:
                msg = self.queue.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._log_line(msg[1])
                elif kind == "progress":
                    _kind, done, total, text = msg
                    self.pb.configure(value=done)
                    self.var_status.set(f"{done}/{total} 页   {text}")
                elif kind == "done":
                    self._on_done(msg[1], msg[2])
                elif kind == "failed":
                    self._on_failed(msg[1], msg[2], msg[3])
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def _finish(self) -> None:
        self.running = False
        self.btn_go.configure(state="normal")
        self.btn_stop.configure(state="disabled")

    def _on_done(self, results, opts: Options) -> None:
        self._finish()
        images = sum(len(r.images) for r in results)
        errors = [e for r in results for e in r.errors]
        size = sum(i.size for r in results for i in r.images)
        secs = sum(r.seconds for r in results)
        self.pb.configure(value=self.pb.cget("maximum"))
        self.var_status.set(f"完成：{images} 张图片，{size/1024/1024:.1f} MB，{secs:.1f} 秒")
        self._log_line(
            f"✔ 完成：{len(results)} 个 PDF / {images} 张图片 / "
            f"{size/1024/1024:.1f} MB / {secs:.1f}s", "ok"
        )
        for err in errors[:20]:
            self._log_line(f"✗ {err}", "err")
        if errors:
            self._log_line(f"共 {len(errors)} 处失败", "err")
        self.last_out = str(Path(results[0].out_dir if results else opts.out_dir))
        if self.var_open.get() and images:
            self._open_out()

    def _on_failed(self, exc: BaseException, cancelled: bool, detail: str) -> None:
        self._finish()
        if cancelled:
            self.var_status.set("已停止")
            self._log_line("■ 已按用户要求停止", "dim")
            return
        self.var_status.set("转换失败")
        self._log_line(f"✗ {exc}{detail}", "err")
        messagebox.showerror(APP_TITLE, f"转换失败：\n{exc}")


def main() -> None:
    app = App()
    app._sync_state()
    app._sync_mode()
    app.var_value.set("300")
    # 支持把 PDF 拖到 exe 上：直接开转
    pdfs = [a for a in sys.argv[1:] if a.lower().endswith(".pdf") and Path(a).exists()]
    if pdfs:
        app.preload(pdfs)
    app.mainloop()


if __name__ == "__main__":
    main()
