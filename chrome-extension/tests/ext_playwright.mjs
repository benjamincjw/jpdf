// Playwright 로 실제 Chromium 에 확장을 로드해 검증한다.
//   NODE_PATH=$(npm root -g) node tests/ext_playwright.mjs <fixtures_dir> <out_dir> [http_base]
import { chromium } from "playwright";
import { mkdirSync, readFileSync, existsSync, copyFileSync } from "node:fs";
import { join, resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const [fixtures, outDir, httpBase] = process.argv.slice(2);
const EXT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
mkdirSync(outDir, { recursive: true });
const shots = (n) => join(outDir, n);
const results = { ok: [], fail: [] };
const check = (name, cond, extra = "") => { (cond ? results.ok : results.fail).push(name + (extra ? " — " + extra : "")); if (!cond) console.error("FAIL:", name, extra); };

const context = await chromium.launchPersistentContext(join(outDir, "profile"), {
  headless: true, channel: "chromium",
  args: [`--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`],
  acceptDownloads: true, viewport: { width: 1280, height: 900 },
});
try {
  let [sw] = context.serviceWorkers();
  if (!sw) sw = await context.waitForEvent("serviceworker", { timeout: 15000 });
  const extId = new URL(sw.url()).host;
  check("service worker registered", !!extId, sw.url());

  // 메뉴가 만들어졌는지
  await new Promise((r) => setTimeout(r, 800));
  const menuCount = await sw.evaluate(() => new Promise((res) => { try { chrome.contextMenus.removeAll(() => res(-1)); } catch (e) { res(-2); } }));
  check("contextMenus API usable in SW", menuCount === -1, String(menuCount));
  await sw.evaluate(() => { chrome.contextMenus.create({ id: "t1", title: "t", contexts: ["image"] }); });

  // ---- 앱 페이지: 변환 ----
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  await page.goto(`chrome-extension://${extId}/app.html`);
  await page.waitForSelector("#drop-images");
  // (Playwright 의 setInputFiles 는 비ASCII/괄호 파일명을 못 넘기므로, 한글 이름은 페이지 안에서 직접 추가한다)
  const files = ["rgb_baseline.jpg", "portrait_o6.jpg", "rgba.png", "palette.png", "lossless.webp", "anim.gif", "bitmap.bmp"].map((n) => join(fixtures, n)).filter(existsSync);
  await page.setInputFiles("#file-images", files);
  await page.waitForFunction((n) => document.querySelectorAll("#image-list .item").length === n, files.length);
  if (httpBase) {
    await page.evaluate(async (base) => {
      const r = await fetch(base + "/" + encodeURIComponent("한글 이름 (테스트).jpg"));
      await window.jpdfApp.addBytes("한글 이름 (테스트).jpg", "image/jpeg", new Uint8Array(await r.arrayBuffer()));
    }, httpBase);
    files.push(join(fixtures, "한글 이름 (테스트).jpg"));
    await page.waitForFunction((n) => document.querySelectorAll("#image-list .item").length === n, files.length);
  }
  await page.waitForFunction(() => [...document.querySelectorAll("#image-list .item img")].every((i) => i.getAttribute("src")), null, { timeout: 15000 }).catch(() => {});
  await page.screenshot({ path: shots("ext_1_list.png") });
  const notes = await page.$$eval("#image-list .meta", (els) => els.map((e) => e.textContent));
  check("list shows lossless notes", notes.every((n) => n.includes("무손실")), notes.join(" | "));

  // Playwright 는 다운로드를 가로채 임시 이름으로 저장하므로(파일명 검증 불가) 상태와 내용만 확인한다
  const waitDownload = (minCount) => page.evaluate(async (n) => {
    for (let i = 0; i < 80; i++) {
      const ds = await chrome.downloads.search({ orderBy: ["-startTime"] });
      if (ds.length >= n && ds[0].state === "complete") return { filename: ds[0].filename, bytes: ds[0].fileSize, mime: ds[0].mime };
      await new Promise((r) => setTimeout(r, 250));
    }
    return null;
  }, minCount);
  await page.click("#btn-convert");
  await page.waitForFunction(() => document.getElementById("status").textContent.startsWith("완료") || document.getElementById("status").classList.contains("err"), null, { timeout: 60000 });
  const status = await page.textContent("#status");
  check("convert status ok", status.startsWith("완료"), status);
  const dl = await waitDownload(1);
  check("jpdf downloaded via chrome.downloads", !!dl && dl.bytes > 30000, JSON.stringify(dl));
  await page.screenshot({ path: shots("ext_2_converted.png") });
  const jpdfPath = dl ? dl.filename : null;
  if (jpdfPath) {
    const head = readFileSync(jpdfPath).subarray(0, 40).toString("latin1");
    check("jpdf header", head.startsWith("%PDF-1.7") && head.includes("%JPDF-1.0"), head.replace(/[^\x20-\x7e]/g, "."));
    copyFileSync(jpdfPath, join(outDir, "ext_result.jpdf"));
  }

  // ---- 열기 탭 ----
  await page.click("#btn-open-result");
  await page.waitForSelector("#pdf-info:not(.hidden)");
  const summary = await page.textContent("#pdf-summary");
  check("open summary", summary.includes("JPDF") && summary.includes(`${files.length}페이지`), summary);
  await page.click("#btn-show-images");
  await page.waitForFunction((n) => document.querySelectorAll("#gallery figure").length === n, files.length, { timeout: 30000 });
  const badges = await page.$$eval("#gallery figure", (figs) => figs.map((f) => f.querySelector("figcaption").textContent.replace(/\s+/g, " ").trim()));
  check("gallery has SHA badges for jpeg", badges.filter((b) => b.includes("원본과 동일")).length >= 3, badges.join(" || "));
  await page.screenshot({ path: shots("ext_3_gallery.png"), fullPage: true });
  await page.click("#btn-show-pdf");
  await page.waitForTimeout(1500);
  await page.screenshot({ path: shots("ext_4_pdfview.png") });

  // ZIP
  await page.click("#btn-zip");
  const zdl = await waitDownload(2);
  check("zip download", !!zdl && zdl.bytes > 1000, JSON.stringify(zdl));
  if (zdl) copyFileSync(zdl.filename, join(outDir, "ext_images.zip"));

  // ---- 서비스 워커 경로: 우클릭 저장과 같은 함수 ----
  if (httpBase) {
    const r = await sw.evaluate(async (base) => {
      const { fetchImageBytes, J } = self.jpdfTest;
      const { bytes, type } = await fetchImageBytes(base + "/lossless.webp");
      const out = await J.convertImages([{ name: "lossless.webp", bytes, type }], { jpdf: true }, J.browserDecoders);
      const id = await chrome.downloads.download({ url: "data:application/pdf;base64," + btoa(String.fromCharCode(...out.bytes)), filename: "sw_test.jpdf", saveAs: false });
      let state = "?";
      for (let i = 0; i < 40; i++) { const [d] = await chrome.downloads.search({ id }); state = d?.state; if (state === "complete" || state === "interrupted") break; await new Promise((r) => setTimeout(r, 250)); }
      return { size: out.bytes.length, plan: out.report.images[0].plan, state };
    }, httpBase);
    check("SW fetch+convert(webp via OffscreenCanvas)", r.plan === "other" && r.size > 1000, JSON.stringify(r));
    check("SW chrome.downloads works", r.state === "complete", r.state);
  }

  check("no page errors", errors.length === 0, errors.join(" | "));
} finally {
  await context.close();
}
console.log(JSON.stringify(results, null, 1));
process.exit(results.fail.length ? 1 : 0);
