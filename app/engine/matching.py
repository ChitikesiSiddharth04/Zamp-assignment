"""Resolve the invoice against the vendor master and the open POs.

Two things worth saying about the design:

* Vendor identity is established from more than the name. A name is easy to
  imitate; the remittance domain and the bank account are the details a
  fraudulent invoice has to get wrong in order to be worth sending. So we
  resolve on name *and* domain and treat any disagreement between them as
  signal rather than noise.

* A PO reference that is not printed on the document can often be worked out,
  but "worked out" is not the same as "read". An inferred match is returned
  with its method recorded so policy can refuse to auto-approve on one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from rapidfuzz import fuzz

from ..config import VENDOR_MATCH_ACCEPT, VENDOR_MATCH_SUSPECT
from . import master
from .master import PurchaseOrder, Vendor


def normalise_invoice_number(raw: str | None) -> str:
    """INV-2041, 'INV 2041' and 'inv2041' are the same invoice to a vendor."""
    if not raw:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(raw).upper())


def _clean_name(n: str) -> str:
    n = re.sub(r"(?i)\b(inc|llc|ltd|limited|co|corp|corporation|gmbh|pvt|private|plc)\b\.?", " ", n)
    return re.sub(r"[^a-z0-9 ]", " ", n.lower()).strip()


@dataclass
class VendorMatch:
    vendor: Vendor | None = None
    score: float = 0.0
    matched_on: str = "none"          # domain | name | domain+name | none
    name_best: str = ""
    domain_vendor: Vendor | None = None
    name_vendor: Vendor | None = None
    identity_conflict: bool = False   # name says one vendor, domain says another
    name_exact: bool = False          # printed name matches a master name character for character
    notes: list[str] = field(default_factory=list)


def resolve_vendor(vendor_name: str | None, remit_domain: str | None) -> VendorMatch:
    m = VendorMatch()
    vs = master.vendors().values()

    if remit_domain:
        dom = remit_domain.lower().strip()
        m.domain_vendor = next((v for v in vs if v.remit_domain == dom), None)
        if not m.domain_vendor:
            m.notes.append(f"remittance domain '{dom}' is not on any vendor record")

    best, best_score = None, 0.0
    if vendor_name:
        target = _clean_name(vendor_name)
        for v in vs:
            for candidate in v.names:
                s = max(fuzz.token_set_ratio(target, _clean_name(candidate)),
                        fuzz.ratio(target, _clean_name(candidate)))
                if s > best_score:
                    best, best_score = v, s
    m.name_vendor, m.score = best, round(best_score, 1)
    m.name_best = best.legal_name if best else ""
    if best and vendor_name:
        m.name_exact = _clean_name(vendor_name) in {_clean_name(c) for c in best.names}
        if not m.name_exact:
            m.notes.append(
                f"printed name '{vendor_name.strip()}' is close to but not identical to "
                f"'{best.legal_name}' (similarity {best_score:.0f}%)")

    if m.domain_vendor and m.name_vendor and m.domain_vendor.vendor_id != m.name_vendor.vendor_id:
        m.identity_conflict = True
        m.notes.append("the name on the letterhead and the remittance domain point at different vendors")

    if m.domain_vendor:
        m.vendor, m.matched_on = m.domain_vendor, "domain+name" if m.name_vendor is m.domain_vendor else "domain"
    elif best_score >= VENDOR_MATCH_SUSPECT:
        m.vendor, m.matched_on = best, "name"
    return m


@dataclass
class POMatch:
    po: PurchaseOrder | None = None
    method: str = "none"              # explicit | inferred | none
    stated_ref: str | None = None
    candidates: list[str] = field(default_factory=list)
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)


def resolve_po(stated_ref: str | None, vendor: Vendor | None, amount: float | None,
               invoice_date: date | None) -> POMatch:
    m = POMatch(stated_ref=stated_ref)
    pos = master.purchase_orders()

    if stated_ref:
        key = stated_ref.upper().replace(" ", "-")
        po = pos.get(key)
        if po is None:  # tolerate PO10231 / PO-10231 / po 10231
            digits = re.sub(r"\D", "", key)
            po = next((p for p in pos.values() if re.sub(r"\D", "", p.po_number) == digits), None)
            if po:
                m.notes.append(f"'{stated_ref}' normalised to {po.po_number}")
        if po:
            m.po, m.method, m.confidence = po, "explicit", 0.97
            return m
        m.notes.append(f"purchase order '{stated_ref}' is printed on the invoice but does not exist")
        return m

    if not vendor:
        m.notes.append("no PO stated and no vendor to search open orders against")
        return m

    open_pos = master.open_pos_for(vendor.vendor_id)
    m.candidates = [p.po_number for p in open_pos]
    if not open_pos:
        m.notes.append(f"no PO stated and {vendor.legal_name} has no open purchase orders")
        return m

    fits = []
    for p in open_pos:
        if amount is None:
            fits.append(p)
            continue
        headroom = p.po_amount * (1 + p.tolerance_pct / 100) + p.tolerance_abs
        dated_ok = invoice_date is None or invoice_date >= p.po_date
        if amount <= headroom and dated_ok:
            fits.append(p)

    if len(fits) == 1:
        m.po, m.method, m.confidence = fits[0], "inferred", 0.62
        m.notes.append(
            f"no PO printed on the document; {fits[0].po_number} is the only open order for "
            f"{vendor.legal_name} that this amount fits inside")
    elif len(fits) > 1:
        m.method = "ambiguous"
        m.notes.append(f"{len(fits)} open orders could fit this invoice: {', '.join(p.po_number for p in fits)}")
    else:
        m.notes.append(f"no open order for {vendor.legal_name} can accommodate this amount")
    return m
