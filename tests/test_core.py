# -*- coding: utf-8 -*-
"""jpdf_core 자동 테스트.

실행:  python -m pytest tests -q
렌더링 비교 테스트는 poppler(pdftoppm)가 있을 때만 실행된다. qpdf가 있으면 구조 검사도 한다.
"""
from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

import pytest
from PIL import Image, ImageChops

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jpdf_core as jc  # noqa: E402
from tests.make_fixtures import make_fixtures  # noqa: E402

HAVE_PDFTOPPM = shutil.which("pdftoppm") is not None
HAVE_QPDF = shutil.which("qpdf") is not None


@pytest.fixture(scope="session")
def fixtures(tmp_path_factory) -> dict[str, Path]:
    return make_fixtures(tmp_path_factory.mktemp("fixtures"))


try:
    import pypdfium2 as _pdfium
    HAVE_PDFIUM = True
except Exception:  # noqa: BLE001
    HAVE_PDFIUM = False
HAVE_RENDERER = HAVE_PDFIUM or HAVE_PDFTOPPM


def _render(pdf: Path, dpi: int = 72) -> list[Image.Image]:
    """페이지를 72dpi 기준 1:1 픽셀로 렌더링. pdfium은 정확히 1:1, poppler는 4배 렌더 후 박스 축소
    (poppler의 1:1 렌더는 반 픽셀 리샘플링 오차가 있어 비교용으로 부적합)."""
    if HAVE_PDFIUM:
        doc = _pdfium.PdfDocument(str(pdf))
        return [doc[i].render(scale=dpi / 72).to_pil().convert("RGB") for i in range(len(doc))]
    outdir = pdf.parent / (pdf.stem + "_render")
    outdir.mkdir(exist_ok=True)
    subprocess.run(["pdftoppm", "-r", str(dpi * 4), "-png", str(pdf), str(outdir / "p")], check=True)
    pages = []
    for p in sorted(outdir.glob("p-*.png")):
        big = Image.open(p).convert("RGB")
        pages.append(big.resize((big.width // 4, big.height // 4), Image.BOX))
    return pages


def _reference(src: Path, orientation: int) -> Image.Image:
    """소스 이미지를 뷰어가 보여줄 모습(EXIF 회전, 흰 배경 합성)으로 만든다."""
    im = Image.open(src)
    im.load()
    if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info:
        rgba = im.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, rgba).convert("RGB")
    elif im.mode == "I;16":
        im = Image.fromarray((__import__("numpy").asarray(im) // 257).astype("uint8")).convert("RGB")
    else:
        im = im.convert("RGB")
    return jc.apply_orientation(im, orientation)


def _mean_abs_diff(a: Image.Image, b: Image.Image) -> float:
    assert a.size == b.size, (a.size, b.size)
    diff = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
    h = diff.histogram()
    total = 0
    for ch in range(3):
        total += sum(i * c for i, c in enumerate(h[ch * 256:(ch + 1) * 256]))
    return total / (a.size[0] * a.size[1] * 3)


# ---------------------------------------------------------------------------
def test_analysis_plans(fixtures):
    plans = {name: jc.analyze_image(p).plan for name, p in fixtures.items()}
    assert plans["rgb_baseline.jpg"] == "jpeg-verbatim"
    assert plans["rgb_progressive.jpg"] == "jpeg-verbatim"
    assert plans["cmyk.jpg"] == "jpeg-verbatim"
    assert plans["rgb8.png"] == "png-direct"
    assert plans["gray16.png"] == "png-direct"
    assert plans["palette.png"] == "png-direct"
    assert plans["rgb_colorkey.png"] == "png-direct"
    assert plans["rgba.png"] == "png-reencode"
    assert plans["palette_trns.png"] == "png-reencode"
    assert plans["interlaced.png"] == "png-reencode"
    assert plans["bitmap.bmp"] == "other"
    assert all(jc.analyze_image(p).lossless for p in fixtures.values())


def test_exif_orientation_detected(fixtures):
    for o in range(2, 9):
        assert jc.analyze_image(fixtures[f"exif_o{o}.jpg"]).orientation == o
    an = jc.analyze_image(fixtures["portrait_o6.jpg"])
    assert (an.width, an.height) == (200, 320) and an.display_size == (320, 200)


def test_convert_all_and_structure(fixtures, tmp_path):
    out = tmp_path / "all.jpdf"
    rep = jc.convert_images(list(fixtures.values()), out)
    assert out.exists() and rep.pages == len(fixtures)
    head = out.read_bytes()[:64]
    assert head.startswith(b"%PDF-1.7\n") and b"%JPDF-1.0" in head
    assert jc.is_jpdf_file(out)
    info = jc.inspect_pdf(out)
    assert info.is_jpdf and info.pages == len(fixtures) and len(info.images) == len(fixtures)
    names = [i["name"] for i in info.images]
    assert "한글 이름 (테스트).jpg" in names  # UTF-16 이름 왕복
    if HAVE_QPDF:
        r = subprocess.run(["qpdf", "--check", str(out)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
    # 저장 용량: JPEG/PNG 직접 삽입은 원본 크기와 거의 같아야 한다
    for r in rep.images:
        if r.plan in ("jpeg-verbatim", "png-direct"):
            assert r.bytes_stored <= r.bytes_in


def test_pdf_extension_has_no_signature_but_metadata(fixtures, tmp_path):
    out = tmp_path / "plain.pdf"
    jc.convert_images([fixtures["rgb_baseline.jpg"]], out)
    assert b"%JPDF-" not in out.read_bytes()[:64]
    assert jc.is_jpdf_file(out)  # 카탈로그 /JPDF 로 식별
    assert jc.inspect_pdf(out).is_jpdf


def test_jpeg_bytes_verbatim_in_pdf(fixtures, tmp_path):
    src = fixtures["rgb_progressive.jpg"]
    out = tmp_path / "one.jpdf"
    jc.convert_images([src], out)
    data = out.read_bytes()
    assert src.read_bytes() in data           # 바이트 그대로 포함
    assert len(data) < src.stat().st_size + 2500  # 오버헤드는 수 KB 이내


def test_png_idat_verbatim_in_pdf(fixtures, tmp_path):
    src = fixtures["rgb8.png"]
    png = jc._parse_png_chunks(src.read_bytes())
    out = tmp_path / "one.jpdf"
    jc.convert_images([src], out)
    assert png["idat"] in out.read_bytes()


def test_extract_roundtrip_bit_exact_jpeg(fixtures, tmp_path):
    srcs = [fixtures[n] for n in ("rgb_baseline.jpg", "rgb_progressive.jpg", "gray.jpg", "cmyk.jpg",
                                  "exif_o6.jpg", "한글 이름 (테스트).jpg")]
    out = tmp_path / "jp.jpdf"
    jc.convert_images(srcs, out)
    res = jc.extract_images(out, tmp_path / "ex")
    assert len(res) == len(srcs)
    for src, r in zip(srcs, res):
        assert r.method == "verbatim" and r.bit_exact is True
        assert r.path.name == src.name
        assert hashlib.sha256(r.path.read_bytes()).hexdigest() == hashlib.sha256(src.read_bytes()).hexdigest()


def test_extract_roundtrip_pixel_exact_png(fixtures, tmp_path):
    names = ["rgb8.png", "gray8.png", "gray1.png", "gray16.png", "palette.png", "palette4bit.png",
             "rgba.png", "gray_alpha.png", "palette_trns.png", "rgb_colorkey.png", "interlaced.png",
             "bitmap.bmp", "lossless.webp", "tiff_rgb.tif", "tiny_1x1.png"]
    srcs = [fixtures[n] for n in names if n in fixtures]
    out = tmp_path / "png.jpdf"
    jc.convert_images(srcs, out)
    res = jc.extract_images(out, tmp_path / "ex")
    assert len(res) == len(srcs)
    for src, r in zip(srcs, res):
        assert r.method == "rebuilt-png", (src.name, r.method)
        a = Image.open(src); a.load()
        b = Image.open(r.path); b.load()
        assert a.size == b.size
        if a.mode in ("RGBA", "LA", "PA") or "transparency" in a.info:
            ra, rb = a.convert("RGBA"), b.convert("RGBA")
            assert ImageChops.difference(ra, rb).getbbox() is None, src.name
        elif a.mode == "I;16":
            assert a.tobytes() == b.tobytes(), src.name
        else:
            assert ImageChops.difference(a.convert("RGB"), b.convert("RGB")).getbbox() is None, src.name
    # 직접 삽입된 PNG는 압축 데이터(IDAT)도 그대로 돌아온다
    direct = [(s, r) for s, r in zip(srcs, res) if jc.analyze_image(s).plan == "png-direct"]
    for s, r in direct:
        assert jc._parse_png_chunks(s.read_bytes())["idat"] == jc._parse_png_chunks(r.path.read_bytes())["idat"]


def test_embed_originals_attachment_roundtrip(fixtures, tmp_path):
    srcs = [fixtures["rgba.png"], fixtures["rgb_baseline.jpg"]]
    out = tmp_path / "att.jpdf"
    opt = jc.LayoutOptions(embed_originals=True)
    rep = jc.convert_images(srcs, out, opt)
    assert out.stat().st_size > sum(s.stat().st_size for s in srcs) * 1.8
    info = jc.inspect_pdf(out)
    assert info.has_attachments
    res = jc.extract_images(out, tmp_path / "ex")
    for src, r in zip(srcs, res):
        assert r.method == "attachment" and r.bit_exact is True
        assert r.path.read_bytes() == src.read_bytes()
    if HAVE_QPDF:
        r = subprocess.run(["qpdf", "--check", str(out)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr


def test_layout_fixed_page_sizes(fixtures, tmp_path):
    from pypdf import PdfReader
    src = fixtures["wide_4000x300.jpg"]
    for size, orient, expect in [("A4", "auto", (841.89, 595.276)), ("A4", "portrait", (595.276, 841.89)),
                                 ("Letter", "landscape", (792.0, 612.0))]:
        out = tmp_path / f"{size}_{orient}.pdf"
        jc.convert_images([src], out, jc.LayoutOptions(page_size=size, orientation=orient, margin_mm=10))
        box = PdfReader(str(out)).pages[0].mediabox
        assert (round(float(box.width), 3), round(float(box.height), 3)) == expect
    # 여백이 너무 크면 오류
    with pytest.raises(jc.JpdfError):
        jc.convert_images([src], tmp_path / "bad.pdf", jc.LayoutOptions(page_size="A5", margin_mm=300))


def test_layout_image_size_uses_dpi(fixtures, tmp_path):
    from pypdf import PdfReader
    out = tmp_path / "dpi.pdf"
    jc.convert_images([fixtures["rgb_baseline.jpg"]], out)  # 150 dpi 메타데이터
    box = PdfReader(str(out)).pages[0].mediabox
    assert abs(float(box.width) - 320 * 72 / 150) < 0.01 and abs(float(box.height) - 200 * 72 / 150) < 0.01
    out2 = tmp_path / "dpi2.pdf"
    jc.convert_images([fixtures["rgb_baseline.jpg"]], out2, jc.LayoutOptions(use_image_dpi=False, default_dpi=72))
    box = PdfReader(str(out2)).pages[0].mediabox
    assert (float(box.width), float(box.height)) == (320.0, 200.0)
    # EXIF 회전 6 → 페이지도 가로/세로가 바뀐다
    out3 = tmp_path / "rot.pdf"
    jc.convert_images([fixtures["portrait_o6.jpg"]], out3, jc.LayoutOptions(use_image_dpi=False, default_dpi=72))
    box = PdfReader(str(out3)).pages[0].mediabox
    assert (float(box.width), float(box.height)) == (320.0, 200.0)


@pytest.mark.skipif(not HAVE_RENDERER, reason="렌더러(pypdfium2/pdftoppm) 없음")
@pytest.mark.parametrize("name", [
    "rgb_baseline.jpg", "rgb_progressive.jpg", "gray.jpg", "optimized_4_2_0.jpg",
    "exif_o2.jpg", "exif_o3.jpg", "exif_o4.jpg", "exif_o5.jpg", "exif_o6.jpg", "exif_o7.jpg", "exif_o8.jpg",
    "portrait_o6.jpg", "rgb8.png", "gray8.png", "gray1.png", "gray16.png", "palette.png", "palette4bit.png",
    "rgba.png", "gray_alpha.png", "palette_trns.png", "rgb_colorkey.png", "interlaced.png",
    "bitmap.bmp", "anim.gif", "tiff_rgb.tif", "lossless.webp",
])
def test_render_matches_source(fixtures, tmp_path, name):
    if name not in fixtures:
        pytest.skip("fixture 없음")
    src = fixtures[name]
    out = tmp_path / (Path(name).stem + ".pdf")
    jc.convert_images([src], out, jc.LayoutOptions(use_image_dpi=False, default_dpi=72))
    page = _render(out, 72)[0]
    ref = _reference(src, jc.analyze_image(src).orientation)
    assert page.size == ref.size
    mad = _mean_abs_diff(page, ref)
    tol = 2.5 if src.suffix.lower() in (".jpg", ".jpeg") else 0.5
    assert mad <= tol, f"{name}: mean abs diff {mad:.3f}"


@pytest.mark.skipif(not HAVE_RENDERER, reason="렌더러(pypdfium2/pdftoppm) 없음")
def test_render_cmyk_not_blank(fixtures, tmp_path):
    out = tmp_path / "cmyk.pdf"
    jc.convert_images([fixtures["cmyk.jpg"]], out, jc.LayoutOptions(use_image_dpi=False, default_dpi=72))
    page = _render(out, 72)[0]
    ref = _reference(fixtures["cmyk.jpg"], 1)
    mad = _mean_abs_diff(page, ref)
    assert mad < 40, mad   # CMYK→RGB 변환 방식 차이만 허용 (반전이 틀리면 ~150 이상 나온다)


@pytest.mark.skipif(not HAVE_RENDERER, reason="렌더러(pypdfium2/pdftoppm) 없음")
def test_tiny_image_gets_minimum_page(fixtures, tmp_path):
    """1×1 px 이미지도 PDF 최소 페이지(3pt) 안에 가운데 놓인다."""
    out = tmp_path / "tiny.pdf"
    jc.convert_images([fixtures["tiny_1x1.png"]], out, jc.LayoutOptions(use_image_dpi=False, default_dpi=72))
    page = _render(out, 72)[0]
    assert page.size == (3, 3)
    assert page.getpixel((1, 1)) == (255, 0, 0)


def test_open_helpers_do_not_launch(fixtures, tmp_path, monkeypatch):
    (tmp_path / "cache").mkdir()
    monkeypatch.setattr(jc, "cache_dir", lambda: tmp_path / "cache")
    out = tmp_path / "x.jpdf"
    jc.convert_images([fixtures["rgb_baseline.jpg"], fixtures["rgba.png"]], out)
    pdf = jc.open_as_pdf(out, launch=False)
    assert pdf.suffix == ".pdf" and pdf.read_bytes() == out.read_bytes()
    imgs = jc.open_as_images(out, launch=False)
    assert [p.path.name for p in imgs] == ["rgb_baseline.jpg", "rgba.png"]
    # 두 번째 호출은 캐시를 쓴다
    imgs2 = jc.open_as_images(out, launch=False)
    assert [p.path for p in imgs2] == [p.path for p in imgs] and imgs2[0].method == "cache"
    assert jc.first_page_thumbnail(out, 64).size[0] <= 64


def test_extract_from_foreign_pdf(tmp_path):
    """다른 도구(Pillow)가 만든 PDF에서도 이미지가 나와야 한다 (pypdf 범용 경로)."""
    im = Image.new("RGB", (40, 30), (10, 200, 30))
    pdf = tmp_path / "pil.pdf"
    im.save(pdf, "PDF")
    res = jc.extract_images(pdf, tmp_path / "ex")
    assert len(res) >= 1 and res[0].path.exists()
    got = Image.open(res[0].path).convert("RGB")
    assert got.size == (40, 30)


def test_errors(tmp_path):
    with pytest.raises(jc.JpdfError):
        jc.convert_images([], tmp_path / "e.jpdf")
    with pytest.raises(jc.JpdfError):
        jc.convert_images([tmp_path / "missing.jpg"], tmp_path / "e.jpdf")
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    with pytest.raises(jc.JpdfError):
        jc.analyze_image(bad)
    assert not (tmp_path / "e.jpdf.part").exists()
