# Live demo runbook + what you will be asked

## Before the call

```bash
cd ~/zamp && ./run.sh                      # http://127.0.0.1:8080
.venv/bin/python scripts/smoke_test.py     # 30 seconds, proves everything still works
```

Then click **Reset demo** in the top right so you start on zero runs, and have these open:

* the app, ~1500px wide, 100% zoom
* `data/master/purchase_orders.csv` in an editor — you will want it (see below)
* nothing else

## The order to run things in

Same as the video, because you have rehearsed it:

1. **Happy path** — the shape of the process, and the net-vs-gross point.
2. **Duplicate resend** — 20 seconds, punchy.
3. **Scan, no PO** — OCR, inference, then *Confirm readings & release* from the queue.
4. **Changed bank account** — the strongest case. Slow down here.
5. **Split → overbill** — run both in order, then *Approve the overage*.
6. **Dashboard** — history, reason codes, honest straight-through rate.

If they only want two, run **the bank change** and **the split**.

## The move that wins the room

Have them pick. Open `data/master/purchase_orders.csv`, change PO-10231's status from `OPEN`
to `CLOSED`, save, and re-run the happy path. It goes from auto-approved to held, and the
reason names the closed order. Nothing is cached; the CSVs are read fresh on every run.

Other one-line changes with visible effects:

| Change | Effect |
|---|---|
| PO-10231 `status` → `CLOSED` | happy path becomes `HOLD_FOR_REVIEW`; `PO_OPEN` fails — **verified** |
| PO-10244 `allow_partial` → `false` | the first freight invoice becomes `ROUTE_TO_BUYER` — **verified** |
| `vendors.csv` V-1001 `bank_last4` → `9902` | `BANK_ON_FILE` now **passes**, and the identity score falls 7 → 4 — **but it is still blocked**, because the lookalike domain and the inexact name alone still reach the threshold — **verified** |
| `AUTO_APPROVE_CEILING` → `40000` in `app/config.py` | the happy path needs a signature |

The bank one is the best of these, and it is worth doing precisely because it does *not*
clear: it shows the block is a genuine composite of independent signals rather than one
hardcoded comparison. Say that out loud — *"I've just made the bank account match, and it
still won't pay it, because two other things are still wrong."*

All three reference-data rows above were verified by actually making the edit and re-running.
They are also the best answer to "is this hardcoded to your demo files?" — you are changing
the reference data, not the code, and the decision moves with it.

They can also **drop in their own PDF**. It has never seen it. Say that out loud before you do
it, and be relaxed about what happens — an unknown vendor correctly comes back as
`HOLD_FOR_REVIEW` with `VENDOR_KNOWN` failed, which *is* the right answer.

---

## Questions you will get

**"How do you know the extraction is right?"**
I do not, and the process does not pretend to. Every field carries a confidence and the
literal text it was read from. Below 85% on a critical field, it will not auto-approve. If a
second extractor is configured, disagreement between them halves the confidence and flags the
field by name. The design assumption is that extraction will sometimes be wrong, so the rules
that spend money never trust a single read.

**"Why not just have the LLM decide?"**
Three reasons. It cannot be audited — "the model thought it looked fine" is not an answer to
a controller in November about a payment made in March. It cannot be tuned — I cannot ask a
model to move a tolerance from 2% to 1.5% and be sure that is all that changed. And it is not
reproducible — the same invoice has to reach the same decision every time. Extraction is
where a model is genuinely better than me. Deciding is not.

**"What happens when it is wrong?"**
Two directions. Wrong-and-cautious is cheap: it lands in the queue, someone confirms in ten
seconds, and the confirmation is recorded against the run. Wrong-and-permissive is expensive,
so the ordering fails closed — fraud signals, duplicates and blockers are all evaluated before
anything can be approved, and the straight-through ceiling means a clean invoice over $75,000
still needs a signature.

**"Why is the straight-through rate only 29%?"**
Because six of my seven documents are edge cases. On this sample that number is meaningless —
I kept it visible rather than seeding it with easy invoices to make it look better. What
matters is that the four rejections and holds each have a named reason and a next step.

**"How would this scale to hundreds a month?"**
The pipeline is stateless per invoice apart from the ledger, so it scales horizontally. The
real constraints are vendor-master lookup — five rows of CSV today, needs blocking and an
index at ten thousand vendors — and OCR, which is about 1.5 seconds a page and belongs in a
worker queue. Neither changes the design.

**"What would you build next, in order?"**
1. Line-level three-way match, so a receipt covers *which* lines rather than a total.
2. Email intake, so nobody clicks anything.
3. Learning from overrides — when AP confirms the same vendor's OCR five times, that vendor's
   layout should stop asking.
4. Real ERP posting and real SMTP.
5. A tax-rate sanity check by jurisdiction, which I scoped out and did not get to.

**"What is the weakest part?"**
The supplier-identity weights. The signals are the right signals, but 3-2-1-1 against a
threshold of 4 is my judgement, not a calibration. In production you tune those against actual
attempted fraud and actual false positives, and you would want to watch the false-positive
rate closely, because a blocked payment to a legitimate vendor has a cost too.

**"How much did you use AI to build this?"**
A lot, deliberately — the brief says to. It was fastest at the parts with a known shape: the
reportlab layouts for the sample invoices, the CSS, the boilerplate around SSE. I made the
decisions that matter myself: the extract/decide boundary, the severity model, the policy
ordering, the identity-risk composite, and which five edge cases were worth building. Those
are the parts I can defend line by line, which is the test of whether you actually made them.

---

## Numbers worth having in your head

| | |
|---|---|
| Happy path | invoice $52,357.25 · matched on **$48,200.00** · PO-10231 · 16/16 checks |
| Freight PO | PO-10244 = **$120,000**, billed $72,500 + $51,900 = **$124,400**, **3.67% over** |
| Overage past the ceiling | **$3,650** (tolerance is the lesser of 2% and $750) |
| Scan | **0** characters of text · 300 dpi · **94.5%** mean OCR confidence · fields at **84%** vs an **85%** floor |
| Fraud | account **9902** vs **4471** on file · domain `apexc1oud.com` · risk **7/10**, threshold **4** |
| Incomplete | lines sum **$14,700** vs stated **$16,400** — **$1,700** unexplained |
| Ceiling | straight-through stops at **$75,000** |
| Speed | ~35–50 ms per digital invoice · ~1.5 s when OCR is needed |
