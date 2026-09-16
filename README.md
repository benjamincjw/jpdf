# JPDF — 이미지 ↔ PDF 무손실 변환기 & `.jpdf` 형식

JPG/PNG를 **재인코딩 없이** PDF에 담고, 그 파일을 열 때 **"PDF로 열기 / 이미지로 열기"**를 고를 수 있게 하는 도구입니다.

* **데스크톱 앱** (이 폴더, Python + tkinter): 변환기, `.jpdf` 열기 선택창, Windows 파일 연결.
* **크롬 확장 프로그램** ([chrome-extension/](chrome-extension/)): 같은 엔진을 JavaScript로 옮긴 것. 변환 페이지,
  `.jpdf` 뷰어(PDF/이미지 전환), 우클릭 "이미지를 JPDF/PDF로 저장", "이 페이지의 이미지 모두 모으기".
  모든 처리는 브라우저 안에서만 이루어집니다. 설치는 [chrome-extension/README.md](chrome-extension/README.md).

`.jpdf` 파일은 그 자체로 유효한 PDF입니다. 확장자를 `.pdf`로 바꿔도 아무 뷰어에서나 열립니다. 각 이미지는 파일 안에
딱 한 번만 들어가므로 용량은 원본 합과 거의 같고, 꺼낼 때 JPEG은 바이트 단위로, PNG는 픽셀 단위로 원본과 같습니다.
형식의 자세한 설계는 [JPDF_FORMAT.md](JPDF_FORMAT.md)를 보세요. 두 구현이 만든 파일은 서로 읽을 수 있습니다
(`tests/test_js_interop.py`).

## 시작하기

1. **Python 3.10 이상**이 필요합니다. 없으면 <https://www.python.org/downloads/> 에서 설치하면서
   *Add python.exe to PATH*를 체크하세요.
2. `run.bat`을 더블클릭합니다. 처음 한 번은 `%LOCALAPPDATA%\jpdf\venv`에 전용 파이썬 환경을 만들고
   패키지를 설치합니다(인터넷 필요, 1분 정도). 이후에는 바로 창이 뜹니다.
3. 앱의 **[도구] → ".jpdf 파일을 이 앱에 연결"**을 한 번 눌러 두면(또는 `register_jpdf.bat`),
   `.jpdf`를 더블클릭할 때 선택창이 뜨고, 탐색기 오른쪽 클릭 메뉴에도 "PDF로 열기 / 이미지로 열기"가 생깁니다.
   현재 사용자 레지스트리(HKCU)만 건드리므로 관리자 권한이 필요 없고, [도구] → "연결 해제"로 되돌릴 수 있습니다.

## 사용법

### 이미지 → PDF / JPDF
* 이미지를 목록에 추가합니다 (버튼, `Ctrl+O`, 또는 창에 드래그 앤 드롭 — 폴더를 놓으면 안의 이미지가 모두 들어갑니다).
* `▲ ▼`로 순서를 바꾸고, 선택하면 오른쪽에 미리보기와 저장 방식(무손실 여부)이 표시됩니다.
* 출력 옵션
  * **형식**: JPDF(.jpdf) 또는 PDF(.pdf). 내용은 같고, 서명 한 줄과 확장자만 다릅니다.
  * **페이지**: `이미지 크기 그대로`(이미지 DPI 기준) 또는 A4·A3·A5·B5·Letter·Legal에 맞춤.
  * **방향**: 고정 페이지일 때 자동(가로 이미지는 가로 페이지)/세로/가로.
  * **여백**(mm), **기본 DPI**(이미지에 DPI 정보가 없을 때).
  * **원본 파일을 첨부파일로도 포함**: 용량은 2배가 되지만 메타데이터까지 완전한 원본을 PDF 첨부로 보관.
* [변환]을 누르면 진행률이 표시되고, 끝나면 "PDF로 열기 / 이미지로 열기 / 폴더 열기" 버튼이 나타납니다.

### PDF / JPDF → 이미지
* 두 번째 탭에서 파일을 고르면 페이지 수·이미지 수·무손실 여부·첫 페이지 썸네일이 표시됩니다.
* [이미지 추출]은 지정한 폴더에 원본 이름으로 꺼냅니다. 결과 목록에 각 파일이
  "JPEG 바이트 그대로 · 원본과 동일(SHA-256 일치)" 같은 식으로 어떻게 복원되었는지 표시됩니다.
* 일반 PDF도 열 수 있습니다(페이지에 든 이미지를 pypdf로 추출).

### `.jpdf` 열기 선택창
`.jpdf`를 더블클릭하면 작은 창이 뜹니다.
* **PDF로 열기**: 기본 PDF 뷰어로 엽니다. (내용이 이미 PDF이므로 캐시 폴더에 `.pdf` 이름으로 복사해 엽니다.)
* **이미지(JPG/PNG)로 열기**: 캐시 폴더에 이미지를 꺼내 기본 이미지 뷰어로 엽니다. 이미지가 여러 장이면 꺼낸 이미지들이 든 폴더도 탐색기로 함께 열립니다(Windows 사진 앱이 폴더 넘겨보기를 항상 지원하지는 않기 때문).
* **JPDF 앱에서 열기**: 추출·정보 탭으로 엽니다.
* "이 선택을 기억"을 켜면 다음부터 묻지 않습니다. Shift를 누른 채 열면 다시 묻고, [도구] → ".jpdf 열기 방식"에서 바꿀 수 있습니다.
* 캐시 폴더: `%LOCALAPPDATA%\jpdf\cache` ([도구] 메뉴에서 열기/비우기).

### 명령줄
```
python app.py 사진.jpg 그림.png          변환기 창을 열면서 미리 추가
python app.py 파일.jpdf                   열기 선택창
python app.py --open-as pdf 파일.jpdf     바로 PDF로
python app.py --open-as image 파일.jpdf   바로 이미지로
python jpdf_core.py convert a.jpg b.png -o out.jpdf --page A4 --margin 10
python jpdf_core.py extract out.jpdf -o 폴더
python jpdf_core.py info out.jpdf
```

## Ben의 PC에서 확인한 것 (2026-09-16)
`run.bat` 첫 실행(전용 환경 생성·패키지 설치 약 40초) → 앱 실행 → 샘플 5장 변환(0.1초, 50.7 KB) →
"PDF로 열기"(Edge에서 5페이지 정상 표시, EXIF 회전 반영) → "이미지로 열기"(사진 앱) →
[도구]에서 .jpdf 연결 등록 → 탐색기에서 JPDF 아이콘 표시·더블클릭 시 선택창 → "이미지로 열기" 동작까지 확인했습니다.
`samples/` 폴더에 그때 쓴 샘플 이미지와 결과 `rgb_baseline 외 4장.jpdf`가 있습니다.

## 실행파일(.exe) 만들기
`build_exe.bat`을 실행하면 PyInstaller로 `dist\JPDF\JPDF.exe`(폴더 배포형)를 만듭니다.
실행파일을 쓸 때는 JPDF.exe를 한 번 실행해 [도구]에서 `.jpdf` 연결을 다시 등록하세요(등록된 실행 명령이 바뀌므로).

## 테스트
`run_tests.bat` (또는 `python -m pytest tests -q`). 다음을 자동으로 확인합니다.
* JPEG: PDF 안의 바이트가 원본과 동일, 꺼낸 파일의 SHA‑256이 원본과 일치
* PNG(회색조/RGB/팔레트/16비트/알파/색 키/인터레이스): 꺼낸 파일이 픽셀 단위로 동일, 직접 삽입된 경우 IDAT까지 동일
* EXIF 방향 1~8: pdfium 렌더링 결과가 Pillow의 회전 결과와 픽셀 단위로 일치
* 페이지 크기·여백·방향, 첨부 옵션, 일반 PDF 추출, qpdf 구조 검사(설치되어 있을 때)
* `tests/test_js_interop.py`: 크롬 확장 엔진과의 상호 운용 (Node.js가 있을 때)

## 파일 구성
```
chrome-extension/ 크롬 확장 프로그램 (엔진 JS 이식본 core/jpdf.js 포함)
app.py            tkinter GUI, 열기 선택창, Windows 파일 연결
jpdf_core.py      변환 엔진 (PDF 작성기, 이미지 분석, 추출기) — 단독 CLI로도 동작
make_icon.py      assets/jpdf.png, jpdf.ico 생성
assets/           아이콘
tests/            pytest 테스트, 픽스처 생성기, GUI 스모크 테스트
run.bat           실행 (첫 실행 시 환경 구성)
register_jpdf.bat .jpdf 연결 등록
build_exe.bat     PyInstaller 빌드
run_tests.bat     테스트 실행
JPDF_FORMAT.md    형식 명세
```
