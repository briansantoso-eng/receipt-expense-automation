"""
STAGE 2 — don't trust the extraction.
----------------------------------------
Stage 1 believes whatever Claude says. If it reads 47.30 as 41.30, the number
goes into a client's books and nobody notices. This module checks the numbers
in Python AFTER the API call and routes anything suspicious to a human.

Run it on anything Stage 1 saved:
    python validate.py output/receipt_bunnings.json
    python validate.py output/*.json
    python validate.py --self-test        # deliberately broken records

Design notes worth reading:

  * It works on a plain dict, not the Pydantic model, so it can validate any
    JSON file on disk without importing Stage 1 or touching the API. That also
    means it's free and instant to run, which is why the tests are exhaustive.

  * NOTHING here calls Claude. Checking arithmetic is a rule you can write
    down, so it belongs in code. That's the whole Python-vs-Claude split.

  * Severity matters. An impossible number is an "error". A merely unusual one
    is a "warning". Flagging everything as urgent means nothing gets read.
"""

import os
import json
import glob
import argparse
import datetime
from dataclasses import dataclass
from typing import Literal

# Rounding slack. Receipts round to the cent, so anything inside 2c is noise.
MONEY_TOLERANCE = 0.02

# Tax regimes where the headline price already includes the tax, and the tax
# is a fixed fraction of the gross total.
#   Australia: 10% GST -> GST is total/11 of a GST-inclusive price
#   New Zealand: 15% GST -> total/7.666...
#   UK/EU: 20% VAT -> total/6
INCLUSIVE_TAX_DIVISOR = {"AUD": 11.0, "NZD": 7.666667, "GBP": 6.0, "EUR": 6.0}

KNOWN_CURRENCIES = {
    "AUD", "NZD", "USD", "GBP", "EUR", "CAD", "SGD", "JPY", "CNY", "HKD",
    "INR", "IDR", "MYR", "PHP", "THB", "KRW", "CHF", "SEK", "NOK", "DKK",
}

# Receipts older than this are outside normal record-keeping and usually mean
# a misread year (2026 read as 2020) rather than a genuinely ancient receipt.
MAX_AGE_YEARS = 7


@dataclass
class Issue:
    severity: Literal["error", "warning"]
    field: str
    message: str


@dataclass
class Result:
    status: Literal["ok", "check", "needs_review"]
    issues: list[Issue]

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]


def over_tolerance(difference: float, tolerance: float = MONEY_TOLERANCE) -> bool:
    """Is this gap bigger than rounding noise?

    The round() is load-bearing. 47.30 and 47.32 differ by exactly 2 cents, but
    in binary floating point the subtraction gives 0.020000000000003, which is
    greater than 0.02 — so a plain `>` rejects a receipt that should pass.
    Rounding to cents before comparing is the fix. This is the same class of
    problem as never storing money in floats at all; at this scale rounding at
    the comparison is enough."""
    return round(abs(difference), 2) > tolerance


def _money(record: dict, key: str) -> float | None:
    """Pull a numeric field, tolerating the field being absent or non-numeric
    rather than raising — a malformed record should produce an issue, not a
    crash, or one bad file kills a whole batch in Stage 3."""
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def check_arithmetic(record: dict) -> list[Issue]:
    """subtotal + tax must equal total. This holds regardless of whether tax
    was included in the headline price, because the schema defines subtotal as
    always excluding tax."""
    issues = []
    subtotal = _money(record, "subtotal")
    tax = _money(record, "tax")
    total = _money(record, "total")

    for name, value in (("subtotal", subtotal), ("tax", tax), ("total", total)):
        if value is None:
            issues.append(Issue("error", name, f"{name} is missing or not a number"))

    if None in (subtotal, tax, total):
        return issues

    expected = round(subtotal + tax, 2)
    if over_tolerance(expected - total):
        issues.append(Issue(
            "error", "total",
            f"subtotal + tax = {expected:.2f} but total = {total:.2f} "
            f"(off by {abs(expected - total):.2f})"))

    if total <= 0:
        issues.append(Issue("error", "total", f"total is {total:.2f}, must be positive"))
    if tax < 0:
        issues.append(Issue("error", "tax", f"tax is negative ({tax:.2f})"))
    if subtotal < 0:
        issues.append(Issue("error", "subtotal",
                            f"subtotal is negative ({subtotal:.2f})"))
    if total > 0 and tax > total:
        issues.append(Issue("error", "tax",
                            f"tax ({tax:.2f}) exceeds total ({total:.2f})"))

    return issues


def check_tax_rate(record: dict) -> list[Issue]:
    """For inclusive-tax countries the tax is a known fraction of the total.

    The asymmetry here is deliberate and is the sort of thing that separates a
    real tool from a demo: tax HIGHER than the expected fraction is impossible
    and therefore an error, but tax LOWER is perfectly normal, because
    GST-free items (fresh food, medical) sit in the same basket as taxed ones.
    Flagging every mixed-basket grocery receipt as broken would train you to
    ignore the flags."""
    issues = []
    currency = (record.get("currency") or "").upper()
    divisor = INCLUSIVE_TAX_DIVISOR.get(currency)
    tax = _money(record, "tax")
    total = _money(record, "total")

    if divisor is None or tax is None or total is None or total <= 0:
        return issues
    if not record.get("tax_included_in_total"):
        return issues
    if tax == 0:
        # Entirely tax-free purchase is plausible; worth a look, not an error.
        issues.append(Issue(
            "warning", "tax",
            f"tax is 0.00 on a {currency} receipt — possible if everything was "
            f"tax-free, but check it wasn't just missed"))
        return issues

    expected = total / divisor
    if tax - expected > MONEY_TOLERANCE and over_tolerance(tax - expected):
        issues.append(Issue(
            "error", "tax",
            f"tax {tax:.2f} is higher than {currency} allows on a total of "
            f"{total:.2f} (max {expected:.2f})"))
    elif expected - tax > MONEY_TOLERANCE and over_tolerance(expected - tax):
        issues.append(Issue(
            "warning", "tax",
            f"tax {tax:.2f} is below the {expected:.2f} expected for "
            f"{currency} — normal if some items were tax-free"))

    return issues


def check_date(record: dict, today: datetime.date | None = None) -> list[Issue]:
    """A date is injectable so the tests don't break when the clock moves."""
    issues = []
    today = today or datetime.date.today()
    raw = record.get("date")

    if not raw:
        issues.append(Issue("error", "date", "date is missing"))
        return issues

    try:
        value = datetime.date.fromisoformat(str(raw))
    except ValueError:
        issues.append(Issue("error", "date", f"date {raw!r} is not ISO format"))
        return issues

    if value > today:
        issues.append(Issue(
            "error", "date",
            f"date {value} is in the future (today is {today}) — most likely a "
            f"day/month swap"))
    elif value < today.replace(year=today.year - MAX_AGE_YEARS):
        issues.append(Issue(
            "warning", "date",
            f"date {value} is more than {MAX_AGE_YEARS} years old — check the "
            f"year wasn't misread"))

    return issues


def check_self_reported(record: dict) -> list[Issue]:
    """Claude was asked to name any field it had to guess. Free information —
    use it."""
    flagged = record.get("unreadable_fields") or []
    if not isinstance(flagged, list) or not flagged:
        return []
    return [Issue("warning", ", ".join(str(f) for f in flagged),
                  "Claude flagged these as guessed from an unclear receipt")]


def check_fields(record: dict) -> list[Issue]:
    """Cheap sanity checks on the non-numeric fields."""
    issues = []

    vendor = (record.get("vendor") or "").strip()
    if not vendor:
        issues.append(Issue("error", "vendor", "vendor is empty"))
    elif vendor.lower() in {"unknown", "n/a", "none", "unclear"}:
        issues.append(Issue("warning", "vendor",
                            f"vendor is {vendor!r} — the name wasn't readable"))

    currency = (record.get("currency") or "").strip().upper()
    if not currency:
        issues.append(Issue("error", "currency", "currency is missing"))
    elif currency not in KNOWN_CURRENCIES:
        issues.append(Issue("warning", "currency",
                            f"{currency!r} isn't a currency code I recognise"))

    if (record.get("category") or "") == "other":
        issues.append(Issue("warning", "category",
                            "category is 'other' — may need manual coding"))

    return issues


def validate(record: dict, today: datetime.date | None = None) -> Result:
    """Run every check. Returns a status plus the reasons behind it.

    ok            -> write it to the spreadsheet, nobody needs to look
    check         -> write it, but mention it in the weekly summary
    needs_review  -> do NOT trust this row until a human has seen it
    """
    issues = []
    issues += check_arithmetic(record)
    issues += check_tax_rate(record)
    issues += check_date(record, today)
    issues += check_self_reported(record)
    issues += check_fields(record)

    if any(i.severity == "error" for i in issues):
        status = "needs_review"
    elif issues:
        status = "check"
    else:
        status = "ok"

    return Result(status=status, issues=issues)


def annotate(record: dict, today: datetime.date | None = None) -> dict:
    """Return the record with the verdict attached, ready to save."""
    result = validate(record, today)
    return {
        **record,
        "status": result.status,
        "issues": [f"[{i.severity}] {i.field}: {i.message}" for i in result.issues],
    }


ICON = {"ok": "OK  ", "check": "CHECK", "needs_review": "REVIEW"}


def print_result(label: str, record: dict, result: Result) -> None:
    print(f"{ICON[result.status]:<7} {label}")
    print(f"        {record.get('vendor', '?')} — "
          f"{record.get('currency', '')} {record.get('total', '?')} on "
          f"{record.get('date', '?')}")
    for issue in result.issues:
        print(f"        [{issue.severity}] {issue.field}: {issue.message}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate receipt JSON saved by extract_receipt.py.")
    parser.add_argument("files", nargs="*",
                        help="JSON files to check (default: output/*.json)")
    parser.add_argument("--write", action="store_true",
                        help="save the status and issues back into each file")
    args = parser.parse_args(argv)

    paths = args.files or sorted(glob.glob(os.path.join("output", "*.json")))
    if not paths:
        raise SystemExit(
            "Nothing to validate. Run extract_receipt.py first, or pass a path.")

    counts = {"ok": 0, "check": 0, "needs_review": 0}

    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"SKIP    {path}: {exc}\n")
            continue

        result = validate(record)
        counts[result.status] += 1
        print_result(path, record, result)

        if args.write:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(annotate(record), handle, indent=2, ensure_ascii=False)

    print("-" * 58)
    print(f"{len(paths)} file(s): {counts['ok']} ok, {counts['check']} to check, "
          f"{counts['needs_review']} needing review")

    # Non-zero exit if anything needs a human, so a scheduled run can react.
    return 1 if counts["needs_review"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
