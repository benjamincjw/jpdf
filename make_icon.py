# -*- coding: utf-8 -*-
"""앱 아이콘(assets/jpdf.png, assets/jpdf.ico)을 코드로 생성한다. 실행: python make_icon.py"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


def draw_icon(size: int = 256) -> Image.Image:
    s = size
    ss = 4  # 슈퍼샘플링
    S = s * ss
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    # 배경: 둥근 사각형 (남색)
    r = S * 0.22
    d.rounded_rectangle((0, 0, S - 1, S - 1), radius=r, fill=(36, 62, 122, 255))
    # 문서 (흰색, 오른쪽 위 접힌 모서리)
    px0, py0, px1, py1 = S * 0.22, S * 0.14, S * 0.78, S * 0.86
    fold = S * 0.16
    d.polygon([(px0, py0), (px1 - fold, py0), (px1, py0 + fold), (px1, py1), (px0, py1)], fill=(250, 250, 250, 255))
    d.polygon([(px1 - fold, py0), (px1 - fold, py0 + fold), (px1, py0 + fold)], fill=(205, 212, 228, 255))
    # 사진: 하늘색 사각형 + 산 + 해
    fx0, fy0, fx1, fy1 = px0 + S * 0.07, py0 + S * 0.24, px1 - S * 0.07, py1 - S * 0.10
    d.rectangle((fx0, fy0, fx1, fy1), fill=(120, 190, 240, 255))
    d.polygon([(fx0, fy1), (fx0 + (fx1 - fx0) * 0.38, fy0 + (fy1 - fy0) * 0.40), (fx0 + (fx1 - fx0) * 0.62, fy1)],
              fill=(60, 140, 90, 255))
    d.polygon([(fx0 + (fx1 - fx0) * 0.45, fy1), (fx0 + (fx1 - fx0) * 0.72, fy0 + (fy1 - fy0) * 0.25), (fx1, fy1)],
              fill=(40, 110, 70, 255))
    sr = (fx1 - fx0) * 0.11
    cx, cy = fx0 + (fx1 - fx0) * 0.24, fy0 + (fy1 - fy0) * 0.26
    d.ellipse((cx - sr, cy - sr, cx + sr, cy + sr), fill=(255, 190, 60, 255))
    # 아래쪽 작은 텍스트 줄 (문서 느낌)
    for i in range(2):
        y = fy1 + S * 0.03 + i * S * 0.035
        d.rounded_rectangle((fx0, y, fx0 + (fx1 - fx0) * (0.9 if i == 0 else 0.6), y + S * 0.018),
                            radius=S * 0.009, fill=(170, 178, 200, 255))
    return im.resize((s, s), Image.LANCZOS)


def main() -> None:
    out = Path(__file__).resolve().parent / "assets"
    out.mkdir(exist_ok=True)
    base = draw_icon(256)
    base.save(out / "jpdf.png")
    sizes = [16, 24, 32, 48, 64, 128, 256]
    base.save(out / "jpdf.ico", sizes=[(x, x) for x in sizes])
    print("saved", out / "jpdf.png", out / "jpdf.ico")


if __name__ == "__main__":
    main()
