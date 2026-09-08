"""HTTP surface: start runs, watch them execute, review the queue, see history."""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import (AUTO_APPROVE_CEILING, COMPANY_NAME, MIN_FIELD_CONFIDENCE,
                     OCR_TOLERANCE_MULTIPLIER, SAMPLES, STALE_INVOICE_DAYS, UPLOADS, WEB)
from .engine import master
from .extract import llm
from .extract.ocr import ocr_available
from .models import DECISION_META, new_id, now_iso
from .pipeline import BUSES, RunBus, run_pipeline

app = FastAPI(title="Northgate AP — invoice to decision")
db.init()

SAMPLE_META = {
    "01_apex_happy_path.pdf": ("Happy path", "Clean digital PDF, PO printed on it, amounts line up."),
    "02_meridian_partial_1of2.pdf": ("Edge 1 · first of a split", "One PO, two invoices. This is the first."),
    "03_meridian_partial_2of2_overbill.pdf": ("Edge 1 · the split goes over", "The second invoice pushes cumulative billing past the order."),
    "04_borealis_scanned_no_po.pdf": ("Edge 2 · scan, no PO printed", "Image-only PDF. The PO has to be inferred."),
    "05_apex_bank_change_fraud.pdf": ("Edge 3 · changed bank account", "Matches the PO perfectly. Remit-to does not match the vendor record."),
    "06_apex_duplicate_resend.pdf": ("Edge 4 · duplicate resend", "Same invoice, different layout, differently punctuated number."),
    "07_lumen_incomplete.pdf": ("Edge 5 · incomplete", "No invoice number and the line items do not add up."),
}


@app.get("/api/bootstrap")
def bootstrap():
    master.reload()
    ocr_ok, ocr_ver = ocr_available()
    samples = []
    for p in sorted(SAMPLES.glob("*.pdf")):
        label, note = SAMPLE_META.get(p.name, (p.stem, ""))
        samples.append({"file": p.name, "label": label, "note": note,
                        "kb": round(p.stat().st_size / 1024)})
    return {
        "company": COMPANY_NAME,
        "samples": samples,
        "decisions": {k: {"label": v[0], "tone": v[1], "meaning": v[2]} for k, v in DECISION_META.items()},
        "policy": {"auto_approve_ceiling": AUTO_APPROVE_CEILING,
                   "min_field_confidence": MIN_FIELD_CONFIDENCE,
                   "ocr_tolerance_multiplier": OCR_TOLERANCE_MULTIPLIER,
                   "stale_invoice_days": STALE_INVOICE_DAYS},
        "extractors": {"layout_parser": True, "ocr": ocr_ok, "ocr_version": ocr_ver,
                       "claude": llm.available()},
        "master": {
            "vendors": [v.__dict__ for v in master.vendors().values()],
            "purchase_orders": [{**p.__dict__, "po_date": p.po_date.isoformat(),
                                 "billed": db.billed_against_po(p.po_number),
                                 "received": master.received_against(p.po_number)}
                                for p in master.purchase_orders().values()],
            "goods_receipts": [{**g.__dict__, "received_date": g.received_date.isoformat()}
                               for g in master.goods_receipts()],
        },
    }


@app.post("/api/run")
async def start_run(sample: str = Form(default=""), file: UploadFile | None = File(default=None)):
    if file is not None and file.filename:
        run_id = new_id("run")
        dest = UPLOADS / f"src_{run_id}.pdf"
        with dest.open("wb") as fh:
            shutil.copyfileobj(file.file, fh)
        src, name = dest, file.filename
    elif sample:
        src = SAMPLES / Path(sample).name
        if not src.exists():
            raise HTTPException(404, f"no sample named {sample}")
        run_id, name = new_id("run"), src.name
    else:
        raise HTTPException(400, "send a sample name or a PDF file")

    BUSES[run_id] = RunBus(run_id)
    asyncio.create_task(run_pipeline(src, filename=name, run_id=run_id, bus=BUSES[run_id]))
    return {"run_id": run_id, "filename": name}


@app.get("/api/runs/{run_id}/events")
async def events(run_id: str):
    bus = BUSES.get(run_id)
    if bus is None:
        raise HTTPException(404, "unknown run")
    q: asyncio.Queue = asyncio.Queue()
    backlog = list(bus.events)
    bus.queues.append(q)

    async def gen():
        try:
            for ev in backlog:
                yield f"data: {json.dumps(ev)}\n\n"
            if bus.done:
                yield 'data: {"type":"done"}\n\n'
                return
            while True:
                ev = await q.get()
                yield f"data: {json.dumps(ev)}\n\n"
                if ev.get("type") == "done":
                    return
        finally:
            if q in bus.queues:
                bus.queues.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs")
def list_runs(decision: str = "", queue: str = "", q: str = ""):
    sql = ("SELECT run_id, created_at, filename, doc_type, vendor_name, invoice_number, po_number,"
           " currency, match_basis, gross_amount, decision, rationale, min_confidence, duration_ms,"
           " queue_status, reason_codes FROM runs WHERE 1=1")
    args: list = []
    if decision:
        sql += " AND decision = ?"; args.append(decision)
    if queue:
        sql += " AND queue_status = ?"; args.append(queue)
    if q:
        sql += " AND (vendor_name LIKE ? OR invoice_number LIKE ? OR po_number LIKE ? OR filename LIKE ?)"
        args += [f"%{q}%"] * 4
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT 300"
    out = db.rows(sql, tuple(args))
    for r in out:
        r["reason_codes"] = json.loads(r["reason_codes"] or "[]")
    return out


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    r = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    if not r:
        raise HTTPException(404, "unknown run")
    r["payload"] = json.loads(r["payload"] or "{}")
    r["reason_codes"] = json.loads(r["reason_codes"] or "[]")
    r["extractors"] = json.loads(r["extractors"] or "[]")
    r["events"] = db.rows("SELECT * FROM events WHERE run_id = ? ORDER BY id", (run_id,))
    for e in r["events"]:
        e["payload"] = json.loads(e["payload"] or "{}")
    r["overrides"] = db.rows("SELECT * FROM overrides WHERE run_id = ? ORDER BY id", (run_id,))
    return r


@app.get("/api/runs/{run_id}/pdf")
def run_pdf(run_id: str):
    p = UPLOADS / f"{run_id}.pdf"
    if not p.exists():
        raise HTTPException(404, "no document stored for this run")
    return FileResponse(p, media_type="application/pdf")


@app.post("/api/runs/{run_id}/resolve")
async def resolve(run_id: str, action: str = Form(...), actor: str = Form("AP reviewer"),
                  note: str = Form("")):
    r = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    if not r:
        raise HTTPException(404, "unknown run")
    payload = json.loads(r["payload"] or "{}")
    src = UPLOADS / f"{run_id}.pdf"
    if not src.exists():
        raise HTTPException(400, "the source document is no longer available")

    if action == "reject":
        db.execute("INSERT INTO overrides (run_id, ts, actor, action, note, from_decision, to_decision)"
                   " VALUES (?,?,?,?,?,?,?)",
                   (run_id, now_iso(), actor, "reject", note, r["decision"], "REJECTED_BY_REVIEWER"))
        db.execute("UPDATE runs SET queue_status='CLOSED', rationale = rationale || ? WHERE run_id = ?",
                   (f" · Closed by {actor} without payment: {note or 'no reason given'}.", run_id))
        return {"run_id": run_id, "outcome": "REJECTED_BY_REVIEWER", "rerun": False}

    overrides: dict = {**db.accumulated_overrides(run_id), "actor": actor, "rerun_of": run_id}
    if action == "confirm":
        weak = []
        for c in payload.get("checks", []):
            if c["code"] == "READ_CONFIDENCE" and not c["passed"]:
                weak = list(c["evidence"].get("weak_fields", {}).keys())
        overrides["confirmed_fields"] = sorted(set(overrides.get("confirmed_fields", [])) | set(weak))
        po = (payload.get("po") or {}).get("po_number")
        if (payload.get("po") or {}).get("method") == "inferred" and po:
            overrides["confirmed_po"] = po
    elif action == "approve":
        overrides["buyer_approved"] = True
        overrides["confirmed_fields"] = sorted(set(overrides.get("confirmed_fields", [])) | {
            f for c in payload.get("checks", []) if c["code"] == "READ_CONFIDENCE" and not c["passed"]
            for f in c["evidence"].get("weak_fields", {})})
    else:
        raise HTTPException(400, f"unknown action {action}")

    db.execute("INSERT INTO overrides (run_id, ts, actor, action, note, from_decision, to_decision,"
               " applied) VALUES (?,?,?,?,?,?,?,?)",
               (run_id, now_iso(), actor, action, note, r["decision"], "re-evaluating",
                db.j({k: v for k, v in overrides.items() if k not in ("actor", "rerun_of")})))

    BUSES[run_id] = RunBus(run_id)
    result = await run_pipeline(src, filename=r["filename"], run_id=run_id,
                                overrides=overrides, bus=BUSES[run_id])
    db.execute("UPDATE overrides SET to_decision = ? WHERE run_id = ? AND to_decision = 're-evaluating'",
               (result["outcome"], run_id))
    return {**result, "rerun": True}


@app.get("/api/stats")
def stats():
    runs = db.rows("SELECT decision, match_basis, gross_amount, duration_ms, queue_status,"
                   " doc_type FROM runs")
    total = len(runs)
    by_decision: dict[str, int] = {}
    for r in runs:
        by_decision[r["decision"]] = by_decision.get(r["decision"], 0) + 1
    touched = sum(float(r["gross_amount"] or 0) for r in runs)
    straight = sum(1 for r in runs if r["decision"] in ("AUTO_APPROVED", "APPROVED_PARTIAL"))
    after_review = sum(1 for r in runs if r["decision"] == "APPROVED_AFTER_REVIEW")
    exceptions = db.rows("SELECT run_id, vendor_name, invoice_number, decision, gross_amount,"
                         " rationale, created_at FROM runs WHERE queue_status='OPEN'"
                         " ORDER BY created_at DESC")
    codes: dict[str, int] = {}
    for r in db.rows("SELECT reason_codes FROM runs"):
        for c in json.loads(r["reason_codes"] or "[]"):
            codes[c] = codes.get(c, 0) + 1
    return {
        "runs": total,
        "straight_through_rate": round(straight / total, 4) if total else 0.0,
        "approved_after_review": after_review,
        "value_processed": round(touched, 2),
        "median_ms": sorted(r["duration_ms"] or 0 for r in runs)[total // 2] if total else 0,
        "open_exceptions": len(exceptions),
        "by_decision": by_decision,
        "top_reason_codes": sorted(codes.items(), key=lambda kv: -kv[1])[:8],
        "exceptions": exceptions,
        "scanned": sum(1 for r in runs if r["doc_type"] == "scanned"),
    }


@app.post("/api/reset")
def reset():
    for t in ("runs", "events", "ledger", "actions", "overrides"):
        db.execute(f"DELETE FROM {t}")
    for p in UPLOADS.glob("*.pdf"):
        p.unlink()
    BUSES.clear()
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


app.mount("/", StaticFiles(directory=WEB), name="web")
