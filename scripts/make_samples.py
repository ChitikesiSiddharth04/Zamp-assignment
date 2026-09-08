"""Generate the master data and the sample invoice PDFs.

Everything is anchored to today's date so the demo never goes stale. The four
layouts are deliberately different from each other -- different labels for the
same concept ("Invoice #" vs "Bill Number" vs "Document Ref"), different table
shapes, tax embedded vs separated -- because a process that only reads one
vendor's template has not solved the problem.

Run:  .venv/bin/python scripts/make_samples.py
"""
from __future__ import annotations

import csv
import io
import random
import sys
from datetime import date, timedelta
from pathlib import Path

from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import MASTER, SAMPLES  # noqa: E402

W, H = A4
TODAY = date.today()


def d(offset: int) -> date:
    return TODAY + timedelta(days=offset)


def money(x: float) -> str:
    return f"{x:,.2f}"


# ---------------------------------------------------------------------------
# Master data: the "procurement system" the process reads from.
# ---------------------------------------------------------------------------

VENDORS = [
    # vendor_id, legal_name, aliases, status, tax_id, country, currency,
    # bank_last4, remit_domain, payment_terms, category
    ("V-1001", "Apex Cloud Systems Inc.", "Apex Cloud|Apex Cloud Systems|Apex Cloud Systems Incorporated",
     "ACTIVE", "US-84-2210934", "US", "USD", "4471", "apexcloud.com", "NET30", "SaaS"),
    ("V-1002", "Meridian Freight Services LLC", "Meridian Freight|Meridian Freight Services",
     "ACTIVE", "US-77-5540192", "US", "USD", "8820", "meridianfreight.com", "NET45", "Logistics"),
    ("V-1003", "Borealis Industrial Supply Co.", "Borealis Industrial|Borealis Supply",
     "ACTIVE", "US-45-1128773", "US", "USD", "3095", "borealis-supply.com", "NET30", "MRO"),
    ("V-1004", "Lumen Design Studio", "Lumen Design|Lumen Studio",
     "ACTIVE", "US-92-3348810", "US", "USD", "7712", "lumendesign.co", "NET15", "Creative"),
    ("V-1005", "Vertex Talent Partners", "Vertex Talent",
     "ON_HOLD", "US-33-9982211", "US", "USD", "5540", "vertextalent.com", "NET30", "Staffing"),
]

PURCHASE_ORDERS = [
    # po_number, vendor_id, description, currency, po_amount, po_date, status,
    # tolerance_pct, tolerance_abs, allow_partial, buyer_name, buyer_email, cost_center
    ("PO-10231", "V-1001", "Cloud infrastructure - annual commitment", "USD", 48200.00,
     d(-58), "OPEN", 2.0, 500.0, "false", "Priya Raman", "priya.raman@northgate.example", "CC-ENG-01"),
    ("PO-10244", "V-1002", "Inbound ocean + drayage freight, rolling quarter", "USD", 120000.00,
     d(-52), "OPEN", 2.0, 750.0, "true", "Daniel Okonkwo", "daniel.okonkwo@northgate.example", "CC-OPS-04"),
    ("PO-10250", "V-1003", "MRO consumables blanket order", "USD", 26000.00,
     d(-46), "OPEN", 3.0, 400.0, "true", "Sara Whitfield", "sara.whitfield@northgate.example", "CC-FAC-02"),
    ("PO-10255", "V-1001", "Security tooling add-on seats", "USD", 15750.00,
     d(-40), "OPEN", 2.0, 500.0, "false", "Priya Raman", "priya.raman@northgate.example", "CC-ENG-01"),
    ("PO-10260", "V-1004", "Brand refresh, phase 1", "USD", 18000.00,
     d(-35), "OPEN", 5.0, 300.0, "true", "Marcus Bell", "marcus.bell@northgate.example", "CC-MKT-03"),
    ("PO-10199", "V-1002", "Prior quarter freight - closed", "USD", 90000.00,
     d(-190), "CLOSED", 2.0, 750.0, "true", "Daniel Okonkwo", "daniel.okonkwo@northgate.example", "CC-OPS-04"),
]

GOODS_RECEIPTS = [
    ("GR-5501", "PO-10231", 48200.00, d(-20), "Service period confirmed by engineering"),
    ("GR-5502", "PO-10244", 72500.00, d(-16), "Partial - February vessel arrivals"),
    ("GR-5503", "PO-10244", 45000.00, d(-6), "Partial - March drayage"),
    ("GR-5510", "PO-10250", 9681.00, d(-9), "Consumables delivered to Plant 2"),
    ("GR-5515", "PO-10255", 15750.00, d(-11), "Seats provisioned"),
]


def write_master() -> None:
    with open(MASTER / "vendors.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["vendor_id", "legal_name", "aliases", "status", "tax_id", "country",
                    "currency", "bank_last4", "remit_domain", "payment_terms", "category"])
        w.writerows(VENDORS)

    with open(MASTER / "purchase_orders.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["po_number", "vendor_id", "description", "currency", "po_amount", "po_date",
                    "status", "tolerance_pct", "tolerance_abs", "allow_partial", "buyer_name",
                    "buyer_email", "cost_center"])
        for r in PURCHASE_ORDERS:
            w.writerow([r[0], r[1], r[2], r[3], f"{r[4]:.2f}", r[5].isoformat(), *r[6:]])

    with open(MASTER / "goods_receipts.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["gr_id", "po_number", "received_amount", "received_date", "note"])
        for r in GOODS_RECEIPTS:
            w.writerow([r[0], r[1], f"{r[2]:.2f}", r[3].isoformat(), r[4]])


# ---------------------------------------------------------------------------
# Layout A -- "Apex": modern SaaS invoice, coloured header, gridded table,
# tax on its own line, labels "Invoice #" / "PO Number" / "Total Due".
# ---------------------------------------------------------------------------

def layout_apex(path: Path, *, vendor_name: str, remit_domain: str, bank_last4: str,
                invoice_no: str, invoice_date: date, po_ref: str | None,
                lines: list[tuple[str, float, float]], tax_rate: float,
                bank_name: str = "First Meridian Bank") -> dict:
    c = rl_canvas.Canvas(str(path), pagesize=A4)
    accent = HexColor("#1F3A93")

    c.setFillColor(accent)
    c.rect(0, H - 34 * mm, W, 34 * mm, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 19)
    c.drawString(20 * mm, H - 18 * mm, vendor_name)
    c.setFont("Helvetica", 8.5)
    c.drawString(20 * mm, H - 24 * mm, f"1180 Harrison Street, Suite 400, San Francisco, CA 94103")
    c.drawString(20 * mm, H - 28.5 * mm, f"billing@{remit_domain}   ·   +1 (415) 555-0148")
    c.setFont("Helvetica-Bold", 22)
    c.drawRightString(W - 20 * mm, H - 20 * mm, "INVOICE")

    c.setFillColor(black)
    y = H - 46 * mm
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(20 * mm, y, "BILL TO")
    c.setFont("Helvetica", 9.5)
    c.drawString(20 * mm, y - 5.5 * mm, "Northgate Manufacturing Inc.")
    c.drawString(20 * mm, y - 10 * mm, "Accounts Payable, 4400 Industrial Parkway")
    c.drawString(20 * mm, y - 14.5 * mm, "Columbus, OH 43219")

    meta = [("Invoice #", invoice_no), ("Invoice Date", invoice_date.strftime("%d %b %Y")),
            ("Due Date", (invoice_date + timedelta(days=30)).strftime("%d %b %Y"))]
    if po_ref:
        meta.insert(2, ("PO Number", po_ref))
    my = y
    for label, value in meta:
        c.setFont("Helvetica", 8.5)
        c.drawRightString(W - 52 * mm, my, f"{label}")
        c.setFont("Helvetica-Bold", 9.5)
        c.drawRightString(W - 20 * mm, my, str(value))
        my -= 6 * mm

    ty = y - 26 * mm
    c.setFillColor(HexColor("#EEF1FA"))
    c.rect(20 * mm, ty - 2 * mm, W - 40 * mm, 8 * mm, stroke=0, fill=1)
    c.setFillColor(black)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(23 * mm, ty + 0.6 * mm, "DESCRIPTION")
    c.drawRightString(W - 78 * mm, ty + 0.6 * mm, "QTY")
    c.drawRightString(W - 50 * mm, ty + 0.6 * mm, "UNIT PRICE")
    c.drawRightString(W - 23 * mm, ty + 0.6 * mm, "AMOUNT")

    subtotal = 0.0
    ry = ty - 9 * mm
    c.setFont("Helvetica", 9)
    for desc, qty, unit in lines:
        amt = round(qty * unit, 2)
        subtotal += amt
        c.drawString(23 * mm, ry, desc)
        c.drawRightString(W - 78 * mm, ry, f"{qty:g}")
        c.drawRightString(W - 50 * mm, ry, money(unit))
        c.drawRightString(W - 23 * mm, ry, money(amt))
        ry -= 7 * mm
    subtotal = round(subtotal, 2)
    tax = round(subtotal * tax_rate, 2)
    total = round(subtotal + tax, 2)

    c.setStrokeColor(HexColor("#C9CEDA"))
    c.line(20 * mm, ry + 2 * mm, W - 20 * mm, ry + 2 * mm)
    ry -= 5 * mm
    for label, value, bold in [("Subtotal", subtotal, False),
                               (f"Sales Tax ({tax_rate*100:.2f}%)", tax, False),
                               ("Total Due (USD)", total, True)]:
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 10.5 if bold else 9)
        c.drawRightString(W - 50 * mm, ry, label)
        c.drawRightString(W - 23 * mm, ry, money(value))
        ry -= 6.5 * mm

    by = ry - 10 * mm
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(20 * mm, by, "REMIT TO")
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, by - 5.5 * mm, f"{bank_name}  ·  Routing 121000248")
    c.drawString(20 * mm, by - 10 * mm, f"Account ending {bank_last4}  ·  Beneficiary: {vendor_name}")
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(20 * mm, 18 * mm, "Payment terms Net 30. Please quote the invoice number on remittance.")
    c.showPage()
    c.save()
    return {"subtotal": subtotal, "tax": tax, "total": total}


# ---------------------------------------------------------------------------
# Layout B -- "Meridian": dense logistics invoice. Labels "Bill Number" /
# "Your PO" / "AMOUNT PAYABLE". Tax is embedded in the line rate, stated as a
# note rather than a separate line -- a common and annoying real-world pattern.
# ---------------------------------------------------------------------------

def layout_meridian(path: Path, *, invoice_no: str, invoice_date: date, po_ref: str,
                    lines: list[tuple[str, str, float]], period: str) -> dict:
    c = rl_canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Courier-Bold", 15)
    c.drawString(18 * mm, H - 20 * mm, "MERIDIAN FREIGHT SERVICES LLC")
    c.setFont("Courier", 8)
    c.drawString(18 * mm, H - 25 * mm, "2200 Port Access Road, Newark NJ 07114 | SCAC MFSL | ap@meridianfreight.com")
    c.setStrokeColor(black)
    c.setLineWidth(1.2)
    c.line(18 * mm, H - 28 * mm, W - 18 * mm, H - 28 * mm)

    c.setFont("Courier-Bold", 11)
    c.drawString(18 * mm, H - 36 * mm, "FREIGHT INVOICE")
    c.setFont("Courier", 9)
    rows = [f"Bill Number ....... {invoice_no}",
            f"Bill Date ......... {invoice_date.strftime('%m/%d/%Y')}",
            f"Your PO ........... {po_ref}",
            f"Service Period .... {period}",
            f"Terms ............. NET 45"]
    yy = H - 44 * mm
    for r in rows:
        c.drawString(18 * mm, yy, r)
        yy -= 5 * mm

    c.setFont("Courier", 9)
    c.drawRightString(W - 18 * mm, H - 44 * mm, "CONSIGNEE:")
    c.drawRightString(W - 18 * mm, H - 49 * mm, "NORTHGATE MANUFACTURING INC")
    c.drawRightString(W - 18 * mm, H - 54 * mm, "4400 INDUSTRIAL PKWY, COLUMBUS OH")

    ty = yy - 8 * mm
    c.setFont("Courier-Bold", 8.5)
    c.drawString(18 * mm, ty, "CHARGE CODE  DESCRIPTION".ljust(58) + "REFERENCE".ljust(16) + "AMOUNT USD")
    c.line(18 * mm, ty - 2 * mm, W - 18 * mm, ty - 2 * mm)

    total = 0.0
    ry = ty - 8 * mm
    c.setFont("Courier", 8.5)
    for code_desc, ref, amt in lines:
        total += amt
        c.drawString(18 * mm, ry, code_desc.ljust(58)[:58] + ref.ljust(16)[:16])
        c.drawRightString(W - 18 * mm, ry, money(amt))
        ry -= 4.6 * mm
    total = round(total, 2)

    c.line(18 * mm, ry, W - 18 * mm, ry)
    ry -= 7 * mm
    c.setFont("Courier-Bold", 11)
    c.drawString(18 * mm, ry, "AMOUNT PAYABLE")
    c.drawRightString(W - 18 * mm, ry, f"USD {money(total)}")

    ry -= 12 * mm
    c.setFont("Courier", 7.5)
    c.drawString(18 * mm, ry, "NOTE: All charges shown are tax-inclusive. Applicable state tax of 6.625% is embedded")
    c.drawString(18 * mm, ry - 4 * mm, "in the line rates above and is not separately stated on this document.")
    c.drawString(18 * mm, ry - 12 * mm, "REMIT: First Meridian Bank / ACCT ENDING 8820 / ROUTING 021000021")
    c.showPage()
    c.save()
    return {"total": total}


# ---------------------------------------------------------------------------
# Layout C -- "Borealis": plain fax-era invoice. This one gets flattened to a
# 300 dpi scan, so the process has no text layer to read at all. It also never
# writes a PO number; it refers to "our blanket order" in prose.
# ---------------------------------------------------------------------------

def layout_borealis(path: Path, *, invoice_no: str, invoice_date: date,
                    lines: list[tuple[str, float, float]]) -> dict:
    c = rl_canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(22 * mm, H - 24 * mm, "BOREALIS INDUSTRIAL SUPPLY CO.")
    c.setFont("Helvetica", 9)
    c.drawString(22 * mm, H - 30 * mm, "715 Foundry Lane, Toledo OH 43604   Tel 419-555-0107")
    c.setLineWidth(0.8)
    c.line(22 * mm, H - 33 * mm, W - 22 * mm, H - 33 * mm)

    c.setFont("Helvetica-Bold", 12)
    c.drawString(22 * mm, H - 43 * mm, "STATEMENT OF CHARGES")
    c.setFont("Helvetica", 10)
    c.drawString(22 * mm, H - 51 * mm, f"Document Ref:  {invoice_no}")
    c.drawString(22 * mm, H - 57 * mm, f"Dated:  {invoice_date.strftime('%B %d, %Y')}")
    c.drawString(22 * mm, H - 63 * mm, "Sold To:  Northgate Manufacturing Inc., Plant 2 Stores")
    c.setFont("Helvetica-Oblique", 9.5)
    c.drawString(22 * mm, H - 71 * mm,
                 "Supplied against our standing blanket order for this quarter. Release note attached to shipment.")

    ty = H - 84 * mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(22 * mm, ty, "ITEM")
    c.drawRightString(W - 72 * mm, ty, "QTY")
    c.drawRightString(W - 47 * mm, ty, "RATE")
    c.drawRightString(W - 22 * mm, ty, "VALUE")
    c.line(22 * mm, ty - 2 * mm, W - 22 * mm, ty - 2 * mm)

    subtotal = 0.0
    ry = ty - 9 * mm
    c.setFont("Helvetica", 9.5)
    for desc, qty, unit in lines:
        amt = round(qty * unit, 2)
        subtotal += amt
        c.drawString(22 * mm, ry, desc)
        c.drawRightString(W - 72 * mm, ry, f"{qty:g}")
        c.drawRightString(W - 47 * mm, ry, money(unit))
        c.drawRightString(W - 22 * mm, ry, money(amt))
        ry -= 6.5 * mm

    subtotal = round(subtotal, 2)
    tax = round(subtotal * 0.0575, 2)
    total = round(subtotal + tax, 2)
    c.line(W - 90 * mm, ry + 1 * mm, W - 22 * mm, ry + 1 * mm)
    ry -= 6 * mm
    c.setFont("Helvetica", 9.5)
    c.drawRightString(W - 47 * mm, ry, "Sub Total")
    c.drawRightString(W - 22 * mm, ry, money(subtotal))
    ry -= 6 * mm
    c.drawRightString(W - 47 * mm, ry, "Tax @ 5.75%")
    c.drawRightString(W - 22 * mm, ry, money(tax))
    ry -= 8 * mm
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(W - 47 * mm, ry, "BALANCE DUE")
    c.drawRightString(W - 22 * mm, ry, f"$ {money(total)}")

    c.setFont("Helvetica", 8.5)
    c.drawString(22 * mm, 30 * mm, "Remittance: First Meridian Bank, account ending 3095.")
    c.drawString(22 * mm, 25 * mm, "Queries: accounts@borealis-supply.com")
    c.showPage()
    c.save()
    return {"subtotal": subtotal, "tax": tax, "total": total}


# ---------------------------------------------------------------------------
# Layout D -- "Lumen": minimal design-studio invoice with no grid, amounts in a
# right-hand column, and (deliberately) no invoice number and broken arithmetic.
# ---------------------------------------------------------------------------

def layout_lumen(path: Path, *, invoice_date: date, lines: list[tuple[str, float]],
                 stated_total: float) -> dict:
    c = rl_canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica-Bold", 28)
    c.drawString(24 * mm, H - 40 * mm, "lumen")
    c.setFont("Helvetica", 9)
    c.drawString(24 * mm, H - 47 * mm, "design studio  ·  hello@lumendesign.co")

    c.setFont("Helvetica", 10)
    c.drawString(24 * mm, H - 70 * mm, "For:  Northgate Manufacturing Inc.")
    c.drawString(24 * mm, H - 76 * mm, f"Issued:  {invoice_date.strftime('%d/%m/%Y')}")
    c.drawString(24 * mm, H - 82 * mm, "Project:  Brand refresh")

    ry = H - 100 * mm
    c.setFont("Helvetica", 10.5)
    subtotal = 0.0
    for desc, amt in lines:
        subtotal += amt
        c.drawString(24 * mm, ry, desc)
        c.drawRightString(W - 24 * mm, ry, money(amt))
        ry -= 8 * mm
    ry -= 6 * mm
    c.setLineWidth(0.6)
    c.line(W - 70 * mm, ry + 4 * mm, W - 24 * mm, ry + 4 * mm)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(W - 70 * mm, ry - 3 * mm, "Total")
    c.drawRightString(W - 24 * mm, ry - 3 * mm, money(stated_total))
    c.setFont("Helvetica", 9)
    c.drawString(24 * mm, 40 * mm, "Payable within 15 days. Bank details as previously shared.")
    c.showPage()
    c.save()
    return {"subtotal": round(subtotal, 2), "total": stated_total}


# ---------------------------------------------------------------------------
# Scan simulation: flatten a PDF page to a noisy, slightly skewed 300 dpi image
# and rebuild a PDF around it. The result has no text layer whatsoever.
# ---------------------------------------------------------------------------

def flatten_to_scan(src: Path, dst: Path, *, rotate: float = -0.55, seed: int = 7) -> None:
    import pymupdf
    from PIL import Image, ImageEnhance, ImageFilter

    random.seed(seed)
    doc = pymupdf.open(src)
    page = doc[0]
    pix = page.get_pixmap(dpi=200)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
    doc.close()

    img = img.rotate(rotate, resample=Image.BICUBIC, expand=False, fillcolor=248)
    img = ImageEnhance.Contrast(img).enhance(1.18)
    img = img.filter(ImageFilter.GaussianBlur(radius=0.45))

    px = img.load()
    w, h = img.size
    for _ in range(int(w * h * 0.004)):
        x, y = random.randrange(w), random.randrange(h)
        px[x, y] = max(0, min(255, px[x, y] + random.choice([-70, -45, 40, 60])))
    # Uneven scanner illumination down the right edge.
    for x in range(w - int(w * 0.06), w):
        fade = int(14 * (x - (w - int(w * 0.06))) / max(1, int(w * 0.06)))
        for y in range(0, h, 2):
            px[x, y] = max(0, px[x, y] - fade)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=72)
    buf.seek(0)

    out = pymupdf.open()
    pg = out.new_page(width=W, height=H)
    pg.insert_image(pymupdf.Rect(0, 0, W, H), stream=buf.read())
    out.save(dst)
    out.close()


# ---------------------------------------------------------------------------

def build() -> None:
    write_master()
    manifest = []

    # 1. HAPPY PATH -- clean digital PDF, explicit PO, exact match. 48,200.00
    r = layout_apex(
        SAMPLES / "01_apex_happy_path.pdf",
        vendor_name="Apex Cloud Systems Inc.", remit_domain="apexcloud.com", bank_last4="4471",
        invoice_no="INV-2041", invoice_date=d(-9), po_ref="PO-10231",
        lines=[("Compute reserved instances - annual commitment", 1, 31500.00),
               ("Managed object storage, 240 TB tier", 1, 8900.00),
               ("Premium support plan, 12 months", 12, 650.00)],
        tax_rate=0.08625,
    )
    assert abs(r["subtotal"] - 48200.00) < 0.01, r
    manifest.append(("01_apex_happy_path.pdf", "Happy path", "AUTO_APPROVED"))

    # 2. EDGE 1a -- first of two invoices against one PO. Partial approval.
    layout_meridian(
        SAMPLES / "02_meridian_partial_1of2.pdf",
        invoice_no="MFS-88012", invoice_date=d(-14), po_ref="PO-10244",
        period=f"{d(-45).strftime('%m/%d')} - {d(-20).strftime('%m/%d')}",
        lines=[("410 OCEAN FREIGHT SHA-LAX 6x40HC", "BL 7741021", 38400.00),
               ("512 DRAYAGE LAX-COLUMBUS", "DR 88113", 14750.00),
               ("330 TERMINAL HANDLING", "THC 5521", 9600.00),
               ("221 CUSTOMS BROKERAGE", "CB 90112", 4250.00),
               ("140 FUEL SURCHARGE 11.5%", "FSC", 5500.00)],
    )
    manifest.append(("02_meridian_partial_1of2.pdf", "Edge 1a - partial billing", "APPROVED_PARTIAL"))

    # 3. EDGE 1b -- second invoice pushes cumulative billing past the PO ceiling.
    #    72,500 + 51,900 = 124,400 against a 120,000 PO -> 4,400 over (3.67%).
    layout_meridian(
        SAMPLES / "03_meridian_partial_2of2_overbill.pdf",
        invoice_no="MFS-88157", invoice_date=d(-3), po_ref="PO-10244",
        period=f"{d(-19).strftime('%m/%d')} - {d(-4).strftime('%m/%d')}",
        lines=[("410 OCEAN FREIGHT SHA-LAX 4x40HC", "BL 7748890", 26800.00),
               ("512 DRAYAGE LAX-COLUMBUS", "DR 88402", 11200.00),
               ("330 TERMINAL HANDLING", "THC 5610", 6400.00),
               ("615 DEMURRAGE 4 DAYS", "DEM 2210", 3900.00),
               ("140 FUEL SURCHARGE 11.5%", "FSC", 3600.00)],
    )
    manifest.append(("03_meridian_partial_2of2_overbill.pdf", "Edge 1b - cumulative over-billing", "ROUTE_TO_BUYER"))

    # 4. EDGE 2 -- scanned image, no text layer, no PO number written anywhere.
    tmp = SAMPLES / "_borealis_source.pdf"
    r = layout_borealis(
        tmp, invoice_no="BIS-5567", invoice_date=d(-8),
        lines=[("Cutting fluid, 55 gal drum", 12, 412.00),
               ("Abrasive discs 7in, box of 50", 30, 88.50),
               ("Nitrile gloves, case", 24, 61.25),
               ("Shop towels, bale", 18, 34.00)],
    )
    flatten_to_scan(tmp, SAMPLES / "04_borealis_scanned_no_po.pdf")
    tmp.unlink()
    manifest.append(("04_borealis_scanned_no_po.pdf", "Edge 2 - scan + inferred PO", "HOLD_FOR_REVIEW"))
    print(f"   borealis: net {r['subtotal']} tax {r['tax']} gross {r['total']}")

    # 5. EDGE 3 -- textbook-perfect match, changed bank account, lookalike name.
    layout_apex(
        SAMPLES / "05_apex_bank_change_fraud.pdf",
        vendor_name="Apex Cloud System Inc.",           # note: "System", not "Systems"
        remit_domain="apexc1oud.com",                    # note: digit one, not letter l
        bank_last4="9902",                               # master has 4471
        invoice_no="INV-2088", invoice_date=d(-5), po_ref="PO-10255",
        lines=[("Security tooling - 150 seats", 150, 92.00),
               ("Onboarding and SSO configuration", 1, 1950.00)],
        tax_rate=0.08625, bank_name="Coastal Trust Bank",
    )
    manifest.append(("05_apex_bank_change_fraud.pdf", "Edge 3 - bank-detail change", "BLOCKED_FRAUD_REVIEW"))

    # 6. EDGE 4 -- the happy-path invoice resent in a different layout with the
    #    number spaced differently. Same vendor, same date, same money.
    c = rl_canvas.Canvas(str(SAMPLES / "06_apex_duplicate_resend.pdf"), pagesize=A4)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(20 * mm, H - 25 * mm, "Apex Cloud Systems Inc.")
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, H - 31 * mm, "billing@apexcloud.com  ·  1180 Harrison Street, San Francisco CA")
    c.setFont("Helvetica-Bold", 12)
    c.drawString(20 * mm, H - 45 * mm, "STATEMENT / COPY OF INVOICE")
    c.setFont("Helvetica", 10)
    for i, txt in enumerate([
            "Invoice No.  :  INV 2041",
            f"Invoice Date :  {d(-9).strftime('%d %b %Y')}",
            "Customer PO  :  PO-10231",
            "Account      :  NORTHGATE MANUFACTURING",
            "",
            "Re-issued at customer request. Original mailed on the invoice date.",
            "",
            "Cloud infrastructure annual commitment .......  31,500.00",
            "Managed object storage 240 TB ................   8,900.00",
            "Premium support plan (12 x 650.00) ...........   7,800.00",
            "",
            "Sub Total ....................................  48,200.00",
            "Sales Tax 8.625% .............................   4,157.25"]):
        c.drawString(20 * mm, H - 55 * mm - i * 6 * mm, txt)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(20 * mm, H - 140 * mm, "TOTAL PAYABLE (USD)          52,357.25")
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, 30 * mm, "Remit to First Meridian Bank, account ending 4471.")
    c.showPage()
    c.save()
    manifest.append(("06_apex_duplicate_resend.pdf", "Edge 4 - duplicate resend", "REJECTED_DUPLICATE"))

    # 7. EDGE 5 -- no invoice number, no PO, and the lines do not sum to the
    #    stated total (7,900 + 4,200 + 2,600 = 14,700, stated 16,400).
    layout_lumen(
        SAMPLES / "07_lumen_incomplete.pdf", invoice_date=d(-6),
        lines=[("Identity system exploration", 7900.00),
               ("Typography and colour direction", 4200.00),
               ("Presentation deck", 2600.00)],
        stated_total=16400.00,
    )
    manifest.append(("07_lumen_incomplete.pdf", "Edge 5 - incomplete + bad math", "REJECTED_INCOMPLETE"))

    print(f"master data -> {MASTER}")
    for name, label, expect in manifest:
        size = (SAMPLES / name).stat().st_size
        print(f"  {name:42s} {label:34s} expect {expect:22s} {size//1024:>4d} KB")


if __name__ == "__main__":
    build()
