"""The process itself: ten stages, each one observable.

Every stage records what it did, how long it took, and the payload it produced,
and pushes that to anyone watching. That trace is not debug output -- it is the
product. An AP controller who has to defend a payment six months from now opens
the run and reads it top to bottom.

The same function serves the first run and every re-run after a human
intervenes, so a confirmed field never skips a check; it just changes one input
and everything is evaluated again.
"""
from __future__ import annotations

import asyncio
import hashlib
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import db
from .actions.notify import build_actions
from .config import STAGE_DELAY_MS, UPLOADS
from .engine import master, matching, policy, validators
from .extract import extract_document
from .models import (APPROVED_AFTER_REVIEW, APPROVED_PARTIAL, APPROVING, AUTO_APPROVED,
                     DECISION_META, new_id, now_iso)

STAGES = [
    ("INGEST", "Ingest document"),
    ("READ", "Read the document"),
    ("EXTRACT", "Extract fields"),
    ("NORMALISE", "Normalise and apply confirmations"),
    ("VENDOR", "Resolve vendor"),
    ("PO", "Match purchase order"),
    ("VALIDATE", "Run validation rules"),
    ("DECIDE", "Apply decision policy"),
    ("ACT", "Produce output"),
    ("RECORD", "Write audit trail"),
]


@dataclass
class RunBus:
    """Buffers a run's events so a client that connects late sees all of them."""
    run_id: str
    events: list[dict] = field(default_factory=list)
    queues: list[asyncio.Queue] = field(default_factory=list)
    done: bool = False

    def publish(self, ev: dict) -> None:
        self.events.append(ev)
        for q in list(self.queues):
            q.put_nowait(ev)

    def finish(self) -> None:
        self.done = True
        for q in list(self.queues):
            q.put_nowait({"type": "done"})


BUSES: dict[str, RunBus] = {}


def _emit(bus: RunBus, run_id: str, seq: int, stage: str, label: str, status: str,
          message: str, payload: dict | None = None, duration_ms: int | None = None) -> dict:
    ev = {"type": "stage", "run_id": run_id, "seq": seq, "stage": stage, "label": label,
          "status": status, "message": message, "payload": payload or {},
          "duration_ms": duration_ms, "ts": now_iso()}
    bus.publish(ev)
    if status != "running":
        db.execute("INSERT INTO events (run_id, seq, ts, stage, status, message, duration_ms, payload)"
                   " VALUES (?,?,?,?,?,?,?,?)",
                   (run_id, seq, ev["ts"], stage, status, message, duration_ms, db.j(payload or {})))
    return ev


async def run_pipeline(pdf_path: Path, *, filename: str, run_id: str | None = None,
                       overrides: dict | None = None, bus: RunBus | None = None) -> dict:
    overrides = overrides or {}
    run_id = run_id or new_id("run")
    bus = bus or BUSES.setdefault(run_id, RunBus(run_id))
    master.reload()
    t_run = time.perf_counter()
    seq = 0
    work_ms = 0  # actual compute, excluding the pacing delay used by the live view

    async def stage(key: str, label: str, fn):
        nonlocal seq, work_ms
        seq += 1
        _emit(bus, run_id, seq, key, label, "running", "…")
        if STAGE_DELAY_MS:
            await asyncio.sleep(STAGE_DELAY_MS / 1000)
        t0 = time.perf_counter()
        status, message, payload = await asyncio.to_thread(fn)
        ms = int((time.perf_counter() - t0) * 1000)
        work_ms += ms
        _emit(bus, run_id, seq, key, label, status, message, payload, ms)
        return payload

    # 1 -- ingest ------------------------------------------------------------
    stored = UPLOADS / f"{run_id}.pdf"
    state: dict = {}

    def _ingest():
        # A re-run reads the document already stored for this run, so there is
        # nothing to copy -- the bytes under review must not change.
        if Path(pdf_path).resolve() != stored.resolve():
            shutil.copyfile(pdf_path, stored)
        h = hashlib.sha256(stored.read_bytes()).hexdigest()
        state["file_hash"] = h
        size = stored.stat().st_size
        return "ok", f"{filename} · {size / 1024:.0f} KB", {
            "filename": filename, "sha256": h[:16], "bytes": size,
            "rerun_of": overrides.get("rerun_of")}

    await stage("INGEST", "Ingest document", _ingest)

    # 2+3 -- read and extract (one call, two reported stages) -----------------
    extract_events: list[tuple[str, dict]] = []

    def _read():
        doc = extract_document(stored, emit=lambda s, p: extract_events.append((s, p)))
        state["doc"] = doc
        read_ev = dict(extract_events[0][1]) if extract_events else {}
        ocr_ev = next((p for s, p in extract_events if s == "ocr"), None)
        if doc.doc_type == "scanned":
            return "warn", (f"No text layer — OCR'd at 300 dpi, mean word confidence "
                            f"{doc.ocr_mean_confidence:.1%}"), {**read_ev, "ocr": ocr_ev}
        return "ok", f"Text layer present · {read_ev.get('char_count', 0)} characters", read_ev

    await stage("READ", "Read the document", _read)

    def _extract():
        doc = state["doc"]
        local = next((p for s, p in extract_events if s == "parse_local"), {})
        llm_ev = next((p for s, p in extract_events if s == "parse_llm"), None)
        msg = f"{len([f for f in doc.fields.values() if f.present()])} fields · {len(doc.line_items)} line items"
        if llm_ev and not llm_ev.get("skipped"):
            msg += f" · two extractors, {llm_ev.get('agreed', 0)} agreed / {llm_ev.get('disagreed', 0)} differed"
        elif llm_ev:
            msg += " · second extractor unavailable, layout parser only"
        else:
            msg += " · layout parser only (no API key set)"
        status = "warn" if doc.disagreements else "ok"
        return status, msg, {"extractors": doc.extractors_used, "local": local, "llm": llm_ev,
                             "fields": {k: v.to_dict() for k, v in doc.fields.items()},
                             "line_items": [li.to_dict() for li in doc.line_items]}

    await stage("EXTRACT", "Extract fields", _extract)

    # 4 -- normalise / apply human confirmations ------------------------------
    def _normalise():
        doc = state["doc"]
        confirmed = overrides.get("confirmed_fields") or []
        for name in confirmed:
            fv = doc.fields.get(name)
            if fv:
                fv.confidence = 0.99
                fv.source = "human_confirmed"
                fv.notes = (fv.notes + " | " if fv.notes else "") + \
                    f"confirmed against the document by {overrides.get('actor', 'AP')}"
        norm = matching.normalise_invoice_number(doc.val("invoice_number"))
        state["invoice_number_norm"] = norm
        return ("ok" if not confirmed else "ok",
                f"Reference normalised to {norm or '—'}" +
                (f" · {len(confirmed)} field(s) confirmed by a human" if confirmed else ""),
                {"invoice_number_norm": norm, "confirmed_fields": confirmed,
                 "date_note": doc.get("invoice_date").notes})

    await stage("NORMALISE", "Normalise and apply confirmations", _normalise)

    # 5 -- vendor -------------------------------------------------------------
    def _vendor():
        doc = state["doc"]
        vm = matching.resolve_vendor(doc.val("vendor_name"), doc.val("remit_domain"))
        state["vm"] = vm
        if not vm.vendor:
            return "fail", "No vendor on the approved master matches", {"notes": vm.notes}
        status = "warn" if (vm.notes or vm.matched_on == "name") else "ok"
        return status, f"{vm.vendor.legal_name} ({vm.vendor.vendor_id}) · matched on {vm.matched_on}", {
            "vendor_id": vm.vendor.vendor_id, "legal_name": vm.vendor.legal_name,
            "matched_on": vm.matched_on, "name_similarity": vm.score, "name_exact": vm.name_exact,
            "status": vm.vendor.status, "bank_on_file": vm.vendor.bank_last4,
            "remit_domain_on_file": vm.vendor.remit_domain, "notes": vm.notes}

    await stage("VENDOR", "Resolve vendor", _vendor)

    # 6 -- purchase order -----------------------------------------------------
    def _po():
        doc, vm = state["doc"], state["vm"]
        basis, _gross, _label = validators.compute_basis(doc)
        from datetime import date as _date
        idate = _date.fromisoformat(doc.val("invoice_date")) if doc.val("invoice_date") else None
        pm = matching.resolve_po(doc.val("po_number"), vm.vendor, basis, idate)
        if overrides.get("confirmed_po") and pm.po and pm.po.po_number == overrides["confirmed_po"]:
            pm.method, pm.confidence = "confirmed", 0.99
            pm.notes.append(f"PO link confirmed by {overrides.get('actor', 'AP')}")
        state["pm"] = pm
        if not pm.po:
            return "fail", " ".join(pm.notes) or "No purchase order matched", {
                "method": pm.method, "candidates": pm.candidates, "notes": pm.notes}
        status = "ok" if pm.method in ("explicit", "confirmed") else "warn"
        return status, f"{pm.po.po_number} · {pm.method} · {pm.po.description}", {
            "po_number": pm.po.po_number, "method": pm.method, "confidence": pm.confidence,
            "po_amount": pm.po.po_amount, "status": pm.po.status, "buyer": pm.po.buyer_name,
            "cost_center": pm.po.cost_center, "allow_partial": pm.po.allow_partial,
            "tolerance_pct": pm.po.tolerance_pct, "tolerance_abs": pm.po.tolerance_abs,
            "notes": pm.notes}

    await stage("PO", "Match purchase order", _po)

    # 7 -- validate -----------------------------------------------------------
    def _validate():
        ctx = validators.build_context(state["doc"], state["vm"], state["pm"], run_id)
        checks = validators.run_all(ctx, run_id)
        if overrides.get("buyer_approved"):
            # The budget owner approving an over-PO invoice is also attesting that
            # the extra work was actually delivered, so their signature stands in
            # for the missing goods receipt. That substitution is written into the
            # check rather than left implicit.
            for c in checks:
                if c.code in ("PO_CUMULATIVE", "AMOUNT_TOLERANCE", "GOODS_RECEIPT") and not c.passed:
                    c.passed = True
                    c.detail += (f" Approved by {overrides.get('actor', 'the budget owner')} on "
                                 f"{now_iso()[:10]}; the variance is accepted and recorded"
                                 + (", and their approval stands in for the goods receipt."
                                    if c.code == "GOODS_RECEIPT" else "."))
                    c.evidence["buyer_approved_by"] = overrides.get("actor")
        state["ctx"], state["checks"] = ctx, checks
        failed = [c for c in checks if not c.passed]
        worst = max((c.severity for c in failed), key=lambda s: {"INFO": 0, "WARNING": 1,
                                                                 "CRITICAL": 2, "BLOCKER": 3}[s],
                    default="INFO")
        status = {"BLOCKER": "fail", "CRITICAL": "fail", "WARNING": "warn", "INFO": "ok"}[worst]
        return status, f"{len(checks) - len(failed)} of {len(checks)} checks passed", {
            "checks": [c.to_dict() for c in checks],
            "basis": ctx.basis, "basis_label": ctx.basis_label, "gross": ctx.gross,
            "tolerance": ctx.tolerance_value, "tolerance_label": ctx.tolerance_label,
            "po_remaining": ctx.po_remaining, "previously_billed": ctx.previously_billed,
            "cumulative": ctx.cumulative, "is_partial": ctx.is_partial,
            "identity_risk": ctx.identity_risk, "identity_signals": ctx.identity_signals}

    await stage("VALIDATE", "Run validation rules", _validate)

    # 8 -- decide -------------------------------------------------------------
    def _decide():
        dec = policy.decide(state["checks"], state["ctx"])
        # An invoice a person had to unblock is not straight-through processing,
        # and should never be counted as if it were.
        touched = any(overrides.get(k) for k in ("confirmed_fields", "confirmed_po", "buyer_approved"))
        if touched and dec.outcome in (AUTO_APPROVED, APPROVED_PARTIAL):
            dec.outcome = APPROVED_AFTER_REVIEW
            dec.owner = overrides.get("actor", "Reviewer")
            dec.rationale = (f"Released by {dec.owner} after review. " + dec.rationale)
        state["decision"] = dec
        label, tone, _ = DECISION_META[dec.outcome]
        return {"pass": "ok", "warn": "warn", "fail": "fail"}[tone], label, {
            "outcome": dec.outcome, "rationale": dec.rationale, "next_step": dec.next_step,
            "owner": dec.owner, "reason_codes": dec.reason_codes,
            "checks_passed": dec.passed_count}

    await stage("DECIDE", "Apply decision policy", _decide)

    # 9 -- act ----------------------------------------------------------------
    def _act():
        acts = build_actions(state["decision"], state["ctx"])
        state["actions"] = acts
        db.execute("DELETE FROM actions WHERE run_id = ?", (run_id,))
        for a in acts:
            db.execute("INSERT INTO actions (run_id, ts, kind, target, subject, body, meta)"
                       " VALUES (?,?,?,?,?,?,?)",
                       (run_id, now_iso(), a["kind"], a["target"], a["subject"], a["body"],
                        db.j(a.get("meta", {}))))
        kinds = ", ".join(sorted({a["kind"] for a in acts}))
        return "ok", f"{len(acts)} output(s): {kinds}", {"actions": acts}

    await stage("ACT", "Produce output", _act)

    # 10 -- record ------------------------------------------------------------
    def _record():
        doc, ctx, dec = state["doc"], state["ctx"], state["decision"]
        vm, pm = state["vm"], state["pm"]
        # Report the work done, not the wall clock: the live view paces itself so a
        # person can follow along, and that pacing should never flatter the numbers.
        duration = work_ms
        wall = int((time.perf_counter() - t_run) * 1000)
        crit_conf = [doc.conf(f) for f in ("invoice_number", "invoice_date", "total_amount")
                     if doc.get(f).present()]
        payload = {
            "extraction": doc.to_dict(),
            "vendor": {"vendor_id": vm.vendor.vendor_id if vm.vendor else None,
                       "legal_name": vm.vendor.legal_name if vm.vendor else None,
                       "matched_on": vm.matched_on, "score": vm.score,
                       "name_exact": vm.name_exact, "notes": vm.notes,
                       "bank_on_file": vm.vendor.bank_last4 if vm.vendor else None},
            "po": ({"po_number": pm.po.po_number, "method": pm.method,
                    "po_amount": pm.po.po_amount, "description": pm.po.description,
                    "buyer_name": pm.po.buyer_name, "buyer_email": pm.po.buyer_email,
                    "cost_center": pm.po.cost_center, "allow_partial": pm.po.allow_partial,
                    "status": pm.po.status, "notes": pm.notes} if pm.po else
                   {"po_number": None, "method": pm.method, "notes": pm.notes}),
            "money": {"basis": ctx.basis, "basis_label": ctx.basis_label, "gross": ctx.gross,
                      "tolerance": ctx.tolerance_value, "tolerance_label": ctx.tolerance_label,
                      "po_remaining": ctx.po_remaining, "previously_billed": ctx.previously_billed,
                      "cumulative": ctx.cumulative, "is_partial": ctx.is_partial},
            "identity": {"risk": ctx.identity_risk, "signals": ctx.identity_signals},
            "checks": [c.to_dict() for c in state["checks"]],
            "decision": {"outcome": dec.outcome, "rationale": dec.rationale,
                         "next_step": dec.next_step, "owner": dec.owner,
                         "reason_codes": dec.reason_codes, "passed": dec.passed_count},
            "actions": state["actions"],
            "duplicate_of": ctx.duplicate_of,
        }
        queue_status = "OPEN" if dec.outcome in ("HOLD_FOR_REVIEW", "ROUTE_TO_BUYER",
                                                 "BLOCKED_FRAUD_REVIEW") else "CLOSED"
        db.execute("""INSERT OR REPLACE INTO runs (run_id, created_at, filename, file_hash, doc_type,
                        vendor_id, vendor_name, invoice_number, invoice_number_norm, invoice_date,
                        po_number, currency, match_basis, match_basis_label, gross_amount, decision,
                        reason_codes, rationale, min_confidence, extractors, duration_ms,
                        queue_status, payload)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (run_id, now_iso(), filename, state.get("file_hash"), doc.doc_type,
                    vm.vendor.vendor_id if vm.vendor else None,
                    vm.vendor.legal_name if vm.vendor else doc.val("vendor_name"),
                    doc.val("invoice_number"), state.get("invoice_number_norm"),
                    doc.val("invoice_date"), pm.po.po_number if pm.po else None,
                    doc.val("currency"), ctx.basis, ctx.basis_label, ctx.gross, dec.outcome,
                    db.j(dec.reason_codes), dec.rationale,
                    round(min(crit_conf), 3) if crit_conf else None,
                    db.j(doc.extractors_used), duration, queue_status, db.j(payload)))

        # The ledger only records invoices that actually created a liability.
        db.execute("DELETE FROM ledger WHERE run_id = ?", (run_id,))
        if dec.outcome in APPROVING and pm.po and ctx.basis is not None:
            db.execute("""INSERT INTO ledger (run_id, created_at, vendor_id, po_number,
                            invoice_number_norm, invoice_date, amount, decision)
                          VALUES (?,?,?,?,?,?,?,?)""",
                       (run_id, now_iso(), vm.vendor.vendor_id if vm.vendor else None,
                        pm.po.po_number, state.get("invoice_number_norm"), doc.val("invoice_date"),
                        ctx.basis, dec.outcome))
            posted = "posted to the ledger"
        else:
            posted = "no payable created"
        return "ok", f"Audit trail written · {posted} · {duration} ms of processing", {
            "run_id": run_id, "processing_ms": duration, "wall_clock_ms": wall,
            "ledger": posted}

    await stage("RECORD", "Write audit trail", _record)

    dec = state["decision"]
    bus.publish({"type": "decision", "run_id": run_id, "outcome": dec.outcome,
                 "rationale": dec.rationale, "next_step": dec.next_step, "owner": dec.owner,
                 "duration_ms": work_ms})
    bus.finish()
    return {"run_id": run_id, "outcome": dec.outcome}
