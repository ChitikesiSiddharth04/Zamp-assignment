# 5-minute demo video — full transcript

Read it as written and it lands at about 4:50. Roughly 740 spoken words at a normal pace.
`[SCREEN]` is what you do. **SAY:** is what you say.

**Before you hit record**

1. `./run.sh`, open `http://127.0.0.1:8080`, click **Reset demo**. Start from zero runs.
2. Browser at 100% zoom, window ~1500px wide so all three columns show.
3. Close everything else. One tab.
4. Do one silent dry run of the whole thing. It takes four minutes and it is the difference
   between a demo and a recovery.

---

### 0:00 – 0:20 · What this is

`[SCREEN] The app, Run view, nothing running yet.`

> **SAY:** This is problem statement one — invoices to decisions. A vendor invoice comes in as
> a PDF, and this process reads it, matches it to the purchase order, applies the rules, and
> produces a decision it can defend.
>
> The one design decision everything else follows from: **a model reads the document,
> deterministic rules decide what happens to the money.** Reading a smudged scan is
> genuinely probabilistic. Approving a payment cannot be. So if this thing approves something,
> it is because sixteen named checks passed, and I can show you every one of them.

---

### 0:20 – 1:05 · Happy path

`[SCREEN] Click "Happy path". Let it run — stages tick down the middle column.`

> **SAY:** Ten stages, and you can watch each one. It ingests and hashes the file, finds a
> text layer, extracts eleven fields, normalises them, resolves the vendor, matches the PO,
> runs the rules, decides, produces the output, writes the audit trail. About forty
> milliseconds of actual processing.

`[SCREEN] Point at the green verdict banner.`

> **SAY:** Auto-approved. And look at these two numbers — the invoice is for **$52,357.25**,
> but it matched on **$48,200**. A purchase order is raised net of tax, so when an invoice
> states tax separately, the subtotal is the number you compare. Get that wrong and you
> generate a tolerance breach on every invoice from every vendor that itemises tax.

`[SCREEN] Click the "What it read" tab.`

> **SAY:** Every field carries a confidence and the literal text it came from — so a reviewer
> can see what the machine actually read, rather than trusting a number in a box.

---

### 1:05 – 1:35 · Duplicate

`[SCREEN] Click "Edge 4 · duplicate resend".`

> **SAY:** Same invoice, sent again. Completely different layout — it is a statement, not the
> branded invoice — and the reference is punctuated differently: `INV space 2041` instead of
> `INV dash 2041`. A `WHERE invoice_number equals` lookup misses that. References get
> normalised to alphanumerics first, so both are `INV2041`.

`[SCREEN] Point at the red banner.`

> **SAY:** Rejected, linked back to the original run, no payable created.

---

### 1:35 – 2:30 · Scanned, no PO — and the human loop

`[SCREEN] Click "Edge 2 · scan, no PO printed". Let it run; it takes about a second and a half.`

> **SAY:** This one is a photograph. Zero characters of text, so it renders the page at 300
> dpi and runs OCR — 94.5% mean word confidence, and that number is carried forward: the
> fields read off this page land at 84%, under my 85% floor. It also **halves the money
> tolerance**, because the thing I am least sure of is exactly the digits I am comparing.
>
> Second problem: the document never prints a PO number. It says "against our standing blanket
> order". So the process infers it — this vendor has one open order and the amount fits. That
> is almost certainly right, and it still will not auto-approve, because **an inferred link is
> a good guess, not evidence.**

`[SCREEN] Exception queue → "Confirm readings & release".`

> **SAY:** A reviewer confirms the three field reads and the PO link against the image — and
> the invoice runs through **every check again**. Confirming an input does not skip a rule.
> It clears as *approved after review*, with the reviewer's name on the audit trail, and it
> does not count toward the straight-through rate.

---

### 2:30 – 3:20 · The bank account changed

`[SCREEN] Run view → "Edge 3 · changed bank account".`

> **SAY:** This is the one I care most about. Every commercial check on this invoice passes.
> The PO exists, it is open, it belongs to this vendor, the amounts match to the cent, the
> goods are receipted. A matcher that only compares numbers pays it.

`[SCREEN] Point at the verdict, then open the Output tab.`

> **SAY:** Three things are subtly wrong, and no single one is proof. The remit-to account
> ends 9902; the vendor record says 4471 — that is three points. The email domain is
> apex-c-one-oud dot com, digit one instead of letter L — two points. The printed name is
> "Apex Cloud **System**", no S — one point. Plus the vendor now only resolves on name, with
> nothing corroborating it. **Seven against a threshold of four. Payment frozen.**
>
> And the escalation tells controls to verify the change on a number already in the vendor
> master, **never one printed on this invoice** — because the invoice is the thing under
> suspicion. There is deliberately no release button on this one.

---

### 3:20 – 4:15 · One PO, two invoices

`[SCREEN] Run "Edge 1 · first of a split".`

> **SAY:** Freight vendor billing a $120,000 order across a quarter. $72,500 — inside the
> order, partial billing allowed, so it is approved and **$47,500 stays open.** Not a
> tolerance failure just because it is not the full amount.

`[SCREEN] Run "Edge 1 · the split goes over".`

> **SAY:** Second invoice, $51,900. On its own it looks ordinary. Cumulatively it takes billing
> to **$124,400 against a $120,000 order** — 3.67% over. That is not something one document
> can tell you, which is why there is a ledger.

`[SCREEN] Open the Output tab.`

> **SAY:** It routes to Daniel, the named buyer on that PO, with the full arithmetic in the
> email — because the overage might well be payable, demurrage usually is, but it is a budget
> decision, not an AP one.

`[SCREEN] Queue → "Approve the overage".`

> **SAY:** He approves, everything re-runs, it posts.

---

### 4:15 – 4:40 · Dashboard

`[SCREEN] Dashboard.`

> **SAY:** History, where things ended up, the most common reason codes, every run replayable.
> Straight-through reads 29% — because six of my seven test documents are deliberately
> pathological. A real month is not shaped like that, and I would rather show you the honest
> number than a flattering one.

---

### 4:40 – 5:00 · What it is made of

> **SAY:** Python and FastAPI, server-sent events for the live view, SQLite for the trail,
> Tesseract for OCR, and a label-anchored parser that always runs. If an Anthropic key is set,
> Claude reads the same document independently and the two are reconciled in the open —
> agreement raises confidence, disagreement halves it and flags the field.
>
> What it is not: it is a two-way match with a receipt check, not a true three-way match at
> line level. Nothing is actually emailed and nothing posts to a real ERP — those drafts are
> written to the run. That is one function, and it is the next thing I would build.

---

## If you are running long

Cut in this order, and say the sentence in brackets instead:

1. **The duplicate** at 1:05 — 30 seconds. *("It also catches duplicate resends across
   different layouts and punctuation.")*
2. **The "What it read" tab** at 0:55 — 12 seconds.
3. **The dashboard** at 4:15 — 25 seconds.

Never cut the bank-change case or the cumulative-billing case. They are the two that show
judgement rather than plumbing.

## If something breaks on camera

Keep talking, do not restart. *"That's the OCR path — it takes about a second and a half."*
Then click on. A silent pause reads as broken; a narrated one reads as normal. If a run
genuinely fails, open a run from the Dashboard instead — every past run replays in full.
