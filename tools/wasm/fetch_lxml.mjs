// Populate node_modules/pyodide with the lxml wheel that build_single_html.py
// embeds. Pyodide's npm package ships the runtime but not the big wheels; the
// first loadPackage downloads and caches lxml into the package dir, which is
// exactly where the build reads it from.
import { loadPyodide } from "pyodide";
const py = await loadPyodide({ stderr: () => {} });
await py.loadPackage("lxml");
console.log("lxml wheel ready in node_modules/pyodide");
