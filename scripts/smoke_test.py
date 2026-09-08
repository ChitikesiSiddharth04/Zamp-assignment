"""End-to-end check: every scenario, plus both human-in-the-loop paths.

Run this before a demo. It exercises the real pipeline against the real sample
documents -- no mocks -- and asserts the outcome of each one, so a regression
shows up here rather than in front of an interviewer.

    .venv/bin/python scripts/smoke_test.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("AP_STAGE_DELAY_MS", "0")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.config import SAMPLES  # noqa: E402
from app.pipeline import run_pipeline  # noqa: E402

EXPECTED = [
    ("01_apex_happy_path.pdf", "AUTO_APPROVED"),
    ("02_meridian_partial_1of2.pdf", "APPROVED_PARTIAL"),
    ("03_meridian_partial_2of2_overbill.pdf", "ROUTE_TO_BUYER"),
    ("04_borealis_scanned_no_po.pdf", "HOLD_FOR_REVIEW"),
    ("05_apex_bank_change_fraud.pdf", "BLOCKED_FRAUD_REVIEW"),
    ("06_apex_duplicate_resend.pdf", "REJECTED_DUPLICATE"),
    ("07_lumen_incomplete.pdf", "REJECTED_INCOMPLETE"),
]


async def main() -> int:
    db.init()
    for t in ("runs", "events", "ledger", "actions", "overrides"):
        db.execute(f"DELETE FROM {t}")

    failures, ids = [], {}
    print(f"{'document':44s} {'expected':22s} {'got':22s}")
    print("-" * 92)
    for name, expected in EXPECTED:
        res = await run_pipeline(SAMPLES / name, filename=name)
        ids[name[:2]] = res["run_id"]
        ok = res["outcome"] == expected
        print(f"{name:44s} {expected:22s} {res['outcome']:22s} {'ok' if ok else 'MISMATCH'}")
        if not ok:
            failures.append((name, expected, res["outcome"]))

    print("\nhuman-in-the-loop")
    print("-" * 92)
    checks = [
        ("04", {"confirmed_fields": ["invoice_number", "invoice_date", "total_amount"],
                "confirmed_po": "PO-10250", "actor": "S. Chitikesi (AP)"},
         "APPROVED_AFTER_REVIEW", "AP confirms the OCR reads and the inferred PO link"),
        ("03", {"buyer_approved": True, "confirmed_fields": ["invoice_date"],
                "actor": "Daniel Okonkwo (buyer)"},
         "APPROVED_AFTER_REVIEW", "the budget owner approves the overage"),
    ]
    for key, overrides, expected, label in checks:
        run_id = ids[key]
        src = Path("data/uploads") / f"{run_id}.pdf"
        res = await run_pipeline(src, filename=f"{key}-rerun.pdf", run_id=run_id, overrides=overrides)
        ok = res["outcome"] == expected
        print(f"{label:44s} {expected:22s} {res['outcome']:22s} {'ok' if ok else 'MISMATCH'}")
        if not ok:
            failures.append((label, expected, res["outcome"]))

    # The ledger must reflect exactly the invoices that created a liability.
    billed = db.billed_against_po("PO-10244")
    print(f"\nledger: PO-10244 cumulative billing = {billed:,.2f} (expected 124,400.00)")
    if abs(billed - 124400.00) > 0.01:
        failures.append(("ledger PO-10244", "124400.00", f"{billed:.2f}"))

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print("  ", f)
        return 1
    print("\nall scenarios behaved as designed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
