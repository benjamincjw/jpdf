// background.js — 서비스 워커: 우클릭 메뉴, 탭 이미지 수집, 확장 아이콘 클릭
import * as J from "./core/jpdf.js";

const MENU = {
  IMG_JPDF: "jpdf-image-jpdf",
  IMG_PDF: "jpdf-image-pdf",
  IMG_APP: "jpdf-image-app",
  PAGE_COLLECT: "jpdf-page-collect",
  PAGE_OPEN: "jpdf-page-open",
};

function setupMenus() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: MENU.IMG_JPDF, title: "이 이미지를 JPDF로 저장", contexts: ["image"] });
    chrome.contextMenus.create({ id: MENU.IMG_PDF, title: "이 이미지를 PDF로 저장", contexts: ["image"] });
    chrome.contextMenus.create({ id: MENU.IMG_APP, title: "JPDF 변환 페이지에 추가…", contexts: ["image"] });
    chrome.contextMenus.create({ id: MENU.PAGE_COLLECT, title: "이 페이지의 이미지 모두 모아 PDF로…", contexts: ["page", "action"] });
    chrome.contextMenus.create({ id: MENU.PAGE_OPEN, title: "JPDF 변환기 열기", contexts: ["action"] });
  });
}

chrome.runtime.onInstalled.addListener(setupMenus);
chrome.runtime.onStartup.addListener(setupMenus);

chrome.action.onClicked.addListener(() => openApp(""));

function openApp(hash) {
  return chrome.tabs.create({ url: chrome.runtime.getURL("app.html") + (hash || "") });
}

// ---------------------------------------------------------------------------
// 이미지 가져오기: 서비스 워커에서 직접 fetch, 안 되면 탭 안에서 fetch (blob: 등)
// ---------------------------------------------------------------------------
async function fetchImageBytes(url, tabId) {
  try {
    const r = await fetch(url, { credentials: "include" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return { bytes: new Uint8Array(await r.arrayBuffer()), type: r.headers.get("content-type") || "" };
  } catch (e) {
    if (tabId == null) throw e;
    const [res] = await chrome.scripting.executeScript({
      target: { tabId },
      func: async (u) => {
        const r = await fetch(u);
        const b = await r.blob();
        const buf = await b.arrayBuffer();
        let s = "";
        const a = new Uint8Array(buf);
        for (let i = 0; i < a.length; i += 0x8000) s += String.fromCharCode.apply(null, a.subarray(i, i + 0x8000));
        return { b64: btoa(s), type: b.type };
      },
      args: [url],
    });
    if (!res || !res.result) throw new Error("이미지를 가져올 수 없습니다.");
    const bin = atob(res.result.b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return { bytes, type: res.result.type };
  }
}

function nameFromUrl(url, fallback = "image") {
  try {
    const u = new URL(url);
    if (u.protocol === "data:") return fallback;
    let base = decodeURIComponent(u.pathname.split("/").filter(Boolean).pop() || "");
    base = base.replace(/[\\/:*?"<>|\x00-\x1f]/g, "_").slice(0, 100);
    return base || fallback;
  } catch { return fallback; }
}

function guessExt(bytes, type, name) {
  if (J.isJpeg(bytes)) return "jpg";
  if (J.isPng(bytes)) return "png";
  const m = /\.([a-z0-9]{2,5})$/i.exec(name);
  if (m) return m[1].toLowerCase();
  const t = (type || "").split("/")[1];
  return t ? t.split(";")[0] : "img";
}

function bytesToDataUrl(bytes, mime) {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return `data:${mime};base64,` + btoa(s);
}

async function saveImageAs(info, tab, asJpdf) {
  const { bytes, type } = await fetchImageBytes(info.srcUrl, tab?.id);
  let name = nameFromUrl(info.srcUrl);
  if (!/\.[a-z0-9]{2,5}$/i.test(name)) name += "." + guessExt(bytes, type, name);
  const settings = (await chrome.storage.local.get("options")).options || {};
  const { bytes: out } = await J.convertImages([{ name, bytes, type }], { ...settings, jpdf: asJpdf }, J.browserDecoders);
  const outName = name.replace(/\.[^.]+$/, "") + (asJpdf ? ".jpdf" : ".pdf");
  await chrome.downloads.download({ url: bytesToDataUrl(out, "application/pdf"), filename: outName, saveAs: false });
}

async function addImageToApp(info, tab) {
  const { bytes, type } = await fetchImageBytes(info.srcUrl, tab?.id);
  let name = nameFromUrl(info.srcUrl);
  if (!/\.[a-z0-9]{2,5}$/i.test(name)) name += "." + guessExt(bytes, type, name);
  await chrome.storage.session.set({ pending: [{ name, type, b64: bytesToDataUrl(bytes, type || "application/octet-stream") }] });
  await openApp("#pending");
}

function collectImagesInPage() {
  const seen = new Set();
  const out = [];
  for (const img of document.images) {
    const src = img.currentSrc || img.src;
    if (!src || seen.has(src)) continue;
    seen.add(src);
    out.push({ src, w: img.naturalWidth, h: img.naturalHeight, alt: img.alt || "" });
  }
  return { title: document.title, url: location.href, items: out };
}

async function collectFromTab(tab) {
  const [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: collectImagesInPage });
  const data = res?.result || { title: tab.title, url: tab.url, items: [] };
  await chrome.storage.session.set({ collect: data });
  await openApp("#collect");
}

chrome.contextMenus.onClicked.addListener((info, tab) => {
  const run = async () => {
    switch (info.menuItemId) {
      case MENU.IMG_JPDF: return saveImageAs(info, tab, true);
      case MENU.IMG_PDF: return saveImageAs(info, tab, false);
      case MENU.IMG_APP: return addImageToApp(info, tab);
      case MENU.PAGE_COLLECT: return collectFromTab(tab);
      case MENU.PAGE_OPEN: return openApp("");
    }
  };
  run().catch((e) => {
    console.error("[JPDF]", e);
    chrome.notifications?.create?.({ type: "basic", iconUrl: "icons/128.png", title: "JPDF", message: String(e.message || e) });
  });
});

// 테스트/디버깅용: 다른 문맥에서 호출할 수 있게 노출
self.jpdfTest = { fetchImageBytes, nameFromUrl, guessExt, bytesToDataUrl, J };
