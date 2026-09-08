"""The rules.

Every check is a small, self-contained function that returns a CheckResult with
a stable machine code, a severity, and the evidence behind it. None of them make
a decision; policy.py does that. Keeping the two apart is what lets us say "the
process rejected this because DUP_EXACT fired, and here is the earlier run" --
rather than "the model thought it looked like a duplicate".

No LLM is involved from here on. Extraction is probabilistic; approving a
payment is not.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .. import db
from ..config import (DUPLICATE_AMOUNT_EPSILON, DUPLICATE_DATE_WINDOW_DAYS, LINE_MATH_EPSILON,
                      MIN_FIELD_CONFIDENCE, OCR_TOLERANCE_MULTIPLIER, STALE_INVOICE_DAYS,
                      VENDOR_MATCH_ACCEPT)
from ..models import BLOCKER, CRITICAL, INFO, WARNING, CheckResult, ExtractedInvoice
from . import master
from .matching import POMatch, VendorMatch, normalise_invoice_number

REQUIRED_FIELDS = {
    "invoice_number": "invoice number",
    "invoice_date": "invoice date",
    "vendor_name": "vendor name",
    "total_amount": "invoice total",
}
CONFIDENCE_CRITICAL_FIELDS = ("invoice_number", "invoice_date", "total_amount", "po_number")


@dataclass
class MatchContext:
    """Everything the rules and the decision need, computed once."""
    doc: ExtractedInvoice
    vm: VendorMatch
    pm: POMatch
    basis: float | None = None
    basis_label: str = ""
    gross: float | None = None
    tolerance_value: float = 0.0
    tolerance_label: str = ""
    po_remaining: float | None = None
    previously_billed: float = 0.0
    cumulative: float | None = None
    is_partial: bool = False
    identity_risk: int = 0
    identity_signals: list[str] = field(default_factory=list)
    duplicate_of: dict | None = None


def compute_basis(doc: ExtractedInvoice) -> tuple[float | None, float | None, str]:
    """Pick the number that should be compared to the purchase order.

    A PO is raised net of tax, so when an invoice states tax separately we
    compare against the subtotal. When tax is baked into the line rates there is
    no net figure to compare, so we fall back to the gross and flag that the
    comparison is being made on a different basis -- which is a judgement the AP
    team should see, not one to hide.
    """
    subtotal = doc.val("subtotal")
    tax = doc.val("tax_amount")
    gross = doc.val("total_amount")
    if subtotal is not None and tax is not None and float(tax) > 0:
        return float(subtotal), (float(gross) if gross is not None else None), \
            "net of separately stated tax"
    if gross is None:
        return None, None, "no comparable amount found"
    return float(gross), float(gross), (
        "gross — tax is embedded in the line rates" if doc.val("tax_inclusive")
        else "gross — no tax stated separately")


def build_context(doc: ExtractedInvoice, vm: VendorMatch, pm: POMatch, run_id: str) -> MatchContext:
    ctx = MatchContext(doc=doc, vm=vm, pm=pm)
    ctx.basis, ctx.gross, ctx.basis_label = compute_basis(doc)

    if pm.po:
        ctx.previously_billed = db.billed_against_po(pm.po.po_number, exclude_run=run_id)
        ctx.po_remaining = round(pm.po.po_amount - ctx.previously_billed, 2)
        if ctx.basis is not None:
            ctx.cumulative = round(ctx.previously_billed + ctx.basis, 2)
            ctx.is_partial = ctx.basis < ctx.po_remaining - 0.01

        pct = pm.po.po_amount * pm.po.tolerance_pct / 100
        tol = min(pct, pm.po.tolerance_abs)
        label = (f"lesser of {pm.po.tolerance_pct:g}% ({pct:,.2f}) and "
                 f"{pm.po.tolerance_abs:,.2f} absolute")
        if doc.doc_type == "scanned":
            tol *= OCR_TOLERANCE_MULTIPLIER
            label += f", halved to {tol:,.2f} because the document was OCR'd"
        ctx.tolerance_value, ctx.tolerance_label = round(tol, 2), label

    # Supplier identity risk: a composite, because no single one of these is
    # proof and any two of them together are worth stopping a payment for.
    risk, signals = 0, []
    if doc.val("remit_domain") and not vm.domain_vendor:
        risk += 2
        signals.append("remittance domain is not on the vendor record (+2)")
    stated_bank = doc.val("bank_last4")
    if vm.vendor and stated_bank and stated_bank != vm.vendor.bank_last4:
        risk += 3
        signals.append(f"remit-to account ends {stated_bank}, vendor record says "
                       f"{vm.vendor.bank_last4} (+3)")
    if vm.vendor and not vm.name_exact:
        risk += 1
        signals.append("printed legal name is not an exact match to the vendor record (+1)")
    if vm.matched_on == "name":
        risk += 1
        signals.append("vendor identified by name alone, with no corroborating domain (+1)")
    if vm.identity_conflict:
        risk += 3
        signals.append("name and domain resolve to different vendors (+3)")
    ctx.identity_risk, ctx.identity_signals = risk, signals
    return ctx


# --- individual rules ------------------------------------------------------

def check_completeness(ctx: MatchContext) -> list[CheckResult]:
    out = []
    missing = [label for f, label in REQUIRED_FIELDS.items() if not ctx.doc.get(f).present()]
    out.append(CheckResult(
        code="FIELDS_COMPLETE", title="Required fields present", passed=not missing,
        severity=BLOCKER,
        detail="All required fields were read from the document." if not missing else
               "The document is missing: " + ", ".join(missing) + ".",
        evidence={"missing": missing,
                  "present": {f: ctx.doc.val(f) for f in REQUIRED_FIELDS if ctx.doc.get(f).present()}}))

    weak = {f: round(ctx.doc.conf(f), 3) for f in CONFIDENCE_CRITICAL_FIELDS
            if ctx.doc.get(f).present() and ctx.doc.conf(f) < MIN_FIELD_CONFIDENCE}
    out.append(CheckResult(
        code="READ_CONFIDENCE", title="Read confidence on critical fields", passed=not weak,
        severity=CRITICAL,
        detail=(f"Every critical field was read with at least {MIN_FIELD_CONFIDENCE:.0%} confidence."
                if not weak else
                "Read below the confidence floor: " +
                ", ".join(f"{k} {v:.0%}" for k, v in weak.items()) +
                f" (floor {MIN_FIELD_CONFIDENCE:.0%}). A human should confirm these against the document."),
        evidence={"weak_fields": weak, "floor": MIN_FIELD_CONFIDENCE,
                  "document_type": ctx.doc.doc_type,
                  "ocr_mean_confidence": ctx.doc.ocr_mean_confidence}))

    if ctx.doc.disagreements:
        out.append(CheckResult(
            code="EXTRACTOR_DISAGREEMENT", title="Extractors agree", passed=False, severity=CRITICAL,
            detail="The layout parser and the model read these fields differently: " +
                   ", ".join(ctx.doc.disagreements) + ".",
            evidence={"fields": ctx.doc.disagreements}))
    return out


def check_dates(ctx: MatchContext) -> list[CheckResult]:
    out = []
    raw = ctx.doc.val("invoice_date")
    if not raw:
        return out
    d = date.fromisoformat(raw)
    today = date.today()
    age = (today - d).days
    out.append(CheckResult(
        code="DATE_SANE", title="Invoice date is sane", passed=d <= today,
        severity=CRITICAL,
        detail=f"Invoice dated {d.isoformat()} ({age} days ago)." if d <= today else
               f"Invoice is dated {d.isoformat()}, which is in the future.",
        evidence={"invoice_date": raw, "age_days": age}))
    if age > STALE_INVOICE_DAYS:
        out.append(CheckResult(
            code="DATE_STALE", title="Invoice is within the payment window", passed=False,
            severity=WARNING,
            detail=f"The invoice is {age} days old, past the {STALE_INVOICE_DAYS}-day window. "
                   "Late-arriving invoices need a reason before payment.",
            evidence={"age_days": age}))
    note = ctx.doc.get("invoice_date").notes
    if note.startswith("ambiguous"):
        out.append(CheckResult(
            code="DATE_AMBIGUOUS", title="Date format is unambiguous", passed=False, severity=WARNING,
            detail=note, evidence={"provenance": ctx.doc.get("invoice_date").provenance}))
    return out


def check_vendor(ctx: MatchContext) -> list[CheckResult]:
    vm, out = ctx.vm, []
    out.append(CheckResult(
        code="VENDOR_KNOWN", title="Vendor is on the approved master", passed=vm.vendor is not None,
        severity=BLOCKER,
        detail=(f"Matched to {vm.vendor.legal_name} ({vm.vendor.vendor_id}) on {vm.matched_on}, "
                f"name similarity {vm.score:.0f}%." if vm.vendor else
                "No vendor on the approved master matches this invoice. Nothing can be paid to an "
                "unknown party."),
        evidence={"matched_on": vm.matched_on, "score": vm.score, "closest": vm.name_best,
                  "notes": vm.notes}))

    if vm.vendor:
        out.append(CheckResult(
            code="VENDOR_ACTIVE", title="Vendor is active", passed=vm.vendor.status == "ACTIVE",
            severity=BLOCKER,
            detail=f"Vendor status is {vm.vendor.status}." +
                   ("" if vm.vendor.status == "ACTIVE" else " Payments to this vendor are suspended."),
            evidence={"status": vm.vendor.status}))

        stated = ctx.doc.val("bank_last4")
        if stated:
            ok = stated == vm.vendor.bank_last4
            out.append(CheckResult(
                code="BANK_ON_FILE", title="Remit-to account matches the vendor record", passed=ok,
                severity=BLOCKER,
                detail=(f"Remit-to account ends {stated}, matching the account on file."
                        if ok else
                        f"The invoice asks for payment to an account ending {stated}. The vendor "
                        f"record has {vm.vendor.bank_last4}. A changed bank account on an otherwise "
                        f"correct invoice is the standard shape of an AP fraud attempt, so this "
                        f"cannot clear automatically no matter how well the amounts match."),
                evidence={"stated": stated, "on_file": vm.vendor.bank_last4,
                          "provenance": ctx.doc.get("bank_last4").provenance}))
        else:
            out.append(CheckResult(
                code="BANK_NOT_STATED", title="Remit-to account stated", passed=False, severity=WARNING,
                detail="The invoice does not print remittance details, so they cannot be verified "
                       "against the vendor record. Payment will use the account already on file.",
                evidence={"on_file": vm.vendor.bank_last4}))

    out.append(CheckResult(
        code="SUPPLIER_IDENTITY_RISK", title="Supplier identity risk score",
        passed=ctx.identity_risk < 4,
        severity=BLOCKER if ctx.identity_risk >= 4 else WARNING,
        detail=(f"Identity risk score {ctx.identity_risk}/10 — below the escalation threshold of 4."
                if ctx.identity_risk < 4 else
                f"Identity risk score {ctx.identity_risk}/10, at or above the escalation threshold "
                f"of 4. No single signal here is proof; together they are enough to stop a payment "
                f"and have a person look."),
        evidence={"score": ctx.identity_risk, "threshold": 4, "signals": ctx.identity_signals}))
    return out


def check_po(ctx: MatchContext) -> list[CheckResult]:
    pm, vm, out = ctx.pm, ctx.vm, []
    out.append(CheckResult(
        code="PO_RESOLVED", title="Purchase order resolved", passed=pm.po is not None,
        severity=CRITICAL,
        detail=(f"Matched {pm.po.po_number} ({pm.method}) — {pm.po.description}." if pm.po else
                "No purchase order could be matched. " + (" ".join(pm.notes) or "")),
        evidence={"method": pm.method, "stated": pm.stated_ref,
                  "candidates": pm.candidates, "notes": pm.notes}))
    if not pm.po:
        return out

    if pm.method == "inferred":
        out.append(CheckResult(
            code="PO_INFERRED", title="Purchase order was printed on the invoice", passed=False,
            severity=CRITICAL,
            detail=(f"{pm.po.po_number} was worked out, not read: " + " ".join(pm.notes).rstrip(".") + ". " +
                    "An inferred link is a good guess, not evidence, so it needs one human "
                    "confirmation before the money moves."),
            evidence={"confidence": pm.confidence, "candidates": pm.candidates}))

    out.append(CheckResult(
        code="PO_VENDOR_MATCH", title="PO belongs to this vendor",
        passed=bool(vm.vendor and pm.po.vendor_id == vm.vendor.vendor_id), severity=BLOCKER,
        detail=(f"{pm.po.po_number} is raised against {vm.vendor.legal_name}."
                if vm.vendor and pm.po.vendor_id == vm.vendor.vendor_id else
                f"{pm.po.po_number} belongs to a different vendor than the one that sent this invoice."),
        evidence={"po_vendor": pm.po.vendor_id, "invoice_vendor": vm.vendor.vendor_id if vm.vendor else None}))

    out.append(CheckResult(
        code="PO_OPEN", title="PO is open for billing", passed=pm.po.status == "OPEN",
        severity=BLOCKER,
        detail=f"{pm.po.po_number} status is {pm.po.status}." +
               ("" if pm.po.status == "OPEN" else " Closed orders cannot take new invoices."),
        evidence={"status": pm.po.status}))

    inv_cur = ctx.doc.val("currency")
    out.append(CheckResult(
        code="CURRENCY_MATCH", title="Currency matches the PO", passed=inv_cur == pm.po.currency,
        severity=BLOCKER,
        detail=f"Invoice and PO are both in {pm.po.currency}." if inv_cur == pm.po.currency else
               f"Invoice is in {inv_cur}; {pm.po.po_number} was raised in {pm.po.currency}.",
        evidence={"invoice": inv_cur, "po": pm.po.currency}))
    return out


def check_amounts(ctx: MatchContext) -> list[CheckResult]:
    doc, pm, out = ctx.doc, ctx.pm, []

    line_sum = round(sum(li.amount or 0 for li in doc.line_items), 2)
    stated = doc.val("subtotal")
    stated_label = "subtotal"
    if stated is None:
        stated, stated_label = doc.val("total_amount"), "stated total"
    if doc.line_items and stated is not None:
        delta = round(line_sum - float(stated), 2)
        ok = abs(delta) <= LINE_MATH_EPSILON
        out.append(CheckResult(
            code="LINE_MATH", title="Line items add up", passed=ok, severity=CRITICAL,
            detail=(f"{len(doc.line_items)} line items sum to {line_sum:,.2f}, matching the "
                    f"{stated_label}." if ok else
                    f"{len(doc.line_items)} line items sum to {line_sum:,.2f} but the {stated_label} "
                    f"reads {float(stated):,.2f} — a difference of {delta:,.2f}. Either the invoice "
                    f"is wrong or a charge was not itemised; both need the vendor to answer."),
            evidence={"line_sum": line_sum, stated_label.replace(" ", "_"): stated, "delta": delta,
                      "lines": [li.to_dict() for li in doc.line_items]}))

    if doc.val("tax_inclusive"):
        out.append(CheckResult(
            code="TAX_BASIS_GROSS", title="Comparison basis is net of tax", passed=False,
            severity=WARNING,
            detail="This vendor embeds tax in the line rates and states no net figure, so the invoice "
                   "is being compared to the PO on a gross basis. Assumption recorded rather than "
                   "silently applied.",
            evidence={"basis": ctx.basis_label, "amount": ctx.basis}))

    if not pm.po or ctx.basis is None:
        return out

    po = pm.po
    remaining = ctx.po_remaining or 0.0
    variance = round(ctx.basis - remaining, 2)

    if ctx.previously_billed > 0:
        out.append(CheckResult(
            code="PO_HISTORY", title="Prior billing against this PO", passed=True, severity=INFO,
            detail=f"{ctx.previously_billed:,.2f} has already been invoiced against {po.po_number}; "
                   f"{remaining:,.2f} of the {po.po_amount:,.2f} order remained before this invoice.",
            evidence={"previously_billed": ctx.previously_billed, "remaining_before": remaining,
                      "lines": db.po_ledger_lines(po.po_number)}))

    cumulative = ctx.cumulative or ctx.basis
    ceiling = round(po.po_amount + ctx.tolerance_value, 2)
    over = round(cumulative - ceiling, 2)
    out.append(CheckResult(
        code="PO_CUMULATIVE", title="Total billed stays inside the PO", passed=over <= 0,
        severity=CRITICAL,
        detail=(f"Cumulative billing of {cumulative:,.2f} sits inside the {po.po_amount:,.2f} order."
                if over <= 0 else
                f"This invoice takes cumulative billing on {po.po_number} to {cumulative:,.2f} "
                f"against an order of {po.po_amount:,.2f} — {over:,.2f} beyond the allowed ceiling "
                f"of {ceiling:,.2f} ({(cumulative / po.po_amount - 1) * 100:.2f}% over the order). "
                f"The overage is real work that may well be payable, but it is the budget owner's "
                f"call, not AP's."),
        evidence={"previously_billed": ctx.previously_billed, "this_invoice": ctx.basis,
                  "cumulative": cumulative, "po_amount": po.po_amount, "ceiling": ceiling,
                  "over_by": max(over, 0), "tolerance": ctx.tolerance_value,
                  "tolerance_label": ctx.tolerance_label}))

    if ctx.is_partial:
        out.append(CheckResult(
            code="PARTIAL_BILLING", title="Partial billing is allowed on this PO",
            passed=po.allow_partial, severity=CRITICAL,
            detail=(f"This invoice bills {ctx.basis:,.2f} of the {remaining:,.2f} still open on "
                    f"{po.po_number}. The order permits partial billing, so the balance stays open."
                    if po.allow_partial else
                    f"This invoice bills {ctx.basis:,.2f} against {remaining:,.2f} open on "
                    f"{po.po_number}, but the order was raised for a single full invoice."),
            evidence={"this_invoice": ctx.basis, "remaining_before": remaining,
                      "remaining_after": round(remaining - ctx.basis, 2),
                      "allow_partial": po.allow_partial}))
    else:
        within = abs(variance) <= ctx.tolerance_value
        out.append(CheckResult(
            code="AMOUNT_TOLERANCE", title="Amount matches the PO within tolerance", passed=within,
            severity=CRITICAL,
            detail=(f"{ctx.basis:,.2f} against {remaining:,.2f} open — variance {variance:,.2f}, "
                    f"inside a tolerance of {ctx.tolerance_value:,.2f} ({ctx.tolerance_label})."
                    if within else
                    f"{ctx.basis:,.2f} against {remaining:,.2f} open — variance {variance:,.2f}, "
                    f"outside the tolerance of {ctx.tolerance_value:,.2f} ({ctx.tolerance_label})."),
            evidence={"basis": ctx.basis, "basis_label": ctx.basis_label, "remaining": remaining,
                      "variance": variance, "tolerance": ctx.tolerance_value,
                      "tolerance_label": ctx.tolerance_label}))

    received = master.received_against(po.po_number)
    invoiced_after = cumulative
    out.append(CheckResult(
        code="GOODS_RECEIPT", title="Goods or services were receipted",
        passed=received > 0 and invoiced_after <= received + ctx.tolerance_value,
        severity=CRITICAL if received > 0 else WARNING,
        detail=(f"{received:,.2f} receipted against {po.po_number}; cumulative invoicing of "
                f"{invoiced_after:,.2f} is covered." if received and invoiced_after <= received + ctx.tolerance_value
                else (f"Only {received:,.2f} has been receipted against {po.po_number} but cumulative "
                      f"invoicing would reach {invoiced_after:,.2f}. Billing ahead of receipt."
                      if received else
                      f"No goods receipt exists against {po.po_number}. This is a two-way match only, "
                      f"so nobody has yet confirmed the work arrived.")),
        evidence={"received": received, "invoiced_after": invoiced_after}))
    return out


def check_duplicates(ctx: MatchContext, run_id: str) -> list[CheckResult]:
    doc, vm, out = ctx.doc, ctx.vm, []
    if not vm.vendor:
        return out
    norm = normalise_invoice_number(doc.val("invoice_number"))
    prior = [p for p in db.prior_invoices(vm.vendor.vendor_id) if p["run_id"] != run_id]

    exact = next((p for p in prior if p["invoice_number_norm"] and p["invoice_number_norm"] == norm), None)
    out.append(CheckResult(
        code="NOT_DUPLICATE", title="Invoice number not seen before", passed=exact is None,
        severity=BLOCKER,
        detail=(f"No prior invoice from {vm.vendor.legal_name} carries reference {norm or 'n/a'}."
                if exact is None else
                f"{vm.vendor.legal_name} reference {norm} was already processed on "
                f"{exact['created_at'][:10]} for {exact['amount']:,.2f} (run {exact['run_id']}). "
                f"The two documents are laid out differently and the number is punctuated "
                f"differently, which is exactly why the comparison is done on the normalised "
                f"reference rather than the raw string."),
        evidence={"normalised": norm, "prior_run": exact["run_id"] if exact else None,
                  "prior": exact}))
    if exact:
        ctx.duplicate_of = exact
        return out

    amt = ctx.basis
    inv_d = doc.val("invoice_date")
    near = None
    if amt is not None and inv_d:
        d0 = date.fromisoformat(inv_d)
        for p in prior:
            try:
                dp = date.fromisoformat(p["invoice_date"]) if p["invoice_date"] else None
            except ValueError:
                dp = None
            if dp and abs((d0 - dp).days) <= DUPLICATE_DATE_WINDOW_DAYS and \
                    abs(float(p["amount"]) - amt) <= DUPLICATE_AMOUNT_EPSILON:
                near = p
                break
    if near:
        out.append(CheckResult(
            code="NEAR_DUPLICATE", title="No near-duplicate in the window", passed=False,
            severity=CRITICAL,
            detail=f"A different invoice number from the same vendor for the same {amt:,.2f} was "
                   f"processed on {near['invoice_date']}, within "
                   f"{DUPLICATE_DATE_WINDOW_DAYS} days of this one.",
            evidence={"prior": near}))
        ctx.duplicate_of = near
    return out


def run_all(ctx: MatchContext, run_id: str) -> list[CheckResult]:
    checks: list[CheckResult] = []
    checks += check_completeness(ctx)
    checks += check_dates(ctx)
    checks += check_vendor(ctx)
    checks += check_duplicates(ctx, run_id)
    checks += check_po(ctx)
    checks += check_amounts(ctx)
    return checks
