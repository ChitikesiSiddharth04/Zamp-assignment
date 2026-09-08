# Decision logic

Every rule, what it means, and how the rules combine into one outcome.
Source: `app/engine/validators.py` (the rules) and `app/engine/policy.py` (the ordering).

## Severities

| Severity | Meaning |
|---|---|
| `BLOCKER` | fails closed. Nothing about the amounts can rescue it. |
| `CRITICAL` | a person must decide. Which person depends on the rule. |
| `WARNING` | recorded, shown, does not by itself stop straight-through processing. |
| `INFO` | context worth keeping in the trail. |

## The rules

### Reading the document

| Code | Severity | Fails when |
|---|---|---|
| `FIELDS_COMPLETE` | BLOCKER | invoice number, date, vendor name or total is absent |
| `READ_CONFIDENCE` | CRITICAL | any critical field was read below the 85% floor |
| `EXTRACTOR_DISAGREEMENT` | CRITICAL | the layout parser and the model read a field differently |

Confidence is not decorative. A labelled match is 0.94, a label-on-the-next-line match 0.86,
a shape-only match 0.74, an inference 0.55. OCR multiplies all of them by the page's mean word
confidence. Agreement between two extractors adds 0.05; disagreement halves the lower of the
two. A human confirmation sets it to 0.99 and stamps the source `human_confirmed`.

### Dates

| Code | Severity | Fails when |
|---|---|---|
| `DATE_SANE` | CRITICAL | the invoice is dated in the future |
| `DATE_STALE` | WARNING | it is more than 180 days old |
| `DATE_AMBIGUOUS` | WARNING | a numeric date could be read two ways |

`08/25/2026` can only be month-first. `02/09/2026` cannot be resolved from the string alone,
so the process picks the reading that is not in the future, **says which and why**, and drops
that field's confidence to 0.75 — which then trips `READ_CONFIDENCE`. Guessing silently would
have been easy and wrong.

### Supplier identity

| Code | Severity | Fails when |
|---|---|---|
| `VENDOR_KNOWN` | BLOCKER | no vendor on the master matches by name or domain |
| `VENDOR_ACTIVE` | BLOCKER | the vendor is on hold or blocked |
| `BANK_ON_FILE` | BLOCKER | remit-to account ≠ the account on the vendor record |
| `BANK_NOT_STATED` | WARNING | no remittance details printed, so none can be verified |
| `SUPPLIER_IDENTITY_RISK` | BLOCKER at ≥ 4 | the composite score below reaches the threshold |

The composite exists because **no single one of these signals is proof, and any two together
are worth stopping a payment for**:

| Signal | Weight |
|---|---|
| remittance domain is not on the vendor record | +2 |
| remit-to account differs from the account on file | +3 |
| printed legal name is not an exact match to the record | +1 |
| vendor identified by name alone, no corroborating domain | +1 |
| name and domain resolve to *different* vendors | +3 |

Threshold 4. The fraud sample scores 7.

### Purchase order

| Code | Severity | Fails when |
|---|---|---|
| `PO_RESOLVED` | CRITICAL | no PO could be matched at all |
| `PO_INFERRED` | CRITICAL | the PO was worked out rather than read off the document |
| `PO_VENDOR_MATCH` | BLOCKER | the PO belongs to a different vendor |
| `PO_OPEN` | BLOCKER | the PO is closed or cancelled |
| `CURRENCY_MATCH` | BLOCKER | invoice currency ≠ PO currency |

`PO_INFERRED` is critical *by design*. Inferring the PO from "this vendor has exactly one open
order and this amount fits inside it" is a good guess and usually right. It is not evidence,
and it must not be allowed to move money on its own. One human confirmation clears it — and
the invoice then goes through every check again.

### Money

| Code | Severity | Fails when |
|---|---|---|
| `LINE_MATH` | CRITICAL | line items do not sum to the subtotal (or the total) |
| `TAX_BASIS_GROSS` | WARNING | tax is embedded, so the comparison is on gross |
| `AMOUNT_TOLERANCE` | CRITICAL | variance to the open PO balance exceeds tolerance |
| `PARTIAL_BILLING` | CRITICAL | the invoice bills part of a PO that forbids partial billing |
| `PO_CUMULATIVE` | CRITICAL | total billed across all invoices exceeds the order + tolerance |
| `GOODS_RECEIPT` | CRITICAL / WARNING | invoicing runs ahead of what was receipted / nothing receipted |
| `ABOVE_CEILING` | CRITICAL | the amount is over the $75,000 straight-through ceiling |
| `PO_HISTORY` | INFO | prior billing exists — always recorded, never a failure |

**Which number gets compared.** A purchase order is raised net of tax. So when an invoice
states tax separately, the process compares the **subtotal**; the happy-path invoice reads
$52,357.25 and is matched on $48,200.00. When tax is embedded in the line rates and no net
figure exists — the freight vendor — there is nothing to compare, so it falls back to gross
and raises `TAX_BASIS_GROSS` to record that the basis changed rather than changing it quietly.

**Tolerance** is the *lesser* of the PO's percentage and its absolute cap, so a large order
does not silently acquire a large allowance. On an OCR'd document it is **halved**, because
the thing we are least certain about is exactly the digits being compared.

### Duplicates

| Code | Severity | Fails when |
|---|---|---|
| `NOT_DUPLICATE` | BLOCKER | the normalised reference was already processed for this vendor |
| `NEAR_DUPLICATE` | CRITICAL | same vendor, same amount, different number, within 7 days |

References are normalised to alphanumerics before comparison: `INV-2041`, `INV 2041` and
`inv2041` are one invoice. Only invoices that actually created a liability enter the ledger,
so a rejected invoice does not block its own corrected reissue.

## How the rules become one decision

Read top to bottom. The first class that has a failure wins.

```
1. supplier identity      BANK_ON_FILE, SUPPLIER_IDENTITY_RISK   → BLOCKED_FRAUD_REVIEW
2. duplicates             NOT_DUPLICATE, NEAR_DUPLICATE          → REJECTED_DUPLICATE
3. unreadable             FIELDS_COMPLETE                        → REJECTED_INCOMPLETE
4. any other BLOCKER      unknown/inactive vendor, closed PO,
                          wrong currency, wrong PO owner         → HOLD_FOR_REVIEW
5. money and authority    PO_CUMULATIVE, AMOUNT_TOLERANCE,
                          PARTIAL_BILLING, GOODS_RECEIPT,
                          LINE_MATH, ABOVE_CEILING               → ROUTE_TO_BUYER
6. any other CRITICAL     READ_CONFIDENCE, PO_INFERRED,
                          EXTRACTOR_DISAGREEMENT, DATE_SANE      → HOLD_FOR_REVIEW
7. partial, clean                                                → APPROVED_PARTIAL
8. clean                                                         → AUTO_APPROVED
```

Why this order:

* **Fraud first.** A redirected payment on an otherwise perfect invoice is the single most
  expensive outcome available, and it is precisely the case that looks clean on the numbers.
* **Duplicates before everything else that is left**, because they are cheap to detect and
  expensive to miss, and because a duplicate's amount failures are consequences rather than
  causes.
* **Money questions are separated from data-quality questions**, because they go to different
  people. A tolerance breach is not AP's call — it belongs to whoever owns the budget line,
  and the process names them.

## Human in the loop

Two actions, both of which **re-run the whole pipeline**:

* **Confirm readings & release** — sets the confirmed fields to 0.99, marks an inferred PO
  link as confirmed, re-extracts and re-validates from the original bytes.
* **Approve the overage** (budget owner) — accepts `PO_CUMULATIVE`, `AMOUNT_TOLERANCE` and
  `GOODS_RECEIPT`, on the stated basis that a budget owner approving over-PO work is also
  attesting it was delivered.

Confirmations are stored against the run, not against an execution of it, so they survive
re-extraction. Both leave a row in `overrides` with who, when, the note, and the decision
before and after. And because the outcome becomes `APPROVED_AFTER_REVIEW` rather than
`AUTO_APPROVED`, the straight-through rate on the dashboard never counts work a person did.

A `BLOCKED_FRAUD_REVIEW` cannot be released from the queue at all. Bank changes get verified
out of band or not at all.
