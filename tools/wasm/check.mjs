// Does formgen run in a browser, and does it produce the same bytes?
//
// The reason this matters is that the alternative is a second
// implementation. A JavaScript template-filler would have to re-derive the
// OPC preservation guarantee, the content-control handling, the field
// mechanics and the deterministic zip -- and would then drift from the
// Python one in ways nobody notices until a document is wrong.
//
// So: no port, no bindings, no shared subset. The same source files are
// copied into Pyodide's filesystem and run there, and the output is compared
// byte for byte against what CPython produced from the same inputs. Equal
// bytes is the whole claim; anything less and there are two implementations
// again, just less visibly.
//
// Usage:  npm install && node check.mjs
import { loadPyodide } from "pyodide";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { createHash } from "node:crypto";

const ROOT = path.resolve(import.meta.dirname, "..", "..");
const SRC = path.join(ROOT, "src");
const PACKAGES = ["lxml", "pyyaml", "python-dateutil", "click",
                  "markdown-it-py"];

const MARKDOWN = `---
report_number: LR-2027-0009
creator: A. Turing
date: 2 October 2027
---

# Introduction

The X-11 radiator exceeds its design margin under worst-case loading.[^1]

[^1]: Measured at 340 K over six hours.

The margin was $\\Delta T = 12.4\\,\\mathrm{K}$.

| Case    | Margin (K) |
|---------|-----------:|
| Nominal |       12.4 |
| Worst   |        3.1 |
`;

const VALUES = { report_number: "LR-2027-0009", creator: "A. Turing",
                 date: "2 October 2027" };

// The work itself, written once and run by both engines.
const WORK = `
import hashlib, json, pathlib, sys
from formgen.content.emit import emit
from formgen.content.fill import fill
from formgen.content.markdown_in import parse
from formgen.learn.formfields import find_fields
from formgen.opc.package import OpcPackage
from formgen.safety.verify import check_integrity

def run(template, markdown, values, out_dir):
    out = {}
    pkg = OpcPackage.open(template)
    out["fields"] = [f.name for f in find_fields(pkg).fields]
    report = fill(pkg, values)
    out["filled"] = sorted(report.filled)
    assert check_integrity(pkg) == [], check_integrity(pkg)
    pkg.save(pathlib.Path(out_dir) / "filled.docx", deterministic=True)

    package, emitted = emit(parse(markdown), pathlib.Path(template))
    assert check_integrity(package) == [], check_integrity(package)
    package.save(pathlib.Path(out_dir) / "generated.docx", deterministic=True)
    out["emitted"] = emitted.summary()
    for name in ("filled.docx", "generated.docx"):
        data = (pathlib.Path(out_dir) / name).read_bytes()
        out[name] = hashlib.sha256(data).hexdigest()
    return out
`;

function sha(file) {
  return createHash("sha256").update(readFileSync(file)).digest("hex");
}

const work = mkdtempSync(path.join(tmpdir(), "formgen-wasm-"));
let failures = 0;
try {
  // 1. A profile, learned by CPython, is the shared input.
  const corpus = path.join(work, "corpus");
  execFileSync("python3", [path.join(ROOT, "tools", "build_qa_corpus.py"), corpus],
               { stdio: "pipe" });
  const profile = path.join(work, "profile");
  execFileSync("python3", ["-c", `
import pathlib, sys
sys.path.insert(0, ${JSON.stringify(SRC)})
from formgen.learn.pipeline import learn
learn(sorted(pathlib.Path(${JSON.stringify(corpus)}).glob("*.docx")),
      pathlib.Path(${JSON.stringify(profile)}), generated="fixed")
`], { stdio: "pipe" });
  const template = path.join(profile, "template.docx");
  writeFileSync(path.join(work, "doc.md"), MARKDOWN);

  // 2. CPython's answer.
  const native = path.join(work, "native");
  const nativeJson = execFileSync("python3", ["-c", `
import json, os, sys
sys.path.insert(0, ${JSON.stringify(SRC)})
${WORK}
os.makedirs(${JSON.stringify(native)}, exist_ok=True)
print(json.dumps(run(${JSON.stringify(template)},
                     open(${JSON.stringify(path.join(work, "doc.md"))}).read(),
                     ${JSON.stringify(VALUES)},
                     ${JSON.stringify(native)})))
`], { encoding: "utf8" });
  const expected = JSON.parse(nativeJson.trim().split("\n").pop());
  console.log("CPython :", expected.emitted);

  // 3. The same source, in WebAssembly.
  const py = await loadPyodide({ stderr: () => {} });
  await py.loadPackage(["micropip"]);
  const micropip = py.pyimport("micropip");
  for (const name of PACKAGES) await micropip.install(name);

  let copied = 0;
  (function copy(from, to) {
    py.FS.mkdirTree(to);
    for (const entry of readdirSync(from, { withFileTypes: true })) {
      const a = path.join(from, entry.name), b = `${to}/${entry.name}`;
      if (entry.isDirectory()) copy(a, b);
      else if (entry.name.endsWith(".py")) {
        py.FS.writeFile(b, readFileSync(a)); copied++;
      }
    }
  })(path.join(SRC, "formgen"), "/lib/formgen");
  py.FS.writeFile("/template.docx", readFileSync(template));
  py.FS.mkdirTree("/out");
  py.runPython(`import sys; sys.path.insert(0, "/lib")`);
  py.runPython(WORK);
  const actual = JSON.parse(py.runPython(`
import json
json.dumps(run("/template.docx", ${JSON.stringify(MARKDOWN)},
               ${JSON.stringify(VALUES)}, "/out"))
`));
  console.log(`WASM    : ${actual.emitted}   (${copied} source files, unmodified)`);

  // 4. The claim.
  for (const key of ["fields", "filled", "emitted"]) {
    const a = JSON.stringify(expected[key]), b = JSON.stringify(actual[key]);
    if (a !== b) { console.error(`  MISMATCH ${key}: ${a} != ${b}`); failures++; }
  }
  for (const name of ["filled.docx", "generated.docx"]) {
    const wasmBytes = Buffer.from(py.FS.readFile(`/out/${name}`));
    writeFileSync(path.join(work, `wasm-${name}`), wasmBytes);
    const native_ = sha(path.join(native, name));
    const wasm_ = sha(path.join(work, `wasm-${name}`));
    if (native_ === wasm_) console.log(`  identical  ${name}  ${wasm_.slice(0, 16)}...`);
    else { console.error(`  DIFFERENT  ${name}\n    cpython ${native_}\n    wasm    ${wasm_}`); failures++; }
  }
} finally {
  rmSync(work, { recursive: true, force: true });
}
console.log(failures ? `\nFAILED (${failures})` : "\nformgen runs unmodified in WebAssembly and produces the same bytes.");
process.exit(failures ? 1 : 0);
