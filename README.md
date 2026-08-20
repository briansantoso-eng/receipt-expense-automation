# Receipt & Expense Automation

Two related back-office automations built on the Claude API. A client sends
documents; a categorised spreadsheet and a review draft come back. Runs
unattended on a schedule, and never sends anything to a client without a human
looking first.

The design principle throughout: **the model reads, the code decides.** Claude
turns pictures and prose into structured data. Every number it returns is
re-checked in plain Python before it reaches anyone's books.

---

## What's here

**`check_receipts.py`** — the receipt pipeline. Watches a Gmail inbox for
receipts forwarded by clients, extracts vendor/date/subtotal/tax/total/category
from photos and PDFs, validates the arithmetic, and files results per client.

**`check_and_reconcile.py`** — the POS reconciliation pipeline. Same shape, text
input: a client emails a POS sales export and their bank deposit figure, and
Claude explains any gap in plain English. Every figure is computed in Python;
Claude only writes the explanation.

Both share the billing gate, the inbox reader and the review-email delivery.

---

## Where the API call is

Exactly one per batch, and it's the only step that leaves the machine.

```
TRIGGER          GATHER            COMPUTE          GATE          API CALL         DELIVER
Task Scheduler → IMAP, save     → totals,        → has this  →  read the      →  email a draft
(weekly)         attachments      validation       client       pictures         to YOU
                                                   paid?
                 free, local      free, local      free         ~3c/receipt      free, local
```

Everything before the API call is free and deterministic. The gate sits
immediately in front of it, so an unpaid client's documents are stored but never
extracted — they cost nothing.

---

## The parts worth reading

### Schema design is the prompt

`extract_receipt.py` defines a Pydantic model whose field *descriptions* are sent
to the model. Most extraction bugs are fixed there rather than in the prompt:

```python
subtotal: float = Field(
    description="The amount excluding tax. If tax is included in the total, "
                "this is total minus tax — not necessarily the number printed "
                "beside the word SUBTOTAL, which on some receipts still "
                "includes tax.")
```

That one description is why an Australian receipt printing `SUBTOTAL 47.30` next
to `Total includes GST of 4.30` yields a subtotal of `43.00` and not `47.30`.

### Validation that assumes the model is wrong

`validate.py` re-checks every extraction with no API involved:

- `subtotal + tax == total`, to the cent
- for tax-inclusive countries, is the tax a legal fraction of the total?
  (higher than 1/11 of an AUD total is impossible → **error**; lower is normal
  when GST-free items share the basket → **warning**)
- dates in the future, or old enough to suggest a misread year
- duplicates: same vendor, date and total — flagged, never auto-deleted, because
  two identical coffees on one day is a real thing that happens

Output is three-state — `ok` / `check` / `needs_review` — so only exceptions
reach a human. Clean rows are never opened. That routing is the actual product.

### Cost engineering lives in the client, not the API

Batching several images per call amortises the ~1,300-token schema. The chunker
bounds each request by **bytes as well as count**, because ten 4 MB phone photos
exceed the API's 32 MB request limit — which would fail the batch, fall back to
individual calls, and cost more than not batching at all.

---

## Testing

73 offline checks across three suites, all runnable with **no API key and no
network**:

```bash
python self_test.py            # reconciliation maths, subject parsing, MIME, the gate
python test_validate.py        # the validators, against deliberately broken records
python test_receipts_inbox.py  # attachment handling, path traversal, slugs
```

The arithmetic is cross-checked against an independent `Decimal`
re-implementation, so a shared rounding mistake can't pass both.

Verified on real receipts — crumpled, photographed sideways, one food-stained,
one legitimately GST-free — 6/6 correct on every total, tax and date. Nothing
incorrect was ever returned unflagged.

**Scope of that claim:** six receipts is a working prototype, not a proven
production system. It has not been run at volume or across many clients.

### One optimisation that was measured and reverted

Downscaling images before sending looked like a clean ~40% saving. On a crumpled
receipt it read the date as June instead of September while the totals stayed
correct. A wrong total is obvious; a receipt filed in the wrong quarter is
invisible until a BAS period fails to reconcile. Reverted — the extra cost buys
back review time, which is worth more than the API spend.

---

## Setup

```bash
pip install anthropic pydantic python-dotenv requests pillow

cp env.example .env            # add your Anthropic key and a Gmail App Password
cp clients.example.json clients.json   # list your clients and their payment status
```

`GMAIL_APP_PASSWORD` needs an App Password from
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords),
not your account password — Google blocks the latter for IMAP/SMTP.

```bash
python make_sample_receipt.py                 # generates test receipts
python batch_receipts.py --folder receipts    # extract a folder, no email involved
python check_receipts.py --dry-run            # inbox + gate, no API spend
python weekly_summary.py --print              # preview the digest, sends nothing
```

Scheduling (Windows), via the `.bat` wrappers, which exist because a scheduled
task starts in `System32` where `.env` isn't:

```
schtasks /create /tn "Receipts" /sc weekly /d FRI /st 18:00 /tr "...\run_receipts.bat"
schtasks /create /tn "Receipt Summary" /sc weekly /d FRI /st 18:30 /tr "...\run_summary.bat"
```

`.env`, `clients.json`, `receipts/`, `output/` and the generated CSVs are all
gitignored — credentials and client data stay local.
