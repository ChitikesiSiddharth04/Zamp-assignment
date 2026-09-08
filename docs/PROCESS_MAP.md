# Process map

Written before any code, which is the point of doing it. The build follows this.

## The job being replaced

Someone on the AP team opens an email, opens the PDF, reads six or seven numbers off it,
opens a spreadsheet, finds the purchase order, compares two amounts, and decides. Between
fifty and a few hundred times a month. The failure mode is not that they cannot do it — it
is that on the two-hundredth one, on a Friday, they approve something they should not have.

Three things make it hard, and each one drives a design choice:

1. **The documents disagree with each other.** Every vendor names the same field differently
   and lays it out differently. Some are photographs. → *extraction must be format-agnostic
   and must report how sure it is.*
2. **The match is not always obvious.** One PO can be billed by several invoices; amounts are
   close but not equal; a PO reference may not be printed at all. → *matching must be able to
   say "I inferred this", and inference must not be allowed to move money on its own.*
3. **The decision has to be defensible.** Someone will ask, months later, why this was paid.
   → *the reasoning is the product, not a by-product.*

## Input

One PDF. Digital or scanned. Any vendor layout. Arrives by upload or drop; in production it
hangs off an AP mailbox.

## Reference data it decides against

Three tables, standing in for the procurement system (`data/master/`):

* **vendors** — legal name, aliases, status, remit domain, bank account last four, terms
* **purchase orders** — amount, currency, status, tolerance (% and absolute), whether partial
  billing is allowed, the named buyer, cost centre
* **goods receipts** — what has actually been received against each PO

Plus one table the process writes itself: the **ledger** of invoices that created a liability.
Without it there is no duplicate detection and no cumulative billing, so it is not optional.

## The stages

```
   PDF
    │
 1  INGEST        store the bytes, hash them  ─────────────► the hash is the audit anchor
    │
 2  READ          is there a text layer?
    │             ├─ yes → read it
    │             └─ no  → render 300 dpi → OCR → keep the mean word confidence
    │
 3  EXTRACT       layout parser (always)
    │             + Claude reading the same document (if configured)
    │             → reconcile: agree ⇒ confidence up, differ ⇒ confidence halved + flagged
    │
 4  NORMALISE     dates (incl. ambiguous DD/MM vs MM/DD), currency, amounts,
    │             invoice reference → INV-2041 / "INV 2041" / inv2041 all → INV2041
    │             apply any human confirmation already recorded against this run
    │
 5  VENDOR        resolve on name AND remittance domain
    │             compute a supplier-identity risk score from their disagreement
    │
 6  PO            explicit reference → look it up
    │             none → infer from the vendor's open orders, and mark it inferred
    │
 7  VALIDATE      run every rule. Change nothing. Record everything.
    │
 8  DECIDE        one ordering of failure classes → one outcome
    │
 9  ACT           approved → ERP posting
    │             not approved → the actual message that has to go out, addressed and written
    │
10  RECORD        run, stage trace, actions, ledger entry if a payable was created
```

## Decision points

| # | Question | Branches |
|---|---|---|
| 1 | Can we read it at all? | text layer → parse · no text layer → OCR |
| 2 | Do two extractors agree? | agree → confident · differ → flag the field |
| 3 | Do we know this supplier? | on the master · lookalike · unknown |
| 4 | Is the supplier who they say they are? | identity risk < 4 · ≥ 4 → freeze |
| 5 | Have we seen this invoice? | new · same reference · same money, near date |
| 6 | Which PO? | printed · inferred · ambiguous · none |
| 7 | Which number do we compare? | net, when tax is separate · gross, and say so |
| 8 | Does the money work? | within tolerance · partial · over the order |
| 9 | Was it received? | receipted · billing ahead of receipt · no receipt at all |
| 10 | Whose signature does this need? | nobody's · AP's · the budget owner's · controls' |

## Output

A decision, its reason codes, a rationale in English, the named next step, its owner, and the
document that has to go out. Plus the full stage trace, kept.

## What is deliberately not in scope

Email intake, real ERP posting, real SMTP, PO creation, payment execution, multi-currency FX,
and line-level three-way matching against quantities. Each is a known increment, none of them
changes the shape above.
