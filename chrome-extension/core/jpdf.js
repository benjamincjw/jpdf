// jpdf.js — JPDF 엔진 (브라우저/서비스 워커/Node 공용, 의존성 없음)
//
// 데스크톱 앱의 jpdf_core.py 와 같은 규칙으로 파일을 만든다.
//  * JPEG  : 원본 바이트를 그대로 /DCTDecode 스트림에 (비트 단위 무손실)
//  * PNG   : 가능하면 IDAT(zlib) 데이터를 그대로 /FlateDecode + PNG 예측자(Predictor 15)로
//            알파·인터레이스·팔레트 투명도는 순수 JS PNG 코덱으로 풀어 색 데이터 + SMask 로 (픽셀 무손실)
//  * 그 외 : 호출자가 넘겨준 디코더(canvas)로 RGBA 를 얻어 Flate 로
//  * EXIF 회전은 페이지 변환 행렬(cm)로 반영, 카탈로그에 /JPDF 메타데이터, 3번째 줄에 %JPDF-1.0 서명
//
// 읽기 쪽은 xref 없이 파일 안의 "N G obj" 를 훑어 이미지 XObject 를 찾는 방식이라
// 다른 도구가 만든 PDF 에서도 JPEG/PNG 계열 이미지를 꺼낼 수 있다.

export const JPDF_VERSION = "1.0";
export const PRODUCER = `JPDF ${JPDF_VERSION} (chrome-extension)`;
const MM_TO_PT = 72 / 25.4;
const MAX_PAGE_PT = 14400;
const PNG_SIG = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

export const PAGE_SIZES_PT = {
  A3: [841.89, 1190.55], A4: [595.276, 841.89], A5: [419.528, 595.276],
  B5: [498.898, 708.661], Letter: [612, 792], Legal: [612, 1008],
};

export class JpdfError extends Error {}

// ---------------------------------------------------------------------------
// 바이트 도우미
// ---------------------------------------------------------------------------
const te = new TextEncoder();

export function concatBytes(parts) {
  let n = 0;
  for (const p of parts) n += p.length;
  const out = new Uint8Array(n);
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}

function ascii(s) { return te.encode(s); }

/** 바이트 → 1:1 문자열 (각 바이트가 코드 유닛 하나). 구조 토큰 검색용. */
export function bytesToBinaryString(u8) {
  let s = "";
  const CH = 0x8000;
  for (let i = 0; i < u8.length; i += CH) s += String.fromCharCode.apply(null, u8.subarray(i, i + CH));
  return s;
}

export function bytesEqual(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

export function indexOfBytes(hay, needle, from = 0) {
  outer: for (let i = from; i <= hay.length - needle.length; i++) {
    for (let j = 0; j < needle.length; j++) if (hay[i + j] !== needle[j]) continue outer;
    return i;
  }
  return -1;
}

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

export function crc32(u8, start = 0, end = u8.length, seed = 0) {
  let c = (seed ^ 0xffffffff) >>> 0;
  for (let i = start; i < end; i++) c = CRC_TABLE[(c ^ u8[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

export async function sha256Hex(u8) {
  const buf = await crypto.subtle.digest("SHA-256", u8);
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export function toHex(u8) {
  let s = "";
  for (let i = 0; i < u8.length; i++) s += u8[i].toString(16).padStart(2, "0");
  return s.toUpperCase();
}

export function fromHex(hex) {
  const clean = hex.replace(/[^0-9a-fA-F]/g, "");
  const out = new Uint8Array(clean.length >> 1);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(clean.substr(i * 2, 2), 16);
  return out;
}

export function humanSize(n) {
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return i === 0 ? `${n} B` : `${n.toFixed(1)} ${units[i]}`;
}

// ---------------------------------------------------------------------------
// zlib (브라우저/Node 공통 CompressionStream)
// ---------------------------------------------------------------------------
async function streamTransform(u8, ts) {
  const stream = new Blob([u8]).stream().pipeThrough(ts);
  return new Uint8Array(await new Response(stream).arrayBuffer());
}
export const inflate = (u8) => streamTransform(u8, new DecompressionStream("deflate"));
export const deflate = (u8) => streamTransform(u8, new CompressionStream("deflate"));

// ---------------------------------------------------------------------------
// PDF 문법 도우미
// ---------------------------------------------------------------------------
export function pdfNum(x) {
  if (Number.isInteger(x)) return String(x);
  let s = x.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
  if (s === "-0" || s === "" || s === "-") s = "0";
  return s;
}

export function pdfTextString(s) {
  if (/^[\x20-\x7e]*$/.test(s)) return "(" + s.replace(/\\/g, "\\\\").replace(/\(/g, "\\(").replace(/\)/g, "\\)") + ")";
  const u16 = [0xfe, 0xff];
  for (const ch of s) {
    let cp = ch.codePointAt(0);
    if (cp > 0xffff) { cp -= 0x10000; const hi = 0xd800 + (cp >> 10), lo = 0xdc00 + (cp & 0x3ff); u16.push(hi >> 8, hi & 255, lo >> 8, lo & 255); }
    else u16.push(cp >> 8, cp & 255);
  }
  return "<" + toHex(Uint8Array.from(u16)) + ">";
}

export function pdfName(s) {
  let out = "/";
  for (const b of te.encode(s)) {
    const ch = String.fromCharCode(b);
    out += b >= 33 && b <= 126 && !"#/()<>[]{}%".includes(ch) ? ch : "#" + b.toString(16).toUpperCase().padStart(2, "0");
  }
  return out;
}

export function pdfDate(d = new Date()) {
  const p = (n) => String(n).padStart(2, "0");
  const off = -d.getTimezoneOffset();
  const sign = off >= 0 ? "+" : "-";
  const a = Math.abs(off);
  return `(D:${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}${sign}${p(Math.floor(a / 60))}'${p(a % 60)}')`;
}

// ---------------------------------------------------------------------------
// JPEG 헤더
// ---------------------------------------------------------------------------
const SOF = new Set([0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf]);

export function isJpeg(u8) { return u8.length > 3 && u8[0] === 0xff && u8[1] === 0xd8; }
export function isPng(u8) { return u8.length > 8 && PNG_SIG.every((b, i) => u8[i] === b); }

function parseExifTiff(tiff) {
  // tiff: APP1 세그먼트에서 "Exif\0\0" 다음의 TIFF 헤더부터
  const out = {};
  if (tiff.length < 8) return out;
  const le = tiff[0] === 0x49 && tiff[1] === 0x49;
  const dv = new DataView(tiff.buffer, tiff.byteOffset, tiff.byteLength);
  const u16 = (o) => dv.getUint16(o, le), u32 = (o) => dv.getUint32(o, le);
  if (u16(2) !== 42) return out;
  const ifd = u32(4);
  if (ifd + 2 > tiff.length) return out;
  const n = u16(ifd);
  for (let i = 0; i < n; i++) {
    const e = ifd + 2 + i * 12;
    if (e + 12 > tiff.length) break;
    const tag = u16(e), type = u16(e + 2), count = u32(e + 4);
    if (tag === 0x0112 && (type === 3 || type === 4)) out.orientation = type === 3 ? u16(e + 8) : u32(e + 8);
    if ((tag === 0x011a || tag === 0x011b) && type === 5 && count >= 1) {
      const off = u32(e + 8);
      if (off + 8 <= tiff.length) { const num = u32(off), den = u32(off + 4); if (den) out[tag === 0x011a ? "xres" : "yres"] = num / den; }
    }
    if (tag === 0x0128 && type === 3) out.resUnit = u16(e + 8);
  }
  return out;
}

export function parseJpeg(u8) {
  if (!isJpeg(u8)) throw new JpdfError("JPEG 파일이 아닙니다.");
  const info = { adobe: false, sof: null, orientation: 1, dpi: null, progressive: false, arithmetic: false, lossless: false, hierarchical: false };
  let i = 2; const n = u8.length;
  let exif = null, jfif = null;
  while (i < n) {
    if (u8[i] !== 0xff) throw new JpdfError("손상된 JPEG입니다 (마커 정렬 오류).");
    while (i < n && u8[i] === 0xff) i++;
    if (i >= n) break;
    const marker = u8[i++];
    if (marker === 0xd8 || marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) continue;
    if (marker === 0xd9) break;
    if (i + 2 > n) break;
    const seglen = (u8[i] << 8) | u8[i + 1];
    const seg = u8.subarray(i + 2, i + seglen);
    if (marker === 0xe0 && seg.length >= 12 && seg[0] === 0x4a && seg[1] === 0x46 && seg[2] === 0x49 && seg[3] === 0x46) {
      jfif = { unit: seg[7], x: (seg[8] << 8) | seg[9], y: (seg[10] << 8) | seg[11] };
    }
    if (marker === 0xe1 && seg.length > 6 && seg[0] === 0x45 && seg[1] === 0x78 && seg[2] === 0x69 && seg[3] === 0x66 && !exif) {
      try { exif = parseExifTiff(seg.subarray(6)); } catch { exif = null; }
    }
    if (marker === 0xee && seg.length >= 5 && seg[0] === 0x41 && seg[1] === 0x64 && seg[2] === 0x6f) info.adobe = true;
    if (SOF.has(marker)) {
      info.sof = marker;
      info.precision = seg[0];
      info.height = (seg[1] << 8) | seg[2];
      info.width = (seg[3] << 8) | seg[4];
      info.components = seg[5];
      info.progressive = [0xc2, 0xc6, 0xca, 0xce].includes(marker);
      info.arithmetic = marker >= 0xc9;
      info.lossless = [0xc3, 0xc7, 0xcb, 0xcf].includes(marker);
      info.hierarchical = [0xc5, 0xc6, 0xc7, 0xcd, 0xce, 0xcf].includes(marker);
      break;
    }
    if (marker === 0xda) break;
    i += seglen;
  }
  if (exif && exif.orientation >= 1 && exif.orientation <= 8) info.orientation = exif.orientation;
  if (jfif && jfif.unit === 1) info.dpi = [jfif.x, jfif.y];
  else if (jfif && jfif.unit === 2) info.dpi = [jfif.x * 2.54, jfif.y * 2.54];
  else if (exif && exif.xres && exif.yres) {
    const unit = exif.resUnit === 3 ? 2.54 : 1;
    info.dpi = [exif.xres * unit, exif.yres * unit];
  }
  info.dpi = validDpi(info.dpi);
  return info;
}

function validDpi(d) {
  if (!d) return null;
  const [x, y] = d.map(Number);
  if (!(x >= 10 && x <= 2400 && y >= 10 && y <= 2400)) return null;
  return [Math.round(x * 1000) / 1000, Math.round(y * 1000) / 1000];
}

// ---------------------------------------------------------------------------
// PNG 청크 / 코덱
// ---------------------------------------------------------------------------
export function parsePngChunks(u8) {
  if (!isPng(u8)) throw new JpdfError("PNG 파일이 아닙니다.");
  const dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
  const info = { ihdr: null, plte: null, trns: null, dpi: null, idat: [] };
  let pos = 8;
  while (pos + 8 <= u8.length) {
    const len = dv.getUint32(pos);
    const type = String.fromCharCode(u8[pos + 4], u8[pos + 5], u8[pos + 6], u8[pos + 7]);
    const body = u8.subarray(pos + 8, pos + 8 + len);
    pos += 12 + len;
    if (type === "IHDR") {
      const bv = new DataView(body.buffer, body.byteOffset, body.byteLength);
      info.ihdr = { width: bv.getUint32(0), height: bv.getUint32(4), bitDepth: body[8], colorType: body[9], compression: body[10], filter: body[11], interlace: body[12] };
    } else if (type === "PLTE") info.plte = body;
    else if (type === "tRNS") info.trns = body;
    else if (type === "pHYs") { const unit = body[8]; if (unit === 1) { const x = new DataView(body.buffer, body.byteOffset).getUint32(0), y = new DataView(body.buffer, body.byteOffset).getUint32(4); info.dpi = validDpi([x * 0.0254, y * 0.0254]); } }
    else if (type === "IDAT") info.idat.push(body);
    else if (type === "IEND") break;
  }
  if (!info.ihdr) throw new JpdfError("손상된 PNG입니다 (IHDR 없음).");
  info.idat = concatBytes(info.idat);
  return info;
}

export function pngDirectOk(ihdr, trns) {
  if (ihdr.interlace !== 0 || ihdr.compression !== 0 || ihdr.filter !== 0) return false;
  const { bitDepth: bd, colorType: ct } = ihdr;
  if (ct === 0) return [1, 2, 4, 8, 16].includes(bd);
  if (ct === 2) return [8, 16].includes(bd);
  if (ct === 3) return [1, 2, 4, 8].includes(bd) && !trns;
  return false;
}

function channelsOf(ct) { return { 0: 1, 2: 3, 3: 1, 4: 2, 6: 4 }[ct]; }

function pngChunk(type, body) {
  const t = ascii(type);
  const out = new Uint8Array(12 + body.length);
  new DataView(out.buffer).setUint32(0, body.length);
  out.set(t, 4); out.set(body, 8);
  new DataView(out.buffer).setUint32(8 + body.length, crc32(out, 4, 8 + body.length));
  return out;
}

/** 조각으로 PNG 파일 만들기 (zdata = 이미 zlib 압축된 필터된 스캔라인) */
export function buildPng({ width, height, bitDepth, colorType, zdata, plte = null, trns = null }) {
  const ihdr = new Uint8Array(13);
  const dv = new DataView(ihdr.buffer);
  dv.setUint32(0, width); dv.setUint32(4, height);
  ihdr[8] = bitDepth; ihdr[9] = colorType; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;
  const parts = [Uint8Array.from(PNG_SIG), pngChunk("IHDR", ihdr)];
  if (plte) parts.push(pngChunk("PLTE", plte));
  if (trns) parts.push(pngChunk("tRNS", trns));
  parts.push(pngChunk("IDAT", zdata), pngChunk("IEND", new Uint8Array(0)));
  return concatBytes(parts);
}

function paeth(a, b, c) {
  const p = a + b - c, pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
  return pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
}

/** 필터된 스캔라인(행마다 필터 바이트) → 순수 샘플 바이트 */
export function unfilterScanlines(raw, rowBytes, height, bpp) {
  const out = new Uint8Array(rowBytes * height);
  let ip = 0;
  for (let y = 0; y < height; y++) {
    const ft = raw[ip++];
    const o = y * rowBytes, po = o - rowBytes;
    for (let i = 0; i < rowBytes; i++) {
      const x = raw[ip++];
      const a = i >= bpp ? out[o + i - bpp] : 0;
      const b = y > 0 ? out[po + i] : 0;
      const c = i >= bpp && y > 0 ? out[po + i - bpp] : 0;
      let v;
      switch (ft) {
        case 0: v = x; break;
        case 1: v = x + a; break;
        case 2: v = x + b; break;
        case 3: v = x + ((a + b) >> 1); break;
        case 4: v = x + paeth(a, b, c); break;
        default: throw new JpdfError("알 수 없는 PNG 필터 " + ft);
      }
      out[o + i] = v & 0xff;
    }
  }
  return out;
}

/** 순수 샘플 바이트 → 적응형 필터가 적용된 스캔라인 (libpng 식 최소합 휴리스틱) */
export function filterScanlines(samples, rowBytes, height, bpp) {
  const out = new Uint8Array((rowBytes + 1) * height);
  const cand = [new Uint8Array(rowBytes), new Uint8Array(rowBytes), new Uint8Array(rowBytes), new Uint8Array(rowBytes), new Uint8Array(rowBytes)];
  for (let y = 0; y < height; y++) {
    const o = y * rowBytes, po = o - rowBytes;
    let best = 0, bestSum = Infinity;
    for (let ft = 0; ft < 5; ft++) {
      const c = cand[ft];
      let sum = 0;
      for (let i = 0; i < rowBytes; i++) {
        const x = samples[o + i];
        const a = i >= bpp ? samples[o + i - bpp] : 0;
        const b = y > 0 ? samples[po + i] : 0;
        const cc = i >= bpp && y > 0 ? samples[po + i - bpp] : 0;
        let v;
        switch (ft) { case 0: v = x; break; case 1: v = x - a; break; case 2: v = x - b; break; case 3: v = x - ((a + b) >> 1); break; default: v = x - paeth(a, b, cc); }
        v &= 0xff; c[i] = v;
        sum += v < 128 ? v : 256 - v;
        if (sum >= bestSum) break;
      }
      if (sum < bestSum) { bestSum = sum; best = ft; }
    }
    out[y * (rowBytes + 1)] = best;
    out.set(cand[best], y * (rowBytes + 1) + 1);
  }
  return out;
}

const ADAM7 = [[0, 0, 8, 8], [4, 0, 8, 8], [0, 4, 4, 8], [2, 0, 4, 4], [0, 2, 2, 4], [1, 0, 2, 2], [0, 1, 1, 2]];

/** PNG 디코딩 (샘플 바이트 단위). 비트 깊이 8/16 만 인터레이스 지원; 그 외 인터레이스는 null */
export async function decodePng(u8) {
  const png = parsePngChunks(u8);
  const { width, height, bitDepth, colorType, interlace } = png.ihdr;
  const ch = channelsOf(colorType);
  const bitsPerPixel = ch * bitDepth;
  const bpp = Math.ceil(bitsPerPixel / 8);
  const rowBytes = Math.ceil((width * bitsPerPixel) / 8);
  const raw = await inflate(png.idat);
  let samples;
  if (interlace === 0) {
    samples = unfilterScanlines(raw, rowBytes, height, bpp);
  } else {
    if (bitDepth < 8) return null;
    samples = new Uint8Array(rowBytes * height);
    let ip = 0;
    for (const [xs, ys, xst, yst] of ADAM7) {
      const pw = Math.ceil((width - xs) / xst), ph = Math.ceil((height - ys) / yst);
      if (pw <= 0 || ph <= 0) continue;
      const prb = pw * bpp;
      const passRaw = raw.subarray(ip, ip + (prb + 1) * ph);
      ip += (prb + 1) * ph;
      const p = unfilterScanlines(passRaw, prb, ph, bpp);
      for (let y = 0; y < ph; y++) for (let x = 0; x < pw; x++) {
        const src = (y * pw + x) * bpp, dst = ((ys + y * yst) * width + (xs + x * xst)) * bpp;
        for (let k = 0; k < bpp; k++) samples[dst + k] = p[src + k];
      }
    }
  }
  return { width, height, bitDepth, colorType, channels: ch, samples, plte: png.plte, trns: png.trns, dpi: png.dpi, rowBytes, bpp };
}

/** 샘플 → PNG 파일 (필터 + deflate) */
export async function encodePng({ width, height, bitDepth, colorType, samples, plte = null, trns = null }) {
  const ch = channelsOf(colorType);
  const bpp = Math.ceil((ch * bitDepth) / 8);
  const rowBytes = Math.ceil((width * ch * bitDepth) / 8);
  const filtered = filterScanlines(samples, rowBytes, height, bpp);
  const zdata = await deflate(filtered);
  return buildPng({ width, height, bitDepth, colorType, zdata, plte, trns });
}

/** 비트 깊이 < 8 인 단일 채널 행에서 샘플 값 읽기 */
function unpackSubByte(samples, width, height, rowBytes, bitDepth) {
  const out = new Uint8Array(width * height);
  const mask = (1 << bitDepth) - 1, per = 8 / bitDepth;
  for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
    const byte = samples[y * rowBytes + Math.floor(x / per)];
    const shift = 8 - bitDepth * (1 + (x % per));
    out[y * width + x] = (byte >> shift) & mask;
  }
  return out;
}

/**
 * 디코딩된 PNG → { base: {colorType, bitDepth, samples}, alpha: Uint8Array|null }
 * 8비트 회색/RGB 색 데이터와 8비트 알파로 나눈다 (16비트 알파 이미지는 8비트로 축소).
 */
export function splitPngAlpha(dec) {
  const { width, height, bitDepth, colorType, samples, rowBytes } = dec;
  const n = width * height;
  if (colorType === 6 || colorType === 4) {
    const ch = colorType === 6 ? 4 : 2, outCh = ch - 1;
    const base = new Uint8Array(n * outCh), alpha = new Uint8Array(n);
    if (bitDepth === 8) {
      for (let i = 0; i < n; i++) { for (let k = 0; k < outCh; k++) base[i * outCh + k] = samples[i * ch + k]; alpha[i] = samples[i * ch + outCh]; }
    } else { // 16비트: 상위 바이트만
      for (let i = 0; i < n; i++) { for (let k = 0; k < outCh; k++) base[i * outCh + k] = samples[(i * ch + k) * 2]; alpha[i] = samples[(i * ch + outCh) * 2]; }
    }
    return { base: { colorType: colorType === 6 ? 2 : 0, bitDepth: 8, samples: base }, alpha, lossless: bitDepth === 8 };
  }
  if (colorType === 3) {
    const idx = bitDepth === 8 ? samples.subarray(0, n) : unpackSubByte(samples, width, height, rowBytes, bitDepth);
    const plte = dec.plte || new Uint8Array(0), trns = dec.trns;
    const base = new Uint8Array(n * 3), alpha = trns ? new Uint8Array(n) : null;
    for (let i = 0; i < n; i++) {
      const p = idx[i] * 3;
      base[i * 3] = plte[p] ?? 0; base[i * 3 + 1] = plte[p + 1] ?? 0; base[i * 3 + 2] = plte[p + 2] ?? 0;
      if (alpha) alpha[i] = idx[i] < trns.length ? trns[idx[i]] : 255;
    }
    return { base: { colorType: 2, bitDepth: 8, samples: base }, alpha, lossless: true };
  }
  // 회색조/RGB (인터레이스였던 경우): 그대로 (tRNS 는 색 키로 유지)
  return { base: { colorType, bitDepth, samples }, alpha: null, lossless: true, trns: dec.trns };
}

// ---------------------------------------------------------------------------
// 이미지 분석 / PDF 이미지 객체
// ---------------------------------------------------------------------------
/**
 * @param {Uint8Array} bytes
 * @param {string} name
 * @param {object} decoders  { decodeToRgba(bytes, mime) → {width,height,rgba} } (브라우저에서 canvas 로 구현)
 */
export async function analyzeImage(bytes, name, mime = "") {
  if (isJpeg(bytes)) {
    const j = parseJpeg(bytes);
    if (j.sof === null || j.height === 0) return { format: "JPEG", plan: "jpeg-decode", lossless: true, note: "JPEG 구조가 특이해 픽셀로 풀어 저장", width: j.width || 0, height: j.height || 0, orientation: j.orientation, dpi: j.dpi, jpeg: j };
    const ok = j.precision === 8 && !j.arithmetic && !j.lossless && !j.hierarchical && [1, 3, 4].includes(j.components);
    if (ok) {
      let note = j.progressive ? "프로그레시브 JPEG 원본 그대로 (무손실)" : "JPEG 원본 바이트 그대로 (무손실)";
      if (j.components === 4) note = "CMYK JPEG 원본 그대로 (Adobe 반전 Decode 적용)";
      if (j.orientation !== 1) note += `, EXIF 회전 ${j.orientation} 반영`;
      return { format: "JPEG", plan: "jpeg-verbatim", lossless: true, note, width: j.width, height: j.height, orientation: j.orientation, dpi: j.dpi, jpeg: j };
    }
    return { format: "JPEG", plan: "jpeg-decode", lossless: true, note: "PDF가 지원하지 않는 JPEG(산술부호/12비트 등) → 픽셀로 풀어 저장", width: j.width, height: j.height, orientation: j.orientation, dpi: j.dpi, jpeg: j };
  }
  if (isPng(bytes)) {
    const png = parsePngChunks(bytes);
    const ih = png.ihdr;
    const base = { format: "PNG", width: ih.width, height: ih.height, orientation: 1, dpi: png.dpi, png };
    if (pngDirectOk(ih, png.trns)) {
      const kind = { 0: "회색조", 2: "RGB", 3: "팔레트" }[ih.colorType];
      let note = `PNG 압축 데이터 그대로 (${kind} ${ih.bitDepth}비트, 무손실)`;
      if (png.trns) note += ", 투명색은 색 키 마스크로";
      return { ...base, plan: "png-direct", lossless: true, note };
    }
    const reasons = [];
    if (ih.interlace) reasons.push("인터레이스");
    if (ih.colorType === 4 || ih.colorType === 6) reasons.push("알파 채널 분리(SMask)");
    else if (ih.colorType === 3 && png.trns) reasons.push("팔레트 투명도 → 알파(SMask)");
    const lossless = !(ih.bitDepth === 16 && (ih.colorType === 4 || ih.colorType === 6));
    return { ...base, plan: "png-reencode", lossless, note: "PNG 재압축: " + (reasons.join(", ") || "형식 변환") + (lossless ? " (픽셀 무손실)" : " (16비트 알파 → 8비트로 축소)") };
  }
  const fmt = (mime || "").split("/")[1]?.toUpperCase() || (name.split(".").pop() || "?").toUpperCase();
  return { format: fmt, plan: "other", lossless: true, note: `${fmt} → 브라우저로 디코딩해 무손실 압축(Flate)으로 저장`, width: 0, height: 0, orientation: 1, dpi: null };
}

function pngPartsToPdfImage(ihdr, zdata, plte, trns, extra = {}) {
  const { width: w, height: h, bitDepth: bd, colorType: ct } = ihdr;
  const colors = ct === 2 ? 3 : 1;
  let cs;
  if (ct === 0) cs = "/DeviceGray";
  else if (ct === 2) cs = "/DeviceRGB";
  else if (ct === 3) { if (!plte) throw new JpdfError("팔레트 PNG인데 PLTE 청크가 없습니다."); cs = `[/Indexed /DeviceRGB ${Math.floor(plte.length / 3) - 1} <${toHex(plte)}>]`; }
  else throw new JpdfError("직접 삽입할 수 없는 PNG 색 형식입니다.");
  let mask = null;
  if (trns && (ct === 0 || ct === 2)) {
    const vals = [];
    for (let i = 0; i + 1 < trns.length; i += 2) { const v = (trns[i] << 8) | trns[i + 1]; vals.push(`${v} ${v}`); }
    mask = `[${vals.join(" ")}]`;
  }
  return { width: w, height: h, colorspace: cs, bpc: bd, filter: "/FlateDecode", data: zdata,
    decodeParms: `<< /Predictor 15 /Colors ${colors} /BitsPerComponent ${bd} /Columns ${w} >>`,
    decodeArray: null, colorKeyMask: mask, smask: null, lossless: true, orientation: 1, dpi: null, note: "", ...extra };
}

async function samplesToPdfImage(width, height, colorType, bitDepth, samples, trns = null) {
  const png = parsePngChunks(await encodePng({ width, height, bitDepth, colorType, samples, trns }));
  return pngPartsToPdfImage(png.ihdr, png.idat, null, png.trns);
}

/**
 * 분석 결과 + 원본 바이트 → PDF 이미지 객체
 * decoders.decodeToRgba(bytes, mime) 는 브라우저에서만 제공 ({width, height, rgba: Uint8ClampedArray})
 */
export async function buildPdfImage(an, bytes, mime = "", decoders = null) {
  if (an.plan === "jpeg-verbatim") {
    const j = an.jpeg;
    const cs = { 1: "/DeviceGray", 3: "/DeviceRGB", 4: "/DeviceCMYK" }[j.components];
    return { width: j.width, height: j.height, colorspace: cs, bpc: 8, filter: "/DCTDecode", data: bytes, decodeParms: null,
      decodeArray: j.components === 4 ? "[1 0 1 0 1 0 1 0]" : null, colorKeyMask: null, smask: null, lossless: true,
      orientation: j.orientation, dpi: j.dpi, note: an.note };
  }
  if (an.plan === "png-direct") {
    const png = an.png;
    return pngPartsToPdfImage(png.ihdr, png.idat, png.plte, png.trns, { dpi: png.dpi, note: an.note });
  }
  if (an.plan === "png-reencode") {
    const dec = await decodePng(bytes);
    if (dec) {
      const split = splitPngAlpha(dec);
      const img = await samplesToPdfImage(dec.width, dec.height, split.base.colorType, split.base.bitDepth, split.base.samples, split.trns || null);
      img.dpi = dec.dpi; img.note = an.note; img.lossless = split.lossless;
      if (split.alpha) img.smask = await samplesToPdfImage(dec.width, dec.height, 0, 8, split.alpha);
      return img;
    }
    // 서브바이트 인터레이스 등: 브라우저 디코더로
  }
  if (!decoders || !decoders.decodeToRgba) throw new JpdfError(`${an.format} 형식은 이 환경에서 디코딩할 수 없습니다.`);
  const { width, height, rgba } = await decoders.decodeToRgba(bytes, mime);
  const n = width * height;
  const rgb = new Uint8Array(n * 3), alpha = new Uint8Array(n);
  let hasAlpha = false;
  for (let i = 0; i < n; i++) { rgb[i * 3] = rgba[i * 4]; rgb[i * 3 + 1] = rgba[i * 4 + 1]; rgb[i * 3 + 2] = rgba[i * 4 + 2]; alpha[i] = rgba[i * 4 + 3]; if (alpha[i] !== 255) hasAlpha = true; }
  const img = await samplesToPdfImage(width, height, 2, 8, rgb);
  img.note = an.note; img.lossless = an.lossless; img.orientation = 1; img.dpi = an.dpi;
  if (hasAlpha) img.smask = await samplesToPdfImage(width, height, 0, 8, alpha);
  return img;
}

// ---------------------------------------------------------------------------
// 페이지 배치
// ---------------------------------------------------------------------------
export const DEFAULT_OPTIONS = { pageSize: "image", orientation: "auto", marginMm: 0, defaultDpi: 96, useImageDpi: true, fitUpscale: true, embedOriginals: false };

export function validateOptions(o) {
  if (o.pageSize !== "image" && !PAGE_SIZES_PT[o.pageSize]) throw new JpdfError("알 수 없는 페이지 크기: " + o.pageSize);
  if (!["auto", "portrait", "landscape"].includes(o.orientation)) throw new JpdfError("알 수 없는 방향");
  if (!(o.marginMm >= 0)) throw new JpdfError("여백은 0 이상이어야 합니다.");
  if (!(o.defaultDpi >= 10 && o.defaultDpi <= 2400)) throw new JpdfError("기본 DPI는 10~2400 사이여야 합니다.");
  if (o.pageSize !== "image") { const [w, h] = PAGE_SIZES_PT[o.pageSize]; if (2 * o.marginMm * MM_TO_PT >= Math.min(w, h)) throw new JpdfError("여백이 너무 커서 이미지를 넣을 공간이 없습니다."); }
}

function layoutPage(img, o) {
  const rotated = [5, 6, 7, 8].includes(img.orientation);
  const iw = rotated ? img.height : img.width, ih = rotated ? img.width : img.height;
  const dpi = o.useImageDpi && img.dpi ? img.dpi : [o.defaultDpi, o.defaultDpi];
  const [dpiX, dpiY] = rotated ? [dpi[1], dpi[0]] : dpi;
  const margin = o.marginMm * MM_TO_PT;
  const natW = (iw * 72) / dpiX, natH = (ih * 72) / dpiY;
  if (o.pageSize === "image") {
    let dw = natW, dh = natH;
    const limit = MAX_PAGE_PT - 2 * margin;
    if (limit <= 0) throw new JpdfError("여백이 너무 큽니다.");
    const s = Math.min(1, limit / dw, limit / dh);
    dw *= s; dh *= s;
    const pw = Math.max(3, dw + 2 * margin), ph = Math.max(3, dh + 2 * margin);
    return { pw, ph, x: (pw - dw) / 2, y: (ph - dh) / 2, dw, dh };
  }
  let [pw, ph] = PAGE_SIZES_PT[o.pageSize];
  if (o.orientation === "landscape" || (o.orientation === "auto" && iw > ih)) [pw, ph] = [ph, pw];
  const aw = pw - 2 * margin, ah = ph - 2 * margin;
  if (aw <= 0 || ah <= 0) throw new JpdfError("여백이 너무 커서 이미지를 넣을 공간이 없습니다.");
  let scale = Math.min(aw / iw, ah / ih);
  if (!o.fitUpscale) scale = Math.min(scale, Math.min(natW / iw, natH / ih));
  const dw = iw * scale, dh = ih * scale;
  return { pw, ph, x: (pw - dw) / 2, y: (ph - dh) / 2, dw, dh };
}

export function ctmFor(o, x, y, dw, dh) {
  let m;
  switch (o) {
    case 2: m = [-dw, 0, 0, dh, x + dw, y]; break;
    case 3: m = [-dw, 0, 0, -dh, x + dw, y + dh]; break;
    case 4: m = [dw, 0, 0, -dh, x, y + dh]; break;
    case 5: m = [0, -dh, -dw, 0, x + dw, y + dh]; break;
    case 6: m = [0, -dh, dw, 0, x, y + dh]; break;
    case 7: m = [0, dh, dw, 0, x, y]; break;
    case 8: m = [0, dh, -dw, 0, x + dw, y]; break;
    default: m = [dw, 0, 0, dh, x, y];
  }
  return m.map(pdfNum).join(" ");
}

// ---------------------------------------------------------------------------
// PDF 작성기
// ---------------------------------------------------------------------------
class PdfWriter {
  constructor(jpdf) {
    this.parts = []; this.pos = 0; this.offsets = new Map(); this.next = 1;
    this.write(`%PDF-1.7\n`); this.write(Uint8Array.from([0x25, 0xe2, 0xe3, 0xcf, 0xd3, 0x0a]));
    if (jpdf) this.write(`%JPDF-${JPDF_VERSION}\n`);
  }
  write(x) { const u = typeof x === "string" ? ascii(x) : x; this.parts.push(u); this.pos += u.length; }
  reserve() { return this.next++; }
  writeObject(num, body) { this.offsets.set(num, this.pos); this.write(`${num} 0 obj\n`); this.write(body); this.write(`\nendobj\n`); }
  addObject(body) { const n = this.reserve(); this.writeObject(n, body); return n; }
  writeStream(num, entries, data) {
    this.offsets.set(num, this.pos);
    this.write(`${num} 0 obj\n<< ${entries} /Length ${data.length} >>\nstream\n`); this.write(data); this.write(`\nendstream\nendobj\n`);
  }
  addStream(entries, data) { const n = this.reserve(); this.writeStream(n, entries, data); return n; }
  finish(root, info, fileIdHex) {
    const size = this.next;
    for (let n = 1; n < size; n++) if (!this.offsets.has(n)) throw new JpdfError("내부 오류: 객체 " + n + " 미작성");
    const xref = this.pos;
    let s = `xref\n0 ${size}\n0000000000 65535 f \n`;
    for (let n = 1; n < size; n++) s += String(this.offsets.get(n)).padStart(10, "0") + " 00000 n \n";
    s += `trailer\n<< /Size ${size} /Root ${root} 0 R /Info ${info} 0 R /ID [<${fileIdHex}> <${fileIdHex}>] >>\nstartxref\n${xref}\n%%EOF\n`;
    this.write(s);
    return concatBytes(this.parts);
  }
}

function imageDictEntries(img, smaskRef) {
  let s = `/Type /XObject /Subtype /Image /Width ${img.width} /Height ${img.height} /ColorSpace ${img.colorspace} /BitsPerComponent ${img.bpc} /Filter ${img.filter}`;
  if (img.decodeParms) s += ` /DecodeParms ${img.decodeParms}`;
  if (img.decodeArray) s += ` /Decode ${img.decodeArray}`;
  if (img.colorKeyMask) s += ` /Mask ${img.colorKeyMask}`;
  if (smaskRef != null) s += ` /SMask ${smaskRef} 0 R`;
  return s;
}

function safeFilename(name) { return (name || "image").replace(/[\\/:*?"<>|\x00-\x1f]/g, "_").trim().slice(0, 120) || "image"; }

/**
 * 이미지들 → PDF/JPDF 바이트
 * @param {Array<{name:string, bytes:Uint8Array, type?:string}>} inputs
 * @param {object} options  DEFAULT_OPTIONS 형태 + { jpdf: true, title: "" }
 * @param {object} decoders { decodeToRgba }
 * @param {(i:number,n:number,msg:string)=>void} progress
 */
export async function convertImages(inputs, options = {}, decoders = null, progress = null) {
  const o = { ...DEFAULT_OPTIONS, ...options };
  validateOptions(o);
  const jpdf = o.jpdf !== false;
  if (!inputs.length) throw new JpdfError("변환할 이미지가 없습니다.");
  const t0 = Date.now();
  const w = new PdfWriter(jpdf);
  const catalogNum = w.reserve(), infoNum = w.reserve(), pagesNum = w.reserve();
  const pageRefs = [], meta = [], attach = [], reports = [];
  const idParts = [];
  for (let idx = 1; idx <= inputs.length; idx++) {
    const src = inputs[idx - 1];
    progress?.(idx - 1, inputs.length, `${src.name} 분석 중…`);
    const an = await analyzeImage(src.bytes, src.name, src.type);
    const img = await buildPdfImage(an, src.bytes, src.type, decoders);
    if (!an.width) { an.width = img.width; an.height = img.height; }
    progress?.(idx - 1, inputs.length, `${src.name} 쓰는 중…`);
    let smaskRef = null;
    if (img.smask) smaskRef = w.addStream(imageDictEntries(img.smask, null), img.smask.data);
    const xobj = w.addStream(imageDictEntries(img, smaskRef), img.data);
    const L = layoutPage(img, o);
    const content = ascii(`q ${ctmFor(img.orientation, L.x, L.y, L.dw, L.dh)} cm /Im0 Do Q`);
    const contentRef = w.addStream("", content);
    pageRefs.push(w.addObject(`<< /Type /Page /Parent ${pagesNum} 0 R /MediaBox [0 0 ${pdfNum(L.pw)} ${pdfNum(L.ph)}] /Resources << /XObject << /Im0 ${xobj} 0 R >> >> /Contents ${contentRef} 0 R >>`));
    const sha = await sha256Hex(src.bytes);
    idParts.push(sha);
    let attachName = null;
    if (o.embedOriginals) {
      attachName = `${String(idx).padStart(3, "0")}_${safeFilename(src.name)}`;
      const subtype = { JPEG: "image/jpeg", PNG: "image/png" }[an.format] || "application/octet-stream";
      const ef = w.addStream(`/Type /EmbeddedFile /Subtype ${pdfName(subtype)} /Params << /Size ${src.bytes.length} /ModDate ${pdfDate()} >>`, src.bytes);
      const fs = w.addObject(`<< /Type /Filespec /F ${pdfTextString(attachName)} /UF ${pdfTextString(attachName)} /EF << /F ${ef} 0 R /UF ${ef} 0 R >> /Desc ${pdfTextString("JPDF 원본 이미지")} /AFRelationship /Source >>`);
      attach.push([attachName, fs]);
    }
    const fmtName = { JPEG: "/JPEG", PNG: "/PNG" }[an.format] || pdfName(an.format);
    meta.push(`<< /Page ${idx} /Name ${pdfTextString(src.name)} /Format ${fmtName} /Width ${an.width} /Height ${an.height} /XObject ${xobj} 0 R /Lossless ${img.lossless ? "true" : "false"} /Orientation ${img.orientation} /Plan ${pdfName(an.plan)} /Size ${src.bytes.length} /SHA256 (${sha})${attachName ? ` /Attachment ${pdfTextString(attachName)}` : ""} >>`);
    reports.push({ name: src.name, page: idx, width: an.width, height: an.height, format: an.format, plan: an.plan, lossless: img.lossless, note: img.note || an.note, bytesIn: src.bytes.length, bytesStored: img.data.length + (img.smask ? img.smask.data.length : 0), pageSizePt: [L.pw, L.ph] });
    progress?.(idx, inputs.length, `${src.name} 완료`);
  }
  w.writeObject(pagesNum, `<< /Type /Pages /Count ${pageRefs.length} /Kids [${pageRefs.map((r) => r + " 0 R").join(" ")}] >>`);
  let catalog = `<< /Type /Catalog /Pages ${pagesNum} 0 R /JPDF << /Version ${pdfTextString(JPDF_VERSION)} /Producer ${pdfTextString(PRODUCER)} /Created ${pdfDate()} /Images [${meta.join(" ")}] >>`;
  if (attach.length) {
    attach.sort((a, b) => (a[0] < b[0] ? -1 : 1));
    catalog += ` /Names << /EmbeddedFiles << /Names [${attach.map(([n, r]) => `${pdfTextString(n)} ${r} 0 R`).join(" ")}] >> >> /AF [${attach.map(([, r]) => r + " 0 R").join(" ")}]`;
  }
  catalog += " >>";
  w.writeObject(catalogNum, catalog);
  w.writeObject(infoNum, `<< /Producer ${pdfTextString(PRODUCER)} /Creator (JPDF) /CreationDate ${pdfDate()} /ModDate ${pdfDate()}${o.title ? ` /Title ${pdfTextString(o.title)}` : ""} /Keywords (jpdf, images) >>`);
  const fileId = (await sha256Hex(ascii(idParts.join("|") + Date.now()))).slice(0, 32).toUpperCase();
  const bytes = w.finish(catalogNum, infoNum, fileId);
  progress?.(inputs.length, inputs.length, "완료");
  return { bytes, report: { pages: inputs.length, bytesOut: bytes.length, elapsedMs: Date.now() - t0, images: reports, isJpdf: jpdf, allLossless: reports.every((r) => r.lossless) } };
}

// ---------------------------------------------------------------------------
// PDF 읽기 (객체 스캔)
// ---------------------------------------------------------------------------
const WS = " \t\r\n\f\0";
function isWs(ch) { return WS.includes(ch); }
function isDelim(ch) { return "()<>[]{}/%".includes(ch); }

/** 아주 작은 PDF 토큰 파서: 사전/배열/이름/숫자/문자열/참조 */
function parseValue(s, i) {
  while (i < s.length && (isWs(s[i]) || s[i] === "%")) {
    if (s[i] === "%") { while (i < s.length && s[i] !== "\n" && s[i] !== "\r") i++; } else i++;
  }
  if (i >= s.length) return [null, i];
  const ch = s[i];
  if (ch === "<" && s[i + 1] === "<") {
    const d = {}; i += 2;
    for (;;) {
      while (i < s.length && (isWs(s[i]) || s[i] === "%")) { if (s[i] === "%") { while (i < s.length && s[i] !== "\n" && s[i] !== "\r") i++; } else i++; }
      if (s[i] === ">" && s[i + 1] === ">") return [d, i + 2];
      if (s[i] !== "/") throw new JpdfError("사전 파싱 오류 @" + i);
      const [key, j] = parseValue(s, i);
      const [val, k] = parseValue(s, j);
      d[key] = val; i = k;
      if (i >= s.length) return [d, i];
    }
  }
  if (ch === "<") { const j = s.indexOf(">", i + 1); return [{ hex: s.slice(i + 1, j) }, j + 1]; }
  if (ch === "[") {
    const arr = []; i++;
    for (;;) {
      while (i < s.length && (isWs(s[i]) || s[i] === "%")) { if (s[i] === "%") { while (i < s.length && s[i] !== "\n" && s[i] !== "\r") i++; } else i++; }
      if (s[i] === "]") return [arr, i + 1];
      const [v, j] = parseValue(s, i); if (j === i) throw new JpdfError("배열 파싱 오류");
      arr.push(v); i = j;
    }
  }
  if (ch === "(") {
    let depth = 1, j = i + 1, out = "";
    while (j < s.length && depth > 0) {
      const c = s[j];
      if (c === "\\") { const n = s[j + 1]; const map = { n: "\n", r: "\r", t: "\t", b: "\b", f: "\f", "(": "(", ")": ")", "\\": "\\" }; if (n in map) { out += map[n]; j += 2; continue; } if (/[0-7]/.test(n)) { const m = s.slice(j + 1, j + 4).match(/^[0-7]{1,3}/)[0]; out += String.fromCharCode(parseInt(m, 8)); j += 1 + m.length; continue; } j += 2; continue; }
      if (c === "(") depth++; else if (c === ")") { depth--; if (depth === 0) { j++; break; } }
      out += c; j++;
    }
    return [{ str: out }, j];
  }
  if (ch === "/") { let j = i + 1; while (j < s.length && !isWs(s[j]) && !isDelim(s[j])) j++; return [s.slice(i, j).replace(/#([0-9A-Fa-f]{2})/g, (_, h) => String.fromCharCode(parseInt(h, 16))), j]; }
  if (/[-+.0-9]/.test(ch)) {
    let j = i; while (j < s.length && /[-+.0-9]/.test(s[j])) j++;
    const num = parseFloat(s.slice(i, j));
    // 참조 "n g R" ?
    const m = s.slice(j, j + 20).match(/^\s+(\d+)\s+R(?![A-Za-z])/);
    if (m && Number.isInteger(num) && num >= 0) return [{ ref: num }, j + m[0].length];
    return [num, j];
  }
  const kw = s.slice(i, i + 5);
  if (kw.startsWith("true")) return [true, i + 4];
  if (kw.startsWith("false")) return [false, i + 5];
  if (kw.startsWith("null")) return [null, i + 4];
  // 알 수 없는 키워드 (stream, endobj 등)
  let j = i; while (j < s.length && !isWs(s[j]) && !isDelim(s[j])) j++;
  return [{ kw: s.slice(i, j) }, j === i ? i + 1 : j];
}

function textOf(v) { if (v == null) return ""; if (typeof v === "string") return v; if (v.str != null) return v.str; if (v.hex != null) { const b = fromHex(v.hex); if (b[0] === 0xfe && b[1] === 0xff) { let s = ""; for (let i = 2; i + 1 < b.length; i += 2) s += String.fromCharCode((b[i] << 8) | b[i + 1]); return s; } return bytesToBinaryString(b); } return String(v); }

/** 파일 전체를 훑어 객체 표를 만든다: num → {dict, streamStart, streamEnd, offset} */
export function scanPdfObjects(u8) {
  const s = bytesToBinaryString(u8);
  const objs = new Map();
  const re = /(\d+)\s+(\d+)\s+obj\b/g;
  let m;
  while ((m = re.exec(s))) {
    const num = parseInt(m[1], 10);
    let i = m.index + m[0].length;
    let val;
    try { [val, i] = parseValue(s, i); } catch { continue; }
    const entry = { dict: val && typeof val === "object" && !Array.isArray(val) && !("ref" in val) && !("str" in val) && !("hex" in val) && !("kw" in val) ? val : null, value: val, offset: m.index, streamStart: -1, streamEnd: -1 };
    // stream?
    let j = i; while (j < s.length && isWs(s[j])) j++;
    if (s.startsWith("stream", j) && entry.dict) {
      j += 6;
      if (s[j] === "\r") j++;
      if (s[j] === "\n") j++;
      entry.streamStart = j;
      const len = entry.dict["/Length"];
      let end = -1;
      if (typeof len === "number") { end = j + len; if (!/^\s*endstream/.test(s.slice(end, end + 20))) end = -1; }
      if (end < 0) {
        const e = s.indexOf("endstream", j);
        end = e < 0 ? s.length : e;
        if (s[end - 1] === "\n") end--; if (s[end - 1] === "\r") end--;
      }
      entry.streamEnd = end;
      re.lastIndex = Math.max(re.lastIndex, end);
    }
    objs.set(num, entry); // 같은 번호가 여러 번이면(증분 갱신) 뒤의 것이 이긴다
  }
  return objs;
}

function resolve(objs, v) { return v && typeof v === "object" && "ref" in v ? objs.get(v.ref)?.value ?? null : v; }
function resolveDict(objs, v) { const r = resolve(objs, v); return r && typeof r === "object" && !Array.isArray(r) ? r : null; }

export function inspectPdf(u8) {
  const head = bytesToBinaryString(u8.subarray(0, 1024));
  if (!head.startsWith("%PDF-")) return { isPdf: false, isJpdf: false, pages: 0, images: [], fileSize: u8.length };
  const objs = scanPdfObjects(u8);
  let catalog = null;
  for (const [, e] of objs) if (e.dict && e.dict["/Type"] === "/Catalog") catalog = e.dict;
  let pages = 0;
  const pagesDict = catalog ? resolveDict(objs, catalog["/Pages"]) : null;
  if (pagesDict && typeof pagesDict["/Count"] === "number") pages = pagesDict["/Count"];
  if (!pages) for (const [, e] of objs) if (e.dict && e.dict["/Type"] === "/Page") pages++;
  const jp = catalog ? resolveDict(objs, catalog["/JPDF"]) : null;
  const images = [];
  if (jp) {
    for (const e of resolve(objs, jp["/Images"]) || []) {
      const d = resolveDict(objs, e); if (!d) continue;
      images.push({ page: d["/Page"] ?? 0, name: textOf(d["/Name"]), format: String(d["/Format"] || "").replace(/^\//, ""), width: d["/Width"] ?? 0, height: d["/Height"] ?? 0, lossless: d["/Lossless"] !== false, orientation: d["/Orientation"] ?? 1, plan: String(d["/Plan"] || "").replace(/^\//, ""), size: d["/Size"] ?? 0, sha256: textOf(d["/SHA256"]), attachment: textOf(d["/Attachment"]), xobject: d["/XObject"]?.ref ?? null });
    }
  }
  const isJpdf = head.includes("%JPDF-") || !!jp;
  return { isPdf: true, isJpdf, version: jp ? textOf(jp["/Version"]) : "", pages, images, fileSize: u8.length, hasAttachments: !!(catalog && catalog["/Names"]), objs, catalog };
}

function asList(v) { return v == null ? [] : Array.isArray(v) ? v : [v]; }

/** Flate + PNG 예측자 XObject → PNG 파일 바이트 (불가하면 null) */
async function flateXObjectToPng(objs, entry, u8) {
  const d = entry.dict;
  if (asList(resolve(objs, d["/Filter"])).map(String).join() !== "/FlateDecode") return null;
  const parms = resolveDict(objs, asList(resolve(objs, d["/DecodeParms"]))[0]);
  if (!parms || (parms["/Predictor"] ?? 1) < 10) return null;
  if (d["/Decode"] != null) return null;
  const width = d["/Width"], height = d["/Height"], bpc = d["/BitsPerComponent"] ?? 8, colors = parms["/Colors"] ?? 1;
  if ((parms["/Columns"] ?? 1) !== width || (parms["/BitsPerComponent"] ?? 8) !== bpc) return null;
  let cs = resolve(objs, d["/ColorSpace"]);
  let colorType, plte = null;
  if (Array.isArray(cs)) {
    if (cs.length === 4 && cs[0] === "/Indexed" && resolve(objs, cs[1]) === "/DeviceRGB") {
      const lookup = resolve(objs, cs[3]);
      if (lookup && lookup.hex != null) plte = fromHex(lookup.hex);
      else if (lookup && lookup.str != null) plte = Uint8Array.from(lookup.str, (c) => c.charCodeAt(0) & 255);
      else { const le = objs.get(cs[3]?.ref); if (le && le.streamStart >= 0) plte = await inflateIfNeeded(objs, le, u8); }
      if (!plte) return null;
      plte = plte.subarray(0, 3 * ((cs[2] ?? 255) + 1));
      colorType = 3; if (colors !== 1 || ![1, 2, 4, 8].includes(bpc)) return null;
    } else return null;
  } else if (cs === "/DeviceGray" && colors === 1 && [1, 2, 4, 8, 16].includes(bpc)) colorType = 0;
  else if (cs === "/DeviceRGB" && colors === 3 && [8, 16].includes(bpc)) colorType = 2;
  else return null;
  let trns = null;
  const mask = resolve(objs, d["/Mask"]);
  if (mask != null) {
    if (!Array.isArray(mask) || colorType === 3) return null;
    const vals = mask.map(Number);
    const need = 2 * (colorType === 2 ? 3 : 1);
    if (vals.length !== need) return null;
    for (let i = 0; i < vals.length; i += 2) if (vals[i] !== vals[i + 1]) return null;
    trns = new Uint8Array(need); for (let i = 0; i < vals.length; i += 2) { trns[i] = vals[i] >> 8; trns[i + 1] = vals[i] & 255; }
  }
  const zdata = u8.subarray(entry.streamStart, entry.streamEnd);
  return buildPng({ width, height, bitDepth: bpc, colorType, zdata, plte, trns });
}

async function inflateIfNeeded(objs, entry, u8) {
  const data = u8.subarray(entry.streamStart, entry.streamEnd);
  const f = asList(resolve(objs, entry.dict["/Filter"])).map(String);
  if (!f.length) return data;
  if (f.join() === "/FlateDecode") return inflate(data);
  return null;
}

/** 이미지 XObject → { bytes, ext } (우리 형식이 아니면 null) */
export async function xobjectToFile(objs, entry, u8) {
  const d = entry.dict;
  const filters = asList(resolve(objs, d["/Filter"])).map(String);
  if (filters.join() === "/DCTDecode") {
    const raw = u8.subarray(entry.streamStart, entry.streamEnd);
    return isJpeg(raw) ? { bytes: raw, ext: "jpg" } : null;
  }
  if (filters.join() === "/FlateDecode") {
    const base = await flateXObjectToPng(objs, entry, u8);
    if (!base) return null;
    const sm = d["/SMask"]?.ref != null ? objs.get(d["/SMask"].ref) : null;
    if (!sm) return { bytes: base, ext: "png" };
    const alphaPng = await flateXObjectToPng(objs, sm, u8);
    if (!alphaPng) return null;
    const b = await decodePng(base), a = await decodePng(alphaPng);
    if (!b || !a || a.colorType !== 0 || a.bitDepth !== 8 || a.width !== b.width || a.height !== b.height) return null;
    // 색 데이터를 8비트 회색/RGB 로 정규화
    let rgb, ct;
    if (b.colorType === 2 && b.bitDepth === 8) { rgb = b.samples; ct = 6; }
    else if (b.colorType === 0 && b.bitDepth === 8) { rgb = b.samples; ct = 4; }
    else { const sp = splitPngAlpha(b); rgb = sp.base.samples; ct = sp.base.colorType === 2 ? 6 : 4; }
    const n = b.width * b.height, ch = ct === 6 ? 3 : 1;
    const out = new Uint8Array(n * (ch + 1));
    for (let i = 0; i < n; i++) { for (let k = 0; k < ch; k++) out[i * (ch + 1) + k] = rgb[i * ch + k]; out[i * (ch + 1) + ch] = a.samples[i]; }
    return { bytes: await encodePng({ width: b.width, height: b.height, bitDepth: 8, colorType: ct, samples: out }), ext: "png" };
  }
  return null;
}

/**
 * PDF/JPDF 의 이미지들을 파일로 꺼낸다.
 * @returns {Promise<Array<{name, ext, bytes, page, method, bitExact}>>}
 */
export async function extractImages(u8, progress = null) {
  const info = inspectPdf(u8);
  if (!info.isPdf) throw new JpdfError("PDF 파일이 아닙니다.");
  const { objs } = info;
  const results = [];
  const used = new Set();
  const uniq = (name) => { let cand = name, k = 2; const stem = name.replace(/\.[^.]+$/, ""), ext = name.slice(stem.length); while (used.has(cand)) cand = `${stem} (${k++})${ext}`; used.add(cand); return cand; };

  if (info.images.length) {
    // 첨부파일 표
    let attachments = null;
    if (info.images.some((i) => i.attachment) && info.catalog) {
      attachments = new Map();
      const names = resolveDict(objs, info.catalog["/Names"]);
      const ef = names ? resolveDict(objs, names["/EmbeddedFiles"]) : null;
      const arr = ef ? resolve(objs, ef["/Names"]) : null;
      if (Array.isArray(arr)) for (let i = 0; i + 1 < arr.length; i += 2) {
        const fs = resolveDict(objs, arr[i + 1]); const efd = fs ? resolveDict(objs, fs["/EF"]) : null;
        const st = efd && efd["/F"]?.ref != null ? objs.get(efd["/F"].ref) : null;
        if (st && st.streamStart >= 0) attachments.set(textOf(arr[i]), st);
      }
    }
    for (const [k, im] of info.images.entries()) {
      progress?.(k, info.images.length, `${im.name} 꺼내는 중…`);
      const wanted = safeFilename(im.name || `page${im.page}`);
      if (attachments && im.attachment && attachments.has(im.attachment)) {
        const st = attachments.get(im.attachment);
        const bytes = await inflateIfNeeded(objs, st, u8);
        if (bytes) { results.push({ name: uniq(wanted), ext: wanted.split(".").pop(), bytes, page: im.page, method: "attachment", bitExact: im.sha256 ? (await sha256Hex(bytes)) === im.sha256 : null }); continue; }
      }
      const entry = im.xobject != null ? objs.get(im.xobject) : null;
      const r = entry && entry.dict ? await xobjectToFile(objs, entry, u8) : null;
      if (!r) { results.push({ name: uniq(wanted), ext: "", bytes: null, page: im.page, method: "unsupported", bitExact: null }); continue; }
      let name = wanted;
      const okExt = r.ext === "jpg" ? ["jpg", "jpeg", "jpe", "jfif"] : ["png"];
      if (!okExt.includes((name.split(".").pop() || "").toLowerCase())) name = name.replace(/\.[^.]+$/, "") + "." + r.ext;
      results.push({ name: uniq(name), ext: r.ext, bytes: r.bytes, page: im.page, method: r.ext === "jpg" ? "verbatim" : "rebuilt-png", bitExact: im.sha256 ? (await sha256Hex(r.bytes)) === im.sha256 : null });
    }
    progress?.(info.images.length, info.images.length, "완료");
    return results;
  }
  // 일반 PDF: 이미지 XObject 전부
  const entries = [...objs.entries()].filter(([, e]) => e.dict && e.dict["/Subtype"] === "/Image" && e.streamStart >= 0);
  const smaskRefs = new Set(entries.map(([, e]) => e.dict["/SMask"]?.ref).filter((x) => x != null));
  let k = 0;
  for (const [num, e] of entries) {
    if (smaskRefs.has(num)) continue;
    progress?.(k++, entries.length, `이미지 ${num} 꺼내는 중…`);
    const r = await xobjectToFile(objs, e, u8);
    if (r) results.push({ name: uniq(`image_${num}.${r.ext}`), ext: r.ext, bytes: r.bytes, page: 0, method: r.ext === "jpg" ? "verbatim" : "rebuilt-png", bitExact: null });
  }
  progress?.(entries.length, entries.length, "완료");
  return results;
}

// ---------------------------------------------------------------------------
// ZIP (저장 전용, 압축 없음) — 이미지 묶음 다운로드용
// ---------------------------------------------------------------------------
export function buildZip(files) {
  const parts = [], central = [];
  let offset = 0;
  const now = new Date();
  const dosTime = ((now.getHours() << 11) | (now.getMinutes() << 5) | (now.getSeconds() >> 1)) & 0xffff;
  const dosDate = (((now.getFullYear() - 1980) << 9) | ((now.getMonth() + 1) << 5) | now.getDate()) & 0xffff;
  for (const f of files) {
    const name = te.encode(f.name), data = f.bytes, crc = crc32(data);
    const lh = new Uint8Array(30 + name.length); const dv = new DataView(lh.buffer);
    dv.setUint32(0, 0x04034b50, true); dv.setUint16(4, 20, true); dv.setUint16(6, 0x0800, true); dv.setUint16(8, 0, true);
    dv.setUint16(10, dosTime, true); dv.setUint16(12, dosDate, true); dv.setUint32(14, crc, true); dv.setUint32(18, data.length, true); dv.setUint32(22, data.length, true);
    dv.setUint16(26, name.length, true); dv.setUint16(28, 0, true); lh.set(name, 30);
    const ch = new Uint8Array(46 + name.length); const cv = new DataView(ch.buffer);
    cv.setUint32(0, 0x02014b50, true); cv.setUint16(4, 20, true); cv.setUint16(6, 20, true); cv.setUint16(8, 0x0800, true); cv.setUint16(10, 0, true);
    cv.setUint16(12, dosTime, true); cv.setUint16(14, dosDate, true); cv.setUint32(16, crc, true); cv.setUint32(20, data.length, true); cv.setUint32(24, data.length, true);
    cv.setUint16(28, name.length, true); cv.setUint32(42, offset, true); ch.set(name, 46);
    parts.push(lh, data); central.push(ch); offset += lh.length + data.length;
  }
  const cdSize = central.reduce((a, c) => a + c.length, 0);
  const eocd = new Uint8Array(22); const ev = new DataView(eocd.buffer);
  ev.setUint32(0, 0x06054b50, true); ev.setUint16(8, files.length, true); ev.setUint16(10, files.length, true); ev.setUint32(12, cdSize, true); ev.setUint32(16, offset, true);
  return concatBytes([...parts, ...central, eocd]);
}

// ---------------------------------------------------------------------------
// 브라우저용 디코더 (canvas). 서비스 워커/페이지 모두에서 동작.
// ---------------------------------------------------------------------------
export const browserDecoders = {
  async decodeToRgba(bytes, mime) {
    if (typeof createImageBitmap !== "function") throw new JpdfError("이 환경에서는 이미지를 디코딩할 수 없습니다.");
    const blob = new Blob([bytes], { type: mime || "" });
    const bmp = await createImageBitmap(blob);
    const w = bmp.width, h = bmp.height;
    const canvas = typeof OffscreenCanvas !== "undefined" ? new OffscreenCanvas(w, h) : Object.assign(document.createElement("canvas"), { width: w, height: h });
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(bmp, 0, 0);
    const d = ctx.getImageData(0, 0, w, h);
    bmp.close?.();
    return { width: w, height: h, rgba: d.data };
  },
};

export function suggestOutputName(names, ext) {
  const stem = (names[0] || "images").replace(/\.[^.]+$/, "");
  return (names.length === 1 ? stem : `${stem} 외 ${names.length - 1}장`) + "." + ext;
}
