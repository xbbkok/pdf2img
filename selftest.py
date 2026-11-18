#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest.py —— 环境与功能自检

不依赖任何外部 PDF：先用 PyMuPDF 现场生成一个测试 PDF，再跑一遍
渲染、多格式导出、页范围、尺寸模式、灰度/单色/透明、批量、ZIP 等路径。

用法： python selftest.py
"""

from __future__ import annotations

import sys
import time
import zipfile
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import pymupdf
from PIL import Image

from pdf2img import (
    Options,
    Pdf2ImgError,
    convert_many,
    convert_pdf,
    parse_pages,
    parse_color,
)

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "out" / "_selftest"
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [ok]   {name}  {detail}")
    else:
        FAILS.append(name)
        print(f"  [FAIL] {name}  {detail}")


def make_pdf(path: Path, pages: int = 3) -> Path:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)  # A4
        page.insert_text((60, 90), f"pdf2img selftest - page {i + 1}",
                         fontsize=24, color=(0, 0, 0))
        page.insert_text((60, 130), "The quick brown fox jumps over the lazy dog.",
                         fontsize=11)
        page.draw_rect(pymupdf.Rect(60, 160, 535, 400), color=(0.2, 0.4, 0.9), width=2)
        page.draw_circle((300, 520), 90, color=(0.9, 0.3, 0.2), fill=(0.95, 0.9, 0.2))
        page.insert_text((60, 700), f"第 {i + 1} 页 · 测试中文", fontsize=14)
    doc.save(path)
    doc.close()
    return path


def main() -> int:
    print(f"PyMuPDF {pymupdf.__version__} | Pillow {Image.__version__}")
    WORK.mkdir(parents=True, exist_ok=True)

    # ---------- 1. 纯函数 ----------
    print("\n[1] 参数解析")
    check("parse_pages 列表", parse_pages("2,4", 5) == [1, 3])
    check("parse_pages 区间", parse_pages("2-4", 5) == [1, 2, 3])
    check("parse_pages 开区间", parse_pages("3-", 5) == [2, 3, 4])
    check("parse_pages 前 N 页", parse_pages("-2", 5) == [0, 1])
    check("parse_pages 全部", parse_pages("all", 5) == [0, 1, 2, 3, 4])
    try:
        parse_pages("9", 5)
        check("parse_pages 越界报错", False)
    except Pdf2ImgError:
        check("parse_pages 越界报错", True)
    check("parse_color hex", parse_color("#0a0B0c") == (10, 11, 12))
    check("parse_color 逗号", parse_color("10,20,30") == (10, 20, 30))

    # ---------- 2. 生成测试 PDF ----------
    print("\n[2] 生成测试 PDF")
    src = make_pdf(WORK / "selftest.pdf", 3)
    check("测试 PDF", src.exists(), f"{src.stat().st_size} B")

    # ---------- 3. 各格式导出 ----------
    print("\n[3] 格式导出（120 DPI）")
    sizes = {}
    for fmt in ("png", "jpg", "webp", "tif", "bmp"):
        out = WORK / f"fmt_{fmt}"
        opts = Options(out_dir=out, fmt=fmt, dpi=120, log=None)
        t = time.perf_counter()
        res = convert_pdf(src, opts, out_dir=out)
        dt = time.perf_counter() - t
        files = sorted(out.glob(f"*.{opts.ext}"))
        size = files[0].stat().st_size if files else 0
        sizes[fmt] = size
        check(f"{fmt} 生成 3 页",
              len(files) == 3 and not res.errors,
              f"{len(files)} 个 / {size/1024:.0f} KB / {dt:.2f}s")

        if fmt in ("png", "bmp", "jpg", "webp"):
            with Image.open(files[0]) as im:
                im.load()
                w, h = im.size
            expect = (595 / 72 * 120, 842 / 72 * 120)
            # MuPDF 渲染按向上取整，允许 ±1 像素
            ok_size = abs(w - expect[0]) <= 1 and abs(h - expect[1]) <= 1
            check(f"{fmt} 尺寸正确", ok_size, f"{w}x{h} 期望约 {expect[0]:.1f}x{expect[1]:.1f}")
        elif fmt == "tif":
            head = files[0].read_bytes()[:8]
            check("tif 文件头合法", head[:4] == b"II*\x00" and int.from_bytes(head[4:6], "little") > 0,
                  head.hex())

    # ---------- 4. 页范围 / 命名 ----------
    print("\n[4] 页范围与命名")
    out = WORK / "range"
    opts = Options(out_dir=out, fmt="png", dpi=72, pages="1,3",
                   name_template="{stem}-{page}of{total}")
    res = convert_pdf(src, opts, out_dir=out)
    names = sorted(p.name for p in out.glob("*.png"))
    check("只转 1、3 页", [r.page for r in res.images] == [1, 3], str([r.page for r in res.images]))
    check("命名模板生效", names[0] == "selftest-001of3.png", names[0])

    # ---------- 5. 尺寸模式 ----------
    print("\n[5] 尺寸模式")
    out = WORK / "width"
    res = convert_pdf(src, Options(out_dir=out, fmt="png", width=1000), out_dir=out)
    with Image.open(res.images[0].path) as im:
        check("按宽度 1000", im.width == 1000, f"{im.size}")

    out = WORK / "long"
    res = convert_pdf(src, Options(out_dir=out, fmt="png", long_side=1600), out_dir=out)
    with Image.open(res.images[0].path) as im:
        check("按长边 1600", max(im.size) == 1600, f"{im.size}")

    # ---------- 6. 灰度 / 单色 / 透明 ----------
    print("\n[6] 色彩模式")
    out = WORK / "gray"
    res = convert_pdf(src, Options(out_dir=out, fmt="png", gray=True, dpi=100, pages="1"), out_dir=out)
    with Image.open(res.images[0].path) as im:
        check("灰度输出", im.mode == "L", im.mode)

    out = WORK / "mono"
    res = convert_pdf(src, Options(out_dir=out, fmt="png", mono=True, dpi=100, pages="1"), out_dir=out)
    with Image.open(res.images[0].path) as im:
        check("单色输出", im.mode == "1", f"{im.mode} {im.size}")

    out = WORK / "alpha"
    res = convert_pdf(src, Options(out_dir=out, fmt="png", transparent=True, dpi=100, pages="1"), out_dir=out)
    with Image.open(res.images[0].path) as im:
        check("透明 PNG", im.mode == "RGBA", im.mode)

    # ---------- 7. 批量 + ZIP ----------
    print("\n[7] 批量与打包")
    sub = WORK / "batch_in"
    sub.mkdir(exist_ok=True)
    make_pdf(sub / "a.pdf", 1)
    make_pdf(sub / "b.pdf", 2)
    out = WORK / "batch_out"
    opts = Options(out_dir=out, fmt="jpg", dpi=72, make_zip=True, quality=85)
    results = convert_many([sub / "a.pdf", sub / "b.pdf"], opts)
    check("批量两个 PDF", len(results) == 2, f"{len(results)}")
    check("按文件建子目录", (out / "a").is_dir() and (out / "b").is_dir())
    zips = list(out.glob("*/*_images.zip"))
    check("ZIP 生成", len(zips) == 2, f"{len(zips)} 个")
    if zips:
        with zipfile.ZipFile(zips[0]) as zf:
            check("ZIP 内容可读", zf.testzip() is None and len(zf.namelist()) >= 1)

    # ---------- 8. 错误处理 ----------
    print("\n[8] 错误处理")
    try:
        convert_pdf(WORK / "missing.pdf", Options(out_dir=WORK / "x"))
        check("缺失文件报错", False)
    except Pdf2ImgError:
        check("缺失文件报错", True)
    try:
        convert_pdf(src, Options(out_dir=WORK / "x", pages="9"))
        check("页码越界报错", False)
    except Pdf2ImgError:
        check("页码越界报错", True)
    try:
        convert_pdf(src, Options(out_dir=WORK / "x", name_template="{nope}"))
        check("模板错误报错", False)
    except Pdf2ImgError:
        check("模板错误报错", True)

    # ---------- 汇总 ----------
    total = sum(sizes.values())
    print(f"\n五格式合计 {total/1024/1024:.2f} MB，单页 120DPI 平均 "
          f"{sum(sizes.values())/len(sizes)/1024/3:.0f} KB")
    if FAILS:
        print(f"\n自检失败 {len(FAILS)} 项：{', '.join(FAILS)}")
        print(f"产物保留在 {WORK} 便于排查")
        return 1

    import shutil

    shutil.rmtree(WORK, ignore_errors=True)
    print("\n全部自检通过 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
