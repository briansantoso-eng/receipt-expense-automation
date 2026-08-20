# Design decisions

A record of what was decided, what was argued about, and what got reverted. Kept
because the reasoning is more reusable than the code, and because two of the
decisions here were reversals of things that looked obviously correct.

---

## 1. Where the Claude API is allowed to be

**Decision:** the API reads pictures and writes prose. It never does arithmetic,
never looks anything up, never decides who to bill.

**The challenge that produced it:** *"I don't need the Claude API, right? If I
can run it in Python I don't need it."* Largely correct, and worth taking
seriously rather than defending against.

For the POS reconciliation pipeline the challenge holds. Python computes every
total; Claude only writes the paragraph explaining a gap. A template would cover
most of those explanations, so the API is close to optional there.

For receipts it does not hold. A photograph is 512,000 coloured pixels — the
strings `Coles` and `63.92` do not exist anywhere in the file. No amount of
Python extracts a total from that.

**The rule that came out of it:**

> Can the question be answered by a lookup or a rule you can write down?
> → **code.** Does it need eyes, or judgement about something you can't
> enumerate in advance? → **API.**

| Question | Where | Why |
|---|---|---|
| Has this client paid? | code | it's a field in a file |
| Any unread mail from them? | code | it's an IMAP query |
| Does 58.11 + 5.81 = 63.92? | code | it's arithmetic |
| What does this photo say? | **API** | nothing to look up |
| Which of 14 unfamiliar columns is the total? | **API** | can't list every POS format |

Cost is *not* the criterion. The payment gate runs 10,000 times in 2.7ms with
one distinct answer every time; an LLM offers no such guarantee and no audit
trail. If a client disputes a charge, "line 183 of the gate" is a defensible
answer and "the model decided" is not.

---

## 2. The billing gate sits in front of the API call

**Decision:** senders are matched against a roster before anything is extracted.
Unpaid and unknown senders have their documents saved but never read.

**Concern that drove it:** the first working version processed *any* unread email
matching the expected shape. Anyone who learned the format — or a spam message
with a photo attached — would spend API credit on an unattended schedule.

**Incident that settled the details:** a wildcard `*` roster entry was added to
make testing easier. On a real inbox it matched **360 unread messages** and
extracted a Salvation Army appreciation certificate as a receipt.

Two things came out of that:

- **Wildcards are per-domain, never global.** `*@client.com` is a genuine
  requirement — a business often has three staff who send receipts. A bare `*`
  is not a test convenience, it's "process my entire inbox."
- **The gate keys on the sender address, not any name in the subject.** The
  subject is typed by the client; the From header isn't. An unpaid client can't
  get through by naming a paid one.

The validation layer caught the certificate — `total is 0.00, must be positive`,
routed to `needs_review`, zero fields trusted. That was reassuring, but the
gate should have stopped it earlier and now does.

---

## 3. Validation assumes the model is wrong

**Decision:** every extracted number is re-checked in Python. Nothing reaches a
spreadsheet as trusted on the model's word alone.

**Concern:** a misread `47.30` as `41.30` is printed confidently, saved
confidently, and lands in a client's books with nothing to flag it.

Checks that run with no API involved: `subtotal + tax == total` to the cent; tax
as a legal fraction of a tax-inclusive total; dates in the future or old enough
to imply a misread year; the model's own `unreadable_fields` list; duplicates.

**Asymmetry that matters:** for AUD, tax *above* 1/11 of the total is
arithmetically impossible → **error**. Tax *below* it is normal, because GST-free
items (fresh food, medical) share a basket with taxed ones → **warning**. Failing
every mixed grocery receipt would train the operator to ignore the flags.

**Three states, not pass/fail** — `ok` / `check` / `needs_review`. If everything
urgent-flags, nothing gets read. Only exceptions reach a human; clean rows are
never opened. That routing is the product; the extraction is what makes it
possible.

---

## 4. Duplicates are flagged, never deleted

**Concern:** the first CSV reported **$94.60 of supplies against $47.30 of actual
spend** — the same receipt under two filenames. Clients forward things twice
constantly, and a duplicate silently inflates a category total.

**Decision:** detect on vendor + date + total, and **flag**. Two identical
coffees at the same cafe on the same day is a real thing that happens; a tool
that silently drops legitimate rows is worse than one that asks. The summary
reports how much of the headline total is at risk and leaves the call to a human.

---

## 5. Image downscaling — measured, then reverted

**Proposal:** phone photos are 4 MB; downscaling to 1200px cut tokens ~40%.

**Test result:**

| receipt | size | date read | total | tax |
|---|---|---|---|---|
| dry-cleaning docket (clean print) | 1200px | ✅ correct | ✅ | ✅ |
| TK Maxx (crumpled) | 1200px | ❌ **June instead of September** | ✅ | ✅ |
| TK Maxx (crumpled) | 900px | ❌ **2025-01-01** | ✅ | ❌ off by 1c |

**Decision: reverted.** Money amounts survived; dates didn't. A wrong total is
obvious. A receipt filed in the wrong quarter is invisible until a BAS period
fails to reconcile.

The model flagged `date` as guessed every time it got it wrong, so the
validation layer would have caught them — but that means *more rows routed to
manual review*, and operator time is worth far more than the ~$0.008 per receipt
saved. Full resolution retained.

**Related and rejected:** an adaptive scheme — send small, retry at full size
when flagged — needs 70% of receipts to be pristine just to break even, and
saves at most $21.60/year. The pattern is worth remembering for cases where the
price gap is wide; it isn't one here.

---

## 6. Batching: kept, but the benefit was overstated

**Decision:** several images per API call, bounded by count **and** bytes.

**Correction on record:** batching was first measured at ~60% cheaper. That was
on small synthetic test images where the ~1,300-token schema dominated the call.
On real 4 MB photos the image is 79% of the cost, so the true saving is **~13%**
— $1.17 instead of $1.34 for 40 receipts. The 60% figure survives only for
scans, PDFs and POS exports.

**The bytes bound is load-bearing.** Ten 4 MB photos is ~53 MB base64, over the
API's 32 MB request limit. Without it the batch fails, falls back to individual
calls, and costs *more* than not batching. On the first real run of six 4 MB
photos the chunker split them `[3, 3]` on its own.

**Attribution is verified, not assumed.** Ten images in, ten records out —
trusting array order would silently assign a total to the wrong receipt. Each
image is labelled with its filename before it appears, `source_file` is part of
the schema, and the returned set is checked against the sent set. A mismatch
voids the batch rather than writing it.

**Failure isolation preserved.** A failed batch retries one-at-a-time
automatically, so the good receipts still land.

---

## 7. Weekly, not hourly

**Initial position:** hourly, on the grounds that an empty run is free (it is —
the API is never called when there's nothing to process).

**Challenge:** *"what if we run the batch per week instead?"*

**Correct, and it reversed the advice.** Batching only saves money if receipts
have accumulated. Hourly runs each carry ~1 receipt, so the schema is paid 40
times instead of 4 — 45% more expensive for the same work. Weekly → monthly
gains almost nothing further, because once the schema is amortised the images
dominate.

**Decision:** weekly. Receipts are a batch job by nature; nobody is waiting.

---

## 8. Failure handling

- **Emails are marked read only after their attachments are safely on disk.** An
  earlier version set the flag during the fetch loop, so an API or SMTP failure
  lost that submission permanently and silently.
- **`BODY.PEEK[]`, not `RFC822`** — reading a message must not set `\Seen` as a
  side effect.
- **Per-item error isolation**, so one corrupt image can't kill a scheduled run.
- **Auth failures stop the run** rather than burning through 50 files one 401 at
  a time.
- **A missing roster is fatal, not a warning.** Defaulting to "process
  everything" is exactly the failure that bills you for unpaid work.
- **`--since-days 30`** means a missed run is not lost work; the next one catches
  up.

---

## 9. Things deliberately not built

| Option | Saving | Why not |
|---|---|---|
| Batch API (async) | ~50% | ~15¢/year at this volume, against a more complex submit-poll-collect flow |
| Haiku instead of Opus | ~80% | untested accuracy tradeoff; would need a proper comparison against known answers first |
| One call for all N receipts | more | loses per-receipt resume and failure isolation |
| Event-driven (Gmail push) | latency | needs an always-on public endpoint; a laptop isn't one, and receipts aren't urgent |

At 200 receipts a quarter the API costs about **$6**. Against that, operator
review time is the real cost centre — which is why the `ok`/`check`/`needs_review`
split matters more than any pricing lever.

---

## 10. Bugs found, and what they taught

- **Two divergent copies of `summarize()`** drifted until one lost its money
  parser and crashed on `$91.05`, and stopped excluding `Cancelled` rows. The
  modules now import one implementation. Duplicated logic is how a tested
  function silently regresses.
- **A subject regex of `([\d.]+)`** rejected `$175.00` and `$4,182.35`, silently
  ignoring those emails — the worst failure mode available, since nothing errors.
- **`load_dotenv()` does not override an existing OS variable.** A stale
  `ANTHROPIC_API_KEY` in the environment produced a 401 while a correct key sat
  in `.env`. Now `override=True`, and every run prints which key it used.
- **`0.02 > 0.02` was `True`** — `47.30` and `47.32` differ by
  `0.020000000000003` in binary floating point, so a valid receipt was rejected.
  Money comparisons round to cents first.
- **A hand-rolled slug produced `expenses_brian-(test.csv`** because
  `strip("()")` only removes parens at the ends. Replaced with the `slugify()`
  that already had tests. Same lesson as the first item.

Every one of these was caught by a test that costs nothing to run, which is the
argument for writing them before wiring up the schedule rather than after.
