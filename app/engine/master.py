"""The procurement system's view of the world, loaded from CSV.

In a real deployment these are API reads against the ERP. Keeping them as CSV
means an interviewer can open the file, change a tolerance or close a PO, and
watch the decision change on the next run.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

from ..config import MASTER


@dataclass
class Vendor:
    vendor_id: str
    legal_name: str
    aliases: list[str]
    status: str
    tax_id: str
    country: str
    currency: str
    bank_last4: str
    remit_domain: str
    payment_terms: str
    category: str

    @property
    def names(self) -> list[str]:
        return [self.legal_name, *self.aliases]


@dataclass
class PurchaseOrder:
    po_number: str
    vendor_id: str
    description: str
    currency: str
    po_amount: float
    po_date: date
    status: str
    tolerance_pct: float
    tolerance_abs: float
    allow_partial: bool
    buyer_name: str
    buyer_email: str
    cost_center: str


@dataclass
class GoodsReceipt:
    gr_id: str
    po_number: str
    received_amount: float
    received_date: date
    note: str


@lru_cache(maxsize=1)
def vendors() -> dict[str, Vendor]:
    out: dict[str, Vendor] = {}
    with open(MASTER / "vendors.csv", newline="") as f:
        for r in csv.DictReader(f):
            out[r["vendor_id"]] = Vendor(
                vendor_id=r["vendor_id"], legal_name=r["legal_name"],
                aliases=[a for a in r["aliases"].split("|") if a], status=r["status"],
                tax_id=r["tax_id"], country=r["country"], currency=r["currency"],
                bank_last4=r["bank_last4"], remit_domain=r["remit_domain"].lower(),
                payment_terms=r["payment_terms"], category=r["category"])
    return out


@lru_cache(maxsize=1)
def purchase_orders() -> dict[str, PurchaseOrder]:
    out: dict[str, PurchaseOrder] = {}
    with open(MASTER / "purchase_orders.csv", newline="") as f:
        for r in csv.DictReader(f):
            out[r["po_number"]] = PurchaseOrder(
                po_number=r["po_number"], vendor_id=r["vendor_id"], description=r["description"],
                currency=r["currency"], po_amount=float(r["po_amount"]),
                po_date=date.fromisoformat(r["po_date"]), status=r["status"],
                tolerance_pct=float(r["tolerance_pct"]), tolerance_abs=float(r["tolerance_abs"]),
                allow_partial=r["allow_partial"].strip().lower() == "true",
                buyer_name=r["buyer_name"], buyer_email=r["buyer_email"], cost_center=r["cost_center"])
    return out


@lru_cache(maxsize=1)
def goods_receipts() -> list[GoodsReceipt]:
    out: list[GoodsReceipt] = []
    with open(MASTER / "goods_receipts.csv", newline="") as f:
        for r in csv.DictReader(f):
            out.append(GoodsReceipt(gr_id=r["gr_id"], po_number=r["po_number"],
                                    received_amount=float(r["received_amount"]),
                                    received_date=date.fromisoformat(r["received_date"]),
                                    note=r["note"]))
    return out


def received_against(po_number: str) -> float:
    return round(sum(g.received_amount for g in goods_receipts() if g.po_number == po_number), 2)


def open_pos_for(vendor_id: str) -> list[PurchaseOrder]:
    return [p for p in purchase_orders().values() if p.vendor_id == vendor_id and p.status == "OPEN"]


def reload() -> None:
    """Drop the caches so an edit to the CSVs takes effect on the next run."""
    vendors.cache_clear()
    purchase_orders.cache_clear()
    goods_receipts.cache_clear()
