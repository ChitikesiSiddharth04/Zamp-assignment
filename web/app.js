/* Northgate AP — interface.
   Plain ES modules-free JavaScript on purpose: no build step, so the thing a
   reviewer clones is the thing that runs. State is deliberately small -- the
   server is the source of truth and the UI just renders what a run produced. */

const $ = (s, r = document) => r.querySelector(s);
const el = (t, c, h) => { const n = document.createElement(t); if (c) n.className = c;
  if (h !== undefined) n.innerHTML = h; return n; };
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const money = (v, cur) => v == null ? "—" :
  new Intl.NumberFormat("en-US", { style: "currency", currency: cur || "USD" }).format(v);
const num = v => v == null ? "—" : Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const pct = v => (v * 100).toFixed(0) + "%";

const S = { boot: null, view: "run", run: null, stages: [], detail: null, filter: "", q: "" };

function toast(msg) {
  const t = el("div", "toast", esc(msg)); document.body.appendChild(t);
  setTimeout(() => t.remove(), 2600);
}

/* ---------------------------------------------------------------- bootstrap */
async function boot() {
  S.boot = await (await fetch("/api/bootstrap")).json();
  const e = S.boot.extractors;
  $("#engines").innerHTML = [
    chip("layout parser", true, "Deterministic label-anchored PDF parser — always on"),
    chip("tesseract OCR", e.ocr, e.ocr ? "OCR " + e.ocr_version : "not installed"),
    chip("claude cross-read", e.claude,
         e.claude ? "Second independent extractor" : "set ANTHROPIC_API_KEY to enable the second extractor"),
  ].join("");
  $("#nav").addEventListener("click", ev => {
    const b = ev.target.closest("button[data-v]"); if (!b) return;
    S.view = b.dataset.v;
    [...$("#nav").querySelectorAll("button")].forEach(x => x.classList.toggle("sel", x === b));
    render();
  });
  $("#btn-reset").onclick = async () => {
    if (!confirm("Clear every run, the ledger and the queue?")) return;
    await fetch("/api/reset", { method: "POST" });
    S.run = null; S.stages = []; S.detail = null; toast("Demo reset"); refreshCounts(); render();
  };
  refreshCounts(); render();
}
const chip = (label, on, title) =>
  `<span class="chip ${on ? "on" : "off"}" title="${esc(title)}"><i class="dot"></i>${esc(label)}</span>`;

async function refreshCounts() {
  const s = await (await fetch("/api/stats")).json();
  $("#n-runs").textContent = s.runs; $("#n-queue").textContent = s.open_exceptions;
  return s;
}

/* --------------------------------------------------------------- run a file */
async function startRun(body) {
  S.stages = S.boot.stageList || []; S.detail = null;
  const r = await (await fetch("/api/run", { method: "POST", body })).json();
  S.run = r.run_id; S.stages = SKELETON.map(s => ({ ...s, status: "pending" }));
  S.view = "run";
  [...$("#nav").querySelectorAll("button")].forEach(x => x.classList.toggle("sel", x.dataset.v === "run"));
  render();
  const src = new EventSource(`/api/runs/${r.run_id}/events`);
  src.onmessage = async ev => {
    const m = JSON.parse(ev.data);
    if (m.type === "stage") {
      const i = S.stages.findIndex(s => s.stage === m.stage);
      const row = { stage: m.stage, label: m.label, status: m.status, message: m.message,
                    payload: m.payload, ms: m.duration_ms };
      if (i >= 0) S.stages[i] = row; else S.stages.push(row);
      paintStages();
    } else if (m.type === "decision") {
      S.detail = await (await fetch(`/api/runs/${S.run}`)).json();
      render(); refreshCounts();
    } else if (m.type === "done") { src.close(); }
  };
  src.onerror = () => src.close();
}
const SKELETON = [
  ["INGEST", "Ingest document"], ["READ", "Read the document"], ["EXTRACT", "Extract fields"],
  ["NORMALISE", "Normalise and apply confirmations"], ["VENDOR", "Resolve vendor"],
  ["PO", "Match purchase order"], ["VALIDATE", "Run validation rules"],
  ["DECIDE", "Apply decision policy"], ["ACT", "Produce output"], ["RECORD", "Write audit trail"],
].map(([stage, label]) => ({ stage, label, status: "pending", message: "", payload: {}, ms: null }));

/* ------------------------------------------------------------------ routing */
function render() {
  const v = $("#view");
  v.innerHTML = "";
  if (S.view === "run") v.appendChild(viewRun());
  else if (S.view === "queue") viewQueue(v);
  else if (S.view === "dash") viewDash(v);
  else viewRef(v);
}

/* ---------------------------------------------------------------- run view */
function viewRun() {
  const g = el("div", "run-grid");

  // left: what to run
  const left = el("div");
  const c = el("div", "card");
  c.appendChild(el("h3", null, "Start a run"));
  const list = el("div", "samples");
  S.boot.samples.forEach(s => {
    const b = el("button", "sample",
      `<b>${esc(s.label)}</b><span>${esc(s.note)}</span><code>${esc(s.file)} · ${s.kb} KB</code>`);
    b.onclick = () => { const f = new FormData(); f.append("sample", s.file); startRun(f); };
    list.appendChild(b);
  });
  c.appendChild(list);
  const drop = el("div", "drop", "or drop any invoice PDF here<br><span class='sub'>the process has never seen it</span>");
  const input = el("input"); input.type = "file"; input.accept = "application/pdf"; input.style.display = "none";
  drop.onclick = () => input.click();
  input.onchange = () => { if (input.files[0]) { const f = new FormData(); f.append("file", input.files[0]); startRun(f); } };
  drop.ondragover = e => { e.preventDefault(); drop.classList.add("hot"); };
  drop.ondragleave = () => drop.classList.remove("hot");
  drop.ondrop = e => { e.preventDefault(); drop.classList.remove("hot");
    const file = e.dataTransfer.files[0]; if (file) { const f = new FormData(); f.append("file", file); startRun(f); } };
  c.appendChild(drop); c.appendChild(input);
  left.appendChild(c);

  const p = S.boot.policy;
  const pol = el("div", "card"); pol.style.marginTop = "12px";
  pol.appendChild(el("h3", null, "Policy in force"));
  pol.appendChild(el("div", "tabbody", `
    <table class="t"><tbody>
      <tr><td>Straight-through ceiling</td><td class="num">${money(p.auto_approve_ceiling)}</td></tr>
      <tr><td>Confidence floor on critical fields</td><td class="num">${pct(p.min_field_confidence)}</td></tr>
      <tr><td>Tolerance multiplier when OCR'd</td><td class="num">${p.ocr_tolerance_multiplier}×</td></tr>
      <tr><td>Stale after</td><td class="num">${p.stale_invoice_days} days</td></tr>
    </tbody></table>
    <p class="sub" style="margin:9px 0 0">Rules and tolerances come from the PO and these settings.
    Nothing is approved by a model — extraction is probabilistic, the decision is not.</p>`));
  left.appendChild(pol);
  g.appendChild(left);

  // centre: live run
  const mid = el("div"); mid.id = "midcol";
  const stageCard = el("div", "card");
  stageCard.appendChild(el("h3", null,
    `Live run <span class="r" id="runid">${S.run ? esc(S.run) : "no run yet"}</span>`));
  const sc = el("div", "stages"); sc.id = "stages";
  stageCard.appendChild(sc);
  mid.appendChild(stageCard);
  const after = el("div"); after.id = "aftercard"; after.style.marginTop = "12px";
  mid.appendChild(after);
  g.appendChild(mid);

  // right: the document
  const doccol = el("div", "doccol");
  const dc = el("div", "card");
  dc.appendChild(el("h3", null, `Document <span class="r">${S.detail ? esc(S.detail.filename) : ""}</span>`));
  dc.appendChild(S.run
    ? Object.assign(el("iframe", "docframe"), { src: `/api/runs/${S.run}/pdf#toolbar=0&view=FitH` })
    : el("div", "dstate", "The invoice appears here once a run starts."));
  doccol.appendChild(dc);
  g.appendChild(doccol);

  setTimeout(() => { paintStages(); if (S.detail) paintDetail(); }, 0);
  return g;
}

function paintStages() {
  const host = $("#stages"); if (!host) return;
  host.innerHTML = "";
  if (!S.stages.length) { host.appendChild(el("div", "dstate", "Pick an invoice on the left to run it.")); return; }
  S.stages.forEach(s => {
    const row = el("div", "stage"); row.dataset.s = s.status;
    const has = s.payload && Object.keys(s.payload).length;
    row.innerHTML = `<div><i class="sdot"></i></div>
      <div><div class="lab">${esc(s.label)}</div><div class="msg">${esc(s.message || (s.status === "pending" ? "waiting" : "…"))}</div>
      ${has ? `<details><summary>what this stage produced</summary><pre class="json">${esc(JSON.stringify(s.payload, null, 2))}</pre></details>` : ""}</div>
      <div class="ms">${s.ms != null ? s.ms + " ms" : ""}</div>`;
    host.appendChild(row);
  });
  const rid = $("#runid"); if (rid && S.run) rid.textContent = S.run;
}

/* ------------------------------------------------------------ run detail UI */
function paintDetail() {
  const host = $("#aftercard"); if (!host || !S.detail) return;
  const d = S.detail, p = d.payload, dec = p.decision, meta = S.boot.decisions[dec.outcome];
  host.innerHTML = "";

  const v = el("div", `verdict ${meta.tone}`);
  v.innerHTML = `<div class="top"><h2>${esc(meta.label)}</h2>
      <span class="amt">${money(d.gross_amount, d.currency)}</span>
      ${d.match_basis != null && d.match_basis !== d.gross_amount
        ? `<span class="amt" title="the figure compared to the purchase order">matched on ${num(d.match_basis)}</span>` : ""}
      <span class="sub" style="margin-left:auto">${d.duration_ms} ms of processing · ${esc(d.doc_type)} · ${esc((d.extractors || []).join(" + "))}</span></div>
    <p>${esc(dec.rationale)}</p>
    <div class="next"><b>Next:</b> ${esc(dec.next_step)} <span class="sub">— owner: ${esc(dec.owner)}</span></div>`;
  host.appendChild(v);

  const card = el("div", "card"); card.style.marginTop = "12px";
  const tabs = el("div", "tabs");
  const bodyEl = el("div", "tabbody");
  const TABS = [["checks", `Checks (${p.checks.length})`], ["fields", "What it read"],
                ["money", "Money"], ["out", `Output (${p.actions.length})`], ["audit", "Audit"]];
  let cur = "checks";
  const paint = () => {
    [...tabs.children].forEach(b => b.classList.toggle("sel", b.dataset.t === cur));
    bodyEl.innerHTML = "";
    bodyEl.appendChild({ checks: tabChecks, fields: tabFields, money: tabMoney,
                         out: tabOut, audit: tabAudit }[cur](d));
  };
  TABS.forEach(([k, label]) => {
    const b = el("button", null, esc(label)); b.dataset.t = k;
    b.onclick = () => { cur = k; paint(); }; tabs.appendChild(b);
  });
  card.appendChild(tabs); card.appendChild(bodyEl); host.appendChild(card); paint();
}

function tabChecks(d) {
  const w = el("div");
  const cs = d.payload.checks;
  const failed = cs.filter(c => !c.passed), ok = cs.filter(c => c.passed);
  if (failed.length) w.appendChild(el("div", "sub", `${failed.length} check${failed.length > 1 ? "s" : ""} did not pass`));
  failed.concat(ok).forEach(c => {
    const n = el("div", "check" + (c.passed ? "" : " f"));
    n.dataset.sev = c.severity;
    n.innerHTML = `<div class="h"><span class="tick ${c.passed ? "y" : "n"}">${c.passed ? "✓" : "✕"}</span>
        <span class="t">${esc(c.title)}</span><span class="code">${esc(c.code)}</span>
        <span class="sev ${c.passed ? "PASS" : c.severity}">${c.passed ? "pass" : c.severity}</span></div>
      <div class="d">${esc(c.detail)}</div>
      ${Object.keys(c.evidence || {}).length
        ? `<details><summary style="font-size:11.5px;color:var(--accent);cursor:pointer;margin-top:5px">evidence</summary><pre class="json">${esc(JSON.stringify(c.evidence, null, 2))}</pre></details>` : ""}`;
    w.appendChild(n);
  });
  return w;
}

function tabFields(d) {
  const f = d.payload.extraction.fields, w = el("div");
  const rows = Object.entries(f).map(([k, v]) => {
    const c = v.confidence, tone = c >= 0.9 ? "g" : c >= 0.85 ? "" : "a";
    return `<tr><td>${esc(k)}</td>
      <td class="mono">${esc(v.value)}</td>
      <td style="width:96px"><div class="bar ${tone}"><i style="width:${Math.round(c * 100)}%"></i></div>
        <span class="sub">${pct(c)}</span></td>
      <td><span class="srcpill ${esc(v.source)}">${esc(v.source)}</span></td>
      <td class="sub">${esc(v.provenance || "")}${v.notes ? `<br><i>${esc(v.notes)}</i>` : ""}</td></tr>`;
  }).join("");
  w.innerHTML = `<table class="t"><thead><tr><th>Field</th><th>Value</th><th>Confidence</th>
      <th>Source</th><th>Where it came from</th></tr></thead><tbody>${rows}</tbody></table>`;
  const li = d.payload.extraction.line_items;
  if (li.length) {
    const t = el("div"); t.style.marginTop = "14px";
    t.innerHTML = `<div class="sub" style="margin-bottom:6px">${li.length} line items ·
      they sum to <b>${num(li.reduce((a, b) => a + (b.amount || 0), 0))}</b></div>
      <table class="t"><thead><tr><th>Description</th><th class="num">Qty</th>
      <th class="num">Unit</th><th class="num">Amount</th></tr></thead><tbody>
      ${li.map(l => `<tr><td>${esc(l.description)}</td><td class="num">${l.quantity ?? "—"}</td>
        <td class="num">${l.unit_price != null ? num(l.unit_price) : "—"}</td>
        <td class="num">${num(l.amount)}</td></tr>`).join("")}</tbody></table>`;
    w.appendChild(t);
  }
  return w;
}

function tabMoney(d) {
  const m = d.payload.money, po = d.payload.po, w = el("div");
  const used = po.po_amount ? Math.min(1, (m.cumulative ?? m.basis ?? 0) / po.po_amount) : 0;
  const over = po.po_amount && (m.cumulative ?? 0) > po.po_amount;
  w.innerHTML = `
    <table class="t"><tbody>
      <tr><td>Compared to the PO on</td><td class="num">${num(m.basis)}</td><td class="sub">${esc(m.basis_label)}</td></tr>
      <tr><td>Gross payable</td><td class="num">${num(m.gross)}</td><td class="sub">what the vendor asked for</td></tr>
      <tr><td>Purchase order</td><td class="num">${num(po.po_amount)}</td><td class="sub">${esc(po.po_number || "—")} · ${esc(po.description || "")}</td></tr>
      <tr><td>Already invoiced before this</td><td class="num">${num(m.previously_billed)}</td><td class="sub">from the run ledger</td></tr>
      <tr><td>Cumulative after this invoice</td><td class="num">${num(m.cumulative)}</td><td class="sub">${over ? "<b style='color:var(--fail)'>over the order</b>" : "inside the order"}</td></tr>
      <tr><td>Tolerance applied</td><td class="num">${num(m.tolerance)}</td><td class="sub">${esc(m.tolerance_label || "—")}</td></tr>
    </tbody></table>
    <div style="margin-top:14px">
      <div class="sub" style="margin-bottom:5px">PO utilisation ${po.po_number ? "· " + esc(po.po_number) : ""}</div>
      <div class="bar ${over ? "r" : "g"}" style="height:12px"><i style="width:${Math.round(used * 100)}%"></i></div>
      <div class="sub" style="margin-top:4px">${num(m.cumulative ?? m.basis)} of ${num(po.po_amount)}</div>
    </div>`;
  return w;
}

function tabOut(d) {
  const w = el("div");
  d.payload.actions.forEach(a => {
    const c = el("div", "msgcard");
    c.innerHTML = `<div class="mh"><span class="kind">${esc(a.kind.replace("_", " "))}</span>
        <b>${esc(a.subject)}</b><span class="sub" style="margin-left:auto">→ ${esc(a.target)}</span></div>
      <pre>${esc(a.body)}</pre>`;
    w.appendChild(c);
  });
  if (!d.payload.actions.length) w.appendChild(el("div", "dstate", "No output produced."));
  return w;
}

function tabAudit(d) {
  const w = el("div");
  w.innerHTML = `<table class="t"><tbody>
    <tr><td>Run</td><td class="mono">${esc(d.run_id)}</td></tr>
    <tr><td>File</td><td class="mono">${esc(d.filename)} · sha256 ${esc((d.file_hash || "").slice(0, 16))}…</td></tr>
    <tr><td>Recorded</td><td class="mono">${esc(d.created_at)}</td></tr>
    <tr><td>Extractors</td><td>${esc((d.extractors || []).join(" + "))}</td></tr>
    <tr><td>Reason codes</td><td class="mono">${d.reason_codes.length ? esc(d.reason_codes.join(", ")) : "none"}</td></tr>
    <tr><td>Vendor identity risk</td><td>${d.payload.identity.risk}/10 ${d.payload.identity.signals.length
      ? "<div class='sub'>" + d.payload.identity.signals.map(esc).join("<br>") + "</div>" : ""}</td></tr>
  </tbody></table>`;
  if (d.overrides && d.overrides.length) {
    const o = el("div"); o.style.marginTop = "14px";
    o.innerHTML = `<div class="sub" style="margin-bottom:6px">Human actions on this run</div>
      <table class="t"><thead><tr><th>When</th><th>Who</th><th>Action</th><th>From → to</th><th>Note</th></tr></thead>
      <tbody>${d.overrides.map(v => `<tr><td class="mono">${esc(v.ts)}</td><td>${esc(v.actor)}</td>
        <td>${esc(v.action)}</td><td class="mono">${esc(v.from_decision)} → ${esc(v.to_decision)}</td>
        <td class="sub">${esc(v.note || "")}</td></tr>`).join("")}</tbody></table>`;
    w.appendChild(o);
  }
  const ev = el("details"); ev.style.marginTop = "14px";
  ev.innerHTML = `<summary style="font-size:12px;color:var(--accent);cursor:pointer">full stage trace as stored</summary>
    <pre class="json" style="max-height:420px">${esc(JSON.stringify(d.events.map(e =>
      ({ stage: e.stage, status: e.status, ms: e.duration_ms, message: e.message })), null, 2))}</pre>`;
  w.appendChild(ev);
  return w;
}

/* ------------------------------------------------------------- queue + dash */
async function openRun(runId) {
  S.run = runId;
  S.detail = await (await fetch(`/api/runs/${runId}`)).json();
  S.stages = S.detail.events.map(e => ({ stage: e.stage, label: e.stage, status: e.status,
    message: e.message, payload: e.payload, ms: e.duration_ms }));
  const seen = new Set(); S.stages = S.stages.filter(s => !seen.has(s.stage) || !seen.add(s.stage));
  S.stages = SKELETON.map(k => S.stages.find(s => s.stage === k.stage) || k)
    .map(s => ({ ...s, label: SKELETON.find(k => k.stage === s.stage)?.label || s.stage }));
  S.view = "run";
  [...$("#nav").querySelectorAll("button")].forEach(x => x.classList.toggle("sel", x.dataset.v === "run"));
  render();
}

async function viewQueue(v) {
  const s = await refreshCounts();
  const head = el("div", "pad");
  head.innerHTML = `<h2 style="margin:0 0 4px;font-size:17px">Exception queue</h2>
    <p class="sub" style="margin:0 0 14px">Everything the process deliberately refused to decide alone.
    Acting here re-runs the invoice through the same rules — a confirmation changes one input, it does not skip a check.</p>`;
  v.appendChild(head);
  const wrap = el("div", "pad"); wrap.style.paddingTop = "0";
  if (!s.exceptions.length) { wrap.appendChild(el("div", "dstate", "Nothing waiting. Run an edge case to fill this.")); }
  s.exceptions.forEach(x => {
    const meta = S.boot.decisions[x.decision];
    const c = el("div", "qcard");
    const isBuyer = x.decision === "ROUTE_TO_BUYER";
    const isFraud = x.decision === "BLOCKED_FRAUD_REVIEW";
    c.innerHTML = `<div class="qh"><span class="pill ${meta.tone}">${esc(meta.label)}</span>
        <b>${esc(x.vendor_name || "—")}</b><span class="mono sub">${esc(x.invoice_number || "no reference")}</span>
        <span class="amt" style="background:var(--line-2)">${money(x.gross_amount)}</span>
        <span class="sub" style="margin-left:auto">${esc(x.created_at)}</span></div>
      <div class="sub" style="margin-top:7px;line-height:1.6">${esc(x.rationale)}</div>`;
    const acts = el("div", "acts");
    const who = el("input"); who.type = "text"; who.placeholder = "your name"; who.value =
      isBuyer ? "Daniel Okonkwo (buyer)" : "S. Chitikesi (AP)";
    acts.appendChild(who);
    const act = async (action, label) => {
      const f = new FormData(); f.append("action", action); f.append("actor", who.value || "reviewer");
      f.append("note", label);
      const r = await (await fetch(`/api/runs/${x.run_id}/resolve`, { method: "POST", body: f })).json();
      toast(`${label} → ${r.outcome.replaceAll("_", " ").toLowerCase()}`);
      openRun(x.run_id);
    };
    if (isBuyer) {
      const b = el("button", "primary", "Approve the overage"); b.onclick = () => act("approve", "budget owner approved the variance");
      acts.appendChild(b);
    } else if (!isFraud) {
      const b = el("button", "primary", "Confirm readings &amp; release");
      b.onclick = () => act("confirm", "fields and PO link confirmed against the document");
      acts.appendChild(b);
    } else {
      acts.appendChild(el("span", "sub",
        "Bank changes are verified out of band by financial controls. This queue cannot release it."));
    }
    const r = el("button", "ghost", "Reject"); r.onclick = () => act("reject", "closed without payment");
    acts.appendChild(r);
    const o = el("button", "ghost", "Open run"); o.onclick = () => openRun(x.run_id);
    acts.appendChild(o);
    c.appendChild(acts);
    wrap.appendChild(c);
  });
  v.appendChild(wrap);
}

async function viewDash(v) {
  const s = await refreshCounts();
  const k = el("div", "kpis");
  const tiles = [
    ["Invoices processed", s.runs, `${s.scanned} needed OCR`],
    ["Straight through", pct(s.straight_through_rate), `no human touched them · ${s.approved_after_review} more cleared after review`],
    ["Value processed", money(s.value_processed), "gross, all decisions"],
    ["Median run", s.median_ms + " ms", "ingest to audit trail, compute only"],
    ["Open exceptions", s.open_exceptions, "waiting on a person"],
  ];
  tiles.forEach(([a, b, c]) => k.appendChild(el("div", "kpi",
    `<div class="k">${esc(a)}</div><div class="v">${esc(b)}</div><div class="s">${esc(c)}</div>`)));
  v.appendChild(k);

  const row = el("div", "kpis"); row.style.gridTemplateColumns = "1fr 1fr";
  const mix = el("div", "card"); mix.appendChild(el("h3", null, "Where invoices ended up"));
  const stack = el("div", "dstack");
  const max = Math.max(1, ...Object.values(s.by_decision));
  Object.entries(s.by_decision).sort((a, b) => b[1] - a[1]).forEach(([d, n]) => {
    const meta = S.boot.decisions[d] || { label: d, tone: "warn" };
    const tone = meta.tone === "pass" ? "g" : meta.tone === "fail" ? "r" : "a";
    stack.appendChild(el("div", "drow",
      `<span>${esc(meta.label)}</span><div class="bar ${tone}"><i style="width:${(n / max) * 100}%"></i></div>
       <span class="mono">${n}</span>`));
  });
  mix.appendChild(stack); row.appendChild(mix);

  const rc = el("div", "card"); rc.appendChild(el("h3", null, "Most common reason codes"));
  const rs = el("div", "dstack");
  const rmax = Math.max(1, ...s.top_reason_codes.map(x => x[1]));
  s.top_reason_codes.forEach(([code, n]) => rs.appendChild(el("div", "drow",
    `<span class="mono" style="font-size:11.5px">${esc(code)}</span>
     <div class="bar"><i style="width:${(n / rmax) * 100}%"></i></div><span class="mono">${n}</span>`)));
  if (!s.top_reason_codes.length) rs.appendChild(el("div", "sub", "Nothing flagged yet."));
  rc.appendChild(rs); row.appendChild(rc);
  v.appendChild(row);

  const f = el("div", "filters");
  const search = el("input"); search.placeholder = "vendor, invoice, PO…"; search.value = S.q;
  search.oninput = () => { S.q = search.value; loadTable(); };
  f.appendChild(search);
  ["", ...Object.keys(S.boot.decisions)].forEach(d => {
    const b = el("button", "fchip" + (S.filter === d ? " sel" : ""),
      esc(d ? S.boot.decisions[d].label : "All"));
    b.onclick = () => { S.filter = d; render(); };
    f.appendChild(b);
  });
  v.appendChild(f);

  const host = el("div", "pad"); host.id = "histhost"; v.appendChild(host);
  async function loadTable() {
    const qs = new URLSearchParams(); if (S.filter) qs.set("decision", S.filter); if (S.q) qs.set("q", S.q);
    const runs = await (await fetch("/api/runs?" + qs)).json();
    host.innerHTML = "";
    const c = el("div", "card");
    c.appendChild(el("h3", null, `Run history <span class="r">${runs.length} shown</span>`));
    if (!runs.length) { c.appendChild(el("div", "dstate", "No runs match.")); host.appendChild(c); return; }
    const t = el("table", "t");
    t.innerHTML = `<thead><tr><th>When</th><th>Vendor</th><th>Invoice</th><th>PO</th>
      <th class="num">Matched on</th><th class="num">Gross</th><th>Decision</th><th class="num">Conf.</th>
      <th class="num">ms</th></tr></thead><tbody>${runs.map(r => {
        const meta = S.boot.decisions[r.decision] || { label: r.decision, tone: "warn" };
        return `<tr class="clickable" data-id="${esc(r.run_id)}">
          <td class="sub mono">${esc(r.created_at.slice(5, 16).replace("T", " "))}</td>
          <td>${esc(r.vendor_name || "—")}${r.doc_type === "scanned" ? ' <span class="srcpill ocr">scan</span>' : ""}</td>
          <td class="mono">${esc(r.invoice_number || "—")}</td>
          <td class="mono">${esc(r.po_number || "—")}</td>
          <td class="num">${num(r.match_basis)}</td>
          <td class="num">${num(r.gross_amount)}</td>
          <td><span class="pill ${meta.tone}">${esc(meta.label)}</span></td>
          <td class="num">${r.min_confidence != null ? pct(r.min_confidence) : "—"}</td>
          <td class="num sub">${r.duration_ms}</td></tr>`; }).join("")}</tbody>`;
    t.onclick = e => { const tr = e.target.closest("tr[data-id]"); if (tr) openRun(tr.dataset.id); };
    c.appendChild(t); host.appendChild(c);
  }
  loadTable();
}

function viewRef(v) {
  const m = S.boot.master;
  const head = el("div", "pad");
  head.innerHTML = `<h2 style="margin:0 0 4px;font-size:17px">Reference data</h2>
    <p class="sub" style="margin:0 0 4px">What the process matches against. In production these are ERP reads;
    here they are three CSV files in <span class="mono">data/master/</span> — edit one, close a PO or move a
    tolerance, and the next run decides differently.</p>`;
  v.appendChild(head);

  const wrap = el("div", "pad"); wrap.style.paddingTop = "0";
  const po = el("div", "card");
  po.appendChild(el("h3", null, "Purchase orders"));
  po.appendChild(el("div", null, `<table class="t"><thead><tr><th>PO</th><th>Vendor</th><th>Description</th>
    <th class="num">Order</th><th class="num">Invoiced</th><th class="num">Receipted</th><th>Utilisation</th>
    <th>Status</th><th class="num">Tolerance</th><th>Partial</th></tr></thead><tbody>
    ${m.purchase_orders.map(p => {
      const u = Math.min(1, p.billed / p.po_amount);
      return `<tr><td class="mono">${esc(p.po_number)}</td><td class="sub">${esc(p.vendor_id)}</td>
      <td>${esc(p.description)}</td><td class="num">${num(p.po_amount)}</td>
      <td class="num">${num(p.billed)}</td><td class="num">${num(p.received)}</td>
      <td style="width:90px"><div class="bar ${u >= 1 ? "r" : "g"}"><i style="width:${u * 100}%"></i></div></td>
      <td>${esc(p.status)}</td><td class="num sub">${p.tolerance_pct}% / ${num(p.tolerance_abs)}</td>
      <td class="sub">${p.allow_partial ? "yes" : "no"}</td></tr>`; }).join("")}</tbody></table>`));
  wrap.appendChild(po);

  const ve = el("div", "card"); ve.style.marginTop = "12px";
  ve.appendChild(el("h3", null, "Vendor master"));
  ve.appendChild(el("div", null, `<table class="t"><thead><tr><th>ID</th><th>Legal name</th>
    <th>Also known as</th><th>Status</th><th>Remit domain</th><th>Bank ends</th><th>Terms</th></tr></thead>
    <tbody>${m.vendors.map(x => `<tr><td class="mono">${esc(x.vendor_id)}</td><td>${esc(x.legal_name)}</td>
      <td class="sub">${esc((x.aliases || []).join(", "))}</td>
      <td>${x.status === "ACTIVE" ? '<span class="pill pass">active</span>' : `<span class="pill warn">${esc(x.status.toLowerCase())}</span>`}</td>
      <td class="mono">${esc(x.remit_domain)}</td><td class="mono">••••${esc(x.bank_last4)}</td>
      <td class="sub">${esc(x.payment_terms)}</td></tr>`).join("")}</tbody></table>`));
  wrap.appendChild(ve);

  const gr = el("div", "card"); gr.style.marginTop = "12px";
  gr.appendChild(el("h3", null, "Goods receipts"));
  gr.appendChild(el("div", null, `<table class="t"><thead><tr><th>Receipt</th><th>PO</th>
    <th class="num">Value</th><th>Date</th><th>Note</th></tr></thead><tbody>
    ${m.goods_receipts.map(g => `<tr><td class="mono">${esc(g.gr_id)}</td><td class="mono">${esc(g.po_number)}</td>
      <td class="num">${num(g.received_amount)}</td><td class="sub">${esc(g.received_date)}</td>
      <td class="sub">${esc(g.note)}</td></tr>`).join("")}</tbody></table>`));
  wrap.appendChild(gr);
  v.appendChild(wrap);
}

boot();
