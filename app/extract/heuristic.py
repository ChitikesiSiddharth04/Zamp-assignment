"""Layout-aware heuristic parser.

This is the extractor that runs on every document, with or without an API key.
It works by label anchoring: find the words a human would look for ("Invoice
#", "Bill Number", "Document Ref"), then read the value that sits next to them.
Every vendor names the same concept differently, so each field owns a list of
patterns rather than one.

Each field comes back with a confidence that reflects *how* it was found -- an
explicit labelled match is worth more than a lucky regex, which is worth more
than something we inferred. Downstream policy reads those numbers, so they have
to mean something.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from ..models import FieldValue, LineItem

# --- primitives ------------------------------------------------------------

MONEY_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+\.\d{2}|\d+)(?![\d.,]*\s*%)(?![\d])")
SEP_RE = re.compile(r"^[\s:.\-–—=|]+")

CONF_LABELLED = 0.94        # value sat right next to its label
CONF_LABEL_NEXTLINE = 0.86  # label on one line, value on the next
CONF_REGEX_ONLY = 0.74      # found by shape, no label
CONF_INFERRED = 0.55        # derived, not read

LABELS: dict[str, list[str]] = {
    "invoice_number": [r"invoice\s*(?:#|no\.?|num(?:ber)?)", r"bill\s*(?:#|no\.?|num(?:ber)?)",
                       r"document\s*ref(?:erence)?", r"inv\s*no", r"our\s*ref(?:erence)?"],
    "invoice_date":   [r"invoice\s*date", r"bill\s*date", r"date\s*of\s*issue", r"\bdated\b",
                       r"\bissued\b", r"^date\b"],
    "po_number":      [r"p\.?\s?o\.?\s*(?:#|no\.?|num(?:ber)?)", r"your\s*p\.?o\.?",
                       r"customer\s*p\.?o\.?", r"purchase\s*order(?:\s*(?:#|no\.?|ref))?",
                       r"order\s*ref(?:erence)?"],
    "subtotal":       [r"sub\s*-?\s*total", r"net\s*amount", r"amount\s*before\s*tax"],
    "tax_amount":     [r"sales\s*tax", r"\btax\s*@", r"\bvat\b", r"\bgst\b", r"\btax\b"],
    "total_amount":   [r"total\s*due", r"amount\s*payable", r"balance\s*due", r"total\s*payable",
                       r"grand\s*total", r"invoice\s*total", r"amount\s*due", r"^total\b"],
}

# Lines that belong to the totals block, never to the line-item table.
TOTALS_MARKER = re.compile(
    r"(sub\s*-?\s*total|sales\s*tax|\btax\b|total\s*due|amount\s*payable|balance\s*due|"
    r"total\s*payable|grand\s*total|invoice\s*total|amount\s*due|^total\b|discount|round(?:ing)?)",
    re.I,
)
TABLE_HEADER = re.compile(
    r"(description|item|charge\s*code|particulars).{0,60}(amount|value|total|rate|price)", re.I)

DATE_FORMATS = ["%d %b %Y", "%d %B %Y", "%B %d, %Y", "%b %d, %Y", "%Y-%m-%d",
                "%d-%b-%Y", "%d %b %y", "%d.%m.%Y"]
NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\b")
DATE_TOKEN_RE = re.compile(
    r"\b(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}|"
    r"\d{4}-\d{2}-\d{2}|\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})\b")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
BANK_RE = re.compile(r"(?:account|acct|a/c)\b[^0-9\n]{0,25}(\d{4})\b", re.I)
CURRENCY_RE = re.compile(r"\b(USD|EUR|GBP|INR|CAD|AUD|SGD)\b")


def _money_tokens(s: str) -> list[float]:
    out = []
    for m in MONEY_RE.finditer(s):
        raw = m.group(1)
        if "," not in raw and "." not in raw and len(raw) > 6:
            continue  # long bare integers are routing numbers / refs, not money
        try:
            out.append(float(raw.replace(",", "")))
        except ValueError:
            pass
    return out


def _strip_sep(s: str) -> str:
    return SEP_RE.sub("", s).strip()


def parse_date(token: str, *, country_hint: str = "US") -> tuple[date | None, str, float]:
    """Return (date, note, confidence_multiplier).

    Numeric dates are genuinely ambiguous. 08/25/2026 can only be MDY, but
    02/09/2026 could be either -- so we say so, pick the reading that is not in
    the future, and discount the confidence instead of pretending we know.
    """
    token = token.strip().rstrip(".,")
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(token, fmt).date(), "", 1.0
        except ValueError:
            continue

    m = NUMERIC_DATE_RE.search(token)
    if not m:
        return None, "unrecognised date format", 0.0

    a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000

    def mk(mm: int, dd: int) -> date | None:
        try:
            return date(y, mm, dd)
        except ValueError:
            return None

    if a > 12:
        d = mk(b, a)
        return d, "read day-first; unambiguous", 1.0
    if b > 12:
        d = mk(a, b)
        return d, "read month-first; unambiguous", 1.0

    mdy, dmy = mk(a, b), mk(b, a)
    today = date.today()
    candidates = [c for c in (mdy, dmy) if c]
    if not candidates:
        return None, "invalid date", 0.0
    not_future = [c for c in candidates if c <= today]
    pick = min(not_future, key=lambda c: (today - c).days) if not_future else min(candidates)
    other = dmy if pick is mdy else mdy
    note = (f"ambiguous numeric date '{token}' — read as {pick.isoformat()}; "
            f"the other reading ({other.isoformat() if other else 'n/a'}) is "
            f"{'in the future' if other and other > today else 'further from today'}")
    return pick, note, 0.80


def _find_labelled(lines: list[str], patterns: list[str], *, consumed: set[int]
                   ) -> tuple[str | None, int | None, float, str]:
    """Find the text following any of `patterns`. Returns (tail, line_idx, conf, raw_line)."""
    for pat in patterns:
        rx = re.compile(pat, re.I)
        for i, line in enumerate(lines):
            if i in consumed:
                continue
            m = rx.search(line)
            if not m:
                continue
            tail = _strip_sep(line[m.end():])
            if tail:
                return tail, i, CONF_LABELLED, line
            for j in range(i + 1, min(i + 3, len(lines))):
                nxt = _strip_sep(lines[j])
                if nxt:
                    return nxt, j, CONF_LABEL_NEXTLINE, lines[j]
    return None, None, 0.0, ""


def _first_ref_token(tail: str) -> str | None:
    """Take the first identifier-looking token, so a merged column doesn't leak in."""
    m = re.match(r"([A-Za-z]{1,6}[\s\-]?\d[\w\-/]*|\d[\w\-/]{2,})", tail.strip())
    return m.group(1).strip() if m else None


def parse(text: str, *, source: str, conf_scale: float = 1.0,
          country_hint: str = "US") -> tuple[dict[str, FieldValue], list[LineItem], dict]:
    """Parse raw invoice text into fields, line items and document-level flags."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    fields: dict[str, FieldValue] = {}
    consumed: set[int] = set()

    def put(name: str, value, conf: float, prov: str, src: str | None = None, notes: str = ""):
        fields[name] = FieldValue(name=name, value=value, confidence=round(min(conf * conf_scale, 0.99), 3),
                                  source=src or source, provenance=prov.strip()[:180], notes=notes)

    # --- vendor identity ---------------------------------------------------
    vendor_line = ""
    for ln in lines[:4]:
        if len(ln) > 3 and not re.fullmatch(r"(?i)(tax\s*)?invoice|statement", ln):
            vendor_line = ln
            break
    if vendor_line:
        put("vendor_name", vendor_line.strip(" .,"), CONF_REGEX_ONLY + 0.10,
            vendor_line, notes="read from the letterhead")

    em = EMAIL_RE.search(text)
    if em:
        put("remit_domain", em.group(1).lower(), CONF_LABELLED, em.group(0))

    bk = BANK_RE.search(text)
    if bk:
        line = next((l for l in lines if bk.group(1) in l and re.search(r"(?i)acc|a/c", l)), bk.group(0))
        put("bank_last4", bk.group(1), CONF_LABELLED, line)

    cur = CURRENCY_RE.search(text)
    put("currency", cur.group(1) if cur else ("USD" if "$" in text else "USD"),
        CONF_LABELLED if cur else CONF_INFERRED,
        cur.group(0) if cur else "no explicit currency code; defaulted to USD",
        src=source if cur else "inferred")

    # --- references --------------------------------------------------------
    tail, idx, conf, raw = _find_labelled(lines, LABELS["invoice_number"], consumed=consumed)
    if tail:
        tok = _first_ref_token(tail)
        if tok:
            consumed.add(idx)
            put("invoice_number", tok.upper(), conf, raw)

    tail, idx, conf, raw = _find_labelled(lines, LABELS["po_number"], consumed=consumed)
    if tail:
        tok = _first_ref_token(tail)
        if tok:
            consumed.add(idx)
            put("po_number", tok.upper().replace(" ", "-"), conf, raw)

    tail, idx, conf, raw = _find_labelled(lines, LABELS["invoice_date"], consumed=consumed)
    if tail:
        dm = DATE_TOKEN_RE.search(tail)
        if dm:
            dt, note, mult = parse_date(dm.group(1), country_hint=country_hint)
            if dt:
                consumed.add(idx)
                put("invoice_date", dt.isoformat(), conf * mult, raw, notes=note)

    # --- money -------------------------------------------------------------
    flags: dict = {"tax_inclusive": bool(re.search(r"tax[- ]inclusive|inclusive of (all )?tax", text, re.I))}

    for name in ("subtotal", "tax_amount", "total_amount"):
        for pat in LABELS[name]:
            rx = re.compile(pat, re.I)
            hit = None
            for i, line in enumerate(lines):
                if i in consumed:
                    continue
                if name == "total_amount" and re.search(r"sub\s*-?\s*total", line, re.I):
                    continue
                if rx.search(line):
                    toks = _money_tokens(line[rx.search(line).end():] or line)
                    if toks:
                        hit = (i, line, toks[-1])
                        break
            if hit:
                i, line, val = hit
                consumed.add(i)
                put(name, val, CONF_LABELLED, line)
                break

    # --- line items --------------------------------------------------------
    start = 0
    for i, line in enumerate(lines):
        if TABLE_HEADER.search(line):
            start = i + 1
            break
    end = len(lines)
    for i in range(start, len(lines)):
        if TOTALS_MARKER.search(lines[i]):
            end = i
            break

    saw_header = start > 0
    items: list[LineItem] = []
    for line in lines[start:end]:
        if TOTALS_MARKER.search(line) or len(re.sub(r"[^A-Za-z]", "", line)) < 4:
            continue
        # The amount must look like money, not like a year or a reference
        # number that happens to sit at the end of a line.
        if not re.search(r"\d[\d,]*\.\d{2}\s*$", line):
            continue
        if not saw_header and any(re.search(p, line, re.I) for p in
                                  LABELS["invoice_number"] + LABELS["invoice_date"] + LABELS["po_number"]):
            continue  # a metadata line, not a charge

        matches = list(MONEY_RE.finditer(line))
        if not matches:
            continue
        # Walk left from the trailing amount while the gap between numbers has
        # no words in it -- that run is the numeric tail, the rest is the label.
        run = len(matches) - 1
        while run > 0:
            gap = line[matches[run - 1].end():matches[run].start()]
            if re.search(r"[A-Za-z]{2,}", gap):
                break
            run -= 1
        toks = [float(m.group(1).replace(",", "")) for m in matches[run:]]
        amount = toks[-1]
        qty = unit = None
        if len(toks) >= 3 and abs(toks[-3] * toks[-2] - amount) < 0.51:
            qty, unit = toks[-3], toks[-2]
        desc = re.sub(r"[.\s]{3,}", " ", line[:matches[run].start()]).strip(" .-|:")
        items.append(LineItem(description=desc[:90], quantity=qty, unit_price=unit, amount=amount))

    flags["line_item_count"] = len(items)
    return fields, items, flags
