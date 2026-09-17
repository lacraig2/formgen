"""The formgen web UI, shared by the localhost bench and the single-file build.

Both surfaces render the same page and run the same app logic; they differ only
in *transport* -- how `window.__bench.{inspect,fill,learn,starter}` reach the
Python engine. `tools/serve.py` wires those to HTTP; `tools/build_single_html.py`
wires them to Pyodide in the page. Keeping the markup, styling and behaviour in
one place is what stops the two from drifting.

Exports `CSS`, `BODY`, and `APP_JS` (plain strings, no f-substitution -- the JS
keeps its own `${...}` and `{}`). A transport defines `window.__bench` and then
calls `window.__startApp()`.
"""

CSS = r"""
:root{
  --bg:#f6f7fb; --card:#ffffff; --fg:#1a1d29; --mut:#6b7280; --line:#e6e8ef;
  --accent:#5b5bd6; --accent-fg:#ffffff; --accent-soft:#eef0ff;
  --good:#0f9d58; --bad:#c0392b; --pill:#eef0ff; --code:#f2f3f9;
  --shadow:0 1px 2px rgba(20,20,50,.05), 0 8px 24px rgba(20,20,50,.06);
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme=light]){
    --bg:#0f1117; --card:#171a22; --fg:#e8eaf2; --mut:#9aa0b4; --line:#262a36;
    --accent:#8f8cff; --accent-fg:#0f1117; --accent-soft:#20233a;
    --pill:#20233a; --code:#1d2130;
    --shadow:0 1px 2px rgba(0,0,0,.3), 0 10px 30px rgba(0,0,0,.35);
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
a{color:var(--accent)}
.wrap{max-width:860px;margin:0 auto;padding:0 20px 80px}
header{display:flex;align-items:center;gap:14px;padding:26px 0 10px;flex-wrap:wrap}
.mark{width:40px;height:40px;border-radius:11px;background:linear-gradient(135deg,var(--accent),#8f7bff);
  display:grid;place-items:center;color:#fff;font-weight:800;font-size:20px;box-shadow:var(--shadow)}
.brand h1{margin:0;font-size:20px;letter-spacing:-.2px}
.brand p{margin:1px 0 0;color:var(--mut);font-size:13px}
header .grow{flex:1}
.btn{appearance:none;border:1px solid var(--line);background:var(--card);color:var(--fg);
  padding:9px 15px;border-radius:10px;font:inherit;font-weight:600;cursor:pointer;
  transition:.12s border-color,.12s transform;text-decoration:none;display:inline-flex;
  align-items:center;gap:7px}
.btn:hover{border-color:var(--accent)}
.btn:active{transform:translateY(1px)}
.btn.primary{background:var(--accent);color:var(--accent-fg);border-color:var(--accent)}
.btn.primary[disabled]{opacity:.5;cursor:default}
.btn.ghost{background:transparent}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;
  padding:22px;margin:16px 0;box-shadow:var(--shadow)}
.card h2{margin:0 0 4px;font-size:16px;letter-spacing:-.2px}
.card .sub{color:var(--mut);font-size:13px;margin:0 0 14px}
.muted{color:var(--mut);font-size:13px}
.bad{color:var(--bad)}
.good{color:var(--good)}
code,.code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px}
code{background:var(--code);padding:2px 6px;border-radius:6px}
.pill{display:inline-block;background:var(--pill);color:var(--accent);border-radius:999px;
  padding:3px 11px;font-size:12px;font-weight:600}
.tabs{display:flex;gap:6px;background:var(--code);padding:4px;border-radius:12px;width:fit-content}
.tab{border:0;background:transparent;color:var(--mut);padding:8px 16px;border-radius:9px;
  font:inherit;font-weight:600;cursor:pointer}
.tab.on{background:var(--card);color:var(--fg);box-shadow:var(--shadow)}
.pane{margin-top:16px}
.drop{display:block;border:1.5px dashed var(--line);border-radius:13px;padding:26px;
  text-align:center;background:var(--code);cursor:pointer;
  transition:.12s border-color,.12s background}
.drop:hover,.drop.over{border-color:var(--accent);background:var(--accent-soft)}
.drop input{display:none}
.drop .big{font-weight:600;margin-bottom:3px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:12px}
.field{margin:14px 0}
.field label{display:block;font-weight:600;margin-bottom:5px}
.field .k{color:var(--mut);font-weight:400;font-size:12px;margin-left:6px}
.field .req{color:var(--bad);font-weight:700}
.field input[type=text],.field input[type=date],.field input[type=number],
.field textarea,.field select{width:100%;padding:9px 11px;border:1px solid var(--line);
  border-radius:10px;background:var(--bg);color:var(--fg);font:inherit}
.field input:focus,.field textarea:focus,.field select:focus{outline:2px solid var(--accent-soft);
  border-color:var(--accent)}
.field textarea{min-height:40px;resize:vertical}
.field.check label{display:inline-flex;align-items:center;gap:9px;cursor:pointer;font-weight:500}
.field.check input{width:17px;height:17px;accent-color:var(--accent)}
.thumb{max-height:64px;border:1px solid var(--line);border-radius:8px;margin-top:8px;display:block}
.group{border:1px solid var(--line);border-radius:13px;padding:15px;margin:14px 0;background:var(--bg)}
.group h3{margin:0 0 4px;font-size:14px}
.grow{display:flex;gap:8px;margin-bottom:8px;align-items:center;flex-wrap:wrap}
.grow input,.grow select{flex:1;min-width:120px;padding:8px 10px;border:1px solid var(--line);
  border-radius:9px;background:var(--card);color:var(--fg);font:inherit}
.grow .cellchk{display:flex;align-items:center;gap:6px;font-size:13px;flex:0 0 auto}
.grow .cellchk input{flex:0 0 auto;min-width:0;accent-color:var(--accent)}
.grow .rm{flex:0 0 auto;background:transparent;color:var(--mut);border:1px solid var(--line);
  border-radius:8px;padding:6px 10px;font-size:13px;cursor:pointer}
.addrow{margin-top:2px;font-size:13px}
.result{margin-top:14px}
.docgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px 22px;margin-top:6px}
.docgrid .d code{display:inline-block}
.docgrid .d p{margin:2px 0 0;color:var(--mut);font-size:12.5px}
@media (max-width:620px){.docgrid{grid-template-columns:1fr}}
[hidden]{display:none!important}
"""

# The visible page. No transport, no engine -- just the shell the app fills in.
BODY = r"""
<div class="wrap">
  <header>
    <div class="mark">f</div>
    <div class="brand"><h1>formgen</h1><p>Fill Word forms without breaking their formatting.</p></div>
    <div class="grow"></div>
    <button class="btn ghost" id="docsBtn">Docs</button>
  </header>

  <section class="card" id="docs" hidden>
    <h2>How it works</h2>
    <p class="sub">Write an ordinary Word document. Wherever a value goes, type a marker &mdash;
      no Developer tab, no content controls. Then upload it here and fill it in.</p>
    <div class="docgrid">
      <div class="d"><code>{{full_name}}</code><p>text (multi-line is fine)</p></div>
      <div class="d"><code>{{date: start}}</code><p>a date picker</p></div>
      <div class="d"><code>{{number: salary}}</code><p>a number</p></div>
      <div class="d"><code>{{image: headshot}}</code><p>a picture</p></div>
      <div class="d"><code>picture &rarr; Alt Text: {{logo}}</code><p>mark a picture you already placed &mdash; its size &amp; position stay, only the image swaps</p></div>
      <div class="d"><code>{{check: agreed}}</code><p>a tick box</p></div>
      <div class="d"><code>{{choice: status | Draft, Final}}</code><p>pick one of a list</p></div>
      <div class="d"><code>{{items.name}}</code> <code>{{items.qty}}</code><p>a repeating table row &mdash; any kind of column works</p></div>
      <div class="d"><code>{{email*}}</code><p>a trailing <code>*</code> makes it required</p></div>
      <div class="d"><code>{{image: logo | Company logo}}</code><p><code>| label</code> gives a prompt &mdash; and a picture's alt text</p></div>
    </div>
    <p class="muted" style="margin-top:14px">The finished document <b>remembers your answers</b>:
      reopen it here and the form comes back filled in, ready to edit. Nothing you upload leaves this
      page.</p>
  </section>

  <section class="card">
    <div class="tabs">
      <button class="tab on" id="tabTemplate">I have a template</button>
      <button class="tab" id="tabLearn">Learn from examples</button>
    </div>

    <div class="pane" id="paneTemplate">
      <label class="drop" id="tplDrop">
        <div class="big">Drop a .docx here, or click to choose</div>
        <div class="muted">Your template with <code>{{markers}}</code> &mdash; or a finished form to edit again</div>
        <input type="file" id="tpl" accept=".docx">
      </label>
      <div class="row">
        <button class="btn ghost" id="starter">Download a starter template</button>
        <span class="muted" id="inspectstate"></span>
      </div>
    </div>

    <div class="pane" id="paneLearn" hidden>
      <label class="drop" id="exDrop">
        <div class="big">Drop 2&ndash;3 filled copies of the same form</div>
        <div class="muted">formgen finds the fields from what changes between them and writes the template for you</div>
        <input type="file" id="examples" accept=".docx" multiple>
      </label>
      <div class="row">
        <button class="btn primary" id="learnBtn" disabled>Build the template</button>
        <span class="muted" id="learnstate"></span>
      </div>
      <div class="result" id="learnresult"></div>
    </div>
  </section>

  <section class="card" id="formcard" hidden>
    <h2 id="formtitle">Fill it in</h2>
    <p class="sub" id="formsub"></p>
    <div id="fields"></div>
    <div id="groups"></div>
    <div class="row">
      <button class="btn primary" id="fill">Fill &amp; download .docx</button>
      <span class="muted" id="fillstate"></span>
    </div>
    <div class="result" id="result"></div>
  </section>
</div>
"""

# The app. Transport-agnostic: calls window.__bench.* and is started by
# window.__startApp() once a transport is ready.
APP_JS = r"""
const $ = id => document.getElementById(id);
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function b64(buf){const b=new Uint8Array(buf);let s='';for(let i=0;i<b.length;i++)s+=String.fromCharCode(b[i]);return btoa(s);}
function b64bytes(s){return Uint8Array.from(atob(s),c=>c.charCodeAt(0));}
function saveDocx(b64s,name){
  const blob=new Blob([b64bytes(b64s)],{type:"application/vnd.openxmlformats-officedocument.wordprocessingml.document"});
  const url=URL.createObjectURL(blob);
  const a=document.createElement("a");a.href=url;a.download=name;document.body.appendChild(a);a.click();a.remove();
  setTimeout(()=>URL.revokeObjectURL(url),4000);
}

let CUR=null, FIELDS=[], GROUPS=[], PREFILL={};
const IMAGES={};
const KIND_LABEL={image:"picture",checkbox:"tick box",choice:"pick one",date:"date",number:"number",text:"text"};
const reqMark=f=>f.required?' <span class="req" title="required">*</span>':'';
const reqAttr=f=>f.required?' data-req="1"':'';

/* ---- inspect a template ---- */
async function inspectBytes(bytes){
  CUR=bytes; for(const k in IMAGES) delete IMAGES[k];
  $("inspectstate").textContent="reading…";
  let d; try { d=await window.__bench.inspect(bytes); } catch(e){ d={ok:false,error:String(e)}; }
  if(d && d.ok===false){ $("inspectstate").innerHTML=`<span class="bad">${esc(d.error||"could not read that .docx")}</span>`;
    $("formcard").hidden=true; return; }
  FIELDS=d.fields||[]; GROUPS=d.groups||[]; PREFILL=d.prefill||{};
  const n=FIELDS.length+GROUPS.length;
  $("inspectstate").innerHTML=(n
    ? `Found <b>${FIELDS.length}</b> field${FIELDS.length===1?'':'s'}`+(GROUPS.length?` and <b>${GROUPS.length}</b> repeating table${GROUPS.length===1?'':'s'}`:'')+`.`
    : `<span class="bad">No markers found. Add e.g. {{name}} to the document.</span>`)
    + (d.prefill?` <span class="pill">Remembers your answers — edit &amp; re-download</span>`:'');
  renderFields(); renderGroups(); applyPrefill();
  $("formsub").textContent = d.prefill ? "This document was filled here before — change anything and download it again."
    : "Formatting stays exactly as the template has it.";
  $("formcard").hidden = !n;
  $("result").innerHTML="";
  if(n) $("formcard").scrollIntoView({behavior:"smooth",block:"start"});
}

/* ---- render single fields ---- */
function renderFields(){
  $("fields").innerHTML = FIELDS.map(f=>{
    const tag=`<span class="k">${KIND_LABEL[f.kind]||f.kind}</span>`;
    if(f.kind==="image") return `<div class="field"><label>${esc(f.label)}${reqMark(f)} ${tag}</label>
      <input type="file" accept="image/*" data-img="${esc(f.name)}"${reqAttr(f)}><img class="thumb" data-thumb="${esc(f.name)}" hidden></div>`;
    if(f.kind==="checkbox") return `<div class="field check"><label>
      <input type="checkbox" data-chk="${esc(f.name)}"${reqAttr(f)}> ${esc(f.label)}${reqMark(f)} ${tag}</label></div>`;
    if(f.kind==="choice"){
      const opts=(f.choices||[]).map(o=>`<option>${esc(o)}</option>`).join("");
      return `<div class="field"><label>${esc(f.label)}${reqMark(f)} ${tag}</label>
        <select data-sel="${esc(f.name)}"${reqAttr(f)}><option value=""></option>${opts}</select></div>`;
    }
    if(f.kind==="date"||f.kind==="number") return `<div class="field"><label>${esc(f.label)}${reqMark(f)} ${tag}</label>
      <input type="${f.kind}" data-txt="${esc(f.name)}"${reqAttr(f)} value="${esc(f.value||'')}"></div>`;
    return `<div class="field"><label>${esc(f.label)}${reqMark(f)} ${tag}</label>
      <textarea data-txt="${esc(f.name)}" rows="1"${reqAttr(f)} placeholder="${esc(f.label)}">${esc(f.value||'')}</textarea></div>`;
  }).join("");
  $("fields").querySelectorAll('input[type=file][data-img]').forEach(inp=>{
    inp.onchange=async()=>{ const file=inp.files[0], name=inp.dataset.img;
      const thumb=$("fields").querySelector(`img[data-thumb="${CSS.escape(name)}"]`);
      if(!file){ delete IMAGES[name]; if(thumb) thumb.hidden=true; return; }
      IMAGES[name]=b64(await file.arrayBuffer());
      if(thumb){ thumb.src=URL.createObjectURL(file); thumb.hidden=false; }
    };
  });
}

/* ---- render repeating groups ---- */
function renderGroups(){
  $("groups").innerHTML = GROUPS.map(g=>`
    <div class="group" data-group="${esc(g.name)}">
      <h3>${esc(g.label)} <span class="k">repeating — one row each</span></h3>
      <div class="rows"></div>
      <button type="button" class="btn ghost addrow">+ Add row</button>
    </div>`).join("");
  GROUPS.forEach(g=>{
    const box=$("groups").querySelector(`[data-group="${CSS.escape(g.name)}"]`);
    const rows=box.querySelector(".rows");
    const cellFor=c=>{
      const k=c.kind||"text";
      if(k==="checkbox") return `<label class="cellchk"><input type="checkbox" data-col="${esc(c.name)}" data-kind="checkbox"> ${esc(c.label)}</label>`;
      if(k==="choice"){ const o=(c.choices||[]).map(x=>`<option>${esc(x)}</option>`).join(""); return `<select data-col="${esc(c.name)}" data-kind="choice" title="${esc(c.label)}"><option value=""></option>${o}</select>`; }
      if(k==="image") return `<input type="file" accept="image/*" data-col="${esc(c.name)}" data-kind="image" title="${esc(c.label)}">`;
      return `<input data-col="${esc(c.name)}" data-kind="text" placeholder="${esc(c.label)}">`;
    };
    const add=record=>{
      const div=document.createElement("div"); div.className="grow";
      div.innerHTML=g.columns.map(cellFor).join("")+`<button type="button" class="rm">×</button>`;
      div.querySelector(".rm").onclick=()=>div.remove();
      div.querySelectorAll('input[type=file][data-kind=image]').forEach(inp=>{
        inp.onchange=async()=>{ const f=inp.files[0]; inp._b64=f?b64(await f.arrayBuffer()):null; };
      });
      if(record) div.querySelectorAll("[data-col]").forEach(inp=>{
        const v=record[inp.dataset.col]; if(v===undefined) return;
        if(inp.dataset.kind==="checkbox") inp.checked=!!v;
        else if(inp.dataset.kind==="image"){ if(v&&v.image) inp._b64=v.image; }
        else inp.value=v;
      });
      rows.appendChild(div);
    };
    box.querySelector(".addrow").onclick=()=>add();
    const saved=(PREFILL.groups||{})[g.name];
    if(Array.isArray(saved)&&saved.length) saved.forEach(add); else add();
  });
}

/* ---- pre-fill from a remembered answer set ---- */
function applyPrefill(){
  const p=PREFILL||{};
  for(const [name,val] of Object.entries(p.text||{})){
    const el=$("fields").querySelector(`[data-txt="${CSS.escape(name)}"], [data-sel="${CSS.escape(name)}"]`); if(el) el.value=val;
  }
  for(const [name,on] of Object.entries(p.checks||{})){
    const el=$("fields").querySelector(`input[data-chk="${CSS.escape(name)}"]`); if(el) el.checked=!!on;
  }
  for(const [name,b64s] of Object.entries(p.images||{})){
    IMAGES[name]=b64s;
    const thumb=$("fields").querySelector(`img[data-thumb="${CSS.escape(name)}"]`);
    if(thumb){ thumb.src=URL.createObjectURL(new Blob([b64bytes(b64s)])); thumb.hidden=false; }
  }
}

/* ---- fill ---- */
function missingRequired(){
  for(const f of FIELDS){
    if(!f.required) continue;
    if(f.kind==="image"){ if(!IMAGES[f.name]) return f; continue; }
    if(f.kind==="checkbox"){ const c=$("fields").querySelector(`input[data-chk="${CSS.escape(f.name)}"]`); if(c&&!c.checked) return f; continue; }
    const el=$("fields").querySelector(`[data-txt="${CSS.escape(f.name)}"], [data-sel="${CSS.escape(f.name)}"]`);
    if(el&&!(el.value||"").trim()) return f;
  }
  return null;
}
function collect(){
  const values={},checks={},groups={};
  $("fields").querySelectorAll('[data-txt]').forEach(i=>values[i.dataset.txt]=i.value);
  $("fields").querySelectorAll('select[data-sel]').forEach(i=>values[i.dataset.sel]=i.value);
  $("fields").querySelectorAll('input[data-chk]').forEach(i=>checks[i.dataset.chk]=i.checked);
  GROUPS.forEach(g=>{
    const box=$("groups").querySelector(`[data-group="${CSS.escape(g.name)}"]`);
    const records=[];
    box.querySelectorAll(".grow").forEach(row=>{
      const rec={}; let any=false;
      row.querySelectorAll("[data-col]").forEach(inp=>{
        const col=inp.dataset.col,kind=inp.dataset.kind;
        if(kind==="checkbox"){ if(inp.checked){ rec[col]=true; any=true; } }
        else if(kind==="image"){ if(inp._b64){ rec[col]={image:inp._b64}; any=true; } }
        else { rec[col]=inp.value; if(inp.value.trim()) any=true; }
      });
      if(any) records.push(rec);
    });
    groups[g.name]=records;
  });
  return {values,checks,images:IMAGES,groups};
}
async function doFill(){
  if(!CUR) return;
  const miss=missingRequired();
  if(miss){
    $("result").innerHTML=`<p class="bad">Please fill the required field: <b>${esc(miss.label)}</b>.</p>`;
    const el=$("fields").querySelector(`[data-txt="${CSS.escape(miss.name)}"], [data-sel="${CSS.escape(miss.name)}"], [data-chk="${CSS.escape(miss.name)}"], [data-img="${CSS.escape(miss.name)}"]`);
    if(el){ el.focus(); el.scrollIntoView({block:"center"}); } return;
  }
  $("fill").disabled=true; $("fillstate").textContent="filling…";
  let d; try { d=await window.__bench.fill(CUR, collect()); } catch(e){ d={ok:false,error:String(e)}; }
  $("fill").disabled=false; $("fillstate").textContent="";
  if(!d||d.ok===false){ $("result").innerHTML=`<p class="bad">${esc((d&&d.error)||"fill failed")}</p>`; return; }
  saveDocx(d.docx,"filled.docx");
  let h=`<p><span class="pill">${esc(d.summary)}</span> &nbsp;downloaded <b>filled.docx</b></p>`;
  if(d.filled&&d.filled.length) h+=`<p class="muted">filled: ${d.filled.map(esc).join(", ")}</p>`;
  if(d.cleared&&d.cleared.length) h+=`<p class="muted">cleared (left blank): ${d.cleared.map(esc).join(", ")}</p>`;
  if(d.unknown&&d.unknown.length) h+=`<p class="muted">ignored (no such marker): ${d.unknown.map(esc).join(", ")}</p>`;
  if(d.leftover&&d.leftover.length) h+=`<p class="bad">still unfilled — left as literal text: ${d.leftover.map(esc).join(", ")}</p>`;
  $("result").innerHTML=h;
}

/* ---- learn from examples ---- */
let EXAMPLES=[];
async function doLearn(){
  if(EXAMPLES.length<2){ $("learnstate").innerHTML=`<span class="bad">Add at least two copies.</span>`; return; }
  $("learnBtn").disabled=true; $("learnstate").textContent="comparing…";
  let docs; try { docs=await Promise.all(EXAMPLES.map(async f=>b64(await f.arrayBuffer()))); }
  catch(e){ $("learnstate").innerHTML=`<span class="bad">${esc(String(e))}</span>`; $("learnBtn").disabled=false; return; }
  let d; try { d=await window.__bench.learn(docs.map(b64bytes)); } catch(e){ d={ok:false,error:String(e)}; }
  $("learnBtn").disabled=false; $("learnstate").textContent="";
  if(!d||d.ok===false){ $("learnresult").innerHTML=`<p class="bad">${esc((d&&d.error)||"could not learn a template")}</p>`; return; }
  const fields=d.fields||[];
  let h=`<p class="good">Found <b>${fields.length}</b> field${fields.length===1?'':'s'} from ${d.samples} copies.</p>`;
  if(fields.length) h+=`<p class="muted">${fields.map(f=>`${esc(f.name)} <span class="k">(${esc(f.kind)})</span>`).join(",  ")}</p>`;
  (d.warnings||[]).forEach(w=>h+=`<p class="muted">⚠ ${esc(w)}</p>`);
  h+=`<div class="row"><button class="btn" id="dlTemplate">Download the template</button>
      <button class="btn primary" id="useTemplate">Fill it in now →</button></div>`;
  $("learnresult").innerHTML=h;
  $("dlTemplate").onclick=()=>saveDocx(d.template,"template.docx");
  $("useTemplate").onclick=()=>{ selectTab("template"); inspectBytes(b64bytes(d.template)); };
}

/* ---- wiring ---- */
function selectTab(which){
  const t=which==="template";
  $("tabTemplate").classList.toggle("on",t); $("tabLearn").classList.toggle("on",!t);
  $("paneTemplate").hidden=!t; $("paneLearn").hidden=t;
}
function wireDrop(dropId, inputId, onFiles){
  const drop=$(dropId), input=$(inputId);
  drop.addEventListener("dragover",e=>{e.preventDefault();drop.classList.add("over");});
  drop.addEventListener("dragleave",()=>drop.classList.remove("over"));
  drop.addEventListener("drop",e=>{e.preventDefault();drop.classList.remove("over");
    if(e.dataTransfer.files.length) onFiles([...e.dataTransfer.files]); });
  input.addEventListener("change",()=>{ if(input.files.length) onFiles([...input.files]); });
}

window.__startApp = function(){
  $("docsBtn").onclick=()=>{ const d=$("docs"); d.hidden=!d.hidden; if(!d.hidden) d.scrollIntoView({behavior:"smooth",block:"start"}); };
  $("tabTemplate").onclick=()=>selectTab("template");
  $("tabLearn").onclick=()=>selectTab("learn");
  wireDrop("tplDrop","tpl",async files=>inspectBytes(new Uint8Array(await files[0].arrayBuffer())));
  wireDrop("exDrop","examples",files=>{ EXAMPLES=files;
    $("learnBtn").disabled=files.length<2;
    $("learnstate").textContent=files.length?`${files.length} file${files.length===1?'':'s'} ready`:''; });
  $("learnBtn").onclick=doLearn;
  $("fill").onclick=doFill;
  $("starter").onclick=async()=>{ const b=await window.__bench.starter(); saveDocx(b,"starter-template.docx"); };
};
"""
