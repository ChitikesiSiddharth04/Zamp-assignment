"""Decide how to read a document, then read it -- with two extractors if we can.

Order of operations:
  1. Is there a text layer? If not, this is a scan and we OCR it.
  2. Parse with the local layout parser (always).
  3. If Claude is configured, parse again independently and reconcile.

Reconciliation is deliberately blunt and readable: agreement raises confidence,
disagreement halves it and is recorded by name. Nothing is silently merged.
"""
from __future__ import annotations

from pathlib import Path

from ..models import ExtractedInvoice, FieldValue
from . import heuristic, llm
from .ocr import ocr_pdf
from .text_layer import read_text_layer

MONEY_FIELDS = {"subtotal", "tax_amount", "total_amount"}


def _same(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 0.01
    sa, sb = str(a).strip().upper(), str(b).strip().upper()
    return sa.replace(" ", "").replace("-", "") == sb.replace(" ", "").replace("-", "")


def extract_document(pdf_path: Path, *, emit=lambda *_a, **_k: None) -> ExtractedInvoice:
    doc = ExtractedInvoice()

    tl = read_text_layer(pdf_path)
    doc.page_count = tl["page_count"]
    doc.doc_type = "digital" if tl["has_text_layer"] else "scanned"
    emit("text_layer", {"has_text_layer": tl["has_text_layer"], "char_count": tl["char_count"],
                        "pages": tl["page_count"], "chars_per_page": tl["chars_per_page"]})

    conf_scale = 1.0
    if tl["has_text_layer"]:
        text = tl["text"]
        source = "text_layer"
    else:
        res = ocr_pdf(pdf_path)
        text = res["text"]
        source = "ocr"
        doc.ocr_mean_confidence = res["mean_confidence"]
        # A page OCR'd at 96% mean word confidence is not as good as a native
        # text layer, and the fields should say so.
        conf_scale = max(0.55, min(1.0, res["mean_confidence"] * 0.95))
        emit("ocr", {"engine": "tesseract", "dpi": 300, "words": res["word_count"],
                     "mean_confidence": res["mean_confidence"], "confidence_scale": round(conf_scale, 3)})

    doc.raw_text = text

    h_fields, h_items, h_flags = heuristic.parse(text, source=source, conf_scale=conf_scale)
    doc.extractors_used.append(f"layout-parser ({source})")
    emit("parse_local", {"fields_found": len(h_fields), "line_items": len(h_items), **h_flags})

    fields = dict(h_fields)
    items = h_items
    tax_inclusive = bool(h_flags.get("tax_inclusive"))

    if llm.available():
        try:
            l_fields, l_items, l_flags = llm.extract(pdf_path, text, is_scan=(source == "ocr"))
            doc.extractors_used.append("claude (independent read)")
            agree = disagree = 0
            for name, lf in l_fields.items():
                hf = fields.get(name)
                if hf is None or not hf.present():
                    fields[name] = lf
                    continue
                if _same(hf.value, lf.value):
                    hf.confidence = round(min(0.99, max(hf.confidence, lf.confidence) + 0.05), 3)
                    hf.source = "consensus"
                    agree += 1
                else:
                    keep = hf if hf.confidence >= lf.confidence else lf
                    keep.confidence = round(min(hf.confidence, lf.confidence) * 0.5, 3)
                    keep.notes = (keep.notes + " | " if keep.notes else "") + \
                        f"extractors disagree: layout-parser read {hf.value!r}, model read {lf.value!r}"
                    fields[name] = keep
                    doc.disagreements.append(name)
                    disagree += 1
            if len(l_items) > len(items):
                items = l_items
            tax_inclusive = tax_inclusive or bool(l_flags.get("tax_inclusive"))
            emit("parse_llm", {"agreed": agree, "disagreed": disagree,
                               "disagreements": doc.disagreements, "notes": l_flags.get("llm_notes", "")})
        except Exception as exc:  # the process must survive a flaky API
            emit("parse_llm", {"skipped": True, "error": f"{type(exc).__name__}: {exc}"})

    fields["tax_inclusive"] = FieldValue(name="tax_inclusive", value=tax_inclusive, confidence=0.9,
                                         source="derived",
                                         provenance="document states charges are tax-inclusive"
                                         if tax_inclusive else "tax stated separately or not stated")
    doc.fields = fields
    doc.line_items = items
    return doc
