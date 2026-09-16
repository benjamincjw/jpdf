# -*- coding: utf-8 -*-
"""GUI 스모크 테스트 (Xvfb 등 가상 디스플레이에서 실행).

python tests/gui_smoke.py <fixtures_dir> <shots_dir>
  1) 메인 창을 띄우고 이미지들을 추가 → 스크린샷
  2) 변환 실행 → 완료 상태 스크린샷
  3) 추출 탭에서 방금 만든 jpdf 정보 로드 + 추출 → 스크린샷
  4) 열기 선택창 → 스크린샷
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as jpdf_app  # noqa: E402
import jpdf_core as jc  # noqa: E402


def shot(name: str, shots: Path) -> None:
    subprocess.run(["import", "-window", "root", str(shots / name)], check=False)


def pump(root, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def main() -> int:
    fixtures = Path(sys.argv[1])
    shots = Path(sys.argv[2])
    shots.mkdir(parents=True, exist_ok=True)
    files = [fixtures / n for n in ("rgb_baseline.jpg", "portrait_o6.jpg", "rgba.png", "palette.png",
                                    "한글 이름 (테스트).jpg", "gray16.png")]

    root = jpdf_app.make_root()
    win = jpdf_app.MainWindow(root)
    pump(root, 0.5)
    win.add_files(files)
    pump(root, 1.5)
    win.tree.selection_set(str(id(win.items[1])))
    pump(root, 1.0)
    shot("1_main_list.png", shots)

    out = shots / "smoke.jpdf"
    if out.exists():
        out.unlink()
    win.out_var.set(str(out))
    win.do_convert()
    for _ in range(100):
        pump(root, 0.1)
        if win._last_output is not None:
            break
    assert win._last_output == out and out.exists(), "변환 실패"
    pump(root, 0.3)
    shot("2_converted.png", shots)
    info = jc.inspect_pdf(out)
    assert info.is_jpdf and info.pages == len(files), info.summary()

    win.nb.select(win.tab_ext)
    win.pdf_var.set(str(out))
    win._load_pdf_info()
    for _ in range(50):
        pump(root, 0.1)
        if win.extract_btn.instate(["!disabled"]):
            break
    win.outdir_var.set(str(shots / "extracted"))
    win.do_extract()
    for _ in range(100):
        pump(root, 0.1)
        if win._result_paths:
            break
    pump(root, 0.5)
    shot("3_extract.png", shots)
    assert len(win._result_paths) == len(files), win._result_paths
    root.destroy()

    # 열기 선택창 (mainloop 대신 잠깐 띄우고 캡처)
    import tkinter as tk
    orig_mainloop = tk.Tk.mainloop

    def fake_mainloop(self, n=0):
        pump(self, 1.0)
        shot("4_open_chooser.png", shots)
        self.destroy()

    tk.Tk.mainloop = fake_mainloop
    try:
        jpdf_app.show_open_chooser(out)
    finally:
        tk.Tk.mainloop = orig_mainloop
    print("GUI smoke OK:", sorted(p.name for p in shots.glob("*.png")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
