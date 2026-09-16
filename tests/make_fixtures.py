# -*- coding: utf-8 -*-
"""테스트용 이미지 묶음을 생성한다. (JPEG/PNG의 여러 변종 + 기타 형식)"""
from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path

from PIL import Image, ImageDraw


def _scene(w: int, h: int, mode: str = "RGB") -> Image.Image:
    """방향을 구분할 수 있는 비대칭 그림 (왼쪽 위 빨간 원, 오른쪽 아래 파란 사각형, 대각선)."""
    im = Image.new("RGB", (w, h), (245, 240, 225))
    d = ImageDraw.Draw(im)
    d.ellipse((w * 0.05, h * 0.05, w * 0.35, h * 0.35), fill=(220, 40, 40))
    d.rectangle((w * 0.6, h * 0.6, w * 0.95, h * 0.95), fill=(30, 60, 200))
    d.line((0, h, w, 0), fill=(20, 140, 60), width=max(2, w // 40))
    for i in range(0, w, max(8, w // 16)):
        d.line((i, 0, i, h * 0.08), fill=(0, 0, 0), width=1)
    return im.convert(mode) if mode != "RGB" else im


def _png_interlaced(im: Image.Image) -> bytes:
    """Pillow는 인터레이스 PNG를 못 쓰므로, 직접 Adam7 인터레이스 PNG를 만든다 (RGB 8비트)."""
    im = im.convert("RGB")
    w, h = im.size
    px = im.load()
    passes = [(0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)]
    raw = bytearray()
    for sx, sy, dx, dy in passes:
        for y in range(sy, h, dy):
            row = bytearray([0])
            for x in range(sx, w, dx):
                row += bytes(px[x, y])
            if len(row) > 1:
                raw += row
    def chunk(t, b):
        return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 1)) +
            chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def _with_exif_orientation(jpeg_bytes: bytes, orientation: int) -> bytes:
    im = Image.open(io.BytesIO(jpeg_bytes))
    exif = im.getexif()
    exif[0x0112] = orientation
    buf = io.BytesIO()
    # 재인코딩 없이 EXIF만 붙일 수는 없으므로, 여기서는 EXIF를 포함해 저장한다 (테스트 입력 생성용)
    im.save(buf, "JPEG", quality=90, exif=exif.tobytes())
    return buf.getvalue()


def make_fixtures(out: Path) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {}

    def save(name: str, im: Image.Image, **kw) -> Path:
        p = out / name
        im.save(p, **kw)
        files[name] = p
        return p

    base = _scene(320, 200)
    save("rgb_baseline.jpg", base, format="JPEG", quality=85, dpi=(150, 150))
    save("rgb_progressive.jpg", base, format="JPEG", quality=80, progressive=True)
    save("gray.jpg", base.convert("L"), format="JPEG", quality=85)
    save("cmyk.jpg", base.convert("CMYK"), format="JPEG", quality=85)
    save("optimized_4_2_0.jpg", base, format="JPEG", quality=70, subsampling=2, optimize=True)
    for o in range(2, 9):
        p = out / f"exif_o{o}.jpg"
        p.write_bytes(_with_exif_orientation(files["rgb_baseline.jpg"].read_bytes(), o))
        files[p.name] = p
    # 세로 사진 + 방향 6 (실제 카메라 파일과 같은 상황)
    tall = _scene(200, 320)
    buf = io.BytesIO(); tall.save(buf, "JPEG", quality=85)
    p = out / "portrait_o6.jpg"; p.write_bytes(_with_exif_orientation(buf.getvalue(), 6)); files[p.name] = p

    save("rgb8.png", base, format="PNG", dpi=(96, 96))
    save("gray8.png", base.convert("L"), format="PNG")
    save("gray1.png", base.convert("1"), format="PNG")
    save("gray16.png", base.convert("I;16"), format="PNG")
    save("palette.png", base.convert("P", palette=Image.Palette.ADAPTIVE, colors=64), format="PNG")
    save("palette4bit.png", base.convert("P", palette=Image.Palette.ADAPTIVE, colors=16), format="PNG", bits=4)
    rgba = base.convert("RGBA")
    a = Image.linear_gradient("L").resize(base.size)
    rgba.putalpha(a)
    save("rgba.png", rgba, format="PNG")
    la = Image.merge("LA", (base.convert("L"), a))
    save("gray_alpha.png", la, format="PNG")
    pal_t = base.convert("P", palette=Image.Palette.ADAPTIVE, colors=32)
    pal_t.info["transparency"] = 3
    save("palette_trns.png", pal_t, format="PNG", transparency=3)
    ck = base.copy(); ck.info["transparency"] = (245, 240, 225)
    save("rgb_colorkey.png", ck, format="PNG", transparency=(245, 240, 225))
    rgb16 = Image.merge("RGB", [c.convert("I;16") .convert("L") for c in base.split()])  # 8비트 → 그냥 RGB (Pillow 제한)
    p = out / "interlaced.png"; p.write_bytes(_png_interlaced(base)); files[p.name] = p
    rgba16 = None  # Pillow로 16비트 RGBA 생성 불가 → 생략

    save("bitmap.bmp", base, format="BMP")
    save("anim.gif", base.convert("P", palette=Image.Palette.ADAPTIVE), format="GIF")
    save("tiff_rgb.tif", base, format="TIFF")
    try:
        save("lossless.webp", base, format="WEBP", lossless=True)
    except Exception:
        pass
    save("한글 이름 (테스트).jpg", base, format="JPEG", quality=85)
    # 아주 작은 이미지와 큰 이미지
    save("tiny_1x1.png", Image.new("RGB", (1, 1), (255, 0, 0)), format="PNG")
    save("wide_4000x300.jpg", _scene(4000, 300), format="JPEG", quality=60)
    return files


if __name__ == "__main__":
    import sys
    d = Path(sys.argv[1] if len(sys.argv) > 1 else "fixtures")
    for name, p in make_fixtures(d).items():
        print(f"{p.stat().st_size:9d}  {name}")
