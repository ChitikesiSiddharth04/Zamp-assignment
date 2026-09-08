"""Turn a set of failed checks into one decision, and explain it in English.

The order below is the policy, and it is deliberately readable top to bottom:
fraud beats everything, then duplicates, then documents we cannot read, then
things that need authority we do not have, then things that need a second pair
of eyes. Only what survives all of that gets paid without a human.

Two invariants:
  * nothing is approved because a model felt good about it -- every approval is
    the absence of a failed rule;
  * every non-approval names what would unblock it, because "rejected" without
    a next step just moves the work rather than removing it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import AUTO_APPROVE_CEILING
from ..models import (APPROVED_PARTIAL, AUTO_APPROVED, BLOCKED_FRAUD_REVIEW, BLOCKER, CRITICAL,
                      HOLD_FOR_REVIEW, REJECTED_DUPLICATE, REJECTED_INCOMPLETE, ROUTE_TO_BUYER,
                      CheckResult)
from .validators import MatchContext

FRAUD_CODES = {"BANK_ON_FILE", "SUPPLIER_IDENTITY_RISK"}
DUPLICATE_CODES = {"NOT_DUPLICATE", "NEAR_DUPLICATE"}
COMPLETENESS_CODES = {"FIELDS_COMPLETE"}
# Failures that are about money or authority rather than data quality: these
# belong to the person who owns the budget, not to accounts payable.
AUTHORITY_CODES = {"PO_CUMULATIVE", "AMOUNT_TOLERANCE", "PARTIAL_BILLING", "GOODS_RECEIPT",
                   "LINE_MATH", "ABOVE_CEILING"}


@dataclass
class Decision:
    outcome: str
    reason_codes: list[str] = field(default_factory=list)
    rationale: str = ""
    next_step: str = ""
    owner: str = ""
    failed: list[CheckResult] = field(default_factory=list)
    passed_count: int = 0


def decide(checks: list[CheckResult], ctx: MatchContext) -> Decision:
    # A policy ceiling is a check like any other, so it appears in the trail.
    if ctx.basis is not None and ctx.basis > AUTO_APPROVE_CEILING:
        checks.append(CheckResult(
            code="ABOVE_CEILING", title="Within the straight-through ceiling", passed=False,
            severity=CRITICAL,
            detail=f"{ctx.basis:,.2f} is above the {AUTO_APPROVE_CEILING:,.2f} ceiling for "
                   f"straight-through processing. Every check may pass and this still needs a "
                   f"human signature — that is the control working, not a failure.",
            evidence={"amount": ctx.basis, "ceiling": AUTO_APPROVE_CEILING}))

    failed = [c for c in checks if not c.passed]
    passed_count = len(checks) - len(failed)
    codes = {c.code for c in failed}
    blockers = [c for c in failed if c.severity == BLOCKER]
    criticals = [c for c in failed if c.severity == CRITICAL]

    def build(outcome: str, drivers: list[CheckResult], next_step: str, owner: str) -> Decision:
        lead = " ".join(d.detail for d in drivers[:2])
        others = [c for c in failed if c not in drivers]
        tail = ""
        if others:
            tail = " Also flagged: " + "; ".join(f"{c.code} ({c.severity.lower()})" for c in others) + "."
        return Decision(outcome=outcome, reason_codes=[c.code for c in failed],
                        rationale=(lead + tail).strip(), next_step=next_step, owner=owner,
                        failed=failed, passed_count=passed_count)

    # 1. Supplier identity. A perfect three-way match on a redirected payment is
    #    the most expensive thing this process can let through.
    fraud = [c for c in blockers if c.code in FRAUD_CODES]
    if fraud:
        return build(BLOCKED_FRAUD_REVIEW, fraud,
                     "Payment is frozen. Financial controls verify the bank change with the vendor "
                     "through a phone number already on file — never a number printed on the invoice.",
                     "Financial controls")

    # 2. Duplicates. Cheap to detect, expensive to miss.
    dup = [c for c in failed if c.code in DUPLICATE_CODES]
    if dup:
        return build(REJECTED_DUPLICATE, dup,
                     "No payable is created. The original run is linked on this record; if the vendor "
                     "is chasing payment, AP replies from the original.",
                     "Accounts payable")

    # 3. Documents we could not read well enough to act on.
    inc = [c for c in blockers if c.code in COMPLETENESS_CODES]
    if inc:
        return build(REJECTED_INCOMPLETE, inc,
                     "The invoice is returned to the vendor with the specific fields that are missing. "
                     "A drafted email is attached to this run for AP to send.",
                     "Vendor")

    # 4. Anything else that fails closed: unknown vendor, closed PO, wrong currency.
    if blockers:
        return build(HOLD_FOR_REVIEW, blockers,
                     "Held in the exception queue. AP resolves the blocking condition in the "
                     "procurement system, then re-runs the invoice.",
                     "Accounts payable")

    # 5. Money and authority questions go to the person who owns the budget.
    authority = [c for c in criticals if c.code in AUTHORITY_CODES]
    if authority:
        po = ctx.pm.po
        owner = f"{po.buyer_name} ({po.buyer_email})" if po else "Budget owner"
        return build(ROUTE_TO_BUYER, authority,
                     f"An approval request is drafted to {owner} setting out the variance and the "
                     f"exact amount at issue. On approval the invoice posts; on rejection it goes "
                     f"back to the vendor.", owner)

    # 6. Everything else that needs a second pair of eyes.
    if criticals:
        return build(HOLD_FOR_REVIEW, criticals,
                     "Queued for AP review with the uncertain fields highlighted against the document. "
                     "Confirming them releases the invoice through the same rules.",
                     "Accounts payable")

    # 7. Clean.
    po = ctx.pm.po
    if ctx.is_partial and po:
        after = round((ctx.po_remaining or 0) - ctx.basis, 2)
        return Decision(
            outcome=APPROVED_PARTIAL, reason_codes=[c.code for c in failed],
            rationale=(f"No blocking or critical check failed. {ctx.basis:,.2f} is billed against {po.po_number} "
                       f"({ctx.basis_label}), leaving {after:,.2f} of the {po.po_amount:,.2f} order "
                       f"open for the rest of the work." +
                       (" Warnings recorded: " + ", ".join(c.code for c in failed) + "." if failed else "")),
            next_step="Posted to the ERP against the PO. The balance stays open for the next invoice.",
            owner="Automated", failed=failed, passed_count=passed_count)

    return Decision(
        outcome=AUTO_APPROVED, reason_codes=[c.code for c in failed],
        rationale=(f"{passed_count} of {passed_count + len(failed)} checks passed, none blocking. "
                   + (f"{ctx.basis:,.2f} ({ctx.basis_label}) matches {po.po_number} exactly."
                      if po and ctx.basis is not None else "Matched cleanly.")
                   + (" Warnings recorded: " + ", ".join(c.code for c in failed) + "." if failed else "")),
        next_step="Posted to the ERP for payment on the vendor's terms. No human touched it.",
        owner="Automated", failed=failed, passed_count=passed_count)
