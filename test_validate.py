"""
Tests for Stage 2. No API key, no network, no cost — so there's no excuse
for these being thin.

Each case is a receipt record that Claude could plausibly return, including
the specific ways it gets receipts wrong. The point is to prove your code
catches the model being confidently incorrect.

Run it:
    python test_validate.py
"""

import sys
import datetime

from validate import validate, annotate

TODAY = datetime.date(2026, 8, 20)   # fixed, so these never break with the clock

failures = []


def GOOD(**overrides) -> dict:
    """A clean Australian receipt — the one Stage 1 actually produced.
    Tests override single fields so each case changes exactly one thing."""
    return {
        "vendor": "Bunnings Warehouse",
        "date": "2026-08-14",
        "currency": "AUD",
        "total": 47.30,
        "tax": 4.30,
        "tax_included_in_total": True,
        "subtotal": 43.00,
        "category": "supplies",
        "payment_method": "Visa Credit ****4291",
        "unreadable_fields": [],
    } | overrides


def expect(label: str, record: dict, status: str, must_mention: str | None = None):
    result = validate(record, today=TODAY)
    ok = result.status == status
    detail = ""

    if ok and must_mention:
        blob = " ".join(f"{i.field} {i.message}" for i in result.issues).lower()
        if must_mention.lower() not in blob:
            ok = False
            detail = f" (no issue mentioned {must_mention!r})"

    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"         expected status {status!r}, got {result.status!r}{detail}")
        for issue in result.issues:
            print(f"         - [{issue.severity}] {issue.field}: {issue.message}")
        failures.append(label)


def test_clean():
    print("\nthe receipt Stage 1 actually returned")
    expect("real Bunnings extraction passes clean", GOOD(), "ok")


def test_arithmetic():
    print("\narithmetic — the misread-digit cases")
    # The exact failure this stage exists for: a plausible-looking wrong total.
    expect("total misread 47.30 -> 41.30",
           GOOD(total=41.30), "needs_review", "subtotal + tax")
    expect("subtotal misread 43.00 -> 34.00",
           GOOD(subtotal=34.00), "needs_review", "subtotal + tax")
    expect("tax dropped a digit",
           GOOD(tax=0.43), "needs_review", "subtotal + tax")
    expect("2 cent rounding is tolerated",
           GOOD(total=47.32), "ok")
    expect("10 cent gap is not tolerated",
           GOOD(total=47.40), "needs_review", "subtotal + tax")

    print("\narithmetic — impossible values")
    expect("zero total", GOOD(total=0.0, tax=0.0, subtotal=0.0),
           "needs_review", "must be positive")
    expect("negative total", GOOD(total=-47.30, subtotal=-51.60),
           "needs_review", "must be positive")
    expect("negative tax", GOOD(tax=-4.30, subtotal=51.60),
           "needs_review", "negative")
    expect("tax larger than total", GOOD(tax=50.00, subtotal=-2.70),
           "needs_review", "exceeds total")
    expect("total missing entirely", GOOD(total=None),
           "needs_review", "missing or not a number")
    expect("total as a string", GOOD(total="47.30"),
           "needs_review", "missing or not a number")


def test_gst():
    print("\nGST — the Australian trap this project started with")
    # 47.30 inclusive of 10% GST means GST is 4.30. More is arithmetically
    # impossible; less is normal when some items are GST-free.
    expect("correct AU GST", GOOD(), "ok")
    expect("GST too high is impossible",
           GOOD(tax=6.00, subtotal=41.30), "needs_review", "higher than AUD allows")
    expect("GST too low is only a warning (GST-free items exist)",
           GOOD(tax=2.00, subtotal=45.30), "check", "below")
    expect("zero GST on an AUD receipt is flagged, not failed",
           GOOD(tax=0.0, subtotal=47.30), "check", "tax-free")
    expect("tax-exclusive receipt skips the ratio check",
           GOOD(currency="USD", tax_included_in_total=False,
                total=47.30, subtotal=43.00, tax=4.30), "ok")
    expect("UK VAT at 20% inclusive",
           GOOD(currency="GBP", total=60.00, tax=10.00, subtotal=50.00), "ok")
    expect("UK VAT too high",
           GOOD(currency="GBP", total=60.00, tax=15.00, subtotal=45.00),
           "needs_review", "higher than GBP allows")


def test_dates():
    print("\ndates — day/month swaps and misread years")
    expect("yesterday is fine", GOOD(date="2026-08-19"), "ok")
    expect("today is fine", GOOD(date="2026-08-20"), "ok")
    # 14/08 read as 08/14 gives a date that hasn't happened yet.
    expect("future date from a day/month swap",
           GOOD(date="2026-12-14"), "needs_review", "future")
    expect("year misread 2026 -> 2016",
           GOOD(date="2016-08-14"), "check", "years old")
    expect("non-ISO date", GOOD(date="14/08/2026"),
           "needs_review", "not ISO")
    expect("missing date", GOOD(date=None), "needs_review", "missing")


def test_self_reported():
    print("\nClaude's own uncertainty is free information")
    expect("flagged fields trigger a check",
           GOOD(unreadable_fields=["total", "date"]),
           "check", "flagged these as guessed")
    expect("flagged fields on top of a real error stay needs_review",
           GOOD(total=41.30, unreadable_fields=["total"]), "needs_review")


def test_fields():
    print("\nnon-numeric fields")
    expect("empty vendor", GOOD(vendor=""), "needs_review", "empty")
    expect("vendor 'Unknown'", GOOD(vendor="Unknown"), "check", "wasn't readable")
    expect("missing currency", GOOD(currency=""), "needs_review", "missing")
    expect("nonsense currency", GOOD(currency="XYZ"), "check", "recognise")
    expect("category 'other' wants manual coding",
           GOOD(category="other"), "check", "manual coding")


def test_annotate():
    print("\nannotate() attaches the verdict for saving")
    out = annotate(GOOD(total=41.30), today=TODAY)
    checks = [
        ("keeps the original fields", out["vendor"] == "Bunnings Warehouse"),
        ("adds a status", out["status"] == "needs_review"),
        ("issues are readable strings", isinstance(out["issues"][0], str)),
        ("issue text names the problem", "subtotal + tax" in out["issues"][0]),
    ]
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            failures.append(label)

    clean = annotate(GOOD(), today=TODAY)
    ok = clean["status"] == "ok" and clean["issues"] == []
    print(f"  [{'PASS' if ok else 'FAIL'}] a clean receipt gets status ok, no issues")
    if not ok:
        failures.append("clean annotate")


def main() -> int:
    print("=" * 58)
    print("Stage 2 — validation tests (no API, no cost)")
    print("=" * 58)

    test_clean()
    test_arithmetic()
    test_gst()
    test_dates()
    test_self_reported()
    test_fields()
    test_annotate()

    print("\n" + "=" * 58)
    if failures:
        print(f"{len(failures)} FAILED:")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("All validation tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
