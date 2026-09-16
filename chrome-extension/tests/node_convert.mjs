// Node 테스트 도우미: JS 엔진으로 픽스처를 변환/추출한다 (Python 테스트가 결과를 검증).
//   node node_convert.mjs convert <out.jpdf> [--page A4 --margin 10 --dpi 72 --no-image-dpi --embed] <img...>
//   node node_convert.mjs extract <in.pdf> <outdir>
//   node node_convert.mjs info <in.pdf>
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { basename, join } from "node:path";
import * as J from "../core/jpdf.js";

const [cmd, ...rest] = process.argv.slice(2);

if (cmd === "convert") {
  const out = rest.shift();
  const opts = { jpdf: out.toLowerCase().endsWith(".jpdf") };
  const files = [];
  while (rest.length) {
    const a = rest.shift();
    if (a === "--page") opts.pageSize = rest.shift();
    else if (a === "--margin") opts.marginMm = parseFloat(rest.shift());
    else if (a === "--dpi") opts.defaultDpi = parseFloat(rest.shift());
    else if (a === "--no-image-dpi") opts.useImageDpi = false;
    else if (a === "--embed") opts.embedOriginals = true;
    else if (a === "--orientation") opts.orientation = rest.shift();
    else files.push(a);
  }
  const inputs = files.map((f) => ({ name: basename(f), bytes: new Uint8Array(readFileSync(f)) }));
  const { bytes, report } = await J.convertImages(inputs, opts, null);
  writeFileSync(out, bytes);
  console.log(JSON.stringify(report));
} else if (cmd === "extract") {
  const [inp, outdir] = rest;
  mkdirSync(outdir, { recursive: true });
  const u8 = new Uint8Array(readFileSync(inp));
  const res = await J.extractImages(u8);
  const summary = [];
  for (const r of res) {
    if (r.bytes) writeFileSync(join(outdir, r.name), r.bytes);
    summary.push({ name: r.name, page: r.page, method: r.method, bitExact: r.bitExact, size: r.bytes ? r.bytes.length : 0 });
  }
  console.log(JSON.stringify(summary));
} else if (cmd === "info") {
  const u8 = new Uint8Array(readFileSync(rest[0]));
  const info = J.inspectPdf(u8);
  delete info.objs; delete info.catalog;
  console.log(JSON.stringify(info));
} else {
  console.error("usage: convert|extract|info");
  process.exit(2);
}
