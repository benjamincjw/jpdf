// app.js — 변환/열기 페이지
import * as J from "./core/jpdf.js";

const $ = (id) => document.getElementById(id);
const hasChrome = typeof chrome !== "undefined" && chrome.storage;

// ---------------------------------------------------------------------------
// 탭
// ---------------------------------------------------------------------------
function showTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === "panel-" + name));
}
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));

// ---------------------------------------------------------------------------
// 상태
// ---------------------------------------------------------------------------
/** @type {Array<{id:number, name:string, type:string, bytes:Uint8Array, analysis:any, thumb:string}>} */
let items = [];
let nextId = 1;
let lastResult = null; // {bytes, name, url}
let openDoc = null;    // {bytes, name, info, url, extracted}

function setStatus(text, cls = "") { const el = $("status"); el.textContent = text; el.className = "status " + cls; }

/** 파일 저장: 확장 환경이면 chrome.downloads(파일명 보장), 아니면 <a download> */
async function downloadUrl(url, name) {
  if (hasChrome && chrome.downloads) {
    try { await chrome.downloads.download({ url, filename: name, saveAs: false }); return; } catch (e) { console.warn("chrome.downloads 실패, <a download> 로 대체", e); }
  }
  const a = document.createElement("a"); a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
}

// ---------------------------------------------------------------------------
// 이미지 추가
// ---------------------------------------------------------------------------
async function addFiles(fileList) {
  const files = [...fileList];
  let added = 0;
  for (const f of files) {
    if (/\.(jpdf|pdf)$/i.test(f.name) || f.type === "application/pdf") { await openPdfFile(f); showTab("open"); continue; }
    const bytes = new Uint8Array(await f.arrayBuffer());
    await addBytes(f.name, f.type, bytes);
    added++;
  }
  if (added) setStatus(`이미지 ${added}장을 추가했습니다.`);
}

async function addBytes(name, type, bytes) {
  let analysis;
  try { analysis = await J.analyzeImage(bytes, name, type); }
  catch (e) { analysis = { format: "?", plan: "error", lossless: false, note: "오류: " + e.message, width: 0, height: 0, orientation: 1 }; }
  const it = { id: nextId++, name, type, bytes, analysis, thumb: "" };
  items.push(it);
  renderList();
  makeThumb(it).catch(() => {});
}

async function makeThumb(it) {
  const blob = new Blob([it.bytes], { type: it.type || "" });
  try {
    const bmp = await createImageBitmap(blob, { imageOrientation: "from-image" });
    if (!it.analysis.width) { it.analysis.width = bmp.width; it.analysis.height = bmp.height; }
    const scale = Math.min(1, 112 / bmp.width, 84 / bmp.height);
    const c = document.createElement("canvas");
    c.width = Math.max(1, Math.round(bmp.width * scale)); c.height = Math.max(1, Math.round(bmp.height * scale));
    c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
    bmp.close();
    it.thumb = c.toDataURL("image/png");
  } catch { it.thumb = ""; }
  renderList();
}

function renderList() {
  const ol = $("image-list");
  ol.innerHTML = "";
  items.forEach((it, i) => {
    const li = document.createElement("li");
    li.className = "item";
    const a = it.analysis;
    const dims = a.width ? `${a.orientation >= 5 ? a.height : a.width}×${a.orientation >= 5 ? a.width : a.height}` : "";
    li.innerHTML = `
      <img alt="" src="${it.thumb || ""}">
      <div><div class="name">${i + 1}. ${escapeHtml(it.name)}</div>
      <div class="meta ${a.lossless ? "" : "warn"}">${dims} ${escapeHtml(a.format)} · ${J.humanSize(it.bytes.length)} · ${escapeHtml(a.note)}</div></div>
      <div class="ctl"><button data-act="up" title="위로">▲</button><button data-act="down" title="아래로">▼</button><button data-act="del" title="제거">✕</button></div>`;
    li.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
      const act = b.dataset.act;
      if (act === "del") items.splice(i, 1);
      else if (act === "up" && i > 0) [items[i - 1], items[i]] = [items[i], items[i - 1]];
      else if (act === "down" && i < items.length - 1) [items[i + 1], items[i]] = [items[i], items[i + 1]];
      renderList();
    }));
    ol.appendChild(li);
  });
  const total = items.reduce((s, it) => s + it.bytes.length, 0);
  $("count").textContent = items.length ? `이미지 ${items.length}장 · ${J.humanSize(total)}` : "";
  if (!$("opt-name").value && items.length) $("opt-name").placeholder = J.suggestOutputName(items.map((i) => i.name), currentFormat());
}

function escapeHtml(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

// 드롭존
function wireDrop(zone, input, handler) {
  zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", (e) => { e.preventDefault(); zone.classList.remove("over"); handler(e.dataTransfer.files); });
  input.addEventListener("change", () => { handler(input.files); input.value = ""; });
}
wireDrop($("drop-images"), $("file-images"), addFiles);
wireDrop($("drop-pdf"), $("file-pdf"), (files) => files[0] && openPdfFile(files[0]));
document.body.addEventListener("dragover", (e) => e.preventDefault());
document.body.addEventListener("drop", (e) => { if (e.target.closest(".drop")) return; e.preventDefault(); addFiles(e.dataTransfer.files); });

$("btn-clear").addEventListener("click", () => { items = []; renderList(); });
$("btn-sort").addEventListener("click", () => { items.sort((a, b) => a.name.localeCompare(b.name, "ko")); renderList(); });

// ---------------------------------------------------------------------------
// 옵션
// ---------------------------------------------------------------------------
function currentFormat() { return document.querySelector("input[name=fmt]:checked").value; }
function readOptions() {
  return {
    pageSize: $("opt-page").value, orientation: $("opt-orient").value,
    marginMm: parseFloat($("opt-margin").value || "0"), defaultDpi: parseFloat($("opt-dpi").value || "96"),
    useImageDpi: $("opt-usedpi").checked, fitUpscale: $("opt-upscale").checked, embedOriginals: $("opt-embed").checked,
    format: currentFormat(),
  };
}
function applyOptions(o) {
  if (!o) return;
  if (o.pageSize) $("opt-page").value = o.pageSize;
  if (o.orientation) $("opt-orient").value = o.orientation;
  if (o.marginMm != null) $("opt-margin").value = o.marginMm;
  if (o.defaultDpi != null) $("opt-dpi").value = o.defaultDpi;
  if (o.useImageDpi != null) $("opt-usedpi").checked = o.useImageDpi;
  if (o.fitUpscale != null) $("opt-upscale").checked = o.fitUpscale;
  if (o.embedOriginals != null) $("opt-embed").checked = o.embedOriginals;
  if (o.format) document.querySelector(`input[name=fmt][value=${o.format}]`).checked = true;
  onPageChange();
}
function onPageChange() {
  const fixed = $("opt-page").value !== "image";
  $("opt-orient").disabled = !fixed; $("opt-upscale").disabled = !fixed;
}
$("opt-page").addEventListener("change", onPageChange);
document.querySelectorAll("input[name=fmt]").forEach((r) => r.addEventListener("change", () => {
  const n = $("opt-name");
  if (n.value) n.value = n.value.replace(/\.(jpdf|pdf)$/i, "") + "." + currentFormat();
  renderList();
}));
async function saveOptions() { if (hasChrome) await chrome.storage.local.set({ options: readOptions() }); }
async function loadOptions() { if (hasChrome) applyOptions((await chrome.storage.local.get("options")).options); }
["opt-page", "opt-orient", "opt-margin", "opt-dpi", "opt-usedpi", "opt-upscale", "opt-embed"].forEach((id) => $(id).addEventListener("change", saveOptions));
document.querySelectorAll("input[name=fmt]").forEach((r) => r.addEventListener("change", saveOptions));

// ---------------------------------------------------------------------------
// 변환
// ---------------------------------------------------------------------------
$("btn-convert").addEventListener("click", async () => {
  if (!items.length) { setStatus("먼저 이미지를 추가하세요.", "err"); return; }
  const bad = items.filter((i) => i.analysis.plan === "error");
  if (bad.length) { setStatus("열 수 없는 파일이 있습니다: " + bad.map((b) => b.name).join(", "), "err"); return; }
  const o = readOptions();
  const jpdf = o.format === "jpdf";
  let name = $("opt-name").value.trim() || J.suggestOutputName(items.map((i) => i.name), o.format);
  if (!/\.(jpdf|pdf)$/i.test(name)) name += "." + o.format;
  $("btn-convert").disabled = true; $("prog").classList.remove("hidden"); $("prog").value = 0; $("result").classList.add("hidden");
  try {
    J.validateOptions(o);
    const t0 = performance.now();
    const { bytes, report } = await J.convertImages(items.map((i) => ({ name: i.name, bytes: i.bytes, type: i.type })), { ...o, jpdf }, J.browserDecoders,
      (i, n, msg) => { $("prog").value = n ? i / n : 0; setStatus(`[${i}/${n}] ${msg}`); });
    if (lastResult?.url) URL.revokeObjectURL(lastResult.url);
    const url = URL.createObjectURL(new Blob([bytes], { type: "application/pdf" }));
    lastResult = { bytes, name, url, report };
    const stored = report.images.reduce((s, r) => s + r.bytesStored, 0);
    setStatus(`완료: ${name} — ${report.pages}페이지, ${J.humanSize(bytes.length)} (이미지 데이터 ${J.humanSize(stored)}), ${((performance.now() - t0) / 1000).toFixed(1)}초` + (report.allLossless ? "" : " ⚠ 일부 이미지는 형식 변환이 있었습니다."), "ok");
    const a = $("dl-link"); a.href = url; a.download = name; a.textContent = `다시 다운로드 (${name})`;
    a.onclick = (ev) => { ev.preventDefault(); downloadUrl(url, name); };
    $("result").classList.remove("hidden");
    await downloadUrl(url, name); // 바로 저장
  } catch (e) {
    console.error(e);
    setStatus("실패: " + (e.message || e), "err");
  } finally {
    $("btn-convert").disabled = false; $("prog").classList.add("hidden");
  }
});
$("btn-view-pdf").addEventListener("click", () => { if (lastResult) window.open(lastResult.url, "_blank"); });
$("btn-open-result").addEventListener("click", async () => { if (!lastResult) return; await openPdfBytes(lastResult.bytes, lastResult.name); showTab("open"); });

// ---------------------------------------------------------------------------
// 열기 (JPDF / PDF)
// ---------------------------------------------------------------------------
async function openPdfFile(file) { await openPdfBytes(new Uint8Array(await file.arrayBuffer()), file.name); }

async function openPdfBytes(bytes, name) {
  if (openDoc?.url) URL.revokeObjectURL(openDoc.url);
  for (const u of openDoc?.imgUrls || []) URL.revokeObjectURL(u);
  const info = J.inspectPdf(bytes);
  if (!info.isPdf) { $("pdf-info").classList.remove("hidden"); $("pdf-name").textContent = name; $("pdf-summary").textContent = "PDF 파일이 아닙니다."; return; }
  const url = URL.createObjectURL(new Blob([bytes], { type: "application/pdf" }));
  openDoc = { bytes, name, info, url, extracted: null, imgUrls: [] };
  $("pdf-info").classList.remove("hidden");
  $("pdf-name").textContent = name;
  const kind = info.isJpdf ? `JPDF ${info.version || ""}`.trim() : "PDF";
  const fmts = [...new Set(info.images.map((i) => i.format))].join(", ");
  $("pdf-summary").textContent = `${kind} · ${info.pages}페이지` + (info.images.length ? ` · 이미지 ${info.images.length}장 (${fmts})` : "") + ` · ${J.humanSize(bytes.length)}`;
  $("pdf-images").textContent = info.images.length ? "포함: " + info.images.slice(0, 8).map((i) => i.name).join(", ") + (info.images.length > 8 ? " …" : "") +
    (info.images.every((i) => i.lossless) ? " · 모두 무손실로 저장됨" : " · 일부 형식 변환됨") : (info.isJpdf ? "" : "일반 PDF: 페이지의 이미지를 그대로 꺼낼 수 있습니다.");
  const dl = $("dl-pdf"); dl.href = url; dl.download = name.replace(/\.jpdf$/i, "") + ".pdf";
  dl.onclick = (ev) => { ev.preventDefault(); downloadUrl(url, dl.download); };
  $("view-pdf").classList.add("hidden"); $("view-images").classList.add("hidden"); $("gallery").innerHTML = "";
}

$("btn-show-pdf").addEventListener("click", () => {
  if (!openDoc) return;
  $("view-images").classList.add("hidden");
  $("view-pdf").classList.remove("hidden");
  $("pdf-frame").src = openDoc.url;
});
$("btn-newtab-pdf").addEventListener("click", () => { if (openDoc) window.open(openDoc.url, "_blank"); });

async function ensureExtracted() {
  if (!openDoc) return [];
  if (openDoc.extracted) return openDoc.extracted;
  $("images-status").textContent = "이미지를 꺼내는 중…";
  openDoc.extracted = await J.extractImages(openDoc.bytes, (i, n, msg) => { $("images-status").textContent = `[${i}/${n}] ${msg}`; });
  return openDoc.extracted;
}

$("btn-show-images").addEventListener("click", async () => {
  if (!openDoc) return;
  $("view-pdf").classList.add("hidden");
  $("view-images").classList.remove("hidden");
  try {
    const res = await ensureExtracted();
    const g = $("gallery"); g.innerHTML = "";
    const methods = { attachment: "원본 첨부파일", verbatim: "JPEG 바이트 그대로", "rebuilt-png": "PNG 재조립(픽셀 동일)", unsupported: "지원하지 않는 형식" };
    for (const r of res) {
      const fig = document.createElement("figure");
      if (r.bytes) {
        const u = URL.createObjectURL(new Blob([r.bytes], { type: r.ext === "jpg" ? "image/jpeg" : "image/png" }));
        openDoc.imgUrls.push(u);
        fig.innerHTML = `<img src="${u}" alt=""><figcaption><strong>${escapeHtml(r.name)}</strong><br>
          <span class="badge">${methods[r.method] || r.method}</span>${r.bitExact === true ? '<span class="badge ok">원본과 동일 (SHA-256)</span>' : r.bitExact === false ? '<span class="badge">픽셀 동일 · 파일은 재구성</span>' : ""}
          <span class="muted">${J.humanSize(r.bytes.length)}</span><br><a class="btn" href="${u}" download="${escapeHtml(r.name)}">다운로드</a></figcaption>`;
      } else {
        fig.innerHTML = `<figcaption><strong>${escapeHtml(r.name)}</strong><br><span class="badge">${methods[r.method] || r.method}</span></figcaption>`;
      }
      g.appendChild(fig);
    }
    $("images-status").textContent = res.length ? `이미지 ${res.length}장` : "꺼낼 수 있는 이미지가 없습니다.";
  } catch (e) {
    console.error(e);
    $("images-status").textContent = "실패: " + (e.message || e);
  }
});

$("btn-zip").addEventListener("click", async () => {
  if (!openDoc) return;
  try {
    const res = (await ensureExtracted()).filter((r) => r.bytes);
    if (!res.length) { $("images-status").textContent = "꺼낼 수 있는 이미지가 없습니다."; return; }
    const zip = J.buildZip(res.map((r) => ({ name: r.name, bytes: r.bytes })));
    const url = URL.createObjectURL(new Blob([zip], { type: "application/zip" }));
    await downloadUrl(url, openDoc.name.replace(/\.(jpdf|pdf)$/i, "") + "_images.zip");
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (e) { $("images-status").textContent = "실패: " + (e.message || e); }
});

// ---------------------------------------------------------------------------
// 서비스 워커에서 넘어온 작업 (#collect, #pending)
// ---------------------------------------------------------------------------
function dataUrlToBytes(dataUrl) {
  const i = dataUrl.indexOf(",");
  const bin = atob(dataUrl.slice(i + 1));
  const out = new Uint8Array(bin.length);
  for (let k = 0; k < bin.length; k++) out[k] = bin.charCodeAt(k);
  return out;
}

async function handlePending() {
  if (!hasChrome) return;
  const { pending } = await chrome.storage.session.get("pending");
  if (!pending) return;
  await chrome.storage.session.remove("pending");
  for (const p of pending) await addBytes(p.name, p.type, dataUrlToBytes(p.b64));
  setStatus(`이미지 ${pending.length}장을 페이지에서 가져왔습니다.`);
}

async function handleCollect() {
  if (!hasChrome) return;
  const { collect } = await chrome.storage.session.get("collect");
  if (!collect) return;
  await chrome.storage.session.remove("collect");
  const box = $("collect-box"); box.classList.remove("hidden");
  $("collect-title").textContent = `— ${collect.title || collect.url || ""} (${collect.items.length}개)`;
  const grid = $("collect-grid"); grid.innerHTML = "";
  collect.items.forEach((it, i) => {
    const label = document.createElement("label");
    const big = (it.w || 0) >= 100 && (it.h || 0) >= 100;
    label.className = big ? "sel" : "";
    label.innerHTML = `<input type="checkbox" data-i="${i}" ${big ? "checked" : ""}><img src="${escapeHtml(it.src)}" alt="" loading="lazy"><div class="cap">${it.w || "?"}×${it.h || "?"} ${escapeHtml(it.alt || it.src.split("/").pop() || "")}</div>`;
    label.querySelector("input").addEventListener("change", (e) => label.classList.toggle("sel", e.target.checked));
    grid.appendChild(label);
  });
  $("collect-all").onclick = () => grid.querySelectorAll("input").forEach((c) => { c.checked = true; c.closest("label").classList.add("sel"); });
  $("collect-none").onclick = () => grid.querySelectorAll("input").forEach((c) => { c.checked = false; c.closest("label").classList.remove("sel"); });
  $("collect-import").onclick = async () => {
    const chosen = [...grid.querySelectorAll("input:checked")].map((c) => collect.items[+c.dataset.i]);
    let ok = 0, fail = 0;
    for (const it of chosen) {
      $("collect-status").textContent = `가져오는 중… ${ok + fail + 1}/${chosen.length}`;
      try {
        const r = await fetch(it.src, { credentials: "include" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const type = r.headers.get("content-type") || "";
        const bytes = new Uint8Array(await r.arrayBuffer());
        let name = "image";
        try { const u = new URL(it.src); if (u.protocol !== "data:") name = decodeURIComponent(u.pathname.split("/").filter(Boolean).pop() || "image").replace(/[\\/:*?"<>|]/g, "_"); } catch {}
        if (!/\.[a-z0-9]{2,5}$/i.test(name)) name += "." + (J.isJpeg(bytes) ? "jpg" : J.isPng(bytes) ? "png" : (type.split("/")[1] || "img").split(";")[0]);
        await addBytes(name, type, bytes); ok++;
      } catch (e) { console.warn("skip", it.src, e); fail++; }
    }
    $("collect-status").textContent = `가져옴 ${ok}장` + (fail ? `, 실패 ${fail}장` : "");
    if (ok) box.classList.add("hidden");
  };
}

(async () => {
  await loadOptions();
  onPageChange();
  if (location.hash === "#pending") await handlePending();
  if (location.hash === "#collect") await handleCollect();
  history.replaceState(null, "", location.pathname);
})();

// 테스트용 노출
window.jpdfApp = { get items() { return items; }, addBytes, openPdfBytes, get lastResult() { return lastResult; }, get openDoc() { return openDoc; }, J };
