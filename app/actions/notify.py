"""What the process does after it decides.

A decision that stops at "rejected" has not saved anyone any work -- it has
just moved it. So every non-approval produces the actual message that has to
go out, addressed and written, ready for a person to send. Approvals produce
the ERP posting instead.

Nothing is actually emailed here: the drafts are written to the run's action
log where the demo can show them. Swapping the sink for a real SMTP or ERP
client is a one-function change, and deliberately not done in a take-home.
"""
from __future__ import annotations

from ..config import AP_INBOX, COMPANY_NAME, CONTROLS_INBOX
from ..models import (APPROVING, BLOCKED_FRAUD_REVIEW, REJECTED_DUPLICATE,
                      REJECTED_INCOMPLETE, ROUTE_TO_BUYER)
from ..engine.policy import Decision
from ..engine.validators import REQUIRED_FIELDS, MatchContext


def _money(x) -> str:
    return f"{float(x):,.2f}" if x is not None else "—"


def build_actions(decision: Decision, ctx: MatchContext) -> list[dict]:
    doc, po, vendor = ctx.doc, ctx.pm.po, ctx.vm.vendor
    inv = doc.val("invoice_number") or "(no reference)"
    vname = (vendor.legal_name if vendor else doc.val("vendor_name")) or "the vendor"
    out: list[dict] = []

    if decision.outcome in APPROVING:
        out.append({
            "kind": "erp_posting", "target": "ERP · accounts payable",
            "subject": f"Post {inv} — {_money(ctx.basis)} {doc.val('currency')}",
            "body": "\n".join([
                f"Vendor          {vname} ({vendor.vendor_id if vendor else '—'})",
                f"Invoice         {inv} dated {doc.val('invoice_date')}",
                f"Purchase order  {po.po_number if po else '—'}  ·  cost centre {po.cost_center if po else '—'}",
                f"Basis           {_money(ctx.basis)} ({ctx.basis_label})",
                f"Gross payable   {_money(ctx.gross)}",
                f"Terms           {vendor.payment_terms if vendor else '—'}",
                f"PO balance after posting  {_money((ctx.po_remaining or 0) - (ctx.basis or 0))}",
            ]),
            "meta": {"status": "posted"}})
        return out

    if decision.outcome == ROUTE_TO_BUYER and po:
        ev = next((c.evidence for c in decision.failed if c.code == "PO_CUMULATIVE"), {})
        over = ev.get("over_by")
        out.append({
            "kind": "email", "target": po.buyer_email,
            "subject": f"Approval needed — {vname} {inv} on {po.po_number}"
                       + (f" (over by {_money(over)})" if over else ""),
            "body": "\n".join([
                f"Hi {po.buyer_name.split()[0]},",
                "",
                f"{vname} has invoiced {_money(ctx.basis)} against {po.po_number} "
                f"({po.description}).",
                "",
                f"  Order value           {_money(po.po_amount)}",
                f"  Already invoiced      {_money(ctx.previously_billed)}",
                f"  This invoice          {_money(ctx.basis)}",
                f"  Cumulative            {_money(ctx.cumulative)}",
                f"  Over the order by     {_money(over)}" if over else "",
                "",
                "AP cannot clear the overage on its own. Reply approve and it posts today; reply "
                "reject and we go back to the vendor with your reason.",
                "",
                f"Invoice reference {inv}, dated {doc.val('invoice_date')}.",
                "",
                f"— {COMPANY_NAME} accounts payable (automated)",
            ]),
            "meta": {"po": po.po_number, "over_by": over}})
        return out

    if decision.outcome == REJECTED_INCOMPLETE:
        missing = next((c.evidence.get("missing", []) for c in decision.failed
                        if c.code == "FIELDS_COMPLETE"), [])
        extra = [c.detail for c in decision.failed if c.code == "LINE_MATH"]
        out.append({
            "kind": "email", "target": f"accounts@{doc.val('remit_domain') or 'vendor.example'}",
            "subject": f"Cannot process your invoice — information missing",
            "body": "\n".join([
                "Hello,",
                "",
                f"We received an invoice from {vname} but cannot process it as sent. "
                "Please reissue with the following:",
                "",
                *[f"  · {m.capitalize()} — required on every invoice we pay" for m in missing],
                *[f"  · {e}" for e in extra],
                "  · A valid purchase order number. We can only pay against an order raised in "
                "advance; your buyer contact can confirm the reference.",
                "",
                "Once we have a corrected invoice it goes back into the queue automatically and is "
                "usually cleared the same day.",
                "",
                f"— {COMPANY_NAME} accounts payable · {AP_INBOX}",
            ]),
            "meta": {"missing": missing}})
        return out

    if decision.outcome == BLOCKED_FRAUD_REVIEW:
        bank = next((c.evidence for c in decision.failed if c.code == "BANK_ON_FILE"), {})
        risk = next((c.evidence for c in decision.failed if c.code == "SUPPLIER_IDENTITY_RISK"), {})
        out.append({
            "kind": "escalation", "target": CONTROLS_INBOX,
            "subject": f"HOLD — remit-to change on {vname} invoice {inv} ({_money(ctx.gross)})",
            "body": "\n".join([
                "This invoice matched its purchase order on every commercial term and would have "
                "cleared on the numbers alone. It has been stopped on supplier identity.",
                "",
                f"  Remit-to on invoice   account ending {bank.get('stated', '—')}",
                f"  Remit-to on file      account ending {bank.get('on_file', '—')}",
                f"  Name printed          {doc.val('vendor_name') or '—'}",
                f"  Name on file          {vname}",
                f"  Identity risk score   {risk.get('score', '—')}/10 (threshold {risk.get('threshold', 4)})",
                "",
                *[f"  · {s}" for s in risk.get("signals", [])],
                "",
                "Verify any bank-detail change by calling the vendor on a number already held in the "
                "vendor master. Do not use contact details printed on this invoice.",
                "",
                f"Vendor record: {vendor.vendor_id if vendor else '—'} · {vname}",
            ]),
            "meta": risk})
        # A changed bank account is rarely a one-off; freeze the vendor too.
        out.append({
            "kind": "system", "target": "vendor master",
            "subject": f"Suggested: freeze payments to {vendor.vendor_id if vendor else vname} pending verification",
            "body": "All open invoices for this vendor should be held until the remittance change is "
                    "verified out of band, not just this one.",
            "meta": {"suggested": True}})
        return out

    if decision.outcome == REJECTED_DUPLICATE:
        prior = ctx.duplicate_of or {}
        out.append({
            "kind": "note", "target": AP_INBOX,
            "subject": f"Duplicate suppressed — {inv} from {vname}",
            "body": "\n".join([
                f"No payable created. This document repeats run {prior.get('run_id', '—')} "
                f"({prior.get('invoice_number_norm', '—')}, {_money(prior.get('amount'))}, "
                f"{prior.get('invoice_date', '—')}).",
                "",
                "If the vendor is chasing payment, respond against the original run rather than "
                "reprocessing this copy.",
            ]),
            "meta": prior})
        return out

    # HOLD_FOR_REVIEW
    weak = next((c.evidence.get("weak_fields", {}) for c in decision.failed
                 if c.code == "READ_CONFIDENCE"), {})
    out.append({
        "kind": "task", "target": "AP exception queue",
        "subject": f"Confirm {inv} from {vname} — {_money(ctx.gross)}",
        "body": "\n".join([
            decision.rationale,
            "",
            "To clear: confirm the highlighted fields against the document image, then release. "
            "The same rules run again on release — confirming a field does not skip the checks.",
            *([""] + [f"  · {k} read at {v:.0%} confidence" for k, v in weak.items()] if weak else []),
        ]),
        "meta": {"weak_fields": weak}})
    return out
