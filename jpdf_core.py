#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jpdf_core.py — JPDF 변환 엔진 (이미지 → PDF/JPDF, PDF/JPDF → 이미지)

JPDF 파일 형식 요약
-------------------
* .jpdf 파일은 **그 자체로 유효한 PDF 1.7 문서**다. 확장자만 .pdf로 바꿔도 모든 PDF 뷰어에서 열린다.
* 각 이미지는 페이지 하나에 **딱 한 번** 저장된다.
    - JPEG : 원본 파일의 바이트를 그대로 /DCTDecode 이미지 스트림에 넣는다 (재인코딩 없음, 비트 단위 동일).
    - PNG  : 가능하면 원본 IDAT(zlib) 데이터를 그대로 /FlateDecode + PNG 예측자(Predictor 15)로 넣는다
             (픽셀 단위 무손실, 압축 데이터도 그대로). 알파 채널·인터레이스 등은 Pillow로 풀어서
             색 데이터(Flate)와 알파(SMask)로 나눠 넣는다 (픽셀 단위 무손실).
    - 그 외 : Pillow로 디코딩해 Flate로 넣는다.
* EXIF 회전 정보는 픽셀을 건드리지 않고 페이지 변환 행렬(cm)로 반영한다.
* 카탈로그(문서 루트)에 /JPDF 사전을 두어 원본 파일명·형식·해시 등을 기록하고,
  파일 3번째 줄에 `%JPDF-1.0` 주석을 두어 빠르게 식별한다.
* 옵션으로 원본 파일을 PDF 첨부파일(EmbeddedFiles)로도 넣을 수 있다 (용량 2배, 완전한 원본 보존).

"이미지로 열기"는 위 구조를 역으로 읽어 JPEG은 바이트 그대로, PNG는 PNG 컨테이너를 다시 씌워
(픽셀 단위 동일) 꺼낸다. 일반 PDF 파일도 pypdf로 이미지를 추출할 수 있다.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import io
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable, Optional, Sequence

from PIL import Image

Image.MAX_IMAGE_PIXELS = None  # 사용자의 자기 파일을 다루는 로컬 도구이므로 크기 제한 해제

__all__ = [
    "JPDF_VERSION", "JpdfError", "LayoutOptions", "PAGE_SIZES_PT",
    "analyze_image", "convert_images", "ConvertReport", "ImageReport",
    "inspect_pdf", "PdfInfo", "is_jpdf_file", "extract_images", "ExtractedImage",
    "first_page_thumbnail", "open_as_pdf", "open_as_images", "open_with_default_app",
    "cache_dir", "clear_cache", "SUPPORTED_INPUT_EXTENSIONS", "apply_orientation", "human_size",
    "reveal_in_folder",
]

JPDF_VERSION = "1.0"
PRODUCER = f"JPDF {JPDF_VERSION} (jpdf_core.py)"
JPDF_SIGNATURE = b"%JPDF-"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MM_TO_PT = 72.0 / 25.4
MAX_PAGE_PT = 14400.0  # PDF 뷰어 호환 한계 (200 inch)

SUPPORTED_INPUT_EXTENSIONS = (
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp",
)

# 세로 기준 (width, height) pt
PAGE_SIZES_PT: dict[str, tuple[float, float]] = {
    "A3": (841.89, 1190.55),
    "A4": (595.276, 841.89),
    "A5": (419.528, 595.276),
    "B5": (498.898, 708.661),
    "Letter": (612.0, 792.0),
    "Legal": (612.0, 1008.0),
}

ProgressFn = Optional[Callable[[int, int, str], None]]


class JpdfError(Exception):
    """사용자에게 그대로 보여줄 수 있는 오류 메시지를 담는다."""


# ---------------------------------------------------------------------------
# 옵션
# ---------------------------------------------------------------------------
@dataclass
class LayoutOptions:
    page_size: str = "image"        # "image" (이미지 크기 그대로) 또는 PAGE_SIZES_PT의 키
    orientation: str = "auto"       # "auto" | "portrait" | "landscape"  (고정 페이지 크기일 때만 의미)
    margin_mm: float = 0.0          # 여백 (mm)
    default_dpi: float = 96.0       # 이미지에 DPI 정보가 없을 때 쓰는 값 (이미지 크기 모드)
    use_image_dpi: bool = True      # 이미지의 DPI 메타데이터 사용 여부
    fit_upscale: bool = True        # 고정 페이지: 작은 이미지도 페이지에 맞게 확대
    embed_originals: bool = False   # 원본 파일을 PDF 첨부파일로도 포함 (용량 ≈ 2배)
    png_compress_level: int = 6     # Pillow 재인코딩 시 zlib 압축 레벨 (0~9)

    def validate(self) -> None:
        if self.page_size != "image" and self.page_size not in PAGE_SIZES_PT:
            raise JpdfError(f"알 수 없는 페이지 크기: {self.page_size}")
        if self.orientation not in ("auto", "portrait", "landscape"):
            raise JpdfError(f"알 수 없는 방향: {self.orientation}")
        if self.margin_mm < 0:
            raise JpdfError("여백은 0 이상이어야 합니다.")
        if not (10 <= self.default_dpi <= 2400):
            raise JpdfError("기본 DPI는 10~2400 사이여야 합니다.")
        if self.page_size != "image":
            w, h = PAGE_SIZES_PT[self.page_size]
            if 2 * self.margin_mm * MM_TO_PT >= min(w, h):
                raise JpdfError("여백이 너무 커서 이미지를 넣을 공간이 없습니다.")


# ---------------------------------------------------------------------------
# PDF 문법 도우미
# ---------------------------------------------------------------------------
def _num(x: float) -> str:
    """PDF 숫자 표기: 정수는 정수로, 실수는 소수 4자리까지 (불필요한 0 제거)."""
    if isinstance(x, int) or float(x).is_integer():
        return str(int(x))
    s = f"{x:.4f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-", "-0") else "0"


def _text_string(s: str) -> bytes:
    """PDF 텍스트 문자열. ASCII면 (…) 리터럴, 아니면 UTF‑16BE(BOM) 16진 문자열."""
    if all(32 <= ord(c) < 127 for c in s):
        esc = s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        return b"(" + esc.encode("ascii") + b")"
    return b"<FEFF" + s.encode("utf-16-be").hex().upper().encode("ascii") + b">"


def _pdf_date(t: Optional[float] = None) -> bytes:
    d = _dt.datetime.fromtimestamp(t if t is not None else time.time()).astimezone()
    off = d.utcoffset() or _dt.timedelta(0)
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"(D:{d:%Y%m%d%H%M%S}{sign}{total // 3600:02d}'{(total % 3600) // 60:02d}')".encode("ascii")


def _name(s: str) -> bytes:
    """PDF 이름 객체(/…). 특수문자는 #xx 로 이스케이프."""
    out = []
    for ch in s.encode("utf-8"):
        if 33 <= ch <= 126 and chr(ch) not in "#/()<>[]{}%":
            out.append(chr(ch))
        else:
            out.append(f"#{ch:02X}")
    return ("/" + "".join(out)).encode("ascii")


# ---------------------------------------------------------------------------
# 이미지 분석 (헤더만 읽는다)
# ---------------------------------------------------------------------------
@dataclass
class ImageAnalysis:
    path: Path
    format: str                 # "JPEG" | "PNG" | Pillow의 format 이름
    width: int
    height: int
    mode: str                   # Pillow 모드 (예: RGB, RGBA, L, P, CMYK)
    plan: str                   # "jpeg-verbatim" | "jpeg-decode" | "png-direct" | "png-reencode" | "other"
    lossless: bool              # 픽셀(또는 바이트) 단위로 원본이 보존되는가
    note: str                   # 사용자용 설명
    orientation: int = 1        # EXIF 방향 (1~8)
    dpi: Optional[tuple[float, float]] = None
    file_size: int = 0
    has_alpha: bool = False

    @property
    def display_size(self) -> tuple[int, int]:
        """EXIF 회전을 반영한 표시 크기."""
        if self.orientation in (5, 6, 7, 8):
            return self.height, self.width
        return self.width, self.height


def _valid_dpi(dpi) -> Optional[tuple[float, float]]:
    try:
        x, y = float(dpi[0]), float(dpi[1])
    except Exception:
        return None
    if not (10 <= x <= 2400 and 10 <= y <= 2400):
        return None
    return (round(x, 3), round(y, 3))


_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _parse_jpeg_header(data: bytes) -> dict:
    """JPEG 마커를 훑어 SOF 정보와 Adobe APP14 유무를 얻는다 (엔트로피 데이터는 읽지 않음)."""
    if data[:2] != b"\xFF\xD8":
        raise JpdfError("JPEG 파일이 아닙니다 (SOI 마커 없음).")
    info = {"adobe": False, "adobe_transform": None, "sof": None}
    i, n = 2, len(data)
    while i < n:
        if data[i] != 0xFF:
            raise JpdfError("손상된 JPEG입니다 (마커 정렬 오류).")
        while i < n and data[i] == 0xFF:
            i += 1
        if i >= n:
            break
        marker = data[i]
        i += 1
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            continue  # 길이 없는 마커
        if marker == 0xD9:
            break
        if i + 2 > n:
            break
        (seglen,) = struct.unpack(">H", data[i:i + 2])
        seg = data[i + 2:i + seglen]
        if marker == 0xEE and seg[:5] == b"Adobe":
            info["adobe"] = True
            info["adobe_transform"] = seg[11] if len(seg) > 11 else None
        if marker in _SOF_MARKERS:
            precision, h, w, ncomp = struct.unpack(">BHHB", seg[:6])
            info["sof"] = marker
            info["precision"] = precision
            info["width"], info["height"], info["components"] = w, h, ncomp
            info["progressive"] = marker in (0xC2, 0xC6, 0xCA, 0xCE)
            info["arithmetic"] = marker >= 0xC9
            info["lossless"] = marker in (0xC3, 0xC7, 0xCB, 0xCF)
            info["hierarchical"] = marker in (0xC5, 0xC6, 0xC7, 0xCD, 0xCE, 0xCF)
            break
        if marker == 0xDA:  # SOS 전에 SOF가 있어야 정상
            break
        i += seglen
    return info


def _parse_png_chunks(data: bytes) -> dict:
    if data[:8] != PNG_SIGNATURE:
        raise JpdfError("PNG 파일이 아닙니다.")
    info: dict = {"idat": [], "plte": None, "trns": None, "dpi": None, "ihdr": None}
    pos = 8
    while pos + 8 <= len(data):
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            w, h, bd, ct, comp, filt, inter = struct.unpack(">IIBBBBB", body[:13])
            info["ihdr"] = dict(width=w, height=h, bit_depth=bd, color_type=ct,
                                compression=comp, filter=filt, interlace=inter)
        elif ctype == b"PLTE":
            info["plte"] = body
        elif ctype == b"tRNS":
            info["trns"] = body
        elif ctype == b"pHYs":
            x, y, unit = struct.unpack(">IIB", body[:9])
            if unit == 1:
                info["dpi"] = (x * 0.0254, y * 0.0254)
        elif ctype == b"IDAT":
            info["idat"].append(body)
        elif ctype == b"IEND":
            break
    if info["ihdr"] is None:
        raise JpdfError("손상된 PNG입니다 (IHDR 없음).")
    info["idat"] = b"".join(info["idat"])
    return info


def _png_direct_ok(ihdr: dict, trns: Optional[bytes]) -> bool:
    """원본 IDAT를 그대로 PDF에 넣을 수 있는 조건."""
    if ihdr["interlace"] != 0 or ihdr["compression"] != 0 or ihdr["filter"] != 0:
        return False
    bd, ct = ihdr["bit_depth"], ihdr["color_type"]
    if ct == 0:   # 회색조
        return bd in (1, 2, 4, 8, 16)
    if ct == 2:   # RGB
        return bd in (8, 16)
    if ct == 3:   # 팔레트 (tRNS가 있으면 알파가 필요 → 재인코딩)
        return bd in (1, 2, 4, 8) and trns is None
    return False  # 4(회색+알파), 6(RGBA)


def analyze_image(path: os.PathLike | str) -> ImageAnalysis:
    """파일 헤더만 읽어 어떤 방식으로 PDF에 넣을지 결정한다 (픽셀 디코딩 없음)."""
    p = Path(path)
    if not p.is_file():
        raise JpdfError(f"파일을 찾을 수 없습니다: {p}")
    size = p.stat().st_size
    try:
        im = Image.open(p)
    except Exception as e:  # noqa: BLE001
        raise JpdfError(f"이미지를 열 수 없습니다: {p.name} ({e})") from e
    with im:
        fmt = im.format or "?"
        w, h = im.size
        mode = im.mode
        try:
            orientation = int(im.getexif().get(0x0112, 1) or 1)
        except Exception:  # noqa: BLE001
            orientation = 1
        if orientation not in range(1, 9):
            orientation = 1
        dpi = _valid_dpi(im.info.get("dpi"))
        has_alpha = mode in ("RGBA", "LA", "PA") or "transparency" in im.info

        if fmt == "JPEG":
            with open(p, "rb") as f:
                head = f.read(min(size, 4 * 1024 * 1024))
            j = _parse_jpeg_header(head)
            if j["sof"] is None:
                return ImageAnalysis(p, fmt, w, h, mode, "jpeg-decode", True,
                                     "JPEG 구조가 특이해 픽셀로 풀어 저장", orientation, dpi, size)
            if j.get("precision") == 8 and not j.get("arithmetic") and not j.get("lossless") \
                    and not j.get("hierarchical") and j["components"] in (1, 3, 4) \
                    and j["height"] > 0:
                note = "JPEG 원본 바이트 그대로 (무손실)"
                if j.get("progressive"):
                    note = "프로그레시브 JPEG 원본 그대로 (무손실)"
                if j["components"] == 4:
                    note = "CMYK JPEG 원본 그대로 (Adobe 반전 Decode 적용)"
                if orientation != 1:
                    note += f", EXIF 회전 {orientation} 반영"
                return ImageAnalysis(p, fmt, w, h, mode, "jpeg-verbatim", True, note,
                                     orientation, dpi, size)
            return ImageAnalysis(p, fmt, w, h, mode, "jpeg-decode", True,
                                 "PDF가 지원하지 않는 JPEG(산술부호/12비트 등) → 픽셀로 풀어 저장",
                                 orientation, dpi, size)

        if fmt == "PNG":
            with open(p, "rb") as f:
                raw = f.read()
            png = _parse_png_chunks(raw)
            ih = png["ihdr"]
            if ih["width"] == w and ih["height"] == h and _png_direct_ok(ih, png["trns"]):
                bd, ct = ih["bit_depth"], ih["color_type"]
                kind = {0: "회색조", 2: "RGB", 3: "팔레트"}[ct]
                note = f"PNG 압축 데이터 그대로 ({kind} {bd}비트, 무손실)"
                if png["trns"] is not None:
                    note += ", 투명색은 색 키 마스크로"
                return ImageAnalysis(p, fmt, w, h, mode, "png-direct", True, note,
                                     1, _valid_dpi(png["dpi"]) or dpi, size, has_alpha)
            reasons = []
            if ih["interlace"]:
                reasons.append("인터레이스")
            if ih["color_type"] in (4, 6):
                reasons.append("알파 채널 분리(SMask)")
            elif ih["color_type"] == 3 and png["trns"] is not None:
                reasons.append("팔레트 투명도 → 알파(SMask)")
            lossless = not (ih["bit_depth"] == 16 and ih["color_type"] in (4, 6))
            note = "PNG 재압축: " + ", ".join(reasons or ["형식 변환"]) + (
                " (픽셀 무손실)" if lossless else " (16비트 알파 → 8비트로 축소)")
            return ImageAnalysis(p, fmt, w, h, mode, "png-reencode", lossless, note,
                                 1, _valid_dpi(png["dpi"]) or dpi, size, has_alpha)

        lossless = mode not in ("CMYK", "YCbCr", "LAB", "HSV", "I", "F")
        note = f"{fmt} → 무손실 압축(Flate)으로 저장" if lossless else f"{fmt} → RGB로 변환해 저장"
        if fmt == "GIF":
            note = "GIF 첫 프레임 → 무손실 압축(Flate)으로 저장"
        return ImageAnalysis(p, fmt, w, h, mode, "other", lossless, note, orientation, dpi, size, has_alpha)


# ---------------------------------------------------------------------------
# PDF 이미지 XObject 만들기
# ---------------------------------------------------------------------------
@dataclass
class PdfImage:
    width: int
    height: int
    colorspace: bytes             # b"/DeviceRGB" 등, 또는 b"[/Indexed /DeviceRGB 255 <…>]"
    bpc: int
    filter: bytes                 # b"/DCTDecode" | b"/FlateDecode"
    data: bytes
    decode_parms: Optional[bytes] = None
    decode_array: Optional[bytes] = None
    color_key_mask: Optional[bytes] = None
    smask: Optional["PdfImage"] = None
    lossless: bool = True
    orientation: int = 1
    dpi: Optional[tuple[float, float]] = None
    note: str = ""

    def dict_entries(self, smask_ref: Optional[int] = None) -> bytes:
        parts = [b"/Type /XObject /Subtype /Image",
                 b"/Width " + str(self.width).encode(), b"/Height " + str(self.height).encode(),
                 b"/ColorSpace " + self.colorspace,
                 b"/BitsPerComponent " + str(self.bpc).encode(),
                 b"/Filter " + self.filter]
        if self.decode_parms:
            parts.append(b"/DecodeParms " + self.decode_parms)
        if self.decode_array:
            parts.append(b"/Decode " + self.decode_array)
        if self.color_key_mask:
            parts.append(b"/Mask " + self.color_key_mask)
        if smask_ref is not None:
            parts.append(b"/SMask %d 0 R" % smask_ref)
        return b" ".join(parts)


def _png_parts_to_pdfimage(ihdr: dict, idat: bytes, plte: Optional[bytes], trns: Optional[bytes],
                           dpi=None, lossless=True, note="") -> PdfImage:
    """직접 삽입 가능한 PNG 조각(IDAT 원본)을 PDF 이미지로 포장한다."""
    w, h, bd, ct = ihdr["width"], ihdr["height"], ihdr["bit_depth"], ihdr["color_type"]
    colors = 3 if ct == 2 else 1
    if ct == 0:
        cs = b"/DeviceGray"
    elif ct == 2:
        cs = b"/DeviceRGB"
    elif ct == 3:
        if not plte:
            raise JpdfError("팔레트 PNG인데 PLTE 청크가 없습니다.")
        hival = len(plte) // 3 - 1
        cs = b"[/Indexed /DeviceRGB " + str(hival).encode() + b" <" + plte.hex().upper().encode() + b">]"
    else:
        raise JpdfError("직접 삽입할 수 없는 PNG 색 형식입니다.")
    parms = (b"<< /Predictor 15 /Colors %d /BitsPerComponent %d /Columns %d >>" % (colors, bd, w))
    mask = None
    if trns is not None and ct in (0, 2):
        vals = struct.unpack(">%dH" % (len(trns) // 2), trns[:len(trns) // 2 * 2])
        mask = b"[" + b" ".join(b"%d %d" % (v, v) for v in vals) + b"]"
    return PdfImage(w, h, cs, bd, b"/FlateDecode", idat, parms, None, mask, None,
                    lossless, 1, dpi, note)


def _pil_to_png_bytes(im: Image.Image, level: int) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="PNG", compress_level=level)
    return buf.getvalue()


def _pil_to_pdfimage(im: Image.Image, level: int, note: str, lossless: bool = True,
                     dpi=None, orientation: int = 1) -> PdfImage:
    """Pillow 이미지를 (색 데이터 Flate + 필요시 SMask)로 만든다. 픽셀 단위 무손실."""
    im.load()
    mode = im.mode
    alpha: Optional[Image.Image] = None
    base: Image.Image

    if mode == "LA":
        base, alpha = im.getchannel("L"), im.getchannel("A")
    elif mode in ("RGBA", "PA") or (mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        base, alpha = rgba.convert("RGB"), rgba.getchannel("A")
    elif mode in ("1", "L", "P", "RGB"):
        base = im                      # RGB/L + transparency(색 키)는 Pillow가 tRNS로 써 준다
    elif mode in ("I;16", "I;16B", "I;16L", "I;16N"):
        base = im.convert("I;16")
    elif mode == "I":
        base = im.convert("I;16")
        lossless = False
    else:                              # CMYK, YCbCr, LAB, HSV, F ...
        base = im.convert("RGB")
        lossless = False

    png = _parse_png_chunks(_pil_to_png_bytes(base, level))
    if not _png_direct_ok(png["ihdr"], png["trns"]):
        # 예외적 상황(예: 팔레트+tRNS)이면 RGB(A)로 강제 변환
        rgba = base.convert("RGBA")
        base, alpha = rgba.convert("RGB"), (alpha or rgba.getchannel("A"))
        png = _parse_png_chunks(_pil_to_png_bytes(base, level))
    img = _png_parts_to_pdfimage(png["ihdr"], png["idat"], png["plte"], png["trns"],
                                 dpi, lossless, note)
    img.orientation = orientation
    if alpha is not None:
        if alpha.mode != "L":
            alpha = alpha.convert("L")
        apng = _parse_png_chunks(_pil_to_png_bytes(alpha, level))
        img.smask = _png_parts_to_pdfimage(apng["ihdr"], apng["idat"], None, None)
    return img


def build_pdf_image(an: ImageAnalysis, level: int = 6) -> PdfImage:
    """분석 결과에 따라 실제 데이터를 읽어 PDF 이미지 객체를 만든다."""
    p = an.path
    if an.plan == "jpeg-verbatim":
        data = p.read_bytes()
        j = _parse_jpeg_header(data)
        n = j["components"]
        cs = {1: b"/DeviceGray", 3: b"/DeviceRGB", 4: b"/DeviceCMYK"}[n]
        decode = b"[1 0 1 0 1 0 1 0]" if n == 4 else None  # Adobe CMYK JPEG은 반전 저장됨
        return PdfImage(j["width"], j["height"], cs, 8, b"/DCTDecode", data, None, decode,
                        None, None, True, an.orientation, an.dpi, an.note)

    if an.plan == "png-direct":
        png = _parse_png_chunks(p.read_bytes())
        return _png_parts_to_pdfimage(png["ihdr"], png["idat"], png["plte"], png["trns"],
                                      an.dpi, True, an.note)

    # 나머지: Pillow로 디코딩
    try:
        with Image.open(p) as im:
            if an.format == "GIF":
                im.seek(0)
            im.load()
            im2 = im.copy()
    except Exception as e:  # noqa: BLE001
        raise JpdfError(f"이미지를 디코딩할 수 없습니다: {p.name} ({e})") from e
    lossless = an.lossless
    return _pil_to_pdfimage(im2, level, an.note, lossless, an.dpi, an.orientation)


# ---------------------------------------------------------------------------
# 페이지 배치
# ---------------------------------------------------------------------------
def _layout_page(img: PdfImage, opt: LayoutOptions):
    """(page_w, page_h, x, y, dw, dh) — 모두 pt. (x,y)는 표시 사각형의 왼쪽 아래."""
    rotated = img.orientation in (5, 6, 7, 8)
    iw, ih = (img.height, img.width) if rotated else (img.width, img.height)
    dpi = img.dpi if (opt.use_image_dpi and img.dpi) else (opt.default_dpi, opt.default_dpi)
    dpi_x, dpi_y = (dpi[1], dpi[0]) if rotated else dpi
    margin = opt.margin_mm * MM_TO_PT
    natural_w, natural_h = iw * 72.0 / dpi_x, ih * 72.0 / dpi_y

    if opt.page_size == "image":
        dw, dh = natural_w, natural_h
        limit = MAX_PAGE_PT - 2 * margin
        if limit <= 0:
            raise JpdfError("여백이 너무 큽니다.")
        s = min(1.0, limit / dw, limit / dh)
        dw, dh = dw * s, dh * s
        pw, ph = max(3.0, dw + 2 * margin), max(3.0, dh + 2 * margin)  # PDF 최소 페이지 3pt
        return pw, ph, (pw - dw) / 2, (ph - dh) / 2, dw, dh

    pw, ph = PAGE_SIZES_PT[opt.page_size]
    if opt.orientation == "landscape" or (opt.orientation == "auto" and iw > ih):
        pw, ph = ph, pw
    avail_w, avail_h = pw - 2 * margin, ph - 2 * margin
    if avail_w <= 0 or avail_h <= 0:
        raise JpdfError("여백이 너무 커서 이미지를 넣을 공간이 없습니다.")
    scale = min(avail_w / iw, avail_h / ih)
    if not opt.fit_upscale:
        scale = min(scale, min(natural_w / iw, natural_h / ih))
    dw, dh = iw * scale, ih * scale
    return pw, ph, (pw - dw) / 2, (ph - dh) / 2, dw, dh


def _ctm(orientation: int, x: float, y: float, dw: float, dh: float) -> str:
    """EXIF 방향에 맞춰 단위 정사각형(이미지 공간)을 표시 사각형으로 보내는 행렬 [a b c d e f]."""
    o = orientation
    if o == 2:    # 좌우 반전
        m = (-dw, 0, 0, dh, x + dw, y)
    elif o == 3:  # 180°
        m = (-dw, 0, 0, -dh, x + dw, y + dh)
    elif o == 4:  # 상하 반전
        m = (dw, 0, 0, -dh, x, y + dh)
    elif o == 5:  # 전치 (좌우 반전 + 270° CW)
        m = (0, -dh, -dw, 0, x + dw, y + dh)
    elif o == 6:  # 90° CW
        m = (0, -dh, dw, 0, x, y + dh)
    elif o == 7:  # 반전치 (좌우 반전 + 90° CW)
        m = (0, dh, dw, 0, x, y)
    elif o == 8:  # 90° CCW
        m = (0, dh, -dw, 0, x + dw, y)
    else:
        m = (dw, 0, 0, dh, x, y)
    return " ".join(_num(v) for v in m)


# ---------------------------------------------------------------------------
# PDF 작성기 (객체를 파일에 바로바로 쓴다 → 메모리에 이미지 하나씩만)
# ---------------------------------------------------------------------------
class _PdfWriter:
    def __init__(self, fp: BinaryIO, jpdf: bool):
        self.fp = fp
        self.offsets: dict[int, int] = {}
        self.next_num = 1
        header = b"%PDF-1.7\n%\xE2\xE3\xCF\xD3\n"
        if jpdf:
            header += JPDF_SIGNATURE + JPDF_VERSION.encode() + b"\n"
        fp.write(header)

    def reserve(self) -> int:
        n = self.next_num
        self.next_num += 1
        return n

    def write_object(self, num: int, body: bytes) -> None:
        self.offsets[num] = self.fp.tell()
        self.fp.write(b"%d 0 obj\n" % num + body + b"\nendobj\n")

    def add_object(self, body: bytes) -> int:
        n = self.reserve()
        self.write_object(n, body)
        return n

    def write_stream(self, num: int, entries: bytes, data: bytes) -> None:
        body = b"<< " + entries + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"
        self.write_object(num, body)

    def add_stream(self, entries: bytes, data: bytes) -> int:
        n = self.reserve()
        self.write_stream(n, entries, data)
        return n

    def finish(self, root: int, info: int, file_id: bytes) -> None:
        size = self.next_num
        for n in range(1, size):
            if n not in self.offsets:
                raise JpdfError(f"내부 오류: 객체 {n}이 작성되지 않았습니다.")
        xref_pos = self.fp.tell()
        lines = [b"xref\n", b"0 %d\n" % size, b"0000000000 65535 f \n"]
        for n in range(1, size):
            lines.append(b"%010d 00000 n \n" % self.offsets[n])
        self.fp.write(b"".join(lines))
        hexid = b"<" + file_id.hex().upper().encode() + b">"
        self.fp.write(b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R /ID [%s %s] >>\n"
                      % (size, root, info, hexid, hexid))
        self.fp.write(b"startxref\n%d\n%%%%EOF\n" % xref_pos)


# ---------------------------------------------------------------------------
# 변환 (이미지들 → PDF/JPDF)
# ---------------------------------------------------------------------------
@dataclass
class ImageReport:
    source: Path
    page: int
    width: int
    height: int
    format: str
    plan: str
    lossless: bool
    note: str
    bytes_in: int
    bytes_stored: int
    page_size_pt: tuple[float, float]


@dataclass
class ConvertReport:
    output: Path
    is_jpdf: bool
    pages: int
    bytes_out: int
    elapsed: float
    images: list[ImageReport] = field(default_factory=list)

    @property
    def all_lossless(self) -> bool:
        return all(r.lossless for r in self.images)


def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip() or "image"
    return name[:120]


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_ORIENTATION_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180, 4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE, 6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


def apply_orientation(im: Image.Image, orientation: int) -> Image.Image:
    """EXIF 방향값(1~8)대로 픽셀을 실제로 돌린 사본을 돌려준다 (미리보기용)."""
    t = _ORIENTATION_TRANSPOSE.get(orientation)
    return im.transpose(t) if t is not None else im


def convert_images(sources: Sequence[os.PathLike | str], output: os.PathLike | str,
                   options: Optional[LayoutOptions] = None, *, jpdf: Optional[bool] = None,
                   progress: ProgressFn = None, title: Optional[str] = None) -> ConvertReport:
    """이미지 파일들을 하나의 PDF(.pdf) 또는 JPDF(.jpdf)로 만든다.

    jpdf=None 이면 출력 확장자가 .jpdf 인지로 결정한다. JPDF 메타데이터는 일반 PDF 뷰어가 무시하므로
    .pdf 로 저장할 때도 항상 기록한다(파일 3번째 줄 서명은 .jpdf일 때만).
    """
    opt = options or LayoutOptions()
    opt.validate()
    out = Path(output)
    if jpdf is None:
        jpdf = out.suffix.lower() == ".jpdf"
    srcs = [Path(s) for s in sources]
    if not srcs:
        raise JpdfError("변환할 이미지가 없습니다.")
    for s in srcs:
        if not s.is_file():
            raise JpdfError(f"파일을 찾을 수 없습니다: {s}")
    total = len(srcs)
    t0 = time.time()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")

    reports: list[ImageReport] = []
    try:
        with open(tmp, "wb") as fp:
            w = _PdfWriter(fp, jpdf)
            catalog_num, info_num, pages_num = w.reserve(), w.reserve(), w.reserve()
            page_refs: list[int] = []
            meta_entries: list[bytes] = []
            attach_names: list[tuple[str, int]] = []
            id_hash = hashlib.md5(str(out.resolve()).encode("utf-8", "replace"))

            for idx, src in enumerate(srcs, 1):
                if progress:
                    progress(idx - 1, total, f"{src.name} 분석 중…")
                an = analyze_image(src)
                img = build_pdf_image(an, opt.png_compress_level)
                if progress:
                    progress(idx - 1, total, f"{src.name} 쓰는 중…")

                smask_ref = None
                if img.smask is not None:
                    smask_ref = w.add_stream(img.smask.dict_entries(), img.smask.data)
                xobj_ref = w.add_stream(img.dict_entries(smask_ref), img.data)

                pw, ph, x, y, dw, dh = _layout_page(img, opt)
                content = f"q {_ctm(img.orientation, x, y, dw, dh)} cm /Im0 Do Q".encode("ascii")
                content_ref = w.add_stream(b"", content)
                page_body = (b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %s %s] "
                             b"/Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>"
                             % (pages_num, _num(pw).encode(), _num(ph).encode(), xobj_ref, content_ref))
                page_refs.append(w.add_object(page_body))

                sha = _sha256_file(src)
                id_hash.update(sha.encode())
                raw = src.read_bytes() if opt.embed_originals else None

                attach_name = None
                if opt.embed_originals and raw is not None:
                    attach_name = f"{idx:03d}_{_safe_filename(src.name)}"
                    subtype = {"JPEG": "image/jpeg", "PNG": "image/png"}.get(an.format, "application/octet-stream")
                    ef = w.add_stream(
                        b"/Type /EmbeddedFile /Subtype " + _name(subtype) +
                        b" /Params << /Size %d /ModDate %s /CheckSum <%s> >>"
                        % (len(raw), _pdf_date(src.stat().st_mtime), hashlib.md5(raw).hexdigest().upper().encode()),
                        raw)
                    fs = w.add_object(
                        b"<< /Type /Filespec /F " + _text_string(attach_name) + b" /UF " + _text_string(attach_name) +
                        b" /EF << /F %d 0 R /UF %d 0 R >> /Desc " % (ef, ef) + _text_string("JPDF 원본 이미지") +
                        b" /AFRelationship /Source >>")
                    attach_names.append((attach_name, fs))

                fmt_name = {"JPEG": "/JPEG", "PNG": "/PNG"}.get(an.format, _name(an.format).decode())
                entry = (b"<< /Page %d /Name " % idx + _text_string(src.name) +
                         b" /Format " + fmt_name.encode() +
                         b" /Width %d /Height %d /XObject %d 0 R /Lossless %s /Orientation %d"
                         % (an.width, an.height, xobj_ref, b"true" if img.lossless else b"false", img.orientation) +
                         b" /Plan " + _name(an.plan) + b" /Size %d" % an.file_size +
                         (b" /SHA256 (" + sha.encode() + b")" if sha else b"") +
                         (b" /Attachment " + _text_string(attach_name) if attach_name else b"") +
                         b" >>")
                meta_entries.append(entry)
                reports.append(ImageReport(src, idx, an.width, an.height, an.format, an.plan, img.lossless,
                                           img.note, an.file_size, len(img.data) + (len(img.smask.data) if img.smask else 0),
                                           (pw, ph)))
                del img, raw
                if progress:
                    progress(idx, total, f"{src.name} 완료")

            # Pages / 메타 / 카탈로그 / Info
            kids = b" ".join(b"%d 0 R" % r for r in page_refs)
            w.write_object(pages_num, b"<< /Type /Pages /Count %d /Kids [%s] >>" % (len(page_refs), kids))
            jpdf_dict = (b"<< /Version " + _text_string(JPDF_VERSION) + b" /Producer " + _text_string(PRODUCER) +
                         b" /Created " + _pdf_date() + b" /Images [" + b" ".join(meta_entries) + b"] >>")
            catalog = b"<< /Type /Catalog /Pages %d 0 R /JPDF " % pages_num + jpdf_dict
            if attach_names:
                names = b" ".join(_text_string(n) + b" %d 0 R" % r for n, r in sorted(attach_names))
                catalog += b" /Names << /EmbeddedFiles << /Names [" + names + b"] >> >>"
                catalog += b" /AF [" + b" ".join(b"%d 0 R" % r for _, r in attach_names) + b"]"
            catalog += b" >>"
            w.write_object(catalog_num, catalog)
            info = (b"<< /Producer " + _text_string(PRODUCER) + b" /Creator " + _text_string("JPDF") +
                    b" /CreationDate " + _pdf_date() + b" /ModDate " + _pdf_date() +
                    (b" /Title " + _text_string(title) if title else b"") +
                    b" /Keywords " + _text_string("jpdf, images") + b" >>")
            w.write_object(info_num, info)
            w.finish(catalog_num, info_num, id_hash.digest())
        os.replace(tmp, out)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    if progress:
        progress(total, total, "완료")
    return ConvertReport(out, jpdf, len(srcs), out.stat().st_size, time.time() - t0, reports)


# ---------------------------------------------------------------------------
# 읽기 (PDF/JPDF 정보, 이미지 추출)
# ---------------------------------------------------------------------------
def is_jpdf_file(path: os.PathLike | str) -> bool:
    """3번째 줄 서명 또는 카탈로그의 /JPDF 사전으로 판단."""
    p = Path(path)
    try:
        with open(p, "rb") as f:
            head = f.read(1024)
    except OSError:
        return False
    if not head.startswith(b"%PDF-"):
        return False
    if JPDF_SIGNATURE in head:
        return True
    try:
        from pypdf import PdfReader
        return "/JPDF" in PdfReader(str(p)).trailer["/Root"]
    except Exception:  # noqa: BLE001
        return False


@dataclass
class PdfInfo:
    path: Path
    file_size: int
    pages: int
    is_pdf: bool
    is_jpdf: bool
    jpdf_version: str = ""
    images: list[dict] = field(default_factory=list)   # /JPDF /Images 항목들 (dict)
    has_attachments: bool = False
    title: str = ""

    def summary(self) -> str:
        kind = f"JPDF {self.jpdf_version}" if self.is_jpdf else "PDF"
        parts = [kind, f"{self.pages}페이지"]
        if self.images:
            fmts = sorted({i.get("format", "?") for i in self.images})
            parts.append(f"이미지 {len(self.images)}장 ({', '.join(fmts)})")
        parts.append(human_size(self.file_size))
        return " · ".join(parts)


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _pdf_text(v) -> str:
    try:
        return str(v)
    except Exception:  # noqa: BLE001
        return ""


def _pdf_bool(v) -> bool:
    if hasattr(v, "value"):
        return bool(v.value)
    return bool(v)


def _read_jpdf_meta(reader) -> tuple[Optional[dict], list[dict]]:
    root = reader.trailer["/Root"]
    jp = root.get("/JPDF")
    if jp is None:
        return None, []
    jp = jp.get_object()
    images = []
    for e in jp.get("/Images", []):
        e = e.get_object()
        d = {
            "page": int(e.get("/Page", 0)),
            "name": _pdf_text(e.get("/Name", "")),
            "format": _pdf_text(e.get("/Format", "")).lstrip("/"),
            "width": int(e.get("/Width", 0)),
            "height": int(e.get("/Height", 0)),
            "lossless": _pdf_bool(e.get("/Lossless", True)),
            "orientation": int(e.get("/Orientation", 1)),
            "plan": _pdf_text(e.get("/Plan", "")).lstrip("/"),
            "size": int(e.get("/Size", 0)),
            "sha256": _pdf_text(e.get("/SHA256", "")),
            "attachment": _pdf_text(e.get("/Attachment", "")),
        }
        images.append(d)
    return {"version": _pdf_text(jp.get("/Version", "")), "producer": _pdf_text(jp.get("/Producer", ""))}, images


def inspect_pdf(path: os.PathLike | str) -> PdfInfo:
    p = Path(path)
    size = p.stat().st_size
    with open(p, "rb") as f:
        head = f.read(1024)
    if not head.startswith(b"%PDF-"):
        return PdfInfo(p, size, 0, False, False)
    from pypdf import PdfReader
    try:
        reader = PdfReader(str(p))
        pages = len(reader.pages)
        meta, images = _read_jpdf_meta(reader)
        title = ""
        try:
            if reader.metadata and reader.metadata.title:
                title = str(reader.metadata.title)
        except Exception:  # noqa: BLE001
            pass
        has_att = False
        try:
            has_att = bool(reader._list_attachments()) if hasattr(reader, "_list_attachments") else bool(reader.attachments)
        except Exception:  # noqa: BLE001
            has_att = False
        return PdfInfo(p, size, pages, True, meta is not None, (meta or {}).get("version", ""),
                       images, has_att, title)
    except Exception as e:  # noqa: BLE001
        raise JpdfError(f"PDF를 읽을 수 없습니다: {p.name} ({e})") from e


# --- PNG 재조립 ---------------------------------------------------------------
def _png_chunk(ctype: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF)


def _png_from_parts(width: int, height: int, bit_depth: int, color_type: int, zdata: bytes,
                    plte: Optional[bytes] = None, trns: Optional[bytes] = None) -> bytes:
    out = [PNG_SIGNATURE,
           _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, 0))]
    if plte:
        out.append(_png_chunk(b"PLTE", plte))
    if trns:
        out.append(_png_chunk(b"tRNS", trns))
    out.append(_png_chunk(b"IDAT", zdata))
    out.append(_png_chunk(b"IEND", b""))
    return b"".join(out)


def _as_list(v) -> list:
    if v is None:
        return []
    v = v.get_object() if hasattr(v, "get_object") else v
    if isinstance(v, (list, tuple)):
        return [x.get_object() if hasattr(x, "get_object") else x for x in v]
    return [v]


def _raw_stream_bytes(xobj) -> Optional[bytes]:
    data = getattr(xobj, "_data", None)
    return bytes(data) if data is not None else None


def _flate_xobject_to_png(xobj) -> Optional[bytes]:
    """우리 형식(Flate + PNG 예측자)의 XObject를 PNG 파일 바이트로 되돌린다. 불가하면 None."""
    filters = [str(f) for f in _as_list(xobj.get("/Filter"))]
    if filters != ["/FlateDecode"]:
        return None
    parms_list = _as_list(xobj.get("/DecodeParms"))
    parms = parms_list[0] if parms_list else None
    if parms is None or int(parms.get("/Predictor", 1)) < 10:
        return None
    if xobj.get("/Decode") is not None:
        return None
    width, height = int(xobj["/Width"]), int(xobj["/Height"])
    bpc = int(xobj.get("/BitsPerComponent", 8))
    colors = int(parms.get("/Colors", 1))
    if int(parms.get("/Columns", 1)) != width or int(parms.get("/BitsPerComponent", 8)) != bpc:
        return None
    cs = xobj.get("/ColorSpace")
    cs = cs.get_object() if hasattr(cs, "get_object") else cs
    plte = None
    if isinstance(cs, (list, tuple)):
        if len(cs) == 4 and str(cs[0]) == "/Indexed" and str(cs[1].get_object() if hasattr(cs[1], "get_object") else cs[1]) == "/DeviceRGB":
            lookup = cs[3].get_object() if hasattr(cs[3], "get_object") else cs[3]
            if hasattr(lookup, "get_data"):
                plte = bytes(lookup.get_data())
            elif hasattr(lookup, "original_bytes"):
                plte = bytes(lookup.original_bytes)
            else:
                plte = bytes(lookup)
            hival = int(cs[2])
            plte = plte[:3 * (hival + 1)]
            color_type = 3
            if colors != 1 or bpc not in (1, 2, 4, 8):
                return None
        else:
            return None
    else:
        cs = str(cs)
        if cs == "/DeviceGray" and colors == 1 and bpc in (1, 2, 4, 8, 16):
            color_type = 0
        elif cs == "/DeviceRGB" and colors == 3 and bpc in (8, 16):
            color_type = 2
        else:
            return None
    trns = None
    mask = xobj.get("/Mask")
    if mask is not None:
        mvals = _as_list(mask)
        if not mvals or any(hasattr(m, "get_data") for m in mvals):
            return None  # 스텐실 마스크 등은 지원 안 함
        vals = [int(m) for m in mvals]
        if len(vals) != 2 * (3 if color_type == 2 else 1) or any(vals[i] != vals[i + 1] for i in range(0, len(vals), 2)):
            return None
        if color_type == 3:
            return None
        trns = b"".join(struct.pack(">H", vals[i]) for i in range(0, len(vals), 2))
    zdata = _raw_stream_bytes(xobj)
    if zdata is None:
        # 원시 데이터를 못 얻으면 디코딩된 행을 다시 압축 (픽셀 단위 동일)
        rows = xobj.get_data()
        row_len = (width * colors * bpc + 7) // 8
        zdata = zlib.compress(b"".join(b"\x00" + rows[i:i + row_len] for i in range(0, len(rows), row_len)), 6)
    else:
        # zlib 스트림인지 가볍게 검증
        try:
            zlib.decompressobj().decompress(zdata[:64])
        except zlib.error:
            return None
    return _png_from_parts(width, height, bpc, color_type, zdata, plte, trns)


def _xobject_to_file(xobj) -> Optional[tuple[bytes, str]]:
    """이미지 XObject → (파일 바이트, 확장자). 우리 형식이 아니면 None."""
    filters = [str(f) for f in _as_list(xobj.get("/Filter"))]
    if filters == ["/DCTDecode"]:
        raw = _raw_stream_bytes(xobj)
        if raw is not None and raw[:2] == b"\xFF\xD8":
            return raw, "jpg"
        return None
    if filters == ["/FlateDecode"]:
        smask = xobj.get("/SMask")
        base = _flate_xobject_to_png(xobj)
        if base is None:
            return None
        if smask is None:
            return base, "png"
        alpha = _flate_xobject_to_png(smask.get_object())
        if alpha is None:
            return None
        with Image.open(io.BytesIO(base)) as bim, Image.open(io.BytesIO(alpha)) as aim:
            bim.load(); aim.load()
            if aim.mode != "L":
                aim = aim.convert("L")
            if bim.mode == "L":
                merged = Image.merge("LA", (bim, aim))
            else:
                merged = Image.merge("RGBA", (*bim.convert("RGB").split(), aim))
            buf = io.BytesIO()
            merged.save(buf, format="PNG", compress_level=6)
            return buf.getvalue(), "png"
    return None


@dataclass
class ExtractedImage:
    path: Path
    page: int
    source_name: str
    method: str            # "attachment" | "verbatim" | "rebuilt-png" | "pypdf"
    bit_exact: Optional[bool]   # 원본 파일 해시와 일치하는지 (알 수 없으면 None)


def _unique_path(directory: Path, name: str) -> Path:
    cand = directory / name
    if not cand.exists():
        return cand
    stem, suffix = Path(name).stem, Path(name).suffix
    k = 2
    while True:
        cand = directory / f"{stem} ({k}){suffix}"
        if not cand.exists():
            return cand
        k += 1


def extract_images(path: os.PathLike | str, out_dir: os.PathLike | str,
                   progress: ProgressFn = None) -> list[ExtractedImage]:
    """PDF/JPDF의 페이지 이미지를 파일로 꺼낸다. JPDF면 원본 이름·바이트를 최대한 복원한다."""
    from pypdf import PdfReader
    p = Path(path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    try:
        reader = PdfReader(str(p))
    except Exception as e:  # noqa: BLE001
        raise JpdfError(f"PDF를 읽을 수 없습니다: {p.name} ({e})") from e
    meta, entries = _read_jpdf_meta(reader)
    by_page = {e["page"]: e for e in entries}
    attachments = {}
    if any(e.get("attachment") for e in entries):
        try:
            attachments = dict(reader.attachments)
        except Exception:  # noqa: BLE001
            attachments = {}
    results: list[ExtractedImage] = []
    total = len(reader.pages)
    for pno, page in enumerate(reader.pages, 1):
        if progress:
            progress(pno - 1, total, f"{pno}/{total} 페이지 추출 중…")
        entry = by_page.get(pno)
        wanted_name = _safe_filename(entry["name"]) if entry and entry.get("name") else ""
        done = False

        # 1) 첨부된 원본
        if entry and entry.get("attachment") and entry["attachment"] in attachments:
            blobs = attachments[entry["attachment"]]
            blob = blobs[0] if isinstance(blobs, list) else blobs
            dst = _unique_path(out, wanted_name or entry["attachment"])
            dst.write_bytes(blob)
            exact = (hashlib.sha256(blob).hexdigest() == entry["sha256"]) if entry.get("sha256") else None
            results.append(ExtractedImage(dst, pno, entry.get("name", ""), "attachment", exact))
            done = True

        # 2) XObject에서 복원
        if not done:
            try:
                res = page.get("/Resources")
                res = res.get_object() if res is not None else None
                xdict = res.get("/XObject") if res else None
                xdict = xdict.get_object() if xdict is not None else {}
            except Exception:  # noqa: BLE001
                xdict = {}
            names = list(xdict.keys())
            if entry:
                names = ["/Im0"] + [n for n in names if n != "/Im0"]
            for n in names:
                try:
                    xobj = xdict[n].get_object()
                    if str(xobj.get("/Subtype")) != "/Image":
                        continue
                    r = _xobject_to_file(xobj)
                except Exception:  # noqa: BLE001
                    r = None
                if r is None:
                    continue
                data, ext = r
                if wanted_name:
                    base = wanted_name
                    if Path(base).suffix.lower().lstrip(".") not in (("jpg", "jpeg", "jpe", "jfif") if ext == "jpg" else ("png",)):
                        base = f"{Path(base).stem}.{ext}"
                else:
                    base = f"{p.stem}_p{pno:03d}_{n.lstrip('/')}.{ext}"
                dst = _unique_path(out, base)
                dst.write_bytes(data)
                exact = None
                if entry and entry.get("sha256"):
                    exact = hashlib.sha256(data).hexdigest() == entry["sha256"]
                results.append(ExtractedImage(dst, pno, entry.get("name", "") if entry else "",
                                              "verbatim" if ext == "jpg" else "rebuilt-png", exact))
                done = True
                if entry:
                    break  # JPDF 페이지엔 이미지가 하나

        # 3) pypdf 범용 추출
        if not done:
            try:
                for k, img in enumerate(page.images):
                    ext = Path(img.name).suffix or ".png"
                    dst = _unique_path(out, f"{p.stem}_p{pno:03d}_{k + 1}{ext}")
                    dst.write_bytes(img.data)
                    results.append(ExtractedImage(dst, pno, "", "pypdf", None))
            except Exception as e:  # noqa: BLE001
                raise JpdfError(f"{pno}페이지 이미지를 추출하지 못했습니다: {e}") from e
    if progress:
        progress(total, total, "완료")
    return results


def first_page_thumbnail(path: os.PathLike | str, max_px: int = 256) -> Optional[Image.Image]:
    """JPDF/PDF 첫 페이지 이미지의 썸네일 (EXIF 회전 반영). 실패하면 None."""
    from pypdf import PdfReader
    try:
        reader = PdfReader(str(path))
        page = reader.pages[0]
        meta, entries = _read_jpdf_meta(reader)
        orientation = entries[0]["orientation"] if entries else 1
        res = page.get("/Resources")
        xdict = res.get_object().get("/XObject").get_object()
        for n in xdict:
            xobj = xdict[n].get_object()
            if str(xobj.get("/Subtype")) != "/Image":
                continue
            r = _xobject_to_file(xobj)
            if r is None:
                try:
                    im = page.images[0].image
                except Exception:  # noqa: BLE001
                    return None
            else:
                im = Image.open(io.BytesIO(r[0]))
                if r[1] == "jpg":
                    im.draft("RGB", (max_px * 2, max_px * 2))
            im.load()
            if orientation != 1:
                im = apply_orientation(im, orientation)
            im.thumbnail((max_px, max_px))
            return im.convert("RGBA") if im.mode not in ("RGB", "RGBA", "L") else im
    except Exception:  # noqa: BLE001
        return None
    return None


# ---------------------------------------------------------------------------
# "PDF로 열기 / 이미지로 열기"
# ---------------------------------------------------------------------------
def cache_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    d = base / "jpdf" / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def clear_cache() -> int:
    d = cache_dir()
    n = 0
    for child in d.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child)
                n += 1
            else:
                child.unlink()
                if child.suffix != ".stamp":
                    n += 1
        except OSError:
            pass
    return n


def open_with_default_app(path: os.PathLike | str) -> None:
    p = str(path)
    if sys.platform == "win32":
        os.startfile(p)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _cache_key(p: Path) -> str:
    return hashlib.sha1(str(p.resolve()).encode("utf-8", "replace")).hexdigest()[:10]


def _stamp(p: Path) -> str:
    st = p.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


def open_as_pdf(path: os.PathLike | str, launch: bool = True) -> Path:
    """.jpdf를 캐시 폴더에 .pdf 이름으로 복사한 뒤 기본 PDF 뷰어로 연다 (내용은 이미 PDF)."""
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        dst = p
    else:
        dst = cache_dir() / f"{p.stem}-{_cache_key(p)}.pdf"
        stamp = dst.with_suffix(".stamp")
        if not dst.exists() or not stamp.exists() or stamp.read_text() != _stamp(p):
            shutil.copyfile(p, dst)
            stamp.write_text(_stamp(p))
    if launch:
        open_with_default_app(dst)
    return dst


def reveal_in_folder(path: os.PathLike | str) -> None:
    """파일 탐색기(Finder)에서 해당 파일이 선택된 상태로 폴더를 연다."""
    p = Path(path)
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(p)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(p)])
        else:
            open_with_default_app(p.parent)
    except Exception:  # noqa: BLE001
        pass


def open_as_images(path: os.PathLike | str, launch: bool = True,
                   progress: ProgressFn = None, reveal_folder: bool = True) -> list[ExtractedImage]:
    """이미지들을 캐시 폴더에 꺼내고 첫 이미지를 기본 이미지 뷰어로 연다.
    이미지가 여러 장이면 (뷰어가 폴더 탐색을 지원하지 않는 경우를 위해) 폴더도 함께 연다."""
    p = Path(path)
    d = cache_dir() / f"{p.stem}-{_cache_key(p)}"
    stamp = d.with_name(d.name + ".stamp")   # 폴더 밖에 두어 이미지 폴더를 깨끗하게 유지
    results: list[ExtractedImage] = []
    if d.exists() and stamp.exists() and stamp.read_text() == _stamp(p):
        files = sorted(x for x in d.iterdir() if x.is_file() and not x.name.startswith("."))
        results = [ExtractedImage(x, i + 1, x.name, "cache", None) for i, x in enumerate(files)]
    if not results:
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
        results = extract_images(p, d, progress)
        stamp.write_text(_stamp(p))
    if launch and results:
        open_with_default_app(results[0].path)
        if reveal_folder and len(results) > 1:
            reveal_in_folder(results[0].path)
    return results


# ---------------------------------------------------------------------------
# 명령줄 사용 (python jpdf_core.py --help)
# ---------------------------------------------------------------------------
def _cli(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="jpdf_core", description="JPDF 변환 엔진 명령줄")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("convert", help="이미지들 → PDF/JPDF")
    c.add_argument("images", nargs="+")
    c.add_argument("-o", "--output", required=True)
    c.add_argument("--page", default="image", help="image | " + " | ".join(PAGE_SIZES_PT))
    c.add_argument("--orientation", default="auto", choices=["auto", "portrait", "landscape"])
    c.add_argument("--margin", type=float, default=0.0, help="여백 mm")
    c.add_argument("--dpi", type=float, default=96.0)
    c.add_argument("--embed-originals", action="store_true")
    x = sub.add_parser("extract", help="PDF/JPDF → 이미지")
    x.add_argument("pdf")
    x.add_argument("-o", "--outdir", required=True)
    i = sub.add_parser("info", help="PDF/JPDF 정보")
    i.add_argument("pdf")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "convert":
            opt = LayoutOptions(a.page, a.orientation, a.margin, a.dpi, embed_originals=a.embed_originals)
            rep = convert_images(a.images, a.output, opt,
                                 progress=lambda i, n, m: print(f"[{i}/{n}] {m}", file=sys.stderr))
            for r in rep.images:
                print(f"  p{r.page}: {r.source.name} {r.width}x{r.height} {r.format} — {r.note}")
            print(f"→ {rep.output} ({human_size(rep.bytes_out)}, {rep.pages}페이지, {rep.elapsed:.2f}s)")
        elif a.cmd == "extract":
            for r in extract_images(a.pdf, a.outdir):
                exact = "" if r.bit_exact is None else (" [원본과 바이트 동일]" if r.bit_exact else " [재구성]")
                print(f"  p{r.page}: {r.path} ({r.method}){exact}")
        else:
            info = inspect_pdf(a.pdf)
            print(info.summary())
            for im in info.images:
                print(f"  p{im['page']}: {im['name']} {im['width']}x{im['height']} {im['format']} plan={im['plan']}")
        return 0
    except JpdfError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(_cli())
