# -*- coding: utf-8 -*-
"""크롬 확장 엔진(chrome-extension/core/jpdf.js)과 데스크톱 엔진(jpdf_core.py)의 상호 운용 테스트.

Node.js 가 있을 때만 실행된다. JS 로 만든 파일을 Python 이 읽고, Python 이 만든 파일을 JS 가 읽는다.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageChops

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jpdf_core as jc  # noqa: E402
from tests.make_fixtures import make_fixtures  # noqa: E402
from tests.test_core import HAVE_RENDERER, _mean_abs_diff, _reference, _render  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NODE_TOOL = ROOT / "chrome-extension" / "tests" / "node_convert.mjs"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node 없음")

JS_OK = ["rgb_baseline.jpg", "rgb_progressive.jpg", "gray.jpg", "cmyk.jpg", "optimized_4_2_0.jpg",
         "exif_o2.jpg", "exif_o3.jpg", "exif_o4.jpg", "exif_o5.jpg", "exif_o6.jpg", "exif_o7.jpg", "exif_o8.jpg",
         "portrait_o6.jpg", "rgb8.png", "gray8.png", "gray1.png", "gray16.png", "palette.png", "palette4bit.png",
         "rgba.png", "gray_alpha.png", "palette_trns.png", "rgb_colorkey.png", "interlaced.png",
         "한글 이름 (테스트).jpg", "tiny_1x1.png"]


@pytest.fixture(scope="session")
def fixtures(tmp_path_factory) -> dict[str, Path]:
    return make_fixtures(tmp_path_factory.mktemp("fixtures_js"))


def js(*args) -> str:
    r = subprocess.run([NODE, str(NODE_TOOL), *map(str, args)], capture_output=True, text=True, check=True)
    return r.stdout.strip()


def _same_pixels(a: Path, b: Path) -> bool:
    ia, ib = Image.open(a), Image.open(b)
    ia.load(); ib.load()
    if ia.size != ib.size:
        return False
    if ia.mode in ("RGBA", "LA", "PA") or "transparency" in ia.info or ib.mode in ("RGBA", "LA"):
        return ImageChops.difference(ia.convert("RGBA"), ib.convert("RGBA")).getbbox() is None
    if ia.mode == "I;16":
        return ia.tobytes() == ib.convert("I;16").tobytes()
    return ImageChops.difference(ia.convert("RGB"), ib.convert("RGB")).getbbox() is None


def test_js_convert_python_reads(fixtures, tmp_path):
    srcs = [fixtures[n] for n in JS_OK]
    out = tmp_path / "js.jpdf"
    rep = json.loads(js("convert", out, *srcs))
    assert rep["pages"] == len(srcs) and rep["allLossless"]
    info = jc.inspect_pdf(out)
    assert info.is_jpdf and info.pages == len(srcs) and [i["name"] for i in info.images] == [s.name for s in srcs]
    if shutil.which("qpdf"):
        r = subprocess.run(["qpdf", "--check", str(out)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
    res = jc.extract_images(out, tmp_path / "ex")
    assert len(res) == len(srcs)
    for src, r in zip(srcs, res):
        if src.suffix == ".jpg":
            assert r.method == "verbatim" and r.bit_exact is True and r.path.read_bytes() == src.read_bytes(), src.name
        else:
            assert r.method == "rebuilt-png" and _same_pixels(src, r.path), src.name


def test_python_convert_js_reads(fixtures, tmp_path):
    srcs = [fixtures[n] for n in JS_OK]
    out = tmp_path / "py.jpdf"
    jc.convert_images(srcs, out)
    info = json.loads(js("info", out))
    assert info["isJpdf"] and info["pages"] == len(srcs) and len(info["images"]) == len(srcs)
    res = json.loads(js("extract", out, tmp_path / "ex"))
    assert len(res) == len(srcs)
    for src, r in zip(srcs, res):
        got = tmp_path / "ex" / r["name"]
        assert got.exists(), r
        if src.suffix == ".jpg":
            assert r["method"] == "verbatim" and r["bitExact"] is True
            assert hashlib.sha256(got.read_bytes()).hexdigest() == hashlib.sha256(src.read_bytes()).hexdigest()
        else:
            assert r["method"] == "rebuilt-png" and _same_pixels(src, got), src.name


def test_js_convert_js_reads_and_embed(fixtures, tmp_path):
    srcs = [fixtures[n] for n in ("rgba.png", "rgb_baseline.jpg", "palette_trns.png")]
    out = tmp_path / "emb.jpdf"
    js("convert", out, "--embed", *srcs)
    res = json.loads(js("extract", out, tmp_path / "ex"))
    for src, r in zip(srcs, res):
        assert r["method"] == "attachment" and r["bitExact"] is True
        assert (tmp_path / "ex" / r["name"]).read_bytes() == src.read_bytes()
    # Python 도 첨부를 읽는다
    pres = jc.extract_images(out, tmp_path / "ex2")
    assert all(p.method == "attachment" and p.bit_exact for p in pres)


@pytest.mark.skipif(not HAVE_RENDERER, reason="렌더러 없음")
@pytest.mark.parametrize("name", JS_OK)
def test_js_render_matches_source(fixtures, tmp_path, name):
    if name == "tiny_1x1.png":
        pytest.skip("최소 페이지 3pt")
    src = fixtures[name]
    out = tmp_path / (Path(name).stem + ".pdf")
    js("convert", out, "--no-image-dpi", "--dpi", "72", src)
    page = _render(out, 72)[0]
    ref = _reference(src, jc.analyze_image(src).orientation)
    assert page.size == ref.size
    mad = _mean_abs_diff(page, ref)
    tol = 2.5 if src.suffix == ".jpg" else 0.5
    if name == "cmyk.jpg":
        tol = 40
    assert mad <= tol, f"{name}: {mad:.3f}"


def test_js_layout_a4(fixtures, tmp_path):
    from pypdf import PdfReader
    out = tmp_path / "a4.pdf"
    js("convert", out, "--page", "A4", "--margin", "10", fixtures["wide_4000x300.jpg"])
    box = PdfReader(str(out)).pages[0].mediabox
    assert (round(float(box.width), 3), round(float(box.height), 3)) == (841.89, 595.276)


def test_js_extract_foreign_pdf(tmp_path):
    im = Image.new("RGB", (40, 30), (10, 200, 30))
    pdf = tmp_path / "pil.pdf"
    im.save(pdf, "PDF")
    res = json.loads(js("extract", pdf, tmp_path / "ex"))
    assert res and res[0]["method"] in ("verbatim", "rebuilt-png")
    got = Image.open(tmp_path / "ex" / res[0]["name"]).convert("RGB")
    assert got.size == (40, 30) and got.getpixel((5, 5)) == (10, 200, 30)
