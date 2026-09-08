"""SQLite persistence.

Three things are worth storing beyond the run itself:
  * the ledger -- every invoice that created a liability, which is what makes
    duplicate detection and cumulative PO billing possible across runs;
  * the events -- the stage-by-stage trace, so a run can be replayed months
    later without re-running it;
  * the overrides -- what a human did, by name, and why.
Together those are the audit trail. Nothing is deleted; a reversed run is
marked void so the history stays intact.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    filename TEXT,
    file_hash TEXT,
    doc_type TEXT,
    vendor_id TEXT,
    vendor_name TEXT,
    invoice_number TEXT,
    invoice_number_norm TEXT,
    invoice_date TEXT,
    po_number TEXT,
    currency TEXT,
    match_basis REAL,
    match_basis_label TEXT,
    gross_amount REAL,
    decision TEXT,
    reason_codes TEXT,
    rationale TEXT,
    min_confidence REAL,
    extractors TEXT,
    duration_ms INTEGER,
    queue_status TEXT DEFAULT 'CLOSED',
    payload TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    seq INTEGER,
    ts TEXT,
    stage TEXT,
    status TEXT,
    message TEXT,
    duration_ms INTEGER,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    created_at TEXT,
    vendor_id TEXT,
    po_number TEXT,
    invoice_number_norm TEXT,
    invoice_date TEXT,
    amount REAL,
    decision TEXT,
    void INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts TEXT,
    kind TEXT,
    target TEXT,
    subject TEXT,
    body TEXT,
    meta TEXT
);
CREATE TABLE IF NOT EXISTS overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts TEXT,
    actor TEXT,
    action TEXT,
    note TEXT,
    from_decision TEXT,
    to_decision TEXT,
    applied TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
CREATE INDEX IF NOT EXISTS idx_ledger_po ON ledger(po_number);
CREATE INDEX IF NOT EXISTS idx_ledger_vendor ON ledger(vendor_id);
"""


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init() -> None:
    with conn() as c:
        c.executescript(SCHEMA)
        # Older demo databases predate the `applied` column.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(overrides)")}
        if "applied" not in cols:
            c.execute("ALTER TABLE overrides ADD COLUMN applied TEXT")


def accumulated_overrides(run_id: str) -> dict:
    """Every human confirmation ever recorded against this run.

    Re-running re-extracts the document from scratch, so confidences reset. A
    person's confirmation is a fact about the run, not about one execution of
    it, and has to outlive the run that produced it.
    """
    acc: dict = {"confirmed_fields": []}
    for r in rows("SELECT applied FROM overrides WHERE run_id = ? ORDER BY id", (run_id,)):
        a = json.loads(r["applied"] or "{}")
        acc["confirmed_fields"] = sorted(set(acc["confirmed_fields"]) | set(a.get("confirmed_fields", [])))
        if a.get("confirmed_po"):
            acc["confirmed_po"] = a["confirmed_po"]
        if a.get("buyer_approved"):
            acc["buyer_approved"] = True
    return acc


def j(x: Any) -> str:
    return json.dumps(x, default=str)


def rows(sql: str, args: tuple = ()) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def one(sql: str, args: tuple = ()) -> dict | None:
    r = rows(sql, args)
    return r[0] if r else None


def execute(sql: str, args: tuple = ()) -> None:
    with conn() as c:
        c.execute(sql, args)


def prior_invoices(vendor_id: str) -> list[dict]:
    """Everything this vendor has already had accepted into the ledger."""
    return rows("SELECT * FROM ledger WHERE vendor_id = ? AND void = 0 ORDER BY created_at DESC",
                (vendor_id,))


def billed_against_po(po_number: str, exclude_run: str | None = None) -> float:
    sql = "SELECT COALESCE(SUM(amount), 0) AS s FROM ledger WHERE po_number = ? AND void = 0"
    args: tuple = (po_number,)
    if exclude_run:
        sql += " AND run_id != ?"
        args = (po_number, exclude_run)
    r = one(sql, args)
    return float(r["s"]) if r else 0.0


def po_ledger_lines(po_number: str) -> list[dict]:
    return rows("SELECT * FROM ledger WHERE po_number = ? AND void = 0 ORDER BY created_at", (po_number,))
