# Invoice → decision

**PS-1 · Finance / AP.** A working process that takes a vendor invoice PDF, reads it,
matches it against the procurement system, applies a rule set, and produces a decision it
can defend — with every step visible.

Built for the Zamp AI Solutions Associate case study.

```bash
./run.sh          # then open http://127.0.0.1:8080
```

First run installs dependencies into `.venv` and generates the sample documents. Nothing
else is needed; there is no API key requirement, no external service, no build step.

```bash
.venv/bin/python scripts/smoke_test.py   # proves all seven scenarios still behave
```

---

## The one design decision everything else follows from

**An LLM reads the document. Deterministic rules decide what happens to the money.**

Extraction is genuinely hard and genuinely probabilistic — vendors use different words for
the same field, some invoices are photographs, and no amount of engineering makes reading a
smudged scan a certainty. That is the right job for a model.

Approving a payment is not that job. If the process cannot say *"this was approved because
checks 1–16 passed, here is each one and the evidence behind it"*, then nobody can sign off
on it, nobody can audit it six months later, and the first time it pays a fraudulent invoice
there is no answer to "why did it do that?".

So the boundary is hard:

| Stage | Nature | Who does it |
|---|---|---|
| Read the pixels/text | probabilistic | layout parser + OCR + optionally Claude |
| Resolve vendor and PO | fuzzy, but scored and shown | rapidfuzz + explicit thresholds |
| Validate | deterministic | 16–19 rules, no model involved |
| Decide | deterministic | one readable ordering of failure classes |

Every field the process reads carries a **confidence** and a **provenance** — the literal
text it came from. Every rule has a **stable code** and its **evidence**. The decision names
the codes that produced it. That is what makes the output something a controller can accept.

## Two extractors, and what happens when they disagree

The layout parser is label-anchored: it looks for the words a human looks for (`Invoice #`,
`Bill Number`, `Document Ref`, `Your PO`, `Amount Payable`, `Balance Due`) and reads the value
beside them, then reconstructs the line-item table by finding where the numeric tail of each
row starts. It is fast, free, deterministic and it always runs.

If `ANTHROPIC_API_KEY` is set, **Claude reads the same document independently** and the two
results are reconciled in the open:

* both agree → confidence goes **up**, source becomes `consensus`
* they differ → confidence is **halved**, the field is listed under `disagreements`, and
  `EXTRACTOR_DISAGREEMENT` fires as a critical check

A field two independent readers disagree about is exactly the field a human should look at.
The UI states which extractors actually ran on every single run, so a demo never implies
capability it did not use.

## The ten stages

`INGEST → READ → EXTRACT → NORMALISE → VENDOR → PO → VALIDATE → DECIDE → ACT → RECORD`

Each stage reports its status, its duration, and the payload it produced, streamed to the
browser over server-sent events as it happens. That trace is written to SQLite, so a run can
be reopened months later and read top to bottom without re-running it.

* **READ** decides whether the PDF has a text layer at all. If it does not, the page is
  rendered at 300 dpi and OCR'd with Tesseract, and the mean word confidence is carried
  forward as a multiplier on every field read from it.
* **NORMALISE** is where a human's confirmation is applied, if one exists. Confirmations are
  stored against the run, not against an execution of it, so a re-run keeps them.
* **VALIDATE** runs everything and stops nothing. **DECIDE** is the only place an outcome is
  chosen, and it is a single readable ordering.

## What it decides

| Outcome | Meaning |
|---|---|
| `AUTO_APPROVED` | every check passed, under the ceiling — posted, no human involved |
| `APPROVED_PARTIAL` | clean, and bills part of a PO that allows partial billing |
| `APPROVED_AFTER_REVIEW` | a person confirmed one input; the same rules then cleared it |
| `ROUTE_TO_BUYER` | the money question belongs to the budget owner, not to AP |
| `HOLD_FOR_REVIEW` | AP has to resolve something before the rules can conclude |
| `REJECTED_DUPLICATE` | already processed; no payable created, original linked |
| `REJECTED_INCOMPLETE` | cannot be paid as sent; vendor email drafted with the specifics |
| `BLOCKED_FRAUD_REVIEW` | supplier identity is wrong; payment frozen regardless of match |

Policy order — fraud, then duplicates, then unreadable documents, then other fail-closed
blockers, then money questions, then everything needing a second pair of eyes. See
[`docs/DECISION_LOGIC.md`](docs/DECISION_LOGIC.md) for every rule and its severity.

## Edge cases

Five, all non-trivial, all runnable from the left rail. Full write-up in
[`docs/EDGE_CASES.md`](docs/EDGE_CASES.md).

1. **A PO split across two invoices** — the first is approved partially and the balance stays
   open; the second pushes cumulative billing 3.67% past the order and is routed to the named
   buyer with the arithmetic laid out. Requires state across runs, which is why there is a
   ledger.
2. **A scan with no PO printed on it** — no text layer, so OCR; no PO reference, so it is
   *inferred* from the vendor's only open order. An inferred link is a good guess, not
   evidence, so it can never auto-approve. One human confirmation releases it — through the
   same rules, not around them.
3. **A changed bank account** — matches its PO on every commercial term and would sail
   through on the numbers. Blocked on a composite supplier-identity risk score. This is the
   most expensive thing the process can get wrong.
4. **A duplicate resend** — same invoice, different layout, `INV 2041` instead of `INV-2041`.
   Caught on the normalised reference.
5. **An incomplete invoice** — no invoice number and the line items sum 1,700 short of the
   stated total. Rejected with a drafted email naming exactly what is wrong.

## Deliberate choices worth defending

**The PO is compared against the net amount, not the invoice total.** A PO is raised net of
tax. The happy-path invoice reads `$52,357.25` but is matched on `$48,200.00`, because that is
the number the order was for. When a vendor embeds tax in the line rates and states no net
figure — as the freight vendor does — there is nothing to compare, so the process falls back
to gross and raises `TAX_BASIS_GROSS` to say so rather than quietly changing basis.

**OCR tightens the tolerance rather than loosening it.** When the thing we are least sure
about is the digits we are comparing, the tolerance is halved.

**Every non-approval produces the actual message that has to go out.** A decision that stops
at "rejected" has not removed work, it has moved it. The rejected invoice comes with the
vendor email; the over-PO invoice comes with the buyer's approval request and the arithmetic;
the bank-change block comes with an escalation that says *verify on a number already on file,
never one printed on the invoice.*

**A human's confirmation changes one input and re-runs everything.** It does not skip a check.
The audit trail records who confirmed what, when, and what the decision was before and after.

**An invoice a human unblocked is not counted as straight-through.** It gets its own outcome
so the automation rate on the dashboard stays honest. On the seven sample documents the rate
reads 29%, which is what happens when six of your seven test documents are deliberately
pathological — a real month is not shaped like that.

## Layout

```
app/
  config.py            every tunable, in one place
  models.py            FieldValue / CheckResult / decisions
  db.py                SQLite: runs, events, ledger, actions, overrides
  pipeline.py          the ten stages, streamed
  extract/
    text_layer.py      is there a text layer at all?
    ocr.py             300 dpi render + Tesseract, keeps word confidence
    heuristic.py       label-anchored layout parser
    llm.py             optional independent read via Claude
    pipeline_extract.py  reconciliation between the two
  engine/
    master.py          vendors / POs / goods receipts from CSV
    matching.py        vendor identity + PO resolution
    validators.py      the rules
    policy.py          rules -> one decision
  actions/notify.py    ERP posting, vendor email, buyer request, escalation
web/                   the interface (no build step, no framework)
data/master/*.csv      the "procurement system" — edit it and the next run changes
data/samples/*.pdf     seven generated invoices in four different vendor layouts
scripts/
  make_samples.py      regenerates master data + documents, anchored to today
  smoke_test.py        asserts all seven outcomes and both review paths
docs/                  process map, decision logic, edge cases, demo script
```

## Honest limitations

* **Two-way match with a receipt check bolted on**, not a true three-way match against
  quantities per line. Line-level receipting is the obvious next increment.
* **The vendor master is five rows of CSV.** At real scale, vendor resolution needs blocking
  and a proper index, and the identity-risk weights want tuning against actual fraud attempts
  rather than my judgement.
* **Nothing is actually emailed and nothing is posted to a real ERP.** The drafts and the
  posting are written to the run's action log. That sink is one function; swapping it for SMTP
  and an ERP client is deliberate future work, not something to fake in a take-home.
* **No email intake.** In production this hangs off a mailbox; here you click or drop a file.
* **The tax-rate sanity check is not implemented.** I ran the jurisdiction table down my
  priority list and it lost to the identity work. Worth adding.
* **OCR is Tesseract, which is good on a clean scan and fair on a bad one.** A production
  system would fall back to a vision model on low mean confidence — the hook is already there
  in `pipeline_extract.py`.
