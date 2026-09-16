# JPDF 파일 형식 명세 (버전 1.0)

JPDF(`.jpdf`)는 **"확장자만 바꾸면 PDF이고, 앱으로 열면 원본 이미지가 그대로 나오는" 컨테이너**다.
새 바이너리 포맷을 발명하는 대신, PDF가 이미 갖고 있는 두 가지 성질을 그대로 이용한다.

1. PDF는 JPEG 파일을 **재인코딩 없이** 페이지 이미지로 품을 수 있다 (`/DCTDecode`).
2. PDF의 `/FlateDecode` 필터는 **PNG와 똑같은 예측(필터) 방식**을 지원한다 (`/Predictor 15`).
   따라서 PNG의 압축 데이터(IDAT)를 풀지 않고 그대로 넣을 수 있다.

그 결과 `.jpdf` 파일은 (a) 어떤 PDF 뷰어로도 열리고, (b) 이미지가 파일 안에 **딱 한 번**만 들어 있어
용량이 원본 이미지 합과 거의 같으며, (c) 이미지를 다시 꺼낼 때 JPEG은 **바이트 단위**로,
PNG는 **픽셀 단위**로 원본과 동일하다.

---

## 1. 파일 구조

```
%PDF-1.7                      ← 1행: 표준 PDF 헤더
%âãÏÓ                          ← 2행: 바이너리 파일임을 알리는 관례적 주석
%JPDF-1.0                      ← 3행: JPDF 서명 (빠른 식별용, .jpdf로 저장할 때만)
4 0 obj … XObject(이미지) …    ← 이미지 스트림 (페이지당 1개, 알파가 있으면 SMask 1개 추가)
5 0 obj … 콘텐츠 스트림 …      ← "q a b c d e f cm /Im0 Do Q"
6 0 obj … /Type /Page …
…
3 0 obj /Type /Pages
1 0 obj /Type /Catalog /JPDF << … >>   ← JPDF 메타데이터 (아래 3절)
2 0 obj /Info
xref / trailer / startxref / %%EOF     ← 표준 상호참조 테이블 (증분 갱신 없음, 객체 스트림 없음)
```

* PDF 버전 1.7. 암호화·객체 스트림·증분 갱신을 쓰지 않아 어떤 구현으로도 읽기 쉽다.
* 파일 식별: 처음 1 KB 안에 `%JPDF-`가 있으면 JPDF. `.pdf` 확장자로 저장한 경우 서명은 없지만
  카탈로그의 `/JPDF` 사전은 그대로 있으므로 그것으로 식별한다.

## 2. 이미지 저장 규칙

| 원본 | 저장 방식 | 보존 수준 |
|---|---|---|
| JPEG (baseline·progressive, 8비트, Gray/YCbCr·RGB/CMYK) | 파일 바이트 전체를 `/Filter /DCTDecode` 스트림에 그대로 | **바이트 동일** (EXIF·ICC 등 메타데이터 포함) |
| PNG — 회색조 1/2/4/8/16비트, RGB 8/16비트, 팔레트 1/2/4/8비트, 비인터레이스, tRNS 없음(팔레트) | IDAT를 이어붙인 zlib 스트림을 `/Filter /FlateDecode` + `/DecodeParms << /Predictor 15 /Colors c /BitsPerComponent b /Columns w >>` 로 그대로 | **압축 데이터까지 동일**, 픽셀 동일 |
| PNG — 회색조/RGB에 tRNS(단색 투명) | 위와 같고, tRNS 값을 `/Mask [v v …]` (색 키 마스크)로 | 픽셀·투명색 동일 |
| PNG — 알파(RGBA, LA, 팔레트+tRNS), 인터레이스 | Pillow로 디코딩 → 색 채널을 PNG로 재압축해 위 방식으로, 알파는 별도 `/SMask` 이미지로 | **픽셀 동일** (16비트 알파 PNG만 8비트로 축소) |
| 그 외 (BMP, GIF 첫 프레임, TIFF, WebP …) | Pillow로 디코딩 → Flate | 픽셀 동일 (CMYK/YCbCr/Lab 등은 RGB로 변환) |
| PDF가 지원하지 않는 JPEG (산술 부호화, 12비트, 무손실 JPEG, 계층형) | Pillow가 디코딩할 수 있으면 픽셀을 Flate로 | 픽셀 동일 (바이트는 다름) |

* CMYK JPEG은 Adobe 관례대로 반전 저장되어 있으므로 `/Decode [1 0 1 0 1 0 1 0]`을 붙인다.
* 팔레트 PNG는 `/ColorSpace [/Indexed /DeviceRGB hival <PLTE>]`로 저장한다.
* 색 공간은 `DeviceGray / DeviceRGB / DeviceCMYK`로만 표기한다. ICC 프로파일·gAMA는 해석하지 않는다
  (대부분의 뷰어가 PNG를 다룰 때와 같은 동작).

## 3. EXIF 회전

카메라 사진의 EXIF Orientation(1~8)은 **픽셀을 돌리지 않고** 페이지 콘텐츠 스트림의 변환 행렬 `cm`으로 반영한다.
이미지 공간의 단위 정사각형을 표시 사각형 (x, y, dw, dh)로 보내는 행렬 `[a b c d e f]`는 다음과 같다.

| Orientation | 의미 | 행렬 |
|---|---|---|
| 1 | 그대로 | `dw 0 0 dh x y` |
| 2 | 좌우 반전 | `-dw 0 0 dh x+dw y` |
| 3 | 180° | `-dw 0 0 -dh x+dw y+dh` |
| 4 | 상하 반전 | `dw 0 0 -dh x y+dh` |
| 5 | 전치 | `0 -dh -dw 0 x+dw y+dh` |
| 6 | 90° 시계 방향 | `0 -dh dw 0 x y+dh` |
| 7 | 반전치 | `0 dh dw 0 x y` |
| 8 | 90° 반시계 방향 | `0 dh -dw 0 x+dw y` |

(5~8은 표시 폭·높이가 원본의 높이·폭이 된다.) 테스트에서 8가지 모두 Pillow의 `exif_transpose` 결과와
렌더링을 픽셀 단위로 비교해 검증했다.

## 4. `/JPDF` 메타데이터 사전 (카탈로그 안)

```
/JPDF <<
  /Version (1.0)
  /Producer (JPDF 1.0 (jpdf_core.py))
  /Created (D:20260916213000+09'00')
  /Images [
    << /Page 1 /Name (photo.jpg) /Format /JPEG /Width 4032 /Height 3024
       /XObject 4 0 R /Lossless true /Orientation 6 /Plan /jpeg-verbatim
       /Size 2345678 /SHA256 (…64자리 16진수…) >>
    << /Page 2 /Name <FEFF…> /Format /PNG … /Attachment (002_그림.png) >>
  ]
>>
```

* `/Name`: 원본 파일명 (ASCII가 아니면 UTF‑16BE 16진 문자열). 꺼낼 때 이 이름을 되살린다.
* `/SHA256`: 원본 파일의 SHA‑256. 꺼낸 파일과 비교해 "원본과 바이트 동일"인지 알려준다.
* `/Plan`: 저장 방식 (`jpeg-verbatim`, `png-direct`, `png-reencode`, `jpeg-decode`, `other`).
* `/Attachment`: 원본을 첨부파일로도 넣었을 때 첨부 이름.
* PDF 뷰어는 카탈로그의 모르는 키를 무시하므로 호환성에 영향이 없다. (pypdf, qpdf, Acrobat 모두 보존.)

## 5. 원본 첨부 옵션

"원본 파일을 첨부파일로도 포함"을 켜면 각 원본이 표준 PDF 첨부파일(`/EmbeddedFiles` 이름 트리 +
`/Filespec`, `/AFRelationship /Source`)로도 들어간다. 이때는 용량이 두 배가 되지만, PNG의 텍스트 청크·ICC
프로파일 같은 메타데이터까지 완전히 보존되고, Acrobat 같은 뷰어의 첨부파일 패널에서도 바로 꺼낼 수 있다.

## 6. 구현

* `jpdf_core.py` (데스크톱 앱, Python) — 작성기 + pypdf 기반 판독기
* `chrome-extension/core/jpdf.js` (크롬 확장, JavaScript, 의존성 없음) — 같은 규칙의 작성기 + 파일 안의 `N G obj`를
  훑는 스캔 기반 판독기 (xref 없이도 동작, 다른 도구가 만든 PDF에도 적용). 두 구현의 출력은 서로 읽히며
  `tests/test_js_interop.py`가 바이트/픽셀 단위로 확인한다.

## 7. 이미지 꺼내기 ("이미지로 열기")

1. `/Attachment`가 있으면 첨부파일을 그대로 쓴다 (바이트 동일).
2. 페이지의 `/Im0` XObject를 읽는다.
   * `/DCTDecode` → 스트림 원시 바이트가 곧 JPEG 파일.
   * `/FlateDecode` + PNG 예측자 → PNG 서명 + IHDR(+PLTE, +tRNS) + IDAT(원시 스트림) + IEND 로 재조립.
     `/SMask`가 있으면 색·알파 PNG를 각각 만든 뒤 Pillow로 합쳐 RGBA/LA PNG로 저장.
3. 그래도 안 되면(다른 도구가 만든 PDF) pypdf의 범용 이미지 추출을 쓴다.

## 8. 확인 방법

```bash
qpdf --check 파일.jpdf                  # 구조 검사
pdfimages -list 파일.jpdf               # 페이지별 이미지·인코딩(jpeg/image) 확인
pdfimages -j 파일.jpdf out              # JPEG을 그대로 꺼냄 → 원본과 sha256 비교 가능
python jpdf_core.py info 파일.jpdf      # JPDF 메타데이터 출력
python jpdf_core.py extract 파일.jpdf -o 폴더
```

## 9. 한계와 솔직한 주의점

* JPDF는 PDF이므로 다른 프로그램이 "다른 이름으로 저장"하면 `/JPDF` 사전이 사라지거나 이미지가 재압축될 수 있다.
  (증분 저장이나 qpdf/pypdf 재작성은 대체로 보존한다.)
* PNG를 재조립해 꺼내면 픽셀은 동일하지만 원본 파일의 보조 청크(tEXt, gAMA, iCCP 등)는 없다.
  완전한 원본이 필요하면 첨부 옵션을 쓴다.
* `이미지 크기 그대로` 페이지 모드는 이미지의 DPI 메타데이터를 따른다. 카메라 사진은 72 dpi로 기록된 경우가 많아
  페이지가 물리적으로 아주 커질 수 있는데(뷰어에서는 문제없음), 인쇄가 목적이면 A4 등 고정 크기를 쓰는 편이 낫다.
* 하나의 페이지에 이미지 하나만 둔다. 여러 이미지를 한 페이지에 배치하는 기능은 없다.
* JPEG의 EXIF 회전은 `cm` 행렬로만 반영하므로, 픽셀 데이터를 요구하는 일부 도구(`pdfimages`)는 회전 전 원본을 준다 — 의도된 동작이다.
