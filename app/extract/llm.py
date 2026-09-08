"""Optional second extractor: Claude reading the document directly.

The local parser is fast, free and deterministic, but it only knows the label
vocabulary someone thought of in advance. A model reads a layout it has never
seen. Running both and comparing is the point -- where they agree we raise
confidence, where they disagree we lower it and say so, and a field two
independent extractors disagree about is exactly the field a human should look
at.

Enabled automatically when ANTHROPIC_API_KEY is set; the process runs fully
without it, and the UI always states which extractors actually ran.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from ..config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL
from ..models import FieldValue, LineItem

SCHEMA = {
    "name": "record_invoice",
    "description": "Record the fields read from a vendor invoice.",
    "input_schema": {
        "type": "object",
        "properties": {
            "vendor_name": {"type": ["string", "null"]},
            "remit_domain": {"type": ["string", "null"], "description": "domain of the vendor email"},
            "invoice_number": {"type": ["string", "null"]},
            "invoice_date": {"type": ["string", "null"], "description": "ISO 8601 date"},
            "po_number": {"type": ["string", "null"], "description": "null if the document never states one"},
            "currency": {"type": ["string", "null"]},
            "subtotal": {"type": ["number", "null"], "description": "net of separately stated tax"},
            "tax_amount": {"type": ["number", "null"]},
            "total_amount": {"type": ["number", "null"], "description": "the gross amount payable"},
            "bank_last4": {"type": ["string", "null"], "description": "last 4 digits of the remittance account"},
            "tax_inclusive": {"type": "boolean"},
            "line_items": {
                "type": "array",
                "items": {"type": "object", "properties": {
                    "description": {"type": "string"},
                    "quantity": {"type": ["number", "null"]},
                    "unit_price": {"type": ["number", "null"]},
                    "amount": {"type": ["number", "null"]}}},
            },
            "notes": {"type": "string", "description": "anything odd about the document"},
        },
        "required": ["invoice_number", "total_amount", "line_items"],
    },
}

PROMPT = (
    "You are reading a vendor invoice for an accounts-payable system. Record exactly what the "
    "document says. Do not compute, correct or infer values that are not printed: if a field is "
    "absent, return null for it -- a missing purchase-order number is itself important information. "
    "If tax is stated separately, subtotal must be the amount before tax."
)


def available() -> bool:
    return bool(ANTHROPIC_API_KEY)


def _client():
    import anthropic
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def extract(pdf_path: Path, text: str, *, is_scan: bool) -> tuple[dict[str, FieldValue], list[LineItem], dict]:
    """Returns (fields, line_items, flags). Raises on transport errors."""
    content: list[dict] = []
    if is_scan or not text.strip():
        import pymupdf
        doc = pymupdf.open(str(pdf_path))
        try:
            for page in doc[:3]:
                png = page.get_pixmap(dpi=150).tobytes("png")
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": "image/png",
                    "data": base64.standard_b64encode(png).decode()}})
        finally:
            doc.close()
        content.append({"type": "text", "text": "Read this scanned invoice."})
    else:
        content.append({"type": "text", "text": f"Invoice text:\n\n{text[:12000]}"})

    resp = _client().messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=2000,
        system=PROMPT,
        tools=[SCHEMA],
        tool_choice={"type": "tool", "name": "record_invoice"},
        messages=[{"role": "user", "content": content}],
    )
    payload = next((b.input for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
    if payload is None:
        raise RuntimeError("model did not return the record_invoice tool call")

    base_conf = 0.88 if not is_scan else 0.80
    fields: dict[str, FieldValue] = {}
    for key in ("vendor_name", "remit_domain", "invoice_number", "invoice_date", "po_number",
                "currency", "subtotal", "tax_amount", "total_amount", "bank_last4"):
        v = payload.get(key)
        if v in (None, ""):
            continue
        if key in ("invoice_number", "po_number"):
            v = str(v).upper()
        fields[key] = FieldValue(name=key, value=v, confidence=base_conf, source="llm",
                                 provenance=f"{ANTHROPIC_MODEL} structured read")

    items = [LineItem(description=str(li.get("description", ""))[:90],
                      quantity=li.get("quantity"), unit_price=li.get("unit_price"),
                      amount=li.get("amount"))
             for li in payload.get("line_items", []) or []]
    flags = {"tax_inclusive": bool(payload.get("tax_inclusive")), "llm_notes": payload.get("notes", "")}
    return fields, items, flags


def raw_json(payload) -> str:
    return json.dumps(payload, indent=2, default=str)
