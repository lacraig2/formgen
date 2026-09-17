// Open the built single-file in a real browser (Firefox) and prove it actually
// boots and fills -- the one thing check_single.mjs cannot, because it runs in
// Node where packages load from disk and never exercise the in-page fetch shim
// or Pyodide's browser package resolution. This is the check that would have
// caught the missing packageBaseUrl.
import { firefox } from "playwright";
import path from "node:path";

const ROOT = path.resolve(import.meta.dirname, "..", "..");
const url = "file://" + path.join(ROOT, "dist", "formgen.html");

const browser = await firefox.launch();
try {
  const page = await browser.newPage();
  page.on("pageerror", e => console.log("  [pageerror]", String(e).split("\n")[0]));
  await page.goto(url);

  let booted = false;
  for (let i = 0; i < 150; i++) {
    const s = await page.evaluate(() => {
      const b = document.getElementById("boot"), m = document.getElementById("bootmsg");
      return { hidden: b && b.hidden, msg: m && m.textContent };
    });
    if (s.hidden) { booted = true; break; }
    if (s.msg && s.msg.startsWith("Could not start")) throw new Error(s.msg);
    await new Promise(r => setTimeout(r, 1000));
  }
  if (!booted) throw new Error("timed out waiting for the page to boot");

  const out = await page.evaluate(async () => {
    const s = await window.__bench.starter();
    const bytes = Uint8Array.from(atob(s), c => c.charCodeAt(0));
    const info = await window.__bench.inspect(bytes);
    const filled = await window.__bench.fill(bytes, { values: {}, checks: {}, images: {}, groups: {} });
    return { fields: (info.fields || []).length, docx: filled.docx ? atob(filled.docx).length : 0 };
  });
  if (!out.fields || !out.docx) throw new Error("engine produced nothing: " + JSON.stringify(out));
  console.log(`ok  booted in Firefox, discovered ${out.fields} fields, filled ${out.docx} bytes`);
} finally {
  await browser.close();
}
