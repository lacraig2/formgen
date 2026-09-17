"""Build a single, fully self-contained HTML file that fills templates offline.

Everything is carried inside the one file: the formgen source, the Pyodide
runtime (loader, wasm, standard library) and the one wheel the fill path needs
(lxml). Opened in a browser it fetches nothing -- a `fetch` shim serves every
asset from the bytes embedded in the page -- so it works with no network, now
or in ten years, and the document a person fills never leaves their machine.

    python tools/build_single_html.py [OUTPUT.html]

The Pyodide assets come from `tools/wasm/node_modules/pyodide`, so run
`npm install` in `tools/wasm` first. The result is large (~18 MB) -- that is
the cost of carrying a Python runtime -- but it is one file and it is offline.
`tools/wasm/check_single.mjs` extracts the embedded payload, runs it under
Pyodide and proves it produces the same bytes as CPython; CI runs it.
"""

from __future__ import annotations

import base64
import io
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
PYODIDE = ROOT / "tools" / "wasm" / "node_modules" / "pyodide"

sys.path.insert(0, str(Path(__file__).resolve().parent))  # for `import webui`
import webui  # noqa: E402

# Python glue run inside Pyodide: bytes cross the JS boundary as base64, and
# every entry point returns a string.
GLUE = r"""
import base64, json
from formgen import bench

def bench_inspect(b64):
    return json.dumps(bench.inspect(base64.b64decode(b64)))

def bench_starter():
    return base64.b64encode(bench.starter()).decode()

def bench_fill(b64, payload):
    p = json.loads(payload)
    images = {k: base64.b64decode(v) for k, v in (p.get("images") or {}).items()}
    groups = p.get("groups") or {}
    for records in groups.values():
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            for key, value in list(record.items()):
                if isinstance(value, dict) and isinstance(value.get("image"), str):
                    try:
                        record[key] = base64.b64decode(value["image"])
                    except (ValueError, TypeError):
                        record[key] = ""
    docx, info = bench.fill_document(
        base64.b64decode(b64), p.get("values") or {}, p.get("checks") or {},
        images, groups)
    return json.dumps({"docx": base64.b64encode(docx).decode(), **info})

def bench_learn(payload):
    docs = [base64.b64decode(x) for x in json.loads(payload)]
    if len(docs) < 2:
        return json.dumps({"ok": False, "error": "add at least two filled copies"})
    return json.dumps(bench.learn(docs))
"""


def _source_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((SRC / "formgen").rglob("*.py")):
            archive.write(path, path.relative_to(SRC).as_posix())
    return buffer.getvalue()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _asset_script(script_id: str, data_name: str, data_type: str,
                  b64: str) -> str:
    name = f' data-name="{data_name}"' if data_name else ""
    typ = f' data-type="{data_type}"' if data_type else ""
    return f'<script type="text/b64" id="{script_id}"{name}{typ}>{b64}</script>'


def build(output: Path) -> None:
    if not PYODIDE.exists():
        raise SystemExit(
            f"Pyodide assets not found at {PYODIDE}.\n"
            "Run `npm install` in tools/wasm first.")

    lock = json.loads((PYODIDE / "pyodide-lock.json").read_text())
    lxml_name = lock["packages"]["lxml"]["file_name"]

    assets = [
        _asset_script("a-wasm", "pyodide.asm.wasm", "application/wasm",
                      _b64((PYODIDE / "pyodide.asm.wasm").read_bytes())),
        _asset_script("a-stdlib", "python_stdlib.zip", "application/octet-stream",
                      _b64((PYODIDE / "python_stdlib.zip").read_bytes())),
        _asset_script("a-lxml", lxml_name, "application/octet-stream",
                      _b64((PYODIDE / lxml_name).read_bytes())),
        # lock and source carry no data-name: read by id, not served by the shim.
        _asset_script("a-lock", "", "", _b64((PYODIDE / "pyodide-lock.json").read_bytes())),
        _asset_script("a-src", "", "", _b64(_source_zip())),
    ]
    asm_js = (PYODIDE / "pyodide.asm.js").read_text(encoding="utf-8")
    loader_js = (PYODIDE / "pyodide.js").read_text(encoding="utf-8")

    html = "".join([
        HEAD,
        "\n".join(assets),
        f"\n<script>{asm_js}</script>",
        f"\n<script>{loader_js}</script>",
        f"\n<script>\n{webui.APP_JS}\n</script>",
        f"\n<script>\nconst GLUE = {json.dumps(GLUE)};\n{SCRIPT}\n</script>",
        TAIL,
    ])
    output.write_text(html, encoding="utf-8")
    size = len(html.encode("utf-8"))
    print(f"wrote {output}  ({size / 1024 / 1024:.1f} MB, fully offline: "
          f"Pyodide + lxml + formgen all embedded)")


BOOT_CSS = r"""
#boot{position:fixed;inset:0;background:var(--bg);display:flex;align-items:center;
  justify-content:center;flex-direction:column;gap:12px;z-index:9;text-align:center;padding:24px}
.spin{width:28px;height:28px;border:3px solid var(--line);border-top-color:var(--accent);
  border-radius:50%;animation:spin .9s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
"""

BOOT_OVERLAY = r"""
<div id="boot">
  <div class="spin"></div>
  <div id="bootmsg" class="muted">Starting…</div>
  <div class="muted" style="font-size:12px;max-width:360px">Everything runs in your
    browser from this one file — no network, no install. Your document never leaves
    this machine.</div>
</div>
"""

HEAD = ("<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width, initial-scale=1'>"
        "<title>formgen — fill Word forms offline</title><style>"
        + webui.CSS + BOOT_CSS + "</style></head><body>"
        + BOOT_OVERLAY + webui.BODY)

# Transport + boot only -- the app itself is webui.APP_JS, loaded just before
# this script, so here we merely stand up the offline fetch shim, start Pyodide,
# and wire window.__bench to the Python glue before handing off to __startApp.
SCRIPT = r"""
let py = null;

function shimB64(s){ s=(s||"").trim(); const bin=atob(s);
  const u=new Uint8Array(bin.length); for(let i=0;i<bin.length;i++) u[i]=bin.charCodeAt(i); return u; }

// Every asset is embedded in the page; serve them from there so Pyodide (which
// asks for them by fetch) never touches the network.
const ASSETS = {};
for (const el of document.querySelectorAll('script[type="text/b64"][data-name]'))
  ASSETS[el.dataset.name] = { text: el.textContent, type: el.dataset.type || "application/octet-stream" };
const _fetch = (typeof fetch === "function") ? fetch.bind(globalThis) : null;
globalThis.fetch = (input, init) => {
  const s = String(input && input.url ? input.url : input);
  for (const name in ASSETS)
    if (s.endsWith(name))
      return Promise.resolve(new Response(shimB64(ASSETS[name].text),
        {status:200, headers:{"Content-Type":ASSETS[name].type}}));
  return _fetch ? _fetch(input, init) : Promise.reject(new Error("offline: "+s));
};

async function boot(){
  const msg = t => document.getElementById("bootmsg").textContent = t;
  try {
    msg("Starting Python…");
    const lock = new TextDecoder().decode(shimB64(document.getElementById("a-lock").textContent));
    // packageBaseUrl is required in Pyodide 0.28 for the lock's relative wheel
    // names; the fetch shim serves them by filename, so any base ending in "/"
    // works. Without it loadPackage("lxml") fails quietly and the import below
    // blows up -- which no Node-side check catches, only a real browser.
    const here = new URL("./", location.href).href;
    py = await loadPyodide({ indexURL: here, packageBaseUrl: here, lockFileContents: lock });
    msg("Loading libraries…");
    await py.loadPackage("lxml");
    msg("Unpacking formgen…");
    py.FS.writeFile("/formgen-src.zip", shimB64(document.getElementById("a-src").textContent));
    py.runPython(`
import zipfile, sys, os
os.makedirs("/lib", exist_ok=True)
with zipfile.ZipFile("/formgen-src.zip") as z: z.extractall("/lib")
if "/lib" not in sys.path: sys.path.insert(0, "/lib")
`);
    py.runPython(GLUE);
    window.__bench = {
      async inspect(bytes){ return JSON.parse(py.globals.get("bench_inspect")(b64(bytes))); },
      async fill(bytes, payload){ return JSON.parse(py.globals.get("bench_fill")(b64(bytes), JSON.stringify(payload))); },
      async learn(docs){ return JSON.parse(py.globals.get("bench_learn")(JSON.stringify(docs.map(b64)))); },
      async starter(){ return py.globals.get("bench_starter")(); }
    };
    document.getElementById("boot").hidden = true;
    window.__startApp();
  } catch(e){ msg("Could not start: " + e); throw e; }
}
boot();
"""

TAIL = "\n</body></html>\n"


def main(argv: list[str]) -> int:
    output = Path(argv[0]) if argv else ROOT / "dist" / "formgen.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    build(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
