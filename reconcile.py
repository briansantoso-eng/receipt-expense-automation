"""
POS Reconciliation Assistant — manual, one-command-at-a-time version
----------------------------------------------------------------------
Reads a POS sales export (CSV, downloaded by the client) and compares it
against the actual bank deposit total. Claude explains the gap in plain
English. You review the output before it goes to the client.

This is the hands-on tool. For the unattended version that watches your
inbox on a schedule, use check_and_reconcile.py.

Run it:
    python reconcile.py reconciliation_workbook.csv 76825.29
    python reconcile.py reconciliation_workbook.csv 76825.29 --offline

The second argument is the actual bank deposit for that period. --offline
prints the totals and skips the Claude call (no API key needed).

The arithmetic and the API call are imported from check_and_reconcile rather
than copied — two divergent copies of summarize() is how the "$91.05 is not a
float" bug got in.
"""

import csv
import sys

from check_and_reconcile import MODEL, ask_claude_to_explain, fmt_money, summarize


def load_transactions(csv_path: str) -> list[dict]:
    # utf-8-sig because an Excel-exported CSV usually starts with a BOM, which
    # would otherwise end up glued to the first column name.
    with open(csv_path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    offline = "--offline" in sys.argv[1:]

    if len(args) != 2:
        print("Usage: python reconcile.py <csv_file> <bank_deposit_amount> [--offline]")
        return 1

    csv_path, deposit_arg = args
    try:
        bank_deposit = float(deposit_arg.replace("$", "").replace(",", ""))
    except ValueError:
        print(f"Not a valid deposit amount: {deposit_arg!r}")
        return 1

    transactions = load_transactions(csv_path)
    if not transactions:
        print(f"No data rows found in {csv_path}")
        return 1

    totals = summarize(transactions)
    gap = round(totals["net_expected"] - bank_deposit, 2)

    print("=" * 60)
    print("RECONCILIATION SUMMARY (review before sending to client)")
    print("=" * 60)
    print(f"Transactions read:    {len(transactions):,}")
    print(f"Gross sales:          {fmt_money(totals['sales'])}")
    print(f"Refunds:              {fmt_money(totals['refunds'])}")
    print(f"Card fees:            {fmt_money(totals['fees'])}")
    print(f"Voided/cancelled:     {totals['voided_count']} transaction(s), excluded")
    print(f"Net expected:         {fmt_money(totals['net_expected'])}")
    print(f"Actual bank deposit:  {fmt_money(bank_deposit)}")
    print(f"Gap:                  {fmt_money(gap)}")
    print()

    if offline:
        print("(--offline: skipped the Claude call, so there's no explanation here.)")
        return 0

    # Everything above this line was free, local, and exact. Everything below
    # is the single API call. The markers are here so you can see the boundary.
    print("-" * 60)
    print(">> STEP 5: sending those totals to the Claude API now...")
    print(f"   (one request to api.anthropic.com, model {MODEL})")
    print("-" * 60)

    try:
        explanation = ask_claude_to_explain(totals, bank_deposit, transactions)
    except Exception as exc:
        # Print the totals regardless — they're the part that matters, and they
        # were computed locally without the API.
        print(f"\n   [the API call failed: {type(exc).__name__}: {exc}]")
        print("   The totals above are still correct — they never touched the API.")
        return 1

    print("\n>> STEP 6: Claude's reply (this is the only text Claude wrote):\n")
    print(explanation)
    print("\n" + "-" * 60)
    print("Done. In the automated version this paragraph gets emailed to you")
    print("as a draft, instead of printed here.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
