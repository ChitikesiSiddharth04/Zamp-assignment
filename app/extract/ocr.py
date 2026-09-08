"""OCR path for scanned invoices.

Renders each page at 300 dpi and runs Tesseract, keeping the per-word
confidence scores. Those scores are not decoration: they flow into the field
confidence, which in turn tightens the money tolerance downstream. If we are
less sure what the number says, we should be less willing to let it through.
"""
from __future__ import annotations

import io
from pathlib import Path

from ..config import OCR_DPI


def ocr_pdf(pdf_path: Path) -> dict:
    import pymupdf
    import pytesseract
    from PIL import Image

    doc = pymupdf.open(str(pdf_path))
    pages: list[str] = []
    confidences: list[float] = []
    previews: list[str] = []

    try:
        for page in doc:
            pix = page.get_pixmap(dpi=OCR_DPI)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

            data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
            words, line_conf = [], []
            for txt, conf in zip(data["text"], data["conf"]):
                if txt and txt.strip():
                    words.append(txt)
                    try:
                        c = float(conf)
                    except (TypeError, ValueError):
                        c = -1.0
                    if c >= 0:
                        line_conf.append(c)
            confidences.extend(line_conf)
            pages.append(pytesseract.image_to_string(img))

            thumb = img.copy()
            thumb.thumbnail((900, 1300))
            buf = io.BytesIO()
            thumb.save(buf, format="JPEG", quality=70)
            previews.append(buf.getvalue().hex())
    finally:
        doc.close()

    mean_conf = (sum(confidences) / len(confidences) / 100.0) if confidences else 0.0
    return {
        "text": "\n".join(pages).strip(),
        "pages": pages,
        "page_count": len(pages),
        "mean_confidence": round(mean_conf, 4),
        "word_count": len(confidences),
        "page_previews_hex": previews,
    }


def ocr_available() -> tuple[bool, str]:
    try:
        import pytesseract
        return True, pytesseract.get_tesseract_version().__str__()
    except Exception as exc:  # pragma: no cover - environment dependent
        return False, str(exc)
