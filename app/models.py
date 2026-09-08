"""Typed records that move through the pipeline.

Plain dataclasses rather than an ORM: every stage hands the next one a value
object, and the whole thing serialises to JSON for the audit log without any
translation layer.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@dataclass
class FieldValue:
    """One extracted field, with everything needed to defend it later.

    `provenance` is the literal text the value came from, so a reviewer can see
    what the machine actually read instead of trusting a number in a box.
    """

    name: str
    value: Any = None
    confidence: float = 0.0
    source: str = "none"          # text_layer | ocr | llm | consensus | inferred
    provenance: str = ""          # raw snippet the value was read from
    page: int | None = None
    notes: str = ""

    def present(self) -> bool:
        return self.value not in (None, "", [])

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LineItem:
    description: str = ""
    quantity: float | None = None
    unit_price: float | None = None
    amount: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExtractedInvoice:
    """Structured view of one invoice document."""

    fields: dict[str, FieldValue] = field(default_factory=dict)
    line_items: list[LineItem] = field(default_factory=list)
    extractors_used: list[str] = field(default_factory=list)
    disagreements: list[str] = field(default_factory=list)
    raw_text: str = ""
    doc_type: str = "digital"     # digital | scanned
    page_count: int = 0
    ocr_mean_confidence: float | None = None

    def get(self, name: str) -> FieldValue:
        return self.fields.get(name, FieldValue(name=name))

    def val(self, name: str, default: Any = None) -> Any:
        fv = self.fields.get(name)
        return fv.value if fv and fv.present() else default

    def conf(self, name: str) -> float:
        fv = self.fields.get(name)
        return fv.confidence if fv else 0.0

    def to_dict(self) -> dict:
        return {
            "fields": {k: v.to_dict() for k, v in self.fields.items()},
            "line_items": [li.to_dict() for li in self.line_items],
            "extractors_used": self.extractors_used,
            "disagreements": self.disagreements,
            "doc_type": self.doc_type,
            "page_count": self.page_count,
            "ocr_mean_confidence": self.ocr_mean_confidence,
        }


# Severity drives the decision engine. BLOCKER stops payment outright,
# CRITICAL forces a human, WARNING is recorded and shown but does not by
# itself stop straight-through processing.
BLOCKER = "BLOCKER"
CRITICAL = "CRITICAL"
WARNING = "WARNING"
INFO = "INFO"

SEVERITY_RANK = {INFO: 0, WARNING: 1, CRITICAL: 2, BLOCKER: 3}


@dataclass
class CheckResult:
    """The outcome of one validation rule."""

    code: str                  # stable machine code, e.g. TOL_BREACH_PCT
    title: str
    passed: bool
    severity: str = INFO       # severity if it failed
    detail: str = ""
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# Terminal decisions the process can reach.
AUTO_APPROVED = "AUTO_APPROVED"
APPROVED_PARTIAL = "APPROVED_PARTIAL"
ROUTE_TO_BUYER = "ROUTE_TO_BUYER"
HOLD_FOR_REVIEW = "HOLD_FOR_REVIEW"
REJECTED_DUPLICATE = "REJECTED_DUPLICATE"
REJECTED_INCOMPLETE = "REJECTED_INCOMPLETE"
BLOCKED_FRAUD_REVIEW = "BLOCKED_FRAUD_REVIEW"
APPROVED_AFTER_REVIEW = "APPROVED_AFTER_REVIEW"

# Outcomes that create a payable and therefore land in the ledger.
APPROVING = ("AUTO_APPROVED", "APPROVED_PARTIAL", "APPROVED_AFTER_REVIEW")

DECISION_META = {
    AUTO_APPROVED: ("Auto-approved", "pass", "Posted to the ERP for payment on terms."),
    APPROVED_PARTIAL: ("Approved (partial)", "pass", "Posted against the remaining PO balance."),
    ROUTE_TO_BUYER: ("Routed to buyer", "warn", "Needs the budget owner's approval before posting."),
    HOLD_FOR_REVIEW: ("Held for AP review", "warn", "Queued for a human to confirm low-confidence reads."),
    REJECTED_DUPLICATE: ("Rejected — duplicate", "fail", "Already processed; no payment created."),
    REJECTED_INCOMPLETE: ("Rejected — incomplete", "fail", "Returned to the vendor with what is missing."),
    APPROVED_AFTER_REVIEW: ("Approved after review", "pass", "A person confirmed one input; the same rules then cleared it."),
    BLOCKED_FRAUD_REVIEW: ("Blocked — controls review", "fail", "Escalated to financial controls. Payment frozen."),
}
