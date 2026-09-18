"""A local, localhost-only web bench for filling a Word template by hand.

The workflow is meant for someone who is not going to open Word's Developer
tab: write an ordinary document, and where a value goes, type a marker --
``{{full_name}}`` for text, ``{{image: headshot}}`` for a picture. Upload it
here and the page shows one input per marker; fill them and download the
finished .docx. You can also drop in a few *filled* copies of a form and let
formgen write the template for you, and a finished document remembers its
answers so you can reopen it and edit them.

The page and its behaviour are the shared UI in ``tools/webui.py``; this file
only wires that UI's ``__bench`` calls to the real engine over HTTP. It runs
the same ``formgen.bench`` the single-file build runs, binds to 127.0.0.1 only,
and needs nothing outside the standard library and formgen itself.

    python tools/serve.py [PORT]
"""

from __future__ import annotations

import base64
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:      # run as `python tools/serve.py`
    sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # for `import webui`

import webui  # noqa: E402

from formgen import bench  # noqa: E402
from formgen.opc.errors import PackageError  # noqa: E402

PAGE = (
    "<!doctype html><html lang=en><head><meta charset=utf-8>"
    "<meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>formgen — fill Word forms</title><style>" + webui.CSS + "</style></head>"
    "<body>" + webui.BODY
    + "<script>" + webui.APP_JS + "</script>"
    + "<script>" + r"""
window.__bench = {
  async inspect(bytes){
    const r = await fetch("/api/inspect", {method:"POST",
      headers:{"Content-Type":"application/octet-stream"}, body: bytes});
    return r.json();
  },
  async fill(bytes, payload){
    const r = await fetch("/api/fill", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({doc: b64(bytes), ...payload})});
    return r.json();
  },
  async learn(docs){
    const r = await fetch("/api/learn", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({docs: docs.map(b64)})});
    return r.json();
  },
  async starter(){ const r = await fetch("/api/starter"); return (await r.json()).docx; }
};
window.addEventListener("DOMContentLoaded", () => window.__startApp());
""" + "</script></body></html>"
)


# -- the engine, over formgen.bench (shared with the single-file build) -------

def _decode_row_images(groups: dict) -> None:
    """Turn each repeating-row image cell from ``{"image": b64}`` into bytes."""
    for records in (groups or {}).values():
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


def _fill(payload: dict) -> dict:
    data = base64.b64decode(payload.get("doc", ""))
    images: dict = {}
    for name, b64s in (payload.get("images") or {}).items():
        try:
            images[name] = base64.b64decode(b64s)
        except (ValueError, TypeError):
            continue
    groups = payload.get("groups") or {}
    _decode_row_images(groups)
    docx, info = bench.fill_document(data, payload.get("values") or {},
                                     payload.get("checks") or {}, images, groups)
    return {"ok": True, "docx": base64.b64encode(docx).decode(), **info}


def _learn(payload: dict) -> dict:
    docs = []
    for b64s in payload.get("docs") or []:
        try:
            docs.append(base64.b64decode(b64s))
        except (ValueError, TypeError):
            continue
    if len(docs) < 2:
        return {"ok": False, "error": "add at least two filled copies"}
    return {"ok": True, **bench.learn(docs)}


# -- HTTP --------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "formgen-serve"

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))

    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code=200):
        self._send(code, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/starter":
            self._json({"ok": True,
                        "docx": base64.b64encode(bench.starter()).decode()})
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        try:
            if self.path == "/api/inspect":
                try:
                    self._json({"ok": True, **bench.inspect(self._body())})
                except PackageError as exc:
                    self._json({"ok": False,
                                "error": f"not a .docx or .pptx we could read: {exc}"})
            elif self.path == "/api/fill":
                self._json(_fill(json.loads(self._body() or b"{}")))
            elif self.path == "/api/learn":
                self._json(_learn(json.loads(self._body() or b"{}")))
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as exc:  # a bench, not a service: surface the fault
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                       code=500)


def main(argv: list[str]) -> int:
    port = int(argv[0]) if argv else 8765
    host = "127.0.0.1"
    last: OSError | None = None
    for candidate in range(port, port + 20):
        try:
            server = ThreadingHTTPServer((host, candidate), Handler)
            break
        except OSError as exc:
            last = exc
    else:
        print(f"could not bind a port near {port}: {last}", file=sys.stderr)
        return 1

    url = f"http://{host}:{candidate}"
    print(f"formgen is up at {url}")
    print("  (localhost only — not reachable from any other machine)")
    print("  Ctrl-C to stop.\n", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
