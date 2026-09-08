"""Read the embedded text layer of a PDF, and decide whether there is one.

A "scanned" invoice is not a different file format -- it is the same PDF with
pictures instead of characters. So the first real decision the process makes is
whether it can read the document at all.
"""
from __future__ import annotations

from pathlib import Path

import pdfplumber

from ..config import TEXT_LAYER_MIN_CHARS


def read_text_layer(pdf_path: Path) -> dict:
    pages: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
        page_count = len(pdf.pages)

    text = "\n".join(pages).strip()
    chars = len(text)
    return {
        "text": text,
        "pages": pages,
        "page_count": page_count,
        "char_count": chars,
        "has_text_layer": chars >= TEXT_LAYER_MIN_CHARS,
        # Characters per page, useful when only some pages are scanned.
        "chars_per_page": [len(p.strip()) for p in pages],
    }
