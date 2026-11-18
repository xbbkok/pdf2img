#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf2img.py —— PDF 转高清图片工具（核心库 + 命令行）

特点
----
* 渲染引擎：PyMuPDF / MuPDF，无需安装 poppler、Ghostscript 等外部程序
* 高清：可指定 DPI（默认 300）、长边像素、宽/高像素或缩放倍数
* 格式：PNG / JPG / WebP / TIFF / BMP，可调质量与压缩
* 页范围：1-3,5,8- 等写法，1 起始
* 批量：单文件、多文件、整个目录（可递归）
* 并行：-j N 多进程渲染，页数多时明显更快
* 透明背景、灰度、单色（传真/扫描件小体积）可选
* 输出图片写入真实 DPI 元数据，打印/排版时尺寸正确
* 可选打包 ZIP、可选 JSON 执行报告

命令行示例
----------
    python pdf2img.py 文件.pdf -o out
    python pdf2img.py 文件.pdf -o out -f jpg --dpi 450 --quality 95
    python pdf2img.py 扫描件.pdf -f png --long-side 4000
    python pdf2img.py 目录 -o out -f webp -j 4 --recursive

作者：DeepSeek Harness / lead
"""

from __future__ import annotations

import argparse
import glob as _glob
import json
import os
import re
import sys
import time
import zipfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Iterable, Sequence

# ---------------------------------------------------------------- 依赖 ----
try:
    import pymupdf  # PyMuPDF >= 1.24
except ImportError:  # 兼容老版本
    try:
        import fitz as pymupdf  # type: ignore
    except ImportError:  # pragma: no cover
        sys.stderr.write(
            "缺少依赖 PyMuPDF，请先安装：\n\n    pip install pymupdf pillow\n\n"
        )
        raise SystemExit(2)

try:
    from PIL import Image

    HAVE_PIL = True
except ImportError:  # pragma: no cover
    Image = None  # type: ignore
    HAVE_PIL = False


__version__ = "1.0.0"

# 输出格式：用户书写 -> 规范扩展名
FORMAT_ALIASES = {
    "png": "png",
    "jpg": "jpg",
    "jpeg": "jpg",
    "jpe": "jpg",
    "webp": "webp",
    "tif": "tif",
    "tiff": "tif",
    "bmp": "bmp",
}
PIL_FORMATS = {
    "png": "PNG",
    "jpg": "JPEG",
    "webp": "WEBP",
    "tif": "TIFF",
    "bmp": "BMP",
}
# 支持透明通道的格式
ALPHA_FORMATS = {"png", "webp", "tif"}

DEFAULT_DPI = 300.0
MAX_PIXELS_WARN = 150_000_000      # 约 1.5 亿像素时提醒
MAX_PIXELS_ERROR = 800_000_000     # 超过则拒绝渲染，防止爆内存

ProgressCb = Callable[[int, int, "PageResult"], None]
LogCb = Callable[[str], None]
StopCb = Callable[[], bool]

# Pillow >= 9.1 用 Image.Dither，旧版本用模块级常量
_DITHER = getattr(getattr(Image, "Dither", None), "FLOYDSTEINBERG", 0) if Image else 0


class Pdf2ImgError(RuntimeError):
    """可预期的用户错误（参数、输入、依赖等）。"""


class CancelledError(RuntimeError):
    """用户主动中止。"""


# ------------------------------------------------------------- 数据模型 ----
@dataclass
class PageResult:
    page: int          # 1 起始页码
    path: str          # 输出文件绝对路径
    width: int         # 像素宽
    height: int        # 像素高
    size: int          # 字节数
    dpi: float         # 实际 DPI
    seconds: float


@dataclass
class PdfResult:
    pdf: str
    out_dir: str
    pages_total: int
    images: list[PageResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0
    zip_path: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.images)


@dataclass
class Options:
    """一次转换的全部参数。GUI 与 CLI 共用。"""

    out_dir: Path
    fmt: str = "png"
    dpi: float | None = DEFAULT_DPI
    width: int | None = None
    height: int | None = None
    long_side: int | None = None
    scale: float | None = None
    pages: str = "all"
    quality: int = 92
    jpg_subsampling: int = 0          # 0=4:4:4，1=4:2:2，2=4:2:0
    png_compress: int = 3
    webp_method: int = 2
    gray: bool = False
    mono: bool = False
    transparent: bool = False
    bg: tuple[int, int, int] = (255, 255, 255)
    annots: bool = True
    password: str | None = None
    overwrite: bool = True
    jobs: int = 1
    make_zip: bool = False
    name_template: str = "{stem}_p{page}"
    flat: bool = False
    extract_original: bool = False
    log: LogCb | None = None
    should_stop: StopCb | None = None

    # ---- 派生属性 -------------------------------------------------------
    @property
    def ext(self) -> str:
        return self.fmt

    @property
    def pil_format(self) -> str:
        return PIL_FORMATS[self.fmt]

    @property
    def supports_alpha(self) -> bool:
        return self.fmt in ALPHA_FORMATS

    def say(self, msg: str) -> None:
        if self.log:
            self.log(msg)

    def stopped(self) -> bool:
        return bool(self.should_stop and self.should_stop())


# --------------------------------------------------------------- 工具 ----
def _human(n: float) -> str:
    """字节数可读化。"""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} GB"


def _safe_stem(stem: str) -> str:
    """把文件名里的非法字符换成下划线，Windows 更安全。"""
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .")
    return stem or "output"


def parse_color(text: str) -> tuple[int, int, int]:
    """支持 #FFF、#FFFFFF、255,255,255、white 等写法。"""
    t = (text or "").strip().lower()
    named = {
        "white": (255, 255, 255),
        "black": (0, 0, 0),
        "gray": (128, 128, 128),
        "grey": (128, 128, 128),
        "red": (255, 0, 0),
        "blue": (0, 0, 255),
        "green": (0, 128, 0),
    }
    if t in named:
        return named[t]
    if t.startswith("#"):
        t = t[1:]
    if re.fullmatch(r"[0-9a-f]{3}", t):
        return tuple(int(c * 2, 16) for c in t)  # type: ignore[return-value]
    if re.fullmatch(r"[0-9a-f]{6}", t):
        return (int(t[0:2], 16), int(t[2:4], 16), int(t[4:6], 16))
    parts = [p for p in re.split(r"[,\s]+", t) if p]
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        vals = tuple(int(p) for p in parts)
        if all(0 <= v <= 255 for v in vals):
            return vals  # type: ignore[return-value]
    raise Pdf2ImgError(f"无法识别的颜色：{text}")


def parse_pages(spec: str, total: int) -> list[int]:
    """把 '1-3,5,8-' 解析为 0 起始页码列表（去重、保序）。

    支持：单页 5、区间 2-6、开区间 8-、前 n 页 -3、all/*。
    """
    spec = (spec or "").strip()
    if not spec or spec.lower() in {"all", "*", "全部"}:
        return list(range(total))

    chosen: list[int] = []
    for chunk in re.split(r"[,;，、\s]+", spec):
        if not chunk:
            continue
        m = re.fullmatch(r"(\d*)\s*[-~]\s*(\d*)", chunk)
        if m:
            a, b = m.group(1), m.group(2)
            if not a and not b:
                raise Pdf2ImgError(f"页码范围写法有误：{chunk}")
            start = int(a) if a else 1
            end = int(b) if b else total
            step = 1 if end >= start else -1
            rng = range(start, end + step, step)
        elif chunk.isdigit():
            rng = range(int(chunk), int(chunk) + 1)
        else:
            raise Pdf2ImgError(f"无法识别的页码：{chunk}（示例：1-3,5,8-）")

        for p in rng:
            if p < 1 or p > total:
                raise Pdf2ImgError(f"页码 {p} 超出范围（共 {total} 页）")
            idx = p - 1
            if idx not in chosen:
                chosen.append(idx)
    if not chosen:
        raise Pdf2ImgError("页范围为空")
    return chosen


# ------------------------------------------------------------ 输入解析 ----
def resolve_inputs(raw: Sequence[str], recursive: bool = False) -> list[Path]:
    """把命令行/界面的输入展开成 PDF 文件列表。"""
    found: list[Path] = []
    for item in raw:
        if not item:
            continue
        p = Path(item).expanduser()
        if p.is_dir():
            pattern = "**/*.pdf" if recursive else "*.pdf"
            found.extend(sorted(q for q in p.glob(pattern) if q.is_file()))
        elif any(ch in item for ch in "*?["):
            found.extend(sorted(Path(q) for q in _glob.glob(item, recursive=True)))
        elif p.is_file():
            found.append(p)
        else:
            raise Pdf2ImgError(f"输入不存在：{item}")

    out: list[Path] = []
    seen: set[str] = set()
    for p in found:
        if p.suffix.lower() != ".pdf":
            continue
        key = str(p.resolve()).lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
    if not out:
        raise Pdf2ImgError("没有找到任何 PDF 文件")
    return out


def open_document(path: Path | str, password: str | None = None):
    """打开 PDF 并处理加密，返回 Document。

    注意：PyMuPDF 里 needs_pass 表示「文件带密码保护」，认证成功后它仍可能为 1，
    因此只能以 authenticate() 的返回值判断是否解锁。
    """
    try:
        doc = pymupdf.open(str(path))
    except Exception as exc:
        raise Pdf2ImgError(f"无法打开 PDF：{path}（{exc}）") from exc
    if getattr(doc, "needs_pass", False):
        if not password:
            doc.close()
            raise Pdf2ImgError(f"PDF 已加密，需要密码（用 --password 指定）：{path}")
        try:
            rc = doc.authenticate(password)
        except Exception as exc:
            doc.close()
            raise Pdf2ImgError(f"解密失败：{path}（{exc}）") from exc
        if not rc:
            doc.close()
            raise Pdf2ImgError(f"密码错误，无法解密：{path}")
    if doc.page_count <= 0:
        doc.close()
        raise Pdf2ImgError(f"PDF 没有页面：{path}")
    return doc


def pdf_info(path: Path | str, password: str | None = None) -> dict:
    """返回 PDF 概要信息，便于 CLI --info 与 GUI 预览。"""
    doc = open_document(path, password)
    try:
        first = doc[0].rect
        return {
            "path": str(path),
            "pages": doc.page_count,
            "encrypted": bool(password),
            "page_size_pt": [round(first.width, 2), round(first.height, 2)],
            "page_size_in": [
                round(first.width / 72.0, 2),
                round(first.height / 72.0, 2),
            ],
            "title": (doc.metadata or {}).get("title") or "",
            "format": (doc.metadata or {}).get("format") or "",
        }
    finally:
        doc.close()


# --------------------------------------------------------------- 渲染 ----
def _resolve_scale(page, opts: Options) -> tuple[float, tuple[int, int] | None]:
    """根据参数算出缩放倍数；若要求精确像素，第二项给出目标尺寸。"""
    rect = page.rect
    pw, ph = float(rect.width), float(rect.height)
    if pw <= 0 or ph <= 0:
        raise Pdf2ImgError(f"第 {page.number + 1} 页尺寸异常")

    if opts.width or opts.height:
        if opts.width and opts.height:
            sx, sy = opts.width / pw, opts.height / ph
            scale = min(sx, sy)
            tw, th = opts.width, opts.height
        elif opts.width:
            scale = opts.width / pw
            tw, th = opts.width, max(1, round(ph * scale))
        else:
            scale = float(opts.height) / ph  # type: ignore[arg-type]
            tw, th = max(1, round(pw * scale)), int(opts.height)  # type: ignore[arg-type]
        return scale, (int(tw), int(th))

    if opts.long_side:
        scale = float(opts.long_side) / max(pw, ph)
        return scale, None

    if opts.scale:
        return float(opts.scale), None

    dpi = float(opts.dpi or DEFAULT_DPI)
    return dpi / 72.0, None


def _pix_to_image(pix, gray: bool):
    """PyMuPDF Pixmap -> PIL Image。"""
    if not HAVE_PIL:  # pragma: no cover
        raise Pdf2ImgError("缺少 Pillow，无法处理图片：pip install pillow")
    mode = {1: "L", 2: "LA", 3: "RGB", 4: "RGBA"}.get(pix.n)
    if mode is None:
        raise Pdf2ImgError(f"不支持的像素通道数：{pix.n}")
    return Image.frombytes(mode, (pix.width, pix.height), pix.samples)


def _flatten(img, bg: tuple[int, int, int]):
    """透明图按背景色合成，返回 RGB 图。"""
    if img.mode in ("RGBA", "LA"):
        alpha = img.getchannel("A")
        base = Image.new("RGB", img.size, bg)
        base.paste(img.convert("RGB"), mask=alpha)
        return base
    if img.mode in ("L", "1", "P"):
        return img.convert("RGB")
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


# ------------------------------------------------- 内置 TIFF 写入器 ----
# 说明：部分 Windows 环境下 Pillow 自带的 libtiff 在写压缩 TIFF 时会直接
# 崩溃（0xC0000409），因此这里用标准库 zlib 自己拼 TIFF，稳定且无外部依赖。
_TIFF_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}


def _write_tiff(path: Path, img, dpi: tuple[float, float], compress: bool = True) -> None:
    """写出基线 TIFF（RGB / 灰度 / RGBA），deflate 压缩，多 strip。"""
    import datetime
    import struct
    import zlib

    mode = img.mode
    extra_samples: int | None = None
    if mode == "1":
        img, mode = img.convert("L"), "L"
    if mode == "L":
        channels, photo = 1, 1
    elif mode == "RGB":
        channels, photo = 3, 2
    elif mode == "RGBA":
        channels, photo, extra_samples = 4, 2, 2
    elif mode == "LA":
        img, mode = img.convert("L"), "L"
        channels, photo = 1, 1
    else:
        img, mode = img.convert("RGB"), "RGB"
        channels, photo = 3, 2

    width, height = img.size
    raw = img.tobytes()
    row_bytes = width * channels
    rows_per_strip = max(1, min(height, (512 * 1024) // max(1, row_bytes)))

    strips: list[bytes] = []
    for y0 in range(0, height, rows_per_strip):
        chunk = raw[y0 * row_bytes:(y0 + rows_per_strip) * row_bytes]
        strips.append(zlib.compress(chunk, 6) if compress else chunk)

    def layout(items: list[list]) -> tuple[bytes, bytes, int]:
        count = len(items)
        extra_off = 8 + 2 + 12 * count + 4
        extra = bytearray()
        body = bytearray(struct.pack("<H", count))
        for tag, typ, payload in items:
            n = len(payload) if typ == 2 else len(payload) // _TIFF_TYPE_SIZE[typ]
            body += struct.pack("<HHI", tag, typ, n)
            if len(payload) <= 4:
                body += payload.ljust(4, b"\x00")
            else:
                body += struct.pack("<I", extra_off + len(extra))
                extra += payload
                if len(extra) & 1:
                    extra += b"\x00"
        body += struct.pack("<I", 0)  # 下一个 IFD：无
        return bytes(body), bytes(extra), extra_off + len(extra)

    zero_offsets = struct.pack("<" + "I" * len(strips), *([0] * len(strips)))
    counts_payload = struct.pack("<" + "I" * len(strips), *[len(s) for s in strips])
    items: list[list] = [
        [256, 4, struct.pack("<I", width)],
        [257, 4, struct.pack("<I", height)],
        [258, 3, struct.pack("<" + "H" * channels, *([8] * channels))],
        [259, 3, struct.pack("<H", 8 if compress else 1)],
        [262, 3, struct.pack("<H", photo)],
        [273, 4, zero_offsets],
        [277, 3, struct.pack("<H", channels)],
        [278, 4, struct.pack("<I", rows_per_strip)],
        [279, 4, counts_payload],
        [282, 5, struct.pack("<II", max(1, int(round(dpi[0] * 100))), 100)],
        [283, 5, struct.pack("<II", max(1, int(round(dpi[1] * 100))), 100)],
        [284, 3, struct.pack("<H", 1)],
        [296, 3, struct.pack("<H", 2)],
        [305, 2, b"pdf2img\x00"],
        [306, 2, datetime.datetime.now().strftime("%Y:%m:%d %H:%M:%S").encode("ascii") + b"\x00"],
    ]
    if extra_samples is not None:
        items.append([338, 3, struct.pack("<H", extra_samples)])
    items.sort(key=lambda it: it[0])

    _, _, data_start = layout(items)
    offsets, pos = [], data_start
    for s in strips:
        offsets.append(pos)
        pos += len(s)
    offsets_payload = struct.pack("<" + "I" * len(strips), *offsets)
    for it in items:
        if it[0] == 273:
            it[2] = offsets_payload

    ifd, extra, data_start = layout(items)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"II")
        fh.write(struct.pack("<HI", 42, 8))
        fh.write(ifd)
        fh.write(extra)
        for s in strips:
            fh.write(s)


def render_page(doc, pno: int, opts: Options):
    """渲染单页，返回 (PIL.Image, 实际 dpi, 目标尺寸)。"""
    page = doc[pno]
    rect = page.rect
    scale, target = _resolve_scale(page, opts)

    est_w = int(round(rect.width * scale))
    est_h = int(round(rect.height * scale))
    if est_w <= 0 or est_h <= 0:
        raise Pdf2ImgError(f"第 {pno + 1} 页输出尺寸为 0")
    pixels = est_w * est_h
    if pixels > MAX_PIXELS_ERROR:
        raise Pdf2ImgError(
            f"第 {pno + 1} 页将输出 {est_w}x{est_h}（{pixels/1e6:.0f} MP），"
            f"超出安全上限，请降低 DPI"
        )
    if pixels > MAX_PIXELS_WARN:
        opts.say(
            f"  提示：第 {pno + 1} 页 {est_w}x{est_h} 像素较大，"
            f"内存占用约 {pixels * 4 / 1024 / 1024:.0f} MB"
        )

    if opts.gray or opts.mono:
        colorspace = pymupdf.csGRAY
    else:
        colorspace = pymupdf.csRGB

    alpha = bool(opts.transparent and opts.supports_alpha and not (opts.gray or opts.mono))

    pix = page.get_pixmap(
        matrix=pymupdf.Matrix(scale, scale),
        colorspace=colorspace,
        alpha=alpha,
        annots=bool(opts.annots),
    )
    img = _pix_to_image(pix, opts.gray)

    if opts.mono:
        # 先灰度再 1 位抖动，扫描件体积可降到 1/10
        img = img.convert("L").convert("1", dither=_DITHER)

    if target and (img.width, img.height) != target:
        if opts.mono:
            img = img.convert("L")
            img = img.resize(target, Image.LANCZOS).convert("1", dither=_DITHER)
        else:
            img = img.resize(target, Image.LANCZOS)

    dpi_x = img.width / (rect.width / 72.0) if rect.width else 72.0
    dpi_y = img.height / (rect.height / 72.0) if rect.height else 72.0
    return img, (dpi_x, dpi_y)


def save_image(img, path: Path, opts: Options, dpi: tuple[float, float]) -> None:
    """按格式写出图片。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not HAVE_PIL:  # pragma: no cover - 极少见
        raise Pdf2ImgError("缺少 Pillow，无法编码图片：pip install pillow")
    fmt = opts.pil_format
    out = img

    try:
        if fmt == "TIFF":
            if out.mode not in ("1", "L", "RGB", "RGBA"):
                out = out.convert("RGB")
            _write_tiff(path, out, dpi, compress=True)
            return

        params: dict = {}
        if fmt == "JPEG":
            if out.mode == "1":
                out = out.convert("L")
            if out.mode != "RGB":
                out = _flatten(out, opts.bg)
            params.update(
                quality=int(opts.quality),
                optimize=True,
                progressive=True,
                subsampling=int(opts.jpg_subsampling),
            )
        elif fmt == "WEBP":
            if out.mode not in ("RGB", "RGBA"):
                out = out.convert("RGBA") if "A" in out.mode else out.convert("RGB")
            params.update(quality=int(opts.quality), method=int(opts.webp_method))
        elif fmt == "PNG":
            params.update(compress_level=int(opts.png_compress))
        elif fmt == "BMP":
            if out.mode == "1":
                out = out.convert("L")
            elif out.mode not in ("L", "RGB"):
                out = out.convert("RGB")
        else:  # pragma: no cover
            raise Pdf2ImgError(f"不支持导出格式：{fmt}")

        out.save(path, format=fmt, dpi=(round(dpi[0], 2), round(dpi[1], 2)), **params)
    except (OSError, ValueError, KeyError) as exc:
        raise Pdf2ImgError(f"保存失败：{path.name}（{exc}）") from exc


# --------------------------------------------------------- 单文件转换 ----
def validate_name_template(template: str) -> None:
    """提前校验命名模板，避免转换到一半才报错。"""
    try:
        template.format(stem="x", page="001", pageno="1", index="000", total="1", dpi="300")
    except (KeyError, IndexError, ValueError) as exc:
        raise Pdf2ImgError(
            f"命名模板有误：{template}（可用字段：stem/page/pageno/index/total/dpi）"
        ) from exc


def _page_name(template: str, stem: str, pno: int, total: int, dpi: float, ext: str) -> str:
    width = max(3, len(str(total)))
    fields = {
        "stem": stem,
        "page": str(pno + 1).zfill(width),
        "pageno": str(pno + 1),
        "index": str(pno).zfill(width),
        "total": str(total),
        "dpi": str(int(dpi)),
    }
    try:
        base = template.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        raise Pdf2ImgError(
            f"命名模板有误：{template}（可用字段：stem/page/pageno/index/total/dpi）"
        ) from exc
    return f"{_safe_stem(base)}.{ext}"


def _extract_original_page(doc, pno: int, out_dir: Path, stem: str, name_tpl: str,
                           total: int) -> PageResult | None:
    """扫描件/单图页面：直接抽出内嵌原图，画质无损、速度极快。"""
    page = doc[pno]
    try:
        infos = page.get_images(full=True)
    except Exception:
        return None
    if len(infos) != 1:
        return None
    xref = infos[0][0]
    try:
        base = doc.extract_image(xref)
    except Exception:
        return None
    if not base or not base.get("image"):
        return None
    ext = (base.get("ext") or "png").lower()
    if ext in ("jpeg", "jpe"):
        ext = "jpg"
    elif ext in ("tiff",):
        ext = "tif"
    elif ext in ("jpx", "jp2"):
        ext = "jp2"
    width, height = base.get("width", 0), base.get("height", 0)
    if not width or not height:
        return None

    # 判断这张图是否覆盖整页（面积比）
    try:
        rects = page.get_image_rects(xref)
        page_area = abs(page.rect)
        covered = sum(abs(r & page.rect) for r in rects) if rects else 0
        if page_area and covered / page_area < 0.8:
            return None
    except Exception:
        pass

    out_path = out_dir / _page_name(name_tpl, stem, pno, total, 0, ext)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(base["image"])
    dpi = 72.0 * width / (page.rect.width or 1)
    return PageResult(
        page=pno + 1,
        path=str(out_path.resolve()),
        width=width,
        height=height,
        size=len(base["image"]),
        dpi=dpi,
        seconds=0.0,
    )


# 某些环境下 Pillow 的 JPEG 编码不可用（它的 libjpeg 依赖可写的临时目录，
# 受限账户/受限会话里会报 "broken data stream when writing image file"）。
# 一旦发生，本次运行剩余页面自动改用 MuPDF 直接写 JPEG。
_JPEG_VIA_MUPDF = False


def _render_jpeg_mupdf(doc, pno: int, out_dir: Path, stem: str, opts: Options,
                       total: int) -> PageResult:
    """用 MuPDF 自带的 JPEG 编码器写图（不经过 Pillow），作为兜底路径。"""
    t0 = time.perf_counter()
    page = doc[pno]
    scale, _ = _resolve_scale(page, opts)
    colorspace = pymupdf.csGRAY if (opts.gray or opts.mono) else pymupdf.csRGB
    pix = page.get_pixmap(
        matrix=pymupdf.Matrix(scale, scale),
        colorspace=colorspace,
        alpha=False,
        annots=bool(opts.annots),
    )
    dpi = 72.0 * pix.width / (page.rect.width or 1)
    out_path = out_dir / _page_name(opts.name_template, stem, pno, total, dpi, opts.ext)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists() and not opts.overwrite:
        opts.say(f"  跳过已存在：{out_path.name}")
    else:
        pix.save(str(out_path), output="jpg", jpg_quality=int(opts.quality))
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise Pdf2ImgError(f"输出文件为空：{out_path}")
    return PageResult(
        page=pno + 1,
        path=str(out_path.resolve()),
        width=pix.width,
        height=pix.height,
        size=out_path.stat().st_size,
        dpi=dpi,
        seconds=time.perf_counter() - t0,
    )


def convert_page(doc, pno: int, out_dir: Path, stem: str, opts: Options, total: int) -> PageResult:
    """转换一页（渲染 + 写盘）。"""
    global _JPEG_VIA_MUPDF
    t0 = time.perf_counter()
    if opts.extract_original and opts.fmt == "png":
        fast = _extract_original_page(doc, pno, out_dir, stem, opts.name_template, total)
        if fast is not None:
            fast.seconds = time.perf_counter() - t0
            return fast

    if opts.fmt == "jpg" and _JPEG_VIA_MUPDF:
        return _render_jpeg_mupdf(doc, pno, out_dir, stem, opts, total)

    img, dpi = render_page(doc, pno, opts)
    out_path = out_dir / _page_name(opts.name_template, stem, pno, total, dpi[0], opts.ext)
    if out_path.exists() and not opts.overwrite:
        opts.say(f"  跳过已存在：{out_path.name}")
        return PageResult(
            page=pno + 1,
            path=str(out_path.resolve()),
            width=img.width,
            height=img.height,
            size=out_path.stat().st_size,
            dpi=dpi[0],
            seconds=time.perf_counter() - t0,
        )
    try:
        save_image(img, out_path, opts, dpi)
    except Pdf2ImgError:
        if opts.fmt != "jpg":
            raise
        _JPEG_VIA_MUPDF = True
        opts.say("  提示：Pillow 的 JPEG 编码在当前环境不可用，已改用 MuPDF 直接输出 JPEG")
        return _render_jpeg_mupdf(doc, pno, out_dir, stem, opts, total)
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise Pdf2ImgError(f"输出文件为空：{out_path}")
    return PageResult(
        page=pno + 1,
        path=str(out_path.resolve()),
        width=img.width,
        height=img.height,
        size=out_path.stat().st_size,
        dpi=dpi[0],
        seconds=time.perf_counter() - t0,
    )


# --------------------------------------------------------- 多进程支持 ----
_WORKER_DOCS: dict[str, object] = {}


def _worker(task: tuple) -> PageResult | str:
    """子进程入口：打开文档并转换指定页。返回 PageResult 或错误字符串。"""
    pdf_path, password, pno, out_dir, stem, opts, total = task
    try:
        key = f"{pdf_path}\x00{password or ''}"
        doc = _WORKER_DOCS.get(key)
        if doc is None:
            doc = open_document(pdf_path, password)
            _WORKER_DOCS[key] = doc
        return convert_page(doc, pno, Path(out_dir), stem, opts, total)  # type: ignore[arg-type]
    except Pdf2ImgError as exc:
        return f"第 {pno + 1} 页：{exc}"
    except Exception as exc:  # pragma: no cover
        return f"第 {pno + 1} 页：{type(exc).__name__}: {exc}"


# ------------------------------------------------------------- 主流程 ----
def convert_pdf(
    pdf_path: Path | str,
    opts: Options,
    out_dir: Path | None = None,
    progress: ProgressCb | None = None,
) -> PdfResult:
    """把一个 PDF 转成图片序列。"""
    pdf_path = Path(pdf_path)
    t_start = time.perf_counter()
    validate_name_template(opts.name_template)
    target_dir = Path(out_dir) if out_dir else Path(opts.out_dir)
    result = PdfResult(pdf=str(pdf_path.resolve()), out_dir=str(target_dir.resolve()), pages_total=0)

    doc = open_document(pdf_path, opts.password)
    try:
        total = doc.page_count
        result.pages_total = total
        pages = parse_pages(opts.pages, total)
        stem = _safe_stem(pdf_path.stem)
        target_dir.mkdir(parents=True, exist_ok=True)

        if opts.jobs and opts.jobs > 1 and len(pages) > 1:
            done = _convert_parallel(doc, pdf_path, pages, target_dir, stem, opts, total, result, progress)
            if done is None:  # 进程池不可用，退回串行
                done = _convert_sequential(doc, pages, target_dir, stem, opts, total, result, progress)
            result.images = done
        else:
            result.images = _convert_sequential(
                doc, pages, target_dir, stem, opts, total, result, progress
            )

        result.images.sort(key=lambda r: r.page)

        if opts.make_zip and result.images:
            result.zip_path = _make_zip(target_dir, stem, result.images)
    finally:
        doc.close()

    result.seconds = time.perf_counter() - t_start
    return result


def _convert_sequential(doc, pages, target_dir, stem, opts, total, result, progress):
    images: list[PageResult] = []
    for i, pno in enumerate(pages, 1):
        if opts.stopped():
            raise CancelledError("已中止")
        try:
            res = convert_page(doc, pno, target_dir, stem, opts, total)
        except Pdf2ImgError as exc:
            msg = f"第 {pno + 1} 页：{exc}"
            result.errors.append(msg)
            opts.say(f"  ✗ {msg}")
            continue
        images.append(res)
        if progress:
            progress(i, len(pages), res)
    return images


def _convert_parallel(doc, pdf_path, pages, target_dir, stem, opts, total, result, progress):
    """多进程渲染；任何环境问题都返回 None 让调用方退回串行。"""
    try:
        from concurrent.futures import ProcessPoolExecutor, as_completed
    except Exception:  # pragma: no cover
        return None

    # 回调函数通常不可 pickle（例如 CLI 里的 lambda），子进程只带纯数据参数
    worker_opts = Options(**{**opts.__dict__, "log": None, "should_stop": None})
    tasks = [
        (str(pdf_path.resolve()), opts.password, pno, str(target_dir), stem, worker_opts, total)
        for pno in pages
    ]
    images: list[PageResult] = []
    try:
        with ProcessPoolExecutor(max_workers=max(1, int(opts.jobs))) as pool:
            futures = {pool.submit(_worker, t): t[2] for t in tasks}
            done = 0
            for fut in as_completed(futures):
                if opts.stopped():
                    raise CancelledError("已中止")
                res = fut.result()
                done += 1
                if isinstance(res, str):
                    result.errors.append(res)
                    opts.say(f"  ✗ {res}")
                    continue
                images.append(res)
                if progress:
                    progress(done, len(pages), res)
        return images
    except CancelledError:
        raise
    except Exception as exc:
        if isinstance(exc, PermissionError):
            opts.say("  提示：当前环境不允许创建子进程，已自动改为单进程转换")
        else:
            opts.say(f"  提示：并行模式不可用（{type(exc).__name__}），已改为单进程转换")
        return None


def _make_zip(out_dir: Path, stem: str, images: Iterable[PageResult]) -> str:
    zip_path = out_dir / f"{stem}_images.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for img in images:
            p = Path(img.path)
            if p.exists():
                zf.write(p, arcname=p.name)
    return str(zip_path.resolve())


def convert_many(
    pdfs: Sequence[Path],
    opts: Options,
    progress: Callable[[int, int, str], None] | None = None,
    page_progress: ProgressCb | None = None,
) -> list[PdfResult]:
    """批量转换；多个 PDF 时默认各自建子目录（opts.flat 可关闭）。"""
    batch = len(pdfs) > 1
    results: list[PdfResult] = []
    used_names: dict[str, int] = {}

    for i, pdf in enumerate(pdfs, 1):
        if opts.stopped():
            raise CancelledError("已中止")
        sub = Path(opts.out_dir)
        if batch and not opts.flat:
            name = _safe_stem(pdf.stem)
            if name in used_names:
                used_names[name] += 1
                name = f"{name}_{used_names[name]}"
            else:
                used_names[name] = 1
            sub = sub / name
        opts.say(f"[{i}/{len(pdfs)}] {pdf.name}  →  {sub}")
        try:
            res = convert_pdf(pdf, opts, out_dir=sub, progress=page_progress)
        except Pdf2ImgError as exc:
            opts.say(f"  ✗ 失败：{exc}")
            res = PdfResult(pdf=str(pdf.resolve()), out_dir=str(sub), pages_total=0, errors=[str(exc)])
        results.append(res)
        if progress:
            progress(i, len(pdfs), pdf.name)
    return results


# --------------------------------------------------------------- CLI ----
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pdf2img",
        description="PDF 转高清图片（PNG/JPG/WebP/TIFF/BMP），支持 DPI、页范围、批量、并行",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  pdf2img 文件.pdf -o out\n"
            "  pdf2img 文件.pdf -o out -f jpg --dpi 450 --quality 95\n"
            "  pdf2img 扫描件.pdf -f png --long-side 4000\n"
            "  pdf2img 目录 -o out -f webp -j 4 --recursive\n"
            "  pdf2img 文件.pdf -o out -p 1-3,8 -f png --transparent\n"
        ),
    )
    p.add_argument("input", nargs="*", help="PDF 文件、通配符或目录")
    p.add_argument("-o", "--out", default="pdf2img_out", help="输出目录（默认 pdf2img_out）")
    p.add_argument(
        "-f", "--format", default="png",
        choices=sorted(set(FORMAT_ALIASES)), help="输出格式，默认 png",
    )
    q = p.add_argument_group("清晰度")
    q.add_argument("--dpi", type=float, default=DEFAULT_DPI, help=f"渲染 DPI，默认 {DEFAULT_DPI:g}")
    q.add_argument("--width", type=int, help="按像素宽输出（等比压缩）")
    q.add_argument("--height", type=int, help="按像素高输出（等比压缩）")
    q.add_argument("--long-side", type=int, help="按长边像素输出，适合固定尺寸封面")
    q.add_argument("--scale", type=float, help="按倍数缩放，如 2 表示 2 倍（=144dpi）")
    p.add_argument("-p", "--pages", default="all", help="页范围，如 1-3,5,8-（默认全部）")
    g = p.add_argument_group("画质")
    g.add_argument("-q", "--quality", type=int, default=92, help="JPG/WEBP 质量 1-100，默认 92")
    g.add_argument("--jpg-subsampling", type=int, choices=[0, 1, 2], default=0,
                   help="JPG 色度采样：0=4:4:4 最清晰(默认) 1=4:2:2 2=4:2:0 体积最小")
    g.add_argument("--png-compress", type=int, choices=range(0, 10), default=3,
                   help="PNG 压缩级别 0-9，默认 3（实测速度与体积最佳）")
    g.add_argument("--webp-method", type=int, choices=range(0, 7), default=2,
                   help="WebP 编码力度 0-6，越大越慢越小，默认 2")
    g.add_argument("--gray", action="store_true", help="输出灰度图")
    g.add_argument("--mono", action="store_true", help="输出 1 位单色图（扫描件体积小）")
    g.add_argument("--transparent", action="store_true",
                   help="保留透明背景（PNG/WebP/TIFF 有效）")
    g.add_argument("--bg", default="#FFFFFF",
                   help="不透明格式的底色，如 #FFFFFF 或 white")
    p.add_argument("--no-annotations", action="store_true", help="不渲染批注/表单/签名")
    p.add_argument("--password", help="加密 PDF 的打开密码")
    p.add_argument("--name", default="{stem}_p{page}",
                   help="文件名模板，字段：stem/page/pageno/index/total/dpi")
    p.add_argument("--skip-existing", action="store_true", help="已存在同名图片则跳过")
    p.add_argument("-j", "--jobs", type=int, default=1, help="并行进程数，默认 1；0=自动")
    p.add_argument("--zip", action="store_true", help="额外打包成 zip")
    p.add_argument("--flat", action="store_true", help="批量时全部输出到同一目录")
    p.add_argument("--recursive", action="store_true", help="目录输入时递归查找 PDF")
    p.add_argument("--extract-original", action="store_true",
                   help="扫描件优先抽取内嵌原图（无损、更快，仅 png 模式）")
    p.add_argument("--json", metavar="FILE", help="把执行报告写入 JSON")
    p.add_argument("--info", action="store_true", help="只显示 PDF 信息，不转换")
    p.add_argument("--selftest", action="store_true",
                   help="就地自检：现场生成测试 PDF，跑一遍全部导出格式")
    p.add_argument("--dry-run", action="store_true", help="只显示将要做什么，不写文件")
    p.add_argument("--list-formats", action="store_true", help="列出支持的格式")
    p.add_argument("-Q", "--quiet", action="store_true", help="静默模式")
    p.add_argument("-V", "--version", action="version", version=f"pdf2img {__version__}")
    return p


def _print_summary(results: Sequence[PdfResult], opts: Options) -> None:
    total_imgs = sum(len(r.images) for r in results)
    total_pages = sum(r.pages_total for r in results)
    total_bytes = sum(img.size for r in results for img in r.images)
    total_time = sum(r.seconds for r in results)
    errors = [e for r in results for e in r.errors]

    print()
    print("=" * 66)
    print(f"完成：{len(results)} 个 PDF / {total_imgs} 张图片"
          f"（共 {total_pages} 页）/ {_human(total_bytes)} / {total_time:.1f}s")
    fmt = opts.fmt.upper()
    if opts.extract_original and opts.fmt == "png":
        dpi_txt = "原图提取（无损，保持原始编码）"
    elif opts.dpi and not (opts.width or opts.height or opts.long_side or opts.scale):
        dpi_txt = f"{opts.dpi:g} DPI"
    else:
        dpi_txt = "自定义尺寸"
    print(f"格式：{fmt}  {dpi_txt}   输出：{Path(opts.out_dir).resolve()}")
    if len(results) == 1 and results[0].images:
        first = results[0].images[0]
        print(f"首张：{Path(first.path).name}  {first.width}x{first.height} px  "
              f"({_human(first.size)})")
    if errors:
        print(f"失败 {len(errors)} 处：")
        for err in errors[:10]:
            print(f"  - {err}")
        if len(errors) > 10:
            print(f"  ... 其余 {len(errors) - 10} 条省略")
    print("=" * 66)


def _report_json(results: Sequence[PdfResult], opts: Options) -> dict:
    return {
        "tool": f"pdf2img {__version__}",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "options": {
            "format": opts.fmt,
            "dpi": opts.dpi,
            "width": opts.width,
            "height": opts.height,
            "long_side": opts.long_side,
            "pages": opts.pages,
            "quality": opts.quality,
            "gray": opts.gray,
            "mono": opts.mono,
            "transparent": opts.transparent,
        },
        "results": [
            {
                "pdf": r.pdf,
                "out_dir": r.out_dir,
                "pages_total": r.pages_total,
                "images": [asdict(i) for i in r.images],
                "errors": r.errors,
                "seconds": round(r.seconds, 3),
                "zip": r.zip_path,
            }
            for r in results
        ],
        "summary": {
            "pdfs": len(results),
            "images": sum(len(r.images) for r in results),
            "bytes": sum(i.size for r in results for i in r.images),
            "errors": sum(len(r.errors) for r in results),
        },
    }


def run_selftest() -> int:
    """就地自检：现场生成一个 PDF，跑一遍各格式，判断当前机器能否正常使用。"""
    import shutil
    import tempfile

    print("pdf2img 自检")
    print(f"  Python  {sys.version.split()[0]}  ({sys.platform})")
    print(f"  PyMuPDF {pymupdf.__version__}")
    print(f"  Pillow  {Image.__version__ if HAVE_PIL else '缺失'}")

    try:
        base = Path(tempfile.mkdtemp(prefix="pdf2img_selftest_"))
    except Exception:
        base = Path.cwd() / "pdf2img_selftest"
        base.mkdir(parents=True, exist_ok=True)

    checks: list[tuple[str, bool, str]] = []
    try:
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((56, 90), "pdf2img selftest", fontsize=26)
        page.insert_text((56, 130), "The quick brown fox jumps over the lazy dog 0123456789",
                         fontsize=10)
        page.draw_rect(pymupdf.Rect(56, 160, 540, 420), color=(0.2, 0.4, 0.9), width=2)
        page.draw_circle((300, 560), 90, color=(0.9, 0.3, 0.2), fill=(0.95, 0.9, 0.2))
        page.insert_text((56, 720), "中文测试 第 1 页", fontsize=14)
        pdf = base / "selftest.pdf"
        doc.save(pdf)
        doc.close()

        for fmt in ("png", "jpg", "webp", "tif", "bmp"):
            out = base / fmt
            t0 = time.perf_counter()
            try:
                res = convert_pdf(pdf, Options(out_dir=out, fmt=fmt, dpi=100), out_dir=out)
                img = res.images[0] if res.images else None
                ok = bool(img) and Path(img.path).stat().st_size > 0 and not res.errors
                detail = (f"{img.width}x{img.height}  {img.size/1024:.0f} KB  "
                          f"{time.perf_counter() - t0:.2f}s") if ok else (
                    res.errors[0] if res.errors else "没有输出")
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            checks.append((fmt.upper(), ok, detail))

        cn_dir = base / "中文 目录"
        try:
            res = convert_pdf(pdf, Options(out_dir=cn_dir, fmt="png", dpi=60), out_dir=cn_dir)
            ok = bool(res.images) and not res.errors
            detail = Path(res.images[0].path).name if ok else "失败"
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        checks.append(("中文路径", ok, detail))
    finally:
        shutil.rmtree(base, ignore_errors=True)

    for name, ok, detail in checks:
        print(f"  [{'正常' if ok else '失败'}] {name:8s}  {detail}")
    failed = [c for c in checks if not c[1]]
    if failed:
        print(f"\n自检未通过（{len(failed)} 项），请反馈上面的信息。")
        return 1
    print("\n自检通过：本机可正常使用。")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_formats:
        print("支持格式：", ", ".join(sorted(set(FORMAT_ALIASES.values()))))
        print("透明背景：", ", ".join(sorted(ALPHA_FORMATS)))
        return 0

    if args.selftest:
        return run_selftest()

    if not args.input:
        parser.error("缺少输入 PDF（可用 --info/--list-formats 查看信息）")

    try:
        fmt = FORMAT_ALIASES[args.format]
    except KeyError:
        parser.error(f"不支持的格式：{args.format}")

    if args.mono and args.transparent:
        parser.error("--mono 与 --transparent 不能同时使用")
    if args.quality < 1 or args.quality > 100:
        parser.error("--quality 必须在 1-100 之间")
    try:
        bg = parse_color(args.bg)
    except Pdf2ImgError as exc:
        parser.error(str(exc))

    jobs = args.jobs
    if jobs == 0:
        jobs = max(1, min(4, (os.cpu_count() or 2)))

    log = (lambda m: None) if args.quiet else print
    opts = Options(
        out_dir=Path(args.out),
        fmt=fmt,
        dpi=args.dpi,
        width=args.width,
        height=args.height,
        long_side=args.long_side,
        scale=args.scale,
        pages=args.pages,
        quality=args.quality,
        jpg_subsampling=args.jpg_subsampling,
        png_compress=args.png_compress,
        webp_method=args.webp_method,
        gray=args.gray,
        mono=args.mono,
        transparent=args.transparent,
        bg=bg,
        annots=not args.no_annotations,
        password=args.password,
        overwrite=not args.skip_existing,
        jobs=jobs,
        make_zip=args.zip,
        name_template=args.name,
        flat=args.flat,
        extract_original=args.extract_original,
        log=log,
    )

    try:
        pdfs = resolve_inputs(args.input, args.recursive)
    except Pdf2ImgError as exc:
        print(f"错误：{exc}")
        return 2

    if args.info:
        for pdf in pdfs:
            try:
                info = pdf_info(pdf, args.password)
            except Pdf2ImgError as exc:
                print(f"✗ {exc}")
                continue
            w, h = info["page_size_in"]
            print(f"{pdf.name}")
            print(f"  页数：{info['pages']}   首页尺寸：{w} x {h} 英寸")
            print(f"  300DPI 输出约：{int(w*300)} x {int(h*300)} px")
            if info["title"]:
                print(f"  标题：{info['title']}")
        return 0

    if args.dry_run:
        print(f"计划转换 {len(pdfs)} 个 PDF → {Path(args.out).resolve()}  格式 {fmt.upper()}")
        for pdf in pdfs:
            try:
                n = pymupdf.open(str(pdf)).page_count
                sel = len(parse_pages(args.pages, n))
                print(f"  {pdf.name}：{n} 页，选中 {sel} 页")
            except Exception as exc:
                print(f"  {pdf.name}：无法读取（{exc}）")
        return 0

    t0 = time.perf_counter()

    def page_progress(i: int, n: int, res: PageResult) -> None:
        if args.quiet:
            return
        line = (f"  页 {i}/{n} ({i / n * 100:5.1f}%)  {Path(res.path).name}  "
                f"{res.width}x{res.height}  {_human(res.size)}  {res.seconds:.2f}s")
        if sys.stdout.isatty() and i < n:
            print("\r" + line + "      ", end="", flush=True)
        else:
            print(line, flush=True)

    try:
        results = convert_many(pdfs, opts, page_progress=page_progress)
    except CancelledError:
        print("已中止")
        return 130
    except KeyboardInterrupt:
        print("\n已中止")
        return 130

    if not args.quiet:
        _print_summary(results, opts)

    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_report_json(results, opts), ensure_ascii=False, indent=2),
                        encoding="utf-8")
        if not args.quiet:
            print(f"报告已写入：{path.resolve()}")

    if not args.quiet:
        print(f"总耗时 {time.perf_counter() - t0:.1f}s")

    failed = sum(1 for r in results if not r.ok)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
