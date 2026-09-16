#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py — JPDF 데스크톱 앱 (tkinter)

사용법
  python app.py                         변환기 창 열기
  python app.py 파일.jpdf               "PDF로 열기 / 이미지로 열기" 선택창
  python app.py --open-as pdf 파일.jpdf 바로 PDF 뷰어로 열기
  python app.py --open-as image 파일.jpdf 바로 이미지 뷰어로 열기
  python app.py --register              (Windows) 현재 사용자에 .jpdf 연결 프로그램 등록
  python app.py --unregister            (Windows) 연결 해제
  python app.py 사진1.jpg 사진2.png     변환기 창을 열면서 이미지를 미리 추가
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from typing import Callable, Optional

APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
sys.path.insert(0, str(APP_DIR))

import jpdf_core as jc  # noqa: E402

APP_NAME = "JPDF"
APP_TITLE = "JPDF — 이미지 ↔ PDF 변환기"
IMAGE_FILETYPES = [
    ("이미지 파일", "*.jpg *.jpeg *.jpe *.jfif *.png *.bmp *.gif *.tif *.tiff *.webp"),
    ("JPEG", "*.jpg *.jpeg *.jpe *.jfif"), ("PNG", "*.png"), ("모든 파일", "*.*"),
]
PDF_FILETYPES = [("PDF / JPDF", "*.jpdf *.pdf"), ("JPDF", "*.jpdf"), ("PDF", "*.pdf"), ("모든 파일", "*.*")]
PAGE_CHOICES = [("image", "이미지 크기 그대로"), ("A4", "A4"), ("A3", "A3"), ("A5", "A5"),
                ("B5", "B5"), ("Letter", "Letter"), ("Legal", "Legal")]
ORIENT_CHOICES = [("auto", "자동 (이미지에 맞춤)"), ("portrait", "세로"), ("landscape", "가로")]

DEFAULT_SETTINGS = {
    "open_mode": "ask",          # ask | pdf | image
    "output_format": "jpdf",     # jpdf | pdf
    "page_size": "image", "orientation": "auto", "margin_mm": 0.0,
    "default_dpi": 96.0, "use_image_dpi": True, "fit_upscale": True, "embed_originals": False,
    "last_dir": "",
}


# ---------------------------------------------------------------------------
# 설정 / 경로
# ---------------------------------------------------------------------------
def asset_path(name: str) -> Path:
    return APP_DIR / "assets" / name


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    d = base / "jpdf"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update(json.loads((config_dir() / "settings.json").read_text("utf-8")))
    except Exception:  # noqa: BLE001
        pass
    return s


def save_settings(s: dict) -> None:
    try:
        (config_dir() / "settings.json").write_text(json.dumps(s, ensure_ascii=False, indent=2), "utf-8")
    except Exception:  # noqa: BLE001
        pass


def open_folder_of(path: Path) -> None:
    """탐색기에서 파일이 있는 폴더를 열고 파일을 선택한다."""
    try:
        if sys.platform == "win32":
            if path.is_file():
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)] if path.is_file() else ["open", str(path)])
        else:
            jc.open_with_default_app(path.parent if path.is_file() else path)
    except Exception:  # noqa: BLE001
        pass


def shift_pressed() -> bool:
    if sys.platform == "win32":
        try:
            import ctypes
            return bool(ctypes.windll.user32.GetAsyncKeyState(0x10) & 0x8000)
        except Exception:  # noqa: BLE001
            return False
    return False


# ---------------------------------------------------------------------------
# Windows 파일 연결 (HKCU — 관리자 권한 불필요)
# ---------------------------------------------------------------------------
PROGID = "JPDF.Document"


def _launch_prefix() -> str:
    """이 앱을 실행하는 명령의 앞부분 (exe 또는 pythonw + app.py)."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    interp = pyw if pyw.exists() else py
    return f'"{interp}" "{Path(__file__).resolve()}"'


def register_file_association() -> str:
    if sys.platform != "win32":
        raise jc.JpdfError("파일 연결 등록은 Windows에서만 지원합니다.")
    import winreg
    prefix = _launch_prefix()
    icon = asset_path("jpdf.ico")
    icon_value = f'"{icon}",0' if icon.exists() else ""
    hk = winreg.HKEY_CURRENT_USER
    with winreg.CreateKey(hk, r"Software\Classes\.jpdf") as k:
        winreg.SetValue(k, "", winreg.REG_SZ, PROGID)
        winreg.SetValueEx(k, "Content Type", 0, winreg.REG_SZ, "application/x-jpdf")
        winreg.SetValueEx(k, "PerceivedType", 0, winreg.REG_SZ, "document")
    with winreg.CreateKey(hk, rf"Software\Classes\{PROGID}") as k:
        winreg.SetValue(k, "", winreg.REG_SZ, "JPDF 문서 (PDF + 이미지)")
        winreg.SetValueEx(k, "FriendlyTypeName", 0, winreg.REG_SZ, "JPDF 문서")
    if icon_value:
        with winreg.CreateKey(hk, rf"Software\Classes\{PROGID}\DefaultIcon") as k:
            winreg.SetValue(k, "", winreg.REG_SZ, icon_value)
    verbs = [("open", "열기 (선택창)", ""), ("openpdf", "PDF로 열기", "--open-as pdf "),
             ("openimage", "이미지(JPG/PNG)로 열기", "--open-as image "),
             ("openapp", "JPDF 앱에서 열기", "--app ")]
    for verb, label, args in verbs:
        with winreg.CreateKey(hk, rf"Software\Classes\{PROGID}\shell\{verb}") as k:
            winreg.SetValue(k, "", winreg.REG_SZ, label)
            winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, label)
            if icon_value:
                winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, icon_value)
        with winreg.CreateKey(hk, rf"Software\Classes\{PROGID}\shell\{verb}\command") as k:
            winreg.SetValue(k, "", winreg.REG_SZ, f'{prefix} {args}"%1"')
    # 앱 자체를 "연결 프로그램" 목록에 등록
    with winreg.CreateKey(hk, r"Software\Classes\Applications\JPDF.exe\shell\open\command") as k:
        winreg.SetValue(k, "", winreg.REG_SZ, f'{prefix} "%1"')
    with winreg.CreateKey(hk, r"Software\Classes\.jpdf\OpenWithProgids") as k:
        winreg.SetValueEx(k, PROGID, 0, winreg.REG_NONE, b"")
    _notify_assoc_changed()
    return f"등록 완료.\n명령: {prefix} \"%1\""


def _delete_key_tree(root, sub: str) -> None:
    import winreg
    try:
        with winreg.OpenKey(root, sub, 0, winreg.KEY_ALL_ACCESS) as k:
            while True:
                try:
                    child = winreg.EnumKey(k, 0)
                except OSError:
                    break
                _delete_key_tree(root, sub + "\\" + child)
        winreg.DeleteKey(root, sub)
    except FileNotFoundError:
        pass


def unregister_file_association() -> str:
    if sys.platform != "win32":
        raise jc.JpdfError("파일 연결 해제는 Windows에서만 지원합니다.")
    import winreg
    hk = winreg.HKEY_CURRENT_USER
    for sub in (r"Software\Classes\.jpdf", rf"Software\Classes\{PROGID}",
                r"Software\Classes\Applications\JPDF.exe"):
        _delete_key_tree(hk, sub)
    _notify_assoc_changed()
    return "연결을 해제했습니다."


def _notify_assoc_changed() -> None:
    try:
        import ctypes
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)  # SHCNE_ASSOCCHANGED
    except Exception:  # noqa: BLE001
        pass


def is_registered() -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\.jpdf") as k:
            return winreg.QueryValue(k, "") == PROGID
    except OSError:
        return False


# ---------------------------------------------------------------------------
# tkinter 공통
# ---------------------------------------------------------------------------
def _enable_hidpi() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass


def make_root():
    """tkinterdnd2가 있으면 드래그 앤 드롭이 되는 루트를 만든다."""
    _enable_hidpi()
    try:
        from tkinterdnd2 import TkinterDnD
        root = TkinterDnD.Tk()
        root.dnd_available = True  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        import tkinter as tk
        root = tk.Tk()
        root.dnd_available = False  # type: ignore[attr-defined]
    _apply_style(root)
    _set_icon(root)
    return root


def ui_scale(root) -> float:
    """고해상도(125%/150%/200%) 화면 배율. Tk가 DPI 인식 모드면 winfo_fpixels('1i')가 실제 DPI를 준다."""
    try:
        return max(1.0, min(3.0, float(root.winfo_fpixels("1i")) / 96.0))
    except Exception:  # noqa: BLE001
        return 1.0


def _apply_style(root) -> None:
    from tkinter import ttk
    style = ttk.Style(root)
    ui = ui_scale(root)
    themes = style.theme_names()
    for t in ("vista", "winnative", "clam"):
        if t in themes:
            style.theme_use(t)
            break
    try:
        default_font = ("Malgun Gothic", 10) if sys.platform == "win32" else ("TkDefaultFont", 10)
        root.option_add("*Font", default_font)
        style.configure(".", font=default_font)
        style.configure("Treeview", rowheight=int(24 * ui))
        style.configure("Big.TButton", font=(default_font[0], 11, "bold"), padding=(int(14 * ui), int(8 * ui)))
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Ok.TLabel", foreground="#1b7f3b")
        style.configure("Err.TLabel", foreground="#b3261e")
    except Exception:  # noqa: BLE001
        pass


def _set_icon(win) -> None:
    try:
        if sys.platform == "win32" and asset_path("jpdf.ico").exists():
            win.iconbitmap(default=str(asset_path("jpdf.ico")))
        png = asset_path("jpdf.png")
        if png.exists():
            import tkinter as tk
            img = tk.PhotoImage(file=str(png))
            win.iconphoto(True, img)
            win._icon_ref = img  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def _center(win, w: int, h: int) -> None:
    win.update_idletasks()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")


def _parse_dropped(root, data: str) -> list[Path]:
    try:
        parts = root.tk.splitlist(data)
    except Exception:  # noqa: BLE001
        parts = [data]
    return [Path(p) for p in parts if p]


class BackgroundRunner:
    """작업 스레드 ↔ tkinter 메인 루프 사이의 메시지 큐."""

    def __init__(self, widget):
        self.widget = widget
        self.q: queue.Queue = queue.Queue()
        self._after_id = self.widget.after(60, self._poll)
        self.widget.bind("<Destroy>", self._on_destroy, add="+")

    def _on_destroy(self, event):
        if event.widget is self.widget and self._after_id:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:  # noqa: BLE001
                pass
            self._after_id = None

    def run(self, fn: Callable, on_done: Callable, on_error: Callable, on_progress: Optional[Callable] = None):
        def progress(i, n, msg):
            if on_progress:
                self.q.put(("progress", on_progress, (i, n, msg)))

        def worker():
            try:
                result = fn(progress)
                self.q.put(("done", on_done, result))
            except Exception as e:  # noqa: BLE001
                self.q.put(("error", on_error, (e, traceback.format_exc())))

        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, cb, payload = self.q.get_nowait()
                try:
                    if kind == "progress":
                        cb(*payload)
                    elif kind == "done":
                        cb(payload)
                    else:
                        cb(*payload)
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
        except queue.Empty:
            pass
        try:
            self._after_id = self.widget.after(60, self._poll)
        except Exception:  # noqa: BLE001
            self._after_id = None


def _thumbnail(path: Path, box: int, orientation: int = 1):
    from PIL import Image
    im = Image.open(path)
    if im.format == "JPEG":
        im.draft("RGB", (box * 2, box * 2))
    im.load()
    if im.mode not in ("RGB", "RGBA", "L", "LA"):
        im = im.convert("RGBA" if ("transparency" in im.info or im.mode == "PA") else "RGB")
    if orientation != 1:
        im = jc.apply_orientation(im, orientation)
    im.thumbnail((box, box))
    return im


def _checker_composite(im, bg=(238, 238, 238)):
    from PIL import Image
    if im.mode in ("RGBA", "LA"):
        base = Image.new("RGBA", im.size, (*bg, 255))
        return Image.alpha_composite(base, im.convert("RGBA")).convert("RGB")
    return im.convert("RGB")


# ---------------------------------------------------------------------------
# 메인 창
# ---------------------------------------------------------------------------
class ImageItem:
    __slots__ = ("path", "analysis", "error")

    def __init__(self, path: Path):
        self.path = path
        self.analysis: Optional[jc.ImageAnalysis] = None
        self.error: str = ""


class MainWindow:
    def __init__(self, root, initial_files: Optional[list[Path]] = None):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.root = root
        self.root.title(APP_TITLE)
        self.ui = ui = ui_scale(self.root)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        _center(self.root, min(int(1120 * ui), sw - 80), min(int(740 * ui), sh - 120))
        self.root.minsize(min(int(900 * ui), sw - 80), min(int(600 * ui), sh - 120))
        self.settings = load_settings()
        self.items: list[ImageItem] = []
        self.bg = BackgroundRunner(self.root)
        self._preview_ref = None
        self._preview_job = None
        self._last_output: Optional[Path] = None
        self._busy = False
        self._build_menu()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if initial_files:
            self.root.after(100, lambda: self.add_files(initial_files))

    # ---- 메뉴 --------------------------------------------------------------
    def _build_menu(self):
        tk = self.tk
        m = tk.Menu(self.root)
        f = tk.Menu(m, tearoff=False)
        f.add_command(label="이미지 추가…", accelerator="Ctrl+O", command=self.choose_images)
        f.add_command(label="PDF / JPDF 열기…", command=self.choose_pdf)
        f.add_separator()
        f.add_command(label="종료", command=self._on_close)
        m.add_cascade(label="파일", menu=f)

        t = tk.Menu(m, tearoff=False)
        self.open_mode_var = tk.StringVar(value=self.settings.get("open_mode", "ask"))
        t.add_command(label=".jpdf 파일을 이 앱에 연결 (더블클릭하면 열기 선택창)", command=self.do_register)
        t.add_command(label=".jpdf 연결 해제", command=self.do_unregister)
        t.add_separator()
        sub = tk.Menu(t, tearoff=False)
        for val, label in (("ask", "매번 묻기"), ("pdf", "항상 PDF로 열기"), ("image", "항상 이미지로 열기")):
            sub.add_radiobutton(label=label, value=val, variable=self.open_mode_var, command=self._save_open_mode)
        t.add_cascade(label=".jpdf 열기 방식", menu=sub)
        t.add_separator()
        t.add_command(label="캐시 폴더 열기", command=lambda: jc.open_with_default_app(jc.cache_dir()))
        t.add_command(label="캐시 비우기", command=self.do_clear_cache)
        m.add_cascade(label="도구", menu=t)

        h = tk.Menu(m, tearoff=False)
        h.add_command(label="JPDF 형식이란?", command=self.show_format_info)
        h.add_command(label="정보", command=self.show_about)
        m.add_cascade(label="도움말", menu=h)
        self.root.config(menu=m)
        self.root.bind("<Control-o>", lambda e: self.choose_images())

    # ---- UI ----------------------------------------------------------------
    def _build_ui(self):
        ttk = self.ttk
        bar = ttk.Frame(self.root)
        bar.pack(side="bottom", fill="x", padx=12, pady=4)
        dnd = "파일을 창에 끌어다 놓아도 됩니다." if self.root.dnd_available else \
            "드래그 앤 드롭을 쓰려면  pip install tkinterdnd2"
        ttk.Label(bar, text=dnd, style="Hint.TLabel").pack(side="left")
        ttk.Label(bar, text=f"JPDF {jc.JPDF_VERSION}", style="Hint.TLabel").pack(side="right")

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(side="top", fill="both", expand=True, padx=8, pady=(8, 0))
        self.tab_conv = ttk.Frame(self.nb, padding=8)
        self.tab_ext = ttk.Frame(self.nb, padding=8)
        self.nb.add(self.tab_conv, text="  이미지 → PDF / JPDF  ")
        self.nb.add(self.tab_ext, text="  PDF / JPDF → 이미지  ")
        self._build_convert_tab()
        self._build_extract_tab()

        if self.root.dnd_available:
            from tkinterdnd2 import DND_FILES
            for w in (self.root, self.tree, self.tab_ext):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self._on_drop)

    def _build_convert_tab(self):
        tk, ttk = self.tk, self.ttk
        s = self.settings
        ui = self.ui
        # 하단(상태 → 변환 행 → 저장 위치)을 먼저 bottom에 붙여 창이 작아도 잘리지 않게 한다
        self.status_var = tk.StringVar(value="이미지를 추가하고 [변환]을 누르세요.")
        self.status_lbl = ttk.Label(self.tab_conv, textvariable=self.status_var, wraplength=int(1000 * ui), justify="left")
        self.status_lbl.pack(side="bottom", anchor="w", pady=(6, 0))
        actf = ttk.Frame(self.tab_conv)
        actf.pack(side="bottom", fill="x", pady=(8, 0))
        outf = ttk.Frame(self.tab_conv)
        outf.pack(side="bottom", fill="x", pady=(8, 0))

        pane = ttk.Panedwindow(self.tab_conv, orient="horizontal")
        pane.pack(side="top", fill="both", expand=True)
        self._pane = pane
        pane.bind("<Configure>", self._on_pane_configure, add="+")

        # 왼쪽: 목록
        left = ttk.Frame(pane)
        pane.add(left, weight=1)
        cols = ("no", "name", "dims", "fmt", "how")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="extended", height=8)
        for c, text, w, anchor in (("no", "#", 34, "e"), ("name", "파일", 180, "w"), ("dims", "크기(px)", 90, "center"),
                                   ("fmt", "형식", 56, "center"), ("how", "저장 방식", 220, "w")):
            self.tree.heading(c, text=text)
            self.tree.column(c, width=int(w * ui), minwidth=int(w * ui * 0.6), anchor=anchor,
                             stretch=(c in ("name", "how")))
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        self.tree.bind("<Double-1>", lambda e: self._open_selected_source())

        btns = ttk.Frame(left)
        btns.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.count_var = tk.StringVar(value="이미지 0장")
        ttk.Label(btns, textvariable=self.count_var, style="Hint.TLabel").pack(side="right")
        ttk.Button(btns, text="추가…", width=7, command=self.choose_images).pack(side="left")
        ttk.Button(btns, text="제거", width=5, command=self.remove_selected).pack(side="left", padx=(4, 0))
        ttk.Button(btns, text="▲", width=3, command=lambda: self.move_selected(-1)).pack(side="left", padx=(4, 0))
        ttk.Button(btns, text="▼", width=3, command=lambda: self.move_selected(1)).pack(side="left", padx=(4, 0))
        ttk.Button(btns, text="이름순", width=6, command=self.sort_items).pack(side="left", padx=(4, 0))
        ttk.Button(btns, text="비우기", width=6, command=self.clear_items).pack(side="left", padx=(4, 0))

        # 오른쪽: 미리보기 + 옵션
        right = ttk.Frame(pane)
        pane.add(right, weight=1)
        # 옵션을 먼저 아래쪽에 고정하고, 남는 공간을 미리보기가 쓴다
        opt = ttk.Labelframe(right, text="출력 옵션", padding=8)
        opt.pack(side="bottom", fill="x", pady=(8, 0))
        pv = ttk.Labelframe(right, text="미리보기", padding=6)
        pv.pack(side="top", fill="both", expand=True)
        self.preview_info = tk.StringVar(value="이미지를 선택하면 여기에 표시됩니다.")
        ttk.Label(pv, textvariable=self.preview_info, style="Hint.TLabel", wraplength=int(380 * ui),
                  justify="left").pack(side="bottom", anchor="w", pady=(4, 0))
        self.preview = tk.Canvas(pv, bg="#e6e6e6", highlightthickness=0, height=int(120 * ui), width=int(360 * ui))
        self.preview.pack(side="top", fill="both", expand=True)
        self.preview.bind("<Configure>", lambda e: self._schedule_preview())
        self.fmt_var = tk.StringVar(value=s.get("output_format", "jpdf"))
        ttk.Label(opt, text="형식").grid(row=0, column=0, sticky="w")
        ff = ttk.Frame(opt)
        ff.grid(row=0, column=1, columnspan=3, sticky="w")
        ttk.Radiobutton(ff, text="JPDF (.jpdf)", value="jpdf", variable=self.fmt_var,
                        command=self._on_format_change).pack(side="left")
        ttk.Radiobutton(ff, text="PDF (.pdf)", value="pdf", variable=self.fmt_var,
                        command=self._on_format_change).pack(side="left", padx=(12, 0))

        ttk.Label(opt, text="페이지").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.page_var = tk.StringVar()
        self.page_cb = ttk.Combobox(opt, state="readonly", width=20, values=[t for _, t in PAGE_CHOICES],
                                    textvariable=self.page_var)
        self.page_cb.grid(row=1, column=1, sticky="w", pady=(6, 0))
        self.page_cb.current([k for k, _ in PAGE_CHOICES].index(s.get("page_size", "image"))
                             if s.get("page_size", "image") in dict(PAGE_CHOICES) else 0)
        self.page_cb.bind("<<ComboboxSelected>>", lambda e: self._on_page_change())

        ttk.Label(opt, text="방향").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.orient_var = tk.StringVar()
        self.orient_cb = ttk.Combobox(opt, state="readonly", width=20, values=[t for _, t in ORIENT_CHOICES],
                                      textvariable=self.orient_var)
        self.orient_cb.grid(row=2, column=1, sticky="w", pady=(6, 0))
        self.orient_cb.current([k for k, _ in ORIENT_CHOICES].index(s.get("orientation", "auto"))
                               if s.get("orientation", "auto") in dict(ORIENT_CHOICES) else 0)

        ttk.Label(opt, text="여백 (mm)").grid(row=3, column=0, sticky="w", pady=(6, 0))
        mf = ttk.Frame(opt)
        mf.grid(row=3, column=1, columnspan=3, sticky="w", pady=(6, 0))
        self.margin_var = tk.StringVar(value=f"{float(s.get('margin_mm', 0)):g}")
        ttk.Spinbox(mf, from_=0, to=100, increment=1, width=6, textvariable=self.margin_var).pack(side="left")
        ttk.Label(mf, text="기본 DPI").pack(side="left", padx=(16, 6))
        self.dpi_var = tk.StringVar(value=str(int(s.get("default_dpi", 96))))
        ttk.Spinbox(mf, from_=10, to=2400, increment=1, width=6, textvariable=self.dpi_var).pack(side="left")

        self.use_dpi_var = tk.BooleanVar(value=bool(s.get("use_image_dpi", True)))
        self.upscale_var = tk.BooleanVar(value=bool(s.get("fit_upscale", True)))
        self.embed_var = tk.BooleanVar(value=bool(s.get("embed_originals", False)))
        ttk.Checkbutton(opt, text="이미지에 저장된 DPI 사용 (없으면 기본 DPI)", variable=self.use_dpi_var).grid(
            row=4, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.upscale_chk = ttk.Checkbutton(opt, text="작은 이미지도 페이지에 맞게 확대", variable=self.upscale_var)
        self.upscale_chk.grid(row=5, column=0, columnspan=4, sticky="w")
        ttk.Checkbutton(opt, text="원본 파일을 첨부파일로도 포함 (용량 ≈ 2배, 메타데이터 완전 보존)",
                        variable=self.embed_var).grid(row=6, column=0, columnspan=4, sticky="w")
        self._on_page_change()

        # 저장 위치 + 변환
        ttk.Label(outf, text="저장 위치").pack(side="left")
        self.out_var = tk.StringVar()
        ttk.Entry(outf, textvariable=self.out_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(outf, text="찾아보기…", command=self.choose_output).pack(side="left")

        self.convert_btn = ttk.Button(actf, text="변환", style="Big.TButton", command=self.do_convert)
        self.convert_btn.pack(side="left")
        self.progress = ttk.Progressbar(actf, mode="determinate", length=int(260 * ui))
        self.progress.pack(side="left", padx=10, fill="x", expand=True)
        self.after_btns = ttk.Frame(actf)
        self.after_btns.pack(side="left")
        self.btn_open_pdf = ttk.Button(self.after_btns, text="PDF로 열기", command=lambda: self._open_last("pdf"))
        self.btn_open_img = ttk.Button(self.after_btns, text="이미지로 열기", command=lambda: self._open_last("image"))
        self.btn_open_dir = ttk.Button(self.after_btns, text="폴더 열기", command=lambda: self._open_last("dir"))

    def _build_extract_tab(self):
        tk, ttk = self.tk, self.ttk
        f = self.tab_ext
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Label(row, text="PDF / JPDF 파일").pack(side="left")
        self.pdf_var = tk.StringVar()
        e = ttk.Entry(row, textvariable=self.pdf_var)
        e.pack(side="left", fill="x", expand=True, padx=6)
        e.bind("<Return>", lambda ev: self._load_pdf_info())
        ttk.Button(row, text="찾아보기…", command=self.choose_pdf).pack(side="left")

        info = ttk.Labelframe(f, text="파일 정보", padding=8)
        info.pack(fill="x", pady=(8, 0))
        self.pdf_thumb = tk.Label(info, bg="#e6e6e6", width=18, height=8)
        self.pdf_thumb.grid(row=0, column=0, rowspan=3, sticky="nw")
        self.pdf_info_var = tk.StringVar(value="파일을 선택하세요.")
        ttk.Label(info, textvariable=self.pdf_info_var, justify="left", wraplength=int(700 * self.ui)).grid(
            row=0, column=1, sticky="nw", padx=(10, 0))
        ob = ttk.Frame(info)
        ob.grid(row=1, column=1, sticky="w", padx=(10, 0), pady=(8, 0))
        self.btn_pdf_open_pdf = ttk.Button(ob, text="PDF로 열기", command=lambda: self._open_pdf_as("pdf"), state="disabled")
        self.btn_pdf_open_pdf.pack(side="left")
        self.btn_pdf_open_img = ttk.Button(ob, text="이미지로 열기", command=lambda: self._open_pdf_as("image"), state="disabled")
        self.btn_pdf_open_img.pack(side="left", padx=(6, 0))
        info.columnconfigure(1, weight=1)

        row2 = ttk.Frame(f)
        row2.pack(fill="x", pady=(8, 0))
        ttk.Label(row2, text="추출 폴더").pack(side="left")
        self.outdir_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.outdir_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row2, text="찾아보기…", command=self.choose_outdir).pack(side="left")

        row3 = ttk.Frame(f)
        row3.pack(fill="x", pady=(8, 0))
        self.extract_btn = ttk.Button(row3, text="이미지 추출", style="Big.TButton", command=self.do_extract, state="disabled")
        self.extract_btn.pack(side="left")
        self.progress2 = ttk.Progressbar(row3, mode="determinate", length=int(260 * self.ui))
        self.progress2.pack(side="left", padx=10, fill="x", expand=True)
        self.btn_outdir_open = ttk.Button(row3, text="추출 폴더 열기", command=lambda: open_folder_of(Path(self.outdir_var.get())))
        self.status2_var = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.status2_var, wraplength=int(1000 * self.ui), justify="left").pack(anchor="w", pady=(6, 0))

        lf = ttk.Labelframe(f, text="추출 결과", padding=4)
        lf.pack(fill="both", expand=True, pady=(8, 0))
        self.result_list = tk.Listbox(lf, activestyle="none")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.result_list.yview)
        self.result_list.configure(yscrollcommand=sb.set)
        self.result_list.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.result_list.bind("<Double-1>", self._open_result_item)
        self._result_paths: list[Path] = []

    # ---- 목록 조작 -----------------------------------------------------------
    def choose_images(self):
        from tkinter import filedialog
        init = self.settings.get("last_dir") or str(Path.home())
        files = filedialog.askopenfilenames(title="이미지 선택", filetypes=IMAGE_FILETYPES, initialdir=init)
        if files:
            self.add_files([Path(f) for f in files])
            self.nb.select(self.tab_conv)

    def add_files(self, paths: list[Path]):
        existing = {i.path.resolve() for i in self.items}
        new: list[ImageItem] = []
        skipped = 0
        for p in paths:
            if p.is_dir():
                for child in sorted(p.iterdir()):
                    if child.suffix.lower() in jc.SUPPORTED_INPUT_EXTENSIONS and child.resolve() not in existing:
                        new.append(ImageItem(child))
                        existing.add(child.resolve())
                continue
            if p.suffix.lower() in (".pdf", ".jpdf"):
                self.pdf_var.set(str(p))
                self.nb.select(self.tab_ext)
                self._load_pdf_info()
                continue
            if not p.is_file():
                continue
            if p.resolve() in existing:
                skipped += 1
                continue
            new.append(ImageItem(p))
            existing.add(p.resolve())
        if not new:
            if skipped:
                self._set_status(f"이미 목록에 있는 파일 {skipped}개는 건너뛰었습니다.")
            return
        self.settings["last_dir"] = str(new[0].path.parent)
        self.items.extend(new)
        for it in new:
            self.tree.insert("", "end", iid=str(id(it)), values=("", it.path.name, "…", "", "분석 중…"))
        self._renumber()
        if not self.out_var.get():
            self._suggest_output()
        self.bg.run(lambda progress: self._analyze_many(new), self._on_analyzed, self._on_bg_error)
        self._set_status(f"이미지 {len(new)}장을 추가했습니다." + (f" (중복 {skipped}개 제외)" if skipped else ""))

    def _analyze_many(self, items: list[ImageItem]):
        for it in items:
            try:
                it.analysis = jc.analyze_image(it.path)
            except Exception as e:  # noqa: BLE001
                it.error = str(e)
        return items

    def _on_analyzed(self, items: list[ImageItem]):
        for it in items:
            iid = str(id(it))
            if not self.tree.exists(iid):
                continue
            if it.analysis:
                a = it.analysis
                w, h = a.display_size
                self.tree.item(iid, values=(self.tree.set(iid, "no"), it.path.name, f"{w}×{h}", a.format,
                                            a.note + ("" if a.lossless else " ⚠")))
            else:
                self.tree.item(iid, values=(self.tree.set(iid, "no"), it.path.name, "?", "?", "오류: " + it.error))
        self._renumber()

    def _renumber(self):
        total = 0
        for i, it in enumerate(self.items, 1):
            iid = str(id(it))
            if self.tree.exists(iid):
                self.tree.set(iid, "no", str(i))
            try:
                total += it.path.stat().st_size
            except OSError:
                pass
        self.count_var.set(f"이미지 {len(self.items)}장 · {jc.human_size(total)}")

    def _selected_items(self) -> list[ImageItem]:
        sel = set(self.tree.selection())
        return [it for it in self.items if str(id(it)) in sel]

    def remove_selected(self):
        for it in self._selected_items():
            self.items.remove(it)
            self.tree.delete(str(id(it)))
        self._renumber()
        self._schedule_preview()

    def move_selected(self, delta: int):
        sel = self._selected_items()
        if not sel:
            return
        idxs = [self.items.index(it) for it in sel]
        order = sorted(idxs, reverse=(delta > 0))
        for i in order:
            j = i + delta
            if j < 0 or j >= len(self.items) or self.items[j] in sel:
                continue
            self.items[i], self.items[j] = self.items[j], self.items[i]
        for i, it in enumerate(self.items):
            self.tree.move(str(id(it)), "", i)
        self._renumber()

    def sort_items(self):
        self.items.sort(key=lambda it: it.path.name.lower())
        for i, it in enumerate(self.items):
            self.tree.move(str(id(it)), "", i)
        self._renumber()

    def clear_items(self):
        self.items.clear()
        self.tree.delete(*self.tree.get_children())
        self._renumber()
        self._schedule_preview()

    def _open_selected_source(self):
        sel = self._selected_items()
        if sel:
            jc.open_with_default_app(sel[0].path)

    def _on_drop(self, event):
        paths = _parse_dropped(self.root, event.data)
        if paths:
            self.add_files(paths)

    def _on_pane_configure(self, event):
        if getattr(self, "_sash_done", False) or event.width < 400:
            return
        self._sash_done = True
        try:
            self._pane.sashpos(0, int(event.width * 0.56))
        except Exception:  # noqa: BLE001
            pass

    # ---- 미리보기 -----------------------------------------------------------
    def _on_select(self, _event=None):
        self._schedule_preview()

    def _schedule_preview(self):
        if self._preview_job:
            self.root.after_cancel(self._preview_job)
        self._preview_job = self.root.after(120, self._update_preview)

    def _update_preview(self):
        self._preview_job = None
        sel = self._selected_items()
        self.preview.delete("all")
        if not sel:
            self._preview_ref = None
            self.preview_info.set("이미지를 선택하면 여기에 표시됩니다." if self.items else "")
            return
        it = sel[0]
        w = max(64, self.preview.winfo_width() - 8)
        h = max(64, self.preview.winfo_height() - 8)
        orientation = it.analysis.orientation if it.analysis else 1

        def work(progress):
            return _checker_composite(_thumbnail(it.path, max(w, h), orientation))

        def done(im):
            from PIL import ImageTk
            if self._selected_items()[:1] != [it]:
                return
            im.thumbnail((w, h))
            self._preview_ref = ImageTk.PhotoImage(im)
            self.preview.delete("all")
            self.preview.create_image(self.preview.winfo_width() // 2, self.preview.winfo_height() // 2,
                                      image=self._preview_ref, anchor="center")
            a = it.analysis
            if a:
                dpi = f", {a.dpi[0]:g} dpi" if a.dpi else ""
                ori = f", EXIF 방향 {a.orientation}" if a.orientation != 1 else ""
                self.preview_info.set(f"{it.path.name}\n{a.width}×{a.height} {a.format} {a.mode}{dpi}{ori} · "
                                      f"{jc.human_size(a.file_size)}\n{a.note}")
            else:
                self.preview_info.set(f"{it.path.name}\n{it.error}")

        def err(e, tb):
            self.preview_info.set(f"{it.path.name}\n미리보기를 만들 수 없습니다: {e}")

        self.bg.run(work, done, err)

    # ---- 옵션 ---------------------------------------------------------------
    def _page_key(self) -> str:
        return PAGE_CHOICES[self.page_cb.current()][0]

    def _orient_key(self) -> str:
        return ORIENT_CHOICES[self.orient_cb.current()][0]

    def _on_page_change(self):
        fixed = self._page_key() != "image"
        self.orient_cb.configure(state="readonly" if fixed else "disabled")
        self.upscale_chk.configure(state="normal" if fixed else "disabled")

    def _on_format_change(self):
        out = self.out_var.get().strip()
        if out:
            p = Path(out)
            self.out_var.set(str(p.with_suffix("." + self.fmt_var.get())))

    def _suggest_output(self):
        if not self.items:
            return
        first = self.items[0].path
        name = first.stem if len(self.items) == 1 else f"{first.stem} 외 {len(self.items) - 1}장"
        cand = first.parent / f"{name}.{self.fmt_var.get()}"
        k = 2
        while cand.exists():
            cand = first.parent / f"{name} ({k}).{self.fmt_var.get()}"
            k += 1
        self.out_var.set(str(cand))

    def choose_output(self):
        from tkinter import filedialog
        cur = Path(self.out_var.get()) if self.out_var.get() else None
        ext = "." + self.fmt_var.get()
        p = filedialog.asksaveasfilename(
            title="저장 위치", defaultextension=ext,
            filetypes=[("JPDF 파일", "*.jpdf"), ("PDF 파일", "*.pdf")] if ext == ".jpdf" else [("PDF 파일", "*.pdf"), ("JPDF 파일", "*.jpdf")],
            initialdir=str(cur.parent) if cur else self.settings.get("last_dir") or str(Path.home()),
            initialfile=cur.name if cur else "")
        if p:
            self.out_var.set(p)
            if p.lower().endswith(".pdf"):
                self.fmt_var.set("pdf")
            elif p.lower().endswith(".jpdf"):
                self.fmt_var.set("jpdf")

    def _collect_options(self) -> jc.LayoutOptions:
        try:
            margin = float(self.margin_var.get() or 0)
            dpi = float(self.dpi_var.get() or 96)
        except ValueError as e:
            raise jc.JpdfError("여백과 DPI는 숫자여야 합니다.") from e
        opt = jc.LayoutOptions(page_size=self._page_key(), orientation=self._orient_key(), margin_mm=margin,
                               default_dpi=dpi, use_image_dpi=self.use_dpi_var.get(),
                               fit_upscale=self.upscale_var.get(), embed_originals=self.embed_var.get())
        opt.validate()
        return opt

    def _persist_options(self, opt: jc.LayoutOptions):
        self.settings.update(output_format=self.fmt_var.get(), page_size=opt.page_size, orientation=opt.orientation,
                             margin_mm=opt.margin_mm, default_dpi=opt.default_dpi, use_image_dpi=opt.use_image_dpi,
                             fit_upscale=opt.fit_upscale, embed_originals=opt.embed_originals)
        save_settings(self.settings)

    # ---- 변환 ---------------------------------------------------------------
    def do_convert(self):
        from tkinter import messagebox
        if self._busy:
            return
        if not self.items:
            messagebox.showinfo(APP_NAME, "먼저 이미지를 추가하세요.")
            return
        bad = [it for it in self.items if it.error]
        if bad:
            messagebox.showerror(APP_NAME, "열 수 없는 파일이 있습니다:\n" + "\n".join(it.path.name for it in bad[:10]))
            return
        out = self.out_var.get().strip()
        if not out:
            self._suggest_output()
            out = self.out_var.get()
        outp = Path(out)
        if outp.suffix.lower() not in (".pdf", ".jpdf"):
            outp = outp.with_suffix("." + self.fmt_var.get())
            self.out_var.set(str(outp))
        if outp.exists() and not messagebox.askyesno(APP_NAME, f"{outp.name} 파일이 이미 있습니다. 덮어쓸까요?"):
            return
        try:
            opt = self._collect_options()
        except jc.JpdfError as e:
            messagebox.showerror(APP_NAME, str(e))
            return
        self._persist_options(opt)
        srcs = [it.path for it in self.items]
        self._set_busy(True)
        self.progress.configure(maximum=len(srcs), value=0)
        for b in (self.btn_open_pdf, self.btn_open_img, self.btn_open_dir):
            b.pack_forget()

        def work(progress):
            return jc.convert_images(srcs, outp, opt, progress=progress)

        def on_progress(i, n, msg):
            self.progress.configure(value=i)
            self._set_status(f"[{i}/{n}] {msg}")

        def done(rep: jc.ConvertReport):
            self._set_busy(False)
            self._last_output = rep.output
            loss = "" if rep.all_lossless else "  ⚠ 일부 이미지는 형식 변환(색공간 등)이 있었습니다."
            stored = sum(r.bytes_stored for r in rep.images)
            self._set_status(f"완료: {rep.output.name} — {rep.pages}페이지, {jc.human_size(rep.bytes_out)} "
                             f"(이미지 데이터 {jc.human_size(stored)}), {rep.elapsed:.1f}초{loss}", ok=True)
            self.btn_open_pdf.pack(side="left")
            self.btn_open_img.pack(side="left", padx=(6, 0))
            self.btn_open_dir.pack(side="left", padx=(6, 0))
            self.progress.configure(value=self.progress["maximum"])

        def err(e, tb):
            self._set_busy(False)
            self._set_status(f"실패: {e}", err=True)
            messagebox.showerror(APP_NAME, f"변환에 실패했습니다.\n\n{e}" + ("" if isinstance(e, jc.JpdfError) else f"\n\n{tb}"))

        self.bg.run(work, done, err, on_progress)

    def _open_last(self, how: str):
        if not self._last_output or not self._last_output.exists():
            return
        try:
            if how == "pdf":
                jc.open_as_pdf(self._last_output)
            elif how == "image":
                jc.open_as_images(self._last_output)
            else:
                open_folder_of(self._last_output)
        except Exception as e:  # noqa: BLE001
            from tkinter import messagebox
            messagebox.showerror(APP_NAME, str(e))

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.convert_btn.configure(state="disabled" if busy else "normal")
        self.extract_btn.configure(state="disabled" if busy or not self.pdf_var.get() else "normal")

    def _set_status(self, text: str, ok: bool = False, err: bool = False):
        self.status_var.set(text)
        self.status_lbl.configure(style="Ok.TLabel" if ok else ("Err.TLabel" if err else "TLabel"))

    # ---- 추출 탭 ------------------------------------------------------------
    def choose_pdf(self):
        from tkinter import filedialog
        init = self.settings.get("last_dir") or str(Path.home())
        p = filedialog.askopenfilename(title="PDF / JPDF 선택", filetypes=PDF_FILETYPES, initialdir=init)
        if p:
            self.pdf_var.set(p)
            self.nb.select(self.tab_ext)
            self._load_pdf_info()

    def choose_outdir(self):
        from tkinter import filedialog
        p = filedialog.askdirectory(title="추출 폴더", initialdir=self.outdir_var.get() or str(Path.home()))
        if p:
            self.outdir_var.set(p)

    def _load_pdf_info(self):
        p = Path(self.pdf_var.get().strip().strip('"'))
        self.result_list.delete(0, "end")
        self._result_paths = []
        self.pdf_thumb.configure(image="", text="")
        self._pdf_thumb_ref = None
        if not p.is_file():
            self.pdf_info_var.set("파일을 찾을 수 없습니다.")
            self.extract_btn.configure(state="disabled")
            return
        self.settings["last_dir"] = str(p.parent)
        self.pdf_info_var.set("읽는 중…")
        if not self.outdir_var.get():
            self.outdir_var.set(str(p.parent / f"{p.stem}_images"))

        def work(progress):
            info = jc.inspect_pdf(p)
            thumb = jc.first_page_thumbnail(p, int(140 * self.ui)) if info.is_pdf else None
            return info, thumb

        def done(res):
            from PIL import ImageTk
            info, thumb = res
            if not info.is_pdf:
                self.pdf_info_var.set("PDF 파일이 아닙니다.")
                self.extract_btn.configure(state="disabled")
                return
            lines = [f"{p.name}", info.summary()]
            if info.is_jpdf:
                lossless = all(i.get("lossless", True) for i in info.images)
                lines.append("원본 이미지: " + ("모두 무손실로 저장됨" if lossless else "일부 형식 변환됨") +
                             (" · 원본 파일 첨부됨" if info.has_attachments else ""))
                names = [i["name"] for i in info.images][:6]
                if names:
                    lines.append("포함: " + ", ".join(names) + (" …" if len(info.images) > 6 else ""))
            else:
                lines.append("일반 PDF입니다. 페이지에 포함된 이미지를 그대로 꺼낼 수 있습니다.")
            self.pdf_info_var.set("\n".join(lines))
            if thumb is not None:
                self._pdf_thumb_ref = ImageTk.PhotoImage(_checker_composite(thumb))
                self.pdf_thumb.configure(image=self._pdf_thumb_ref, width=thumb.width, height=thumb.height)
            self.extract_btn.configure(state="normal")
            self.btn_pdf_open_pdf.configure(state="normal")
            self.btn_pdf_open_img.configure(state="normal")

        def err(e, tb):
            self.pdf_info_var.set(f"읽을 수 없습니다: {e}")
            self.extract_btn.configure(state="disabled")

        self.bg.run(work, done, err)

    def _open_pdf_as(self, how: str):
        p = Path(self.pdf_var.get().strip())
        if not p.is_file():
            return
        try:
            if how == "pdf":
                jc.open_as_pdf(p)
            else:
                self.status2_var.set("이미지를 꺼내는 중…")
                self.bg.run(lambda progress: jc.open_as_images(p, progress=progress),
                            lambda res: self.status2_var.set(f"이미지 {len(res)}장을 열었습니다. (캐시 폴더: {res[0].path.parent if res else ''})"),
                            lambda e, tb: self.status2_var.set(f"열 수 없습니다: {e}"))
        except Exception as e:  # noqa: BLE001
            from tkinter import messagebox
            messagebox.showerror(APP_NAME, str(e))

    def do_extract(self):
        from tkinter import messagebox
        if self._busy:
            return
        p = Path(self.pdf_var.get().strip())
        outdir = Path(self.outdir_var.get().strip() or (p.parent / f"{p.stem}_images"))
        self._set_busy(True)
        self.result_list.delete(0, "end")
        self._result_paths = []
        self.progress2.configure(value=0, maximum=1)
        self.btn_outdir_open.pack_forget()

        def work(progress):
            return jc.extract_images(p, outdir, progress)

        def on_progress(i, n, msg):
            self.progress2.configure(maximum=max(1, n), value=i)
            self.status2_var.set(msg)

        def done(res: list[jc.ExtractedImage]):
            self._set_busy(False)
            methods = {"attachment": "원본 첨부파일", "verbatim": "JPEG 바이트 그대로", "rebuilt-png": "PNG 재조립(픽셀 동일)",
                       "pypdf": "일반 추출"}
            for r in res:
                tag = methods.get(r.method, r.method)
                if r.bit_exact is True:
                    tag += " · 원본과 동일(SHA-256 일치)"
                elif r.bit_exact is False:
                    tag += " · 원본 파일과는 다름(픽셀은 동일)"
                self.result_list.insert("end", f"p{r.page}  {r.path.name}   [{tag}]")
                self._result_paths.append(r.path)
            self.status2_var.set(f"완료: {len(res)}개 파일 → {outdir}")
            self.btn_outdir_open.pack(side="left")
            self.progress2.configure(value=self.progress2["maximum"])

        def err(e, tb):
            self._set_busy(False)
            self.status2_var.set(f"실패: {e}")
            messagebox.showerror(APP_NAME, f"추출에 실패했습니다.\n\n{e}" + ("" if isinstance(e, jc.JpdfError) else f"\n\n{tb}"))

        self.bg.run(work, done, err, on_progress)

    def _open_result_item(self, _event=None):
        sel = self.result_list.curselection()
        if sel and sel[0] < len(self._result_paths):
            jc.open_with_default_app(self._result_paths[sel[0]])

    # ---- 도구/도움말 ----------------------------------------------------------
    def do_register(self):
        from tkinter import messagebox
        try:
            msg = register_file_association()
            messagebox.showinfo(APP_NAME, ".jpdf 파일을 이 앱에 연결했습니다.\n이제 .jpdf를 더블클릭하면 'PDF로 열기 / 이미지로 열기'를 고를 수 있습니다.\n\n" + msg)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"등록 실패: {e}")

    def do_unregister(self):
        from tkinter import messagebox
        try:
            messagebox.showinfo(APP_NAME, unregister_file_association())
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"해제 실패: {e}")

    def _save_open_mode(self):
        self.settings["open_mode"] = self.open_mode_var.get()
        save_settings(self.settings)

    def do_clear_cache(self):
        from tkinter import messagebox
        n = jc.clear_cache()
        messagebox.showinfo(APP_NAME, f"캐시 항목 {n}개를 지웠습니다.")

    def show_format_info(self):
        from tkinter import messagebox
        messagebox.showinfo("JPDF 형식", FORMAT_INFO)

    def show_about(self):
        from tkinter import messagebox
        dnd = "사용 가능" if self.root.dnd_available else "미설치"
        try:
            disp = (f"화면 {self.root.winfo_screenwidth()}×{self.root.winfo_screenheight()} px, "
                    f"{self.root.winfo_fpixels('1i'):.0f} dpi (배율 {self.ui:.2f}), 창 {self.root.winfo_geometry()}")
        except Exception:  # noqa: BLE001
            disp = ""
        messagebox.showinfo("정보", f"{APP_TITLE}\n형식 버전 {jc.JPDF_VERSION}\n\n"
                                  f"Python {sys.version.split()[0]} · Tk {self.tk.TkVersion} · 드래그 앤 드롭: {dnd}\n"
                                  f"{disp}\n설정: {config_dir() / 'settings.json'}\n캐시: {jc.cache_dir()}")

    def _on_bg_error(self, e, tb):
        self._set_status(f"오류: {e}", err=True)

    def _on_close(self):
        save_settings(self.settings)
        self.root.destroy()


FORMAT_INFO = """JPDF(.jpdf)는 '그 자체로 유효한 PDF'입니다.

• 확장자를 .pdf로 바꾸기만 해도 어떤 PDF 뷰어에서든 열립니다.
• 각 이미지는 페이지 하나에 딱 한 번 저장됩니다.
  - JPEG: 원본 바이트를 재인코딩 없이 그대로 (비트 단위 동일)
  - PNG: 원본 압축 데이터를 그대로, 알파 채널은 SMask로 분리 (픽셀 단위 동일)
• EXIF 회전은 픽셀을 건드리지 않고 페이지 변환 행렬로 반영합니다.
• 문서 카탈로그의 /JPDF 사전에 원본 파일명·형식·SHA-256을 기록하고,
  파일 3번째 줄의 %JPDF-1.0 주석으로 빠르게 식별합니다.
• '이미지로 열기'는 이 구조를 역으로 읽어 JPEG은 바이트 그대로,
  PNG는 컨테이너만 다시 씌워 꺼냅니다 (재압축 없음).
• 옵션으로 원본 파일을 PDF 첨부파일로도 넣을 수 있습니다 (용량 2배).

자세한 내용은 JPDF_FORMAT.md 를 보세요."""


# ---------------------------------------------------------------------------
# .jpdf 열기 선택창
# ---------------------------------------------------------------------------
def show_open_chooser(path: Path) -> None:
    import tkinter as tk
    from tkinter import ttk, messagebox
    root = make_root()
    root.title(f"{path.name} — JPDF")
    root.resizable(False, False)
    frm = ttk.Frame(root, padding=16)
    frm.pack(fill="both", expand=True)

    try:
        info = jc.inspect_pdf(path)
    except Exception as e:  # noqa: BLE001
        root.withdraw()
        messagebox.showerror(APP_NAME, f"파일을 읽을 수 없습니다.\n{e}")
        root.destroy()
        return

    thumb = None
    try:
        thumb = jc.first_page_thumbnail(path, int(160 * ui_scale(root)))
    except Exception:  # noqa: BLE001
        thumb = None
    if thumb is not None:
        from PIL import ImageTk
        ph = ImageTk.PhotoImage(_checker_composite(thumb))
        lbl = tk.Label(frm, image=ph, bg="#e6e6e6", bd=1, relief="solid")
        lbl.image = ph  # type: ignore[attr-defined]
        lbl.grid(row=0, column=0, rowspan=4, sticky="n", padx=(0, 16))
    ttk.Label(frm, text=path.name, font=("Malgun Gothic" if sys.platform == "win32" else "TkDefaultFont", 12, "bold")).grid(
        row=0, column=1, sticky="w")
    ttk.Label(frm, text=info.summary(), style="Hint.TLabel").grid(row=1, column=1, sticky="w", pady=(2, 10))

    remember = tk.BooleanVar(value=False)

    def finish(mode: str):
        if remember.get():
            s = load_settings()
            s["open_mode"] = mode
            save_settings(s)
        root.withdraw()
        try:
            if mode == "pdf":
                jc.open_as_pdf(path)
            elif mode == "image":
                jc.open_as_images(path)
            elif mode == "app":
                root.destroy()
                r2 = make_root()
                w = MainWindow(r2)
                w.pdf_var.set(str(path))
                w.nb.select(w.tab_ext)
                w._load_pdf_info()
                r2.mainloop()
                return
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"열 수 없습니다.\n{e}")
        root.destroy()

    b = ttk.Frame(frm)
    b.grid(row=2, column=1, sticky="w")
    ttk.Button(b, text="PDF로 열기", style="Big.TButton", command=lambda: finish("pdf")).pack(side="left")
    ttk.Button(b, text="이미지(JPG/PNG)로 열기", style="Big.TButton", command=lambda: finish("image")).pack(side="left", padx=(8, 0))
    b2 = ttk.Frame(frm)
    b2.grid(row=3, column=1, sticky="w", pady=(8, 0))
    ttk.Button(b2, text="JPDF 앱에서 열기 (추출·정보)", command=lambda: finish("app")).pack(side="left")
    ttk.Button(b2, text="취소", command=root.destroy).pack(side="left", padx=(8, 0))
    ttk.Checkbutton(frm, text="이 선택을 기억하고 다음부터 묻지 않기 (Shift 키를 누른 채 열면 다시 물어봄)",
                    variable=remember).grid(row=4, column=0, columnspan=2, sticky="w", pady=(14, 0))
    root.bind("<Escape>", lambda e: root.destroy())
    root.bind("<Return>", lambda e: finish("pdf"))
    _center(root, max(560, frm.winfo_reqwidth() + 40), frm.winfo_reqheight() + 40)
    root.update_idletasks()
    root.geometry("")  # 내용 크기에 맞춤
    root.lift()
    root.attributes("-topmost", True)
    root.after(200, lambda: root.attributes("-topmost", False))
    root.mainloop()


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="jpdf", description=APP_TITLE, add_help=True)
    ap.add_argument("files", nargs="*", help=".jpdf 파일(열기) 또는 이미지 파일들(변환기에 추가)")
    ap.add_argument("--open-as", choices=["pdf", "image", "ask"], help=".jpdf를 바로 PDF/이미지로 열기")
    ap.add_argument("--app", action="store_true", help="선택창 없이 변환기 창에서 열기")
    ap.add_argument("--register", action="store_true", help="(Windows) .jpdf 연결 프로그램 등록")
    ap.add_argument("--unregister", action="store_true", help="(Windows) .jpdf 연결 해제")
    a = ap.parse_args(argv)

    if a.register or a.unregister:
        try:
            print(register_file_association() if a.register else unregister_file_association())
            return 0
        except Exception as e:  # noqa: BLE001
            print(f"오류: {e}", file=sys.stderr)
            return 1

    files = [Path(f) for f in a.files]
    docs = [f for f in files if f.suffix.lower() in (".jpdf", ".pdf")]
    if docs and not a.app and len(files) == 1:
        doc = docs[0]
        if not doc.is_file():
            _error_box(f"파일을 찾을 수 없습니다:\n{doc}")
            return 1
        mode = a.open_as or load_settings().get("open_mode", "ask")
        if mode != "ask" and (a.open_as is None) and shift_pressed():
            mode = "ask"
        if mode == "pdf":
            jc.open_as_pdf(doc)
            return 0
        if mode == "image":
            try:
                jc.open_as_images(doc)
            except Exception as e:  # noqa: BLE001
                _error_box(f"이미지로 열 수 없습니다.\n{e}")
                return 1
            return 0
        show_open_chooser(doc)
        return 0

    root = make_root()
    root.jpdf_window = MainWindow(root, initial_files=files or None)  # type: ignore[attr-defined]
    root.mainloop()
    return 0


def _error_box(msg: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(APP_NAME, msg)
        r.destroy()
    except Exception:  # noqa: BLE001
        print(msg, file=sys.stderr)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        _error_box("예상하지 못한 오류가 발생했습니다.\n\n" + traceback.format_exc())
        sys.exit(1)
