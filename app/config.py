"""Central configuration for the Northgate AP automation service.

Everything a finance team would want to tune lives here rather than being
scattered through the pipeline. The defaults are deliberately conservative:
this is a payments system, so it fails closed.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MASTER = DATA / "master"
SAMPLES = DATA / "samples"
UPLOADS = DATA / "uploads"
DB_PATH = DATA / "ap.db"
WEB = ROOT / "web"

for _d in (DATA, MASTER, SAMPLES, UPLOADS):
    _d.mkdir(parents=True, exist_ok=True)

COMPANY_NAME = "Northgate Manufacturing Inc."
AP_INBOX = "ap@northgate.example"
CONTROLS_INBOX = "financial-controls@northgate.example"

# --- Decision policy -------------------------------------------------------
# Ceiling for straight-through processing. Anything above this gets a human
# signature even when every check passes: a policy control, not a technical one.
AUTO_APPROVE_CEILING = 75_000.00

# An invoice whose weakest critical field was read with less confidence than
# this never auto-approves; it goes to the review queue with the field flagged.
MIN_FIELD_CONFIDENCE = 0.85

# When a document had to be OCR'd we tighten the money tolerance, because the
# thing we are least sure about is exactly the digits we are comparing.
OCR_TOLERANCE_MULTIPLIER = 0.5

# Duplicate detection window for "same vendor, same amount, near date".
DUPLICATE_DATE_WINDOW_DAYS = 7
DUPLICATE_AMOUNT_EPSILON = 0.01

# Invoices older than this are stale and need a reason before payment.
STALE_INVOICE_DAYS = 180

# Fuzzy vendor-name matching thresholds (rapidfuzz token_set_ratio, 0-100).
VENDOR_MATCH_ACCEPT = 92
VENDOR_MATCH_SUSPECT = 78  # between suspect and accept => lookalike vendor

# Line-item arithmetic tolerance, in currency units.
LINE_MATH_EPSILON = 0.02

# --- Extraction ------------------------------------------------------------
# "auto" uses Claude when ANTHROPIC_API_KEY is present and falls back to the
# local layout parser otherwise. The UI always shows which extractors ran.
EXTRACTOR_MODE = os.getenv("AP_EXTRACTOR", "auto")
ANTHROPIC_MODEL = os.getenv("AP_MODEL", "claude-sonnet-5")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# A page with less than this much embedded text is treated as a scan and sent
# down the OCR path.
TEXT_LAYER_MIN_CHARS = 60
OCR_DPI = 300

# Pacing for the live run view only; has no effect on decisions.
STAGE_DELAY_MS = int(os.getenv("AP_STAGE_DELAY_MS", "220"))
