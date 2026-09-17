// Verify the fully self-contained dist/formgen.html:
//  1. every embedded binary (wasm, stdlib, lxml wheel, lock) is byte-identical
//     to the real Pyodide asset -- so what the page serves itself is genuine;
//  2. the inlined loader and asm.js are byte-identical to Pyodide's own;
//  3. the embedded formgen source, run under Pyodide, produces the SAME bytes
//     CPython does.
// The browser fetch-shim that wires (1) into Pyodide is exercised in a real
// browser, not here; but here we prove the payload it serves is complete and
// correct, and that the engine on top of it matches CPython.
import { loadPyodide } from "pyodide";
import { execFileSync } from "node:child_process";
import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";

const ROOT = path.resolve(import.meta.dirname, "..", "..");
const PYO = path.join(ROOT, "tools", "wasm", "node_modules", "pyodide");
const html = readFileSync(path.join(ROOT, "dist", "formgen.html"), "utf8");
const sha = (b) => createHash("sha256").update(Buffer.from(b)).digest("hex");
let fails = 0;
const check = (name, cond) => { console.log(`  ${cond ? "ok      " : "FAIL    "} ${name}`); if (!cond) fails++; };

// --- pull an embedded b64 <script> by id ---
function asset(id) {
  const m = html.match(new RegExp(`<script type="text/b64" id="${id}"[^>]*>([^<]*)</script>`));
  if (!m) throw new Error("missing embedded asset " + id);
  return Buffer.from(m[1].trim(), "base64");
}
// --- pull an inlined <script>FILE</script> and match it to a pyodide file ---
function inlined(fileName) {
  const body = readFileSync(path.join(PYO, fileName), "utf8");
  return html.includes(body);
}

console.log("1. embedded binaries are the genuine Pyodide assets:");
const lock = JSON.parse(readFileSync(path.join(PYO, "pyodide-lock.json"), "utf8"));
const lxmlName = lock.packages.lxml.file_name;
check("pyodide.asm.wasm",  sha(asset("a-wasm"))   === sha(readFileSync(path.join(PYO, "pyodide.asm.wasm"))));
check("python_stdlib.zip", sha(asset("a-stdlib")) === sha(readFileSync(path.join(PYO, "python_stdlib.zip"))));
check("lxml wheel",        sha(asset("a-lxml"))   === sha(readFileSync(path.join(PYO, lxmlName))));
check("pyodide-lock.json", sha(asset("a-lock"))   === sha(readFileSync(path.join(PYO, "pyodide-lock.json"))));

console.log("2. loader and asm.js are inlined verbatim:");
check("pyodide.js (loader)", inlined("pyodide.js"));
check("pyodide.asm.js",      inlined("pyodide.asm.js"));

console.log("3. the embedded formgen source runs and matches CPython:");
const VALUES = JSON.stringify({values:{full_name:"K. Ito",date:"2027",report_no:"LR-1"},
  checks:{cleared:true}, images:{}, groups:{items:[{material:"Cu",quantity:"1kg"},{material:"Al",quantity:"2kg"}]}});
const py = await loadPyodide({ stderr: () => {} });
await py.loadPackage("lxml");                 // from the local (== embedded) wheel
py.FS.writeFile("/src.zip", new Uint8Array(asset("a-src")));  // the EMBEDDED source
py.runPython(`
import zipfile, sys, os
os.makedirs("/lib", exist_ok=True)
with zipfile.ZipFile("/src.zip") as z: z.extractall("/lib")
sys.path.insert(0, "/lib")
import base64, json
from formgen import bench
def run(payload):
    p = json.loads(payload)
    s = bench.starter()
    docx, info = bench.fill_document(s, p["values"], p["checks"], {}, p["groups"])
    return json.dumps({"starter": base64.b64encode(s).decode(),
        "fields": [f["name"] for f in bench.inspect(s)["fields"]],
        "filled": info["filled"], "docx": base64.b64encode(docx).decode()})
`);
const got = JSON.parse(py.runPython(`run(${JSON.stringify(VALUES)})`));
const ref = JSON.parse(execFileSync("python3", ["-c", `
import sys, json, base64; sys.path.insert(0, ${JSON.stringify(path.join(ROOT,"src"))})
from formgen import bench
p = json.loads(${JSON.stringify(VALUES)}); s = bench.starter()
docx, info = bench.fill_document(s, p["values"], p["checks"], {}, p["groups"])
print(json.dumps({"starter": base64.b64encode(s).decode(),
  "fields": [f["name"] for f in bench.inspect(s)["fields"]],
  "filled": info["filled"], "docx": base64.b64encode(docx).decode()}))
`], { encoding: "utf8" }).trim());
check("starter bytes",   sha(Buffer.from(got.starter,"base64")) === sha(Buffer.from(ref.starter,"base64")));
check("inspect fields",  JSON.stringify(got.fields) === JSON.stringify(ref.fields));
check("fill report",     JSON.stringify(got.filled) === JSON.stringify(ref.filled));
check("filled .docx",    sha(Buffer.from(got.docx,"base64")) === sha(Buffer.from(ref.docx,"base64")));

console.log(fails ? `\nFAILED (${fails})` : "\nThe single self-contained HTML carries a correct payload and matches CPython.");
process.exit(fails ? 1 : 0);
