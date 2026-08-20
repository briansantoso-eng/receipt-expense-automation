"""
STAGE 1 — one receipt, one API call, structured data out.
------------------------------------------------------------
Reads a receipt (photo, screenshot or PDF) and returns typed fields you could
drop straight into a spreadsheet. No prose, no paragraph — data.

Run it:
    python make_sample_receipt.py          # creates a test receipt first
    python extract_receipt.py samples/receipt_bunnings.png
    python extract_receipt.py my_receipt.pdf --json

This is the whole point of the project, so it's worth reading the file:

  * The Receipt class below IS the contract. You describe the shape you want,
    the API guarantees you get exactly that shape back. You never parse text,
    never regex a total out of a paragraph, never handle "Sure! Here's the
    JSON:" preambles. That guarantee is what makes this a system instead of
    a chatbot.

  * There is exactly ONE API call, marked below. Everything else is local.

  * Claude reads the receipt. Claude does NOT do the arithmetic — Stage 2
    checks its numbers in Python, because a model misreading 47.30 as 41.30
    is a thing that happens and a thing your code can catch.
"""

import os
import sys
import json
import base64
import argparse
import datetime
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

import anthropic

from validate import annotate, validate

try:
    from dotenv import load_dotenv
    # override=True matters. Without it, a stale ANTHROPIC_API_KEY already
    # present in the OS environment silently wins over the one in .env, and
    # you get a 401 while staring at a .env file containing a perfectly good
    # key. The project's .env is the intended source of truth, so it wins.
    load_dotenv(override=True)
except ImportError:
    pass

MODEL = "claude-opus-5"

# Per-million-token rates for MODEL, used only to print what the run cost.
COST_IN, COST_OUT = 5.00, 25.00

MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}

# Fixed list so the output lands in the same buckets every time. A free-text
# category would give you "Office Supplies", "office supplies" and "Stationery"
# for the same thing, and your spreadsheet would be useless for totals.
Category = Literal[
    "supplies", "meals_entertainment", "travel", "software_subscriptions",
    "utilities", "professional_services", "equipment", "fuel_vehicle",
    "rent_facilities", "other",
]


class Receipt(BaseModel):
    """The shape you want back. Every description below is read by the model —
    they are instructions, not comments, so it's worth writing them carefully.
    Most extraction bugs are fixed here rather than in the prompt."""

    vendor: str = Field(
        description="Business name as printed, e.g. 'Bunnings Warehouse'.")
    # Annotated as datetime.date rather than a bare `date`, because a field
    # named the same as its own type confuses Pydantic.
    date: datetime.date = Field(
        description="Transaction date in ISO format. Watch out: DD/MM/YYYY is "
                    "used in Australia and the UK, MM/DD/YYYY in the US. Use "
                    "other clues on the receipt (address, currency, phone "
                    "number format) to decide which it is.")
    currency: str = Field(
        description="Three-letter ISO code, e.g. AUD, USD, GBP.")
    total: float = Field(
        description="The final amount actually charged.")
    tax: float = Field(
        description="Tax/GST/VAT amount shown. Use 0 if none is shown.")
    tax_included_in_total: bool = Field(
        description="True if the tax was ALREADY inside the total (normal for "
                    "Australian GST, UK/EU VAT — receipts say 'total includes "
                    "GST of X'). False if tax was added on top of the subtotal "
                    "(typical US sales tax).")
    subtotal: float = Field(
        description="The amount excluding tax. If tax is included in the "
                    "total, this is total minus tax — not necessarily the "
                    "number printed beside the word SUBTOTAL, which on some "
                    "receipts still includes tax.")
    category: Category = Field(
        description="Best-fit bookkeeping category based on what was bought.")
    payment_method: str = Field(
        description="How it was paid, e.g. 'Visa ****4291', 'cash', 'Amex'. "
                    "Use 'unknown' if the receipt doesn't say.")
    unreadable_fields: list[str] = Field(
        description="Names of any fields above that you had to guess because "
                    "the receipt was blurry, cut off or ambiguous. Empty list "
                    "if everything was clearly legible. Be honest here — a "
                    "human reviews anything you flag, and a wrong number that "
                    "wasn't flagged is far more costly than a flagged guess.")


def build_content_block(path: str) -> dict:
    """PDFs go in as a 'document' block, images as an 'image' block. Same
    base64 payload either way — only the wrapper differs."""
    extension = os.path.splitext(path)[1].lower()
    if extension not in MEDIA_TYPES:
        raise SystemExit(
            f"Don't know how to read '{extension}' files.\n"
            f"Supported: {', '.join(sorted(MEDIA_TYPES))}")

    with open(path, "rb") as handle:
        payload = base64.standard_b64encode(handle.read()).decode("utf-8")

    media_type = MEDIA_TYPES[extension]
    kind = "document" if media_type == "application/pdf" else "image"
    return {
        "type": kind,
        "source": {"type": "base64", "media_type": media_type, "data": payload},
    }


def check_key() -> str:
    """Fail early and legibly on a missing or obviously-wrong key, and report
    a fingerprint so a stale key is never a mystery. Prints no secret."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")

    if not key:
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set.\n"
            "Put a real key in .env (see env.example) and try again.")

    if key == "sk-ant-your-actual-key-here":
        raise SystemExit(
            "ANTHROPIC_API_KEY is still the placeholder from env.example.\n"
            "Open .env, paste your real key over that line, and SAVE the file.")

    if not key.startswith("sk-ant-"):
        raise SystemExit(
            f"ANTHROPIC_API_KEY doesn't look like a key (starts {key[:6]!r}).\n"
            "Keys begin with 'sk-ant-'. Check for stray quotes or a copy slip.")

    return f"{key[:11]}...{key[-4:]} ({len(key)} chars)"


def extract(path: str) -> tuple[Receipt, object]:
    """The one and only API call. Returns the validated receipt plus the raw
    response so the caller can report tokens and cost."""
    client = anthropic.Anthropic()

    # ---------------------------------------------------------------- API CALL
    response = client.messages.parse(
        model=MODEL,
        max_tokens=4000,
        output_config={"effort": "medium"},
        output_format=Receipt,   # <- the schema above becomes a hard guarantee
        messages=[{
            "role": "user",
            "content": [
                build_content_block(path),
                {"type": "text", "text":
                    "Extract the fields from this receipt. Read only what is "
                    "actually printed — do not infer or invent a value that "
                    "isn't there. If a field is unclear, give your best read "
                    "and name it in unreadable_fields."},
            ],
        }],
    )
    # ------------------------------------------------------------ END API CALL

    return response.parsed_output, response



# ── Batched extraction ────────────────────────────────────────────────
# The Receipt schema costs ~1,289 tokens and is charged once per CALL, not
# once per receipt, so it dominates a single-receipt request (64% of it).
# Sending N images in one call amortises it: 10 receipts drop from 19,870
# tokens to 8,017 — about 60% cheaper.
#
# The three things that could go wrong, and how each is handled:
#
#   1. ATTRIBUTION. Ten images in, ten records out — trusting array order to
#      map results back to files would silently mis-assign a total to the
#      wrong receipt. Instead each image is labelled with its filename in the
#      request, source_file is part of the schema, and the returned set is
#      verified against the input set before anything is written.
#
#   2. ERROR ISOLATION. A batch that fails takes all N with it. So a failed
#      batch automatically falls back to one-at-a-time calls for that group,
#      which costs more for that group only and still lands the good ones.
#
#   3. RESUME. Unchanged. Results are still written per receipt to
#      output/<name>.json, so the skip-on-restart logic works exactly as
#      before. Only the API call is batched, not the bookkeeping.

DEFAULT_BATCH_SIZE = 10


class BatchedReceipt(Receipt):
    """A Receipt that also carries the filename it came from, so results can
    be matched to inputs by name rather than by position."""
    source_file: str = Field(
        description="The exact filename given in the label immediately before "
                    "this receipt's image. Copy it verbatim.")


class ReceiptBatch(BaseModel):
    """One record per image supplied, in any order — they're matched by name."""
    receipts: list[BatchedReceipt] = Field(
        description="Exactly one entry per receipt image provided. Do not merge "
                    "receipts, do not skip any, and do not invent extras.")


def extract_batch(paths: list[str]) -> tuple[dict[str, Receipt], object]:
    """Extract several receipts in one API call.

    Returns ({filename: Receipt}, response). Raises ValueError if the model's
    output doesn't account for exactly the files that were sent — the caller
    is expected to fall back to individual calls in that case."""
    if not paths:
        return {}, None

    content = []
    for index, path in enumerate(paths, 1):
        name = os.path.basename(path)
        # Label BEFORE the image: the model reads in order, so the filename
        # is established before it sees what it applies to.
        content.append({"type": "text",
                        "text": f"--- Receipt {index} of {len(paths)}, "
                                f"source_file: {name} ---"})
        content.append(build_content_block(path))

    content.append({"type": "text", "text":
        f"Extract the fields from each of the {len(paths)} receipts above. "
        f"Return exactly {len(paths)} records, one per receipt, and set "
        f"source_file on each to the filename from its label. Read only what "
        f"is actually printed on each receipt — do not carry a value from one "
        f"receipt over to another, and do not invent values that aren't there. "
        f"If a field is unclear, give your best read and name it in "
        f"unreadable_fields."})

    client = anthropic.Anthropic()
    response = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "medium"},
        output_format=ReceiptBatch,
        messages=[{"role": "user", "content": content}],
    )

    if response.stop_reason == "refusal":
        raise ValueError("Claude declined the batch request")

    returned = {r.source_file: r for r in response.parsed_output.receipts}
    expected = {os.path.basename(p) for p in paths}

    # Verify attribution before trusting any of it. A mismatch means we cannot
    # say which number belongs to which receipt, so the whole batch is void.
    missing = expected - returned.keys()
    extra = returned.keys() - expected
    if missing or extra:
        raise ValueError(
            f"batch attribution mismatch — missing {sorted(missing)}, "
            f"unexpected {sorted(extra)}")
    if len(response.parsed_output.receipts) != len(paths):
        raise ValueError(
            f"expected {len(paths)} records, got "
            f"{len(response.parsed_output.receipts)}")

    return returned, response


def report(receipt: Receipt, response) -> None:
    """Print the result, plus the one arithmetic check that Stage 2 will make
    the script's actual job."""
    print()
    print("=" * 58)
    print(f"EXTRACTED FROM RECEIPT")
    print("=" * 58)
    print(f"  Vendor          {receipt.vendor}")
    print(f"  Date            {receipt.date}")
    print(f"  Category        {receipt.category}")
    print(f"  Payment         {receipt.payment_method}")
    print()
    print(f"  Subtotal        {receipt.currency} {receipt.subtotal:,.2f}")
    print(f"  Tax             {receipt.currency} {receipt.tax:,.2f}"
          f"  ({'included in' if receipt.tax_included_in_total else 'added to'} total)")
    print(f"  TOTAL           {receipt.currency} {receipt.total:,.2f}")

    if receipt.unreadable_fields:
        print()
        print(f"  !! Claude flagged as guessed: "
              f"{', '.join(receipt.unreadable_fields)}")
        print(f"     A human should check those before this row is trusted.")

    # Stage 2. Python checks the model's work — see validate.py.
    print()
    print("-" * 58)
    result = validate(receipt.model_dump(mode="json"))
    verdict = {
        "ok": "OK — every check passed, safe to use",
        "check": "CHECK — usable, but worth a glance",
        "needs_review": "NEEDS REVIEW — do not trust this row yet",
    }[result.status]
    print(f"  Validation: {verdict}")
    for issue in result.issues:
        print(f"    [{issue.severity}] {issue.field}: {issue.message}")

    usage = response.usage
    cost = (usage.input_tokens * COST_IN + usage.output_tokens * COST_OUT) / 1_000_000
    print("-" * 58)
    print(f"  {usage.input_tokens:,} in / {usage.output_tokens:,} out tokens"
          f"  =  ${cost:.4f} for this receipt")
    print(f"  (about ${cost * 100:.2f} per 100 receipts)")
    print()


def save_result(receipt: Receipt, source_path: str, out_path: str | None) -> str:
    """Write the extraction to disk. Every run saves by default, because a
    result that only exists in terminal scrollback isn't a result — and
    Stage 3 reads these files to build the spreadsheet."""
    if out_path is None:
        os.makedirs("output", exist_ok=True)
        stem = os.path.splitext(os.path.basename(source_path))[0]
        out_path = os.path.join("output", f"{stem}.json")
    else:
        parent = os.path.dirname(out_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    # annotate() adds "status" and "issues" so the saved file records the
    # verdict, not just the extraction. Stage 3 reads that status to decide
    # which rows go straight to the spreadsheet and which get held back.
    record = annotate(receipt.model_dump(mode="json"))
    # Keep the source filename in the record: when Stage 5 flags a row for
    # review, the first thing you want is the original image to look at.
    record["source_file"] = os.path.basename(source_path)

    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, ensure_ascii=False)

    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract structured fields from one receipt image or PDF.")
    parser.add_argument("receipt", help="path to a .png/.jpg/.pdf receipt")
    parser.add_argument("--json", action="store_true",
                        help="print raw JSON only (for piping into Stage 3)")
    parser.add_argument("--out", metavar="PATH",
                        help="where to save the result "
                             "(default: output/<receipt-name>.json)")
    parser.add_argument("--no-save", action="store_true",
                        help="print only, write nothing to disk")
    args = parser.parse_args()

    if not os.path.exists(args.receipt):
        raise SystemExit(f"No such file: {args.receipt}")

    print(f"Using API key {check_key()}")

    try:
        receipt, response = extract(args.receipt)
    except ValidationError as exc:
        # The model returned something that didn't fit the schema. Rare with
        # structured outputs, but this is where you'd see it.
        print(f"Response did not match the Receipt schema:\n{exc}",
              file=sys.stderr)
        return 1
    except anthropic.APIStatusError as exc:
        print(f"API error {exc.status_code}: {exc.message}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(receipt.model_dump(mode="json"), indent=2))
    else:
        report(receipt, response)

    if not args.no_save:
        saved_to = save_result(receipt, args.receipt, args.out)
        print(f"  Saved to {saved_to}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
