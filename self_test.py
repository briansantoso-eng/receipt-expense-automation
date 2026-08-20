"""
Offline checks for check_and_reconcile.py — no API key, no Gmail, no network.

Covers the things that must be right before any live run: the billing gate,
subject parsing, CSV-out-of-MIME extraction, and the arithmetic. The maths is
cross-checked against a second, independent implementation using Decimal, so a
shared rounding mistake can't pass both.

Run it:
    python self_test.py
"""

import csv
import sys
from decimal import Decimal
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import Header
from email import encoders, message_from_bytes

import check_and_reconcile as app

SAMPLE_CSV = "reconciliation_workbook.csv"

failures = []


def check(label: str, got, expected):
    """Record one assertion. Prints inline so a failure is readable in place."""
    ok = got == expected
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"         expected {expected!r}")
        print(f"         got      {got!r}")
        failures.append(label)


def read_sample() -> list[dict]:
    """Read the sample export the same way the app does."""
    with open(SAMPLE_CSV, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def build_email(subject: str, csv_path: str | None = SAMPLE_CSV,
                filename: str = "july_sales.csv",
                sender: str = "owner@joescafe.example") -> bytes:
    """Build a realistic multipart email the way a mail client would."""
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = "consultant@example.com"
    msg.attach(MIMEText("Hi, here's last month's export. Thanks!", "plain"))

    if csv_path is not None:
        with open(csv_path, "rb") as handle:
            part = MIMEBase("text", "csv")
            part.set_payload(handle.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)

    return msg.as_bytes()


ROSTER = {
    "owner@joescafe.example": {"email": "owner@joescafe.example",
                               "name": "Joe's Cafe", "status": "active"},
    "hello@baybakery.example": {"email": "hello@baybakery.example",
                                "name": "Bay Bakery", "status": "unpaid"},
    "manager@cornerstore.example": {"email": "manager@cornerstore.example",
                                    "name": "Corner Store", "status": "paused"},
    "no@status.example": {"email": "no@status.example", "name": "Blank Status"},
}


def manual_totals(rows: list[dict]) -> dict:
    """Independent re-implementation using Decimal — deliberately does not
    share code with summarize(), so it catches errors in it rather than
    reproducing them."""
    sales = refunds = fees = Decimal("0")
    voided = 0

    for row in rows:
        status = row["Status"].strip().lower()
        if status in ("voided", "cancelled", "canceled"):
            voided += 1
            continue

        raw = row["Amount"].strip().replace("$", "").replace(",", "")
        negative = raw.startswith("(") and raw.endswith(")")
        amount = Decimal(raw.strip("()")) * (-1 if negative else 1)

        if row["Type"].strip().lower() == "refund":
            refunds += amount
        else:
            sales += amount

        fees += Decimal(row["Fee"].strip().replace("$", "").replace(",", ""))

    return {
        "sales": float(round(sales, 2)),
        "refunds": float(round(refunds, 2)),
        "fees": float(round(fees, 2)),
        "voided_count": voided,
        "net_expected": float(round(sales + refunds - fees, 2)),
    }


def test_billing_gate():
    """The gate is the only thing between a stranger's email and your API
    bill, so it gets the most cases."""
    print("\nbilling gate — who is allowed to reach the API")
    check("active client is billable",
          app.check_gate("owner@joescafe.example", ROSTER)[0], True)
    check("unpaid client is blocked",
          app.check_gate("hello@baybakery.example", ROSTER)[0], False)
    check("paused client is blocked",
          app.check_gate("manager@cornerstore.example", ROSTER)[0], False)
    check("roster entry with no status is blocked",
          app.check_gate("no@status.example", ROSTER)[0], False)
    check("stranger is blocked",
          app.check_gate("spammer@example.com", ROSTER)[0], False)
    check("empty sender is blocked", app.check_gate("", ROSTER)[0], False)
    check("None sender is blocked", app.check_gate(None, ROSTER)[0], False)
    check("empty roster blocks everyone", app.check_gate("anyone@x.com", {})[0], False)

    print("\nbilling gate — forgiving about how the address arrives")
    check("uppercased address still matches",
          app.check_gate("Owner@JoesCafe.Example", ROSTER)[0], True)
    check("whitespace-padded address still matches",
          app.check_gate("  owner@joescafe.example  ", ROSTER)[0], True)

    print("\nbilling gate — reports why, not just yes/no")
    check("unpaid reason names the status",
          app.check_gate("hello@baybakery.example", ROSTER)[1], "status is 'unpaid'")
    check("stranger reason is explicit",
          app.check_gate("spammer@example.com", ROSTER)[1],
          "sender is not on the roster")
    check("blank status is called out",
          app.check_gate("no@status.example", ROSTER)[1],
          "roster entry has no status set")

    print("\nbilling gate — the subject line cannot bypass it")
    # The client name in the subject is typed by the client. The gate reads the
    # From address only, so an unpaid client can't claim to be a paid one.
    forged = app.parse_submission(message_from_bytes(build_email(
        "Reconciliation: Joe's Cafe | Bank deposit: $175.00",
        sender="hello@baybakery.example")))
    check("subject naming an active client does not open the gate",
          app.check_gate(forged["sender"], ROSTER)[0], False)


def test_wildcard_matching():
    """Domain wildcards are a real requirement — a client business often has
    several staff who send receipts, and chasing each address individually
    doesn't scale. The catch-all is testing-only and must announce itself."""
    print("\nroster matching — exact, domain wildcard, catch-all")
    registry = {
        "owner@joescafe.example": {"email": "owner@joescafe.example",
                                   "name": "Joe exact", "status": "active"},
        "*@baybakery.example": {"email": "*@baybakery.example",
                                "name": "Bay (whole domain)", "status": "active"},
        "*@lapsed.example": {"email": "*@lapsed.example",
                             "name": "Lapsed Co", "status": "unpaid"},
    }
    check("exact address matches",
          app.check_gate("owner@joescafe.example", registry)[0], True)
    check("any address at a wildcarded domain matches",
          app.check_gate("chef@baybakery.example", registry)[0], True)
    check("wildcard domain is case-insensitive",
          app.check_gate("MANAGER@BayBakery.Example", registry)[0], True)
    check("wildcard domain still respects unpaid status",
          app.check_gate("anyone@lapsed.example", registry)[0], False)
    check("a different domain does not match",
          app.check_gate("someone@other.example", registry)[0], False)
    check("empty sender never matches a wildcard",
          app.check_gate("", registry)[0], False)

    print("\nroster matching — exact wins over the domain wildcard")
    both = dict(registry)
    both["boss@baybakery.example"] = {"email": "boss@baybakery.example",
                                      "name": "Bay boss", "status": "unpaid"}
    entry, how = app.match_roster("boss@baybakery.example", both)
    check("the specific entry is chosen, not the domain one",
          entry.get("name"), "Bay boss")
    check("and it is reported as an exact match", how, "exact address")
    check("so its unpaid status wins",
          app.check_gate("boss@baybakery.example", both)[0], False)

    print("\nroster matching — the catch-all announces itself")
    catch = dict(registry)
    catch["*"] = {"email": "*", "name": "TEST", "status": "active"}
    allowed, reason = app.check_gate("random@spam.example", catch)
    check("catch-all lets an unknown sender through", allowed, True)
    check("and the reason says so loudly", "CATCH-ALL" in reason, True)
    check("exact entries still report as exact",
          app.check_gate("owner@joescafe.example", catch)[1], "active")
    check("catch-all does not override an unpaid entry",
          app.check_gate("anyone@lapsed.example", catch)[0], False)


def test_sender_extraction():
    print("\nsender extraction from the From header")
    cases = [
        ("owner@joescafe.example", "owner@joescafe.example"),
        ("Joe <owner@joescafe.example>", "owner@joescafe.example"),
        ('"Joe, Owner" <Owner@JoesCafe.Example>', "owner@joescafe.example"),
    ]
    for raw, expected in cases:
        sub = app.parse_submission(message_from_bytes(build_email(
            "Reconciliation: Joe's Cafe | Bank deposit: $175.00", sender=raw)))
        check(f"From: {raw}", sub["sender"] if sub else None, expected)


def test_parse_money():
    print("\nparse_money — the formats a POS export actually emits")
    check("plain number", app.parse_money("91.05"), 91.05)
    check("dollar sign", app.parse_money("$91.05"), 91.05)
    check("thousands separator", app.parse_money("$4,182.35"), 4182.35)
    check("accounting negative", app.parse_money("($35.42)"), -35.42)
    check("minus negative", app.parse_money("-35.42"), -35.42)
    check("empty cell", app.parse_money(""), 0.0)
    check("missing column", app.parse_money(None), 0.0)
    check("whitespace padded", app.parse_money("  $12.00 "), 12.0)


def test_subject_parsing():
    print("\nsubject parsing — the documented format and its likely variants")
    cases = [
        ("Reconciliation: Joe's Cafe | Bank deposit: $4,182.35", "Joe's Cafe", 4182.35),
        ("Reconciliation: Joe's Cafe | Bank deposit: $175.00", "Joe's Cafe", 175.0),
        ("Reconciliation: Joe's Cafe | Bank deposit: 175.00", "Joe's Cafe", 175.0),
        ("Reconciliation: Joe's Cafe | Bank deposit: 175", "Joe's Cafe", 175.0),
        ("RE: Reconciliation: Bay Bakery | Bank deposit: $980.10", "Bay Bakery", 980.10),
        ("reconciliation: Corner Store | bank deposit: $12.5", "Corner Store", 12.5),
    ]
    for subject, name, amount in cases:
        match = app.SUBJECT_PATTERN.search(subject)
        if not match:
            check(f"matches {subject!r}", "NO MATCH", f"{name} / {amount}")
            continue
        check(f"name from {subject[:44]!r}...", match.group(1).strip(), name)
        check(f"amount from {subject[:44]!r}...", app.parse_money(match.group(2)), amount)

    print("\nsubject parsing — must NOT match unrelated mail")
    for subject in [
        "Your Amazon order has shipped",
        "Reconciliation question — can we chat?",
        "Bank deposit: $500",
        "",
    ]:
        check(f"ignores {subject!r}", bool(app.SUBJECT_PATTERN.search(subject)), False)


def test_mime_extraction():
    print("\nCSV extraction from a real MIME email")
    expected_rows = len(read_sample())

    raw = build_email("Reconciliation: Joe's Cafe | Bank deposit: $4,182.35")
    submission = app.parse_submission(message_from_bytes(raw))

    if submission is None:
        check("parses a well-formed submission", None, "a submission dict")
        return

    check("client name", submission["client_name"], "Joe's Cafe")
    check("bank deposit", submission["bank_deposit"], 4182.35)
    check("row count matches the file", len(submission["transactions"]), expected_rows)
    check("columns survived", set(submission["transactions"][0]),
          {"Date", "Transaction ID", "Type", "Amount", "Fee", "Status"})

    print("\nCSV extraction — degenerate cases")
    no_csv = app.parse_submission(message_from_bytes(
        build_email("Reconciliation: Joe's Cafe | Bank deposit: $175.00", csv_path=None)
    ))
    check("subject matches but no attachment -> None", no_csv, None)

    wrong_ext = app.parse_submission(message_from_bytes(
        build_email("Reconciliation: Joe's Cafe | Bank deposit: $175.00",
                    filename="sales.xlsx")
    ))
    check("non-CSV attachment -> None", wrong_ext, None)

    # A client whose name forces RFC2047 encoding of the whole subject.
    encoded = MIMEMultipart()
    encoded["Subject"] = Header("Reconciliation: Café Niño | Bank deposit: $250.00",
                                "utf-8")
    encoded["From"] = "owner@joescafe.example"
    with open(SAMPLE_CSV, "rb") as handle:
        part = MIMEBase("text", "csv")
        part.set_payload(handle.read())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename="x.csv")
    encoded.attach(part)

    decoded = app.parse_submission(message_from_bytes(encoded.as_bytes()))
    check("RFC2047-encoded subject decodes",
          decoded["client_name"] if decoded else None, "Café Niño")


def test_math():
    print("\narithmetic — summarize() vs an independent Decimal implementation")
    rows = read_sample()
    got = app.summarize(rows)
    expected = manual_totals(rows)

    for key in ("sales", "refunds", "fees", "voided_count", "net_expected"):
        check(f"{key}", got[key], expected[key])

    print("\narithmetic — hand-built cases with known answers")
    check("voided rows are excluded entirely", app.summarize([
        {"Type": "Sale", "Amount": "$100.00", "Fee": "$3.00", "Status": "Completed"},
        {"Type": "Sale", "Amount": "$999.00", "Fee": "$9.99", "Status": "Voided"},
    ]), {"sales": 100.0, "refunds": 0.0, "fees": 3.0,
         "voided_count": 1, "net_expected": 97.0})

    check("cancelled rows are excluded too", app.summarize([
        {"Type": "Sale", "Amount": "$100.00", "Fee": "$3.00", "Status": "Completed"},
        {"Type": "Sale", "Amount": "$50.00", "Fee": "$1.50", "Status": "Cancelled"},
    ]), {"sales": 100.0, "refunds": 0.0, "fees": 3.0,
         "voided_count": 1, "net_expected": 97.0})

    check("refunds reduce the expected deposit", app.summarize([
        {"Type": "Sale", "Amount": "$200.00", "Fee": "$6.00", "Status": "Completed"},
        {"Type": "Refund", "Amount": "($50.00)", "Fee": "$0.00", "Status": "Completed"},
    ]), {"sales": 200.0, "refunds": -50.0, "fees": 6.0,
         "voided_count": 0, "net_expected": 144.0})

    check("empty export doesn't crash", app.summarize([]),
          {"sales": 0.0, "refunds": 0.0, "fees": 0.0,
           "voided_count": 0, "net_expected": 0.0})


def test_prompt_and_review():
    print("\nprompt + review draft assembly (no API call)")
    rows = read_sample()
    totals = app.summarize(rows)
    prompt = app.build_prompt(totals, 40000.00, rows)

    check("prompt embeds the computed net",
          app.fmt_money(totals["net_expected"]) in prompt, True)
    check("prompt embeds the bank deposit", "$40,000.00" in prompt, True)
    check("negatives read as -$x, not $-x",
          app.fmt_money(totals["refunds"]).startswith("-$"), True)
    check("transactions are CSV, not dict repr", "'Status':" not in prompt, True)
    check("every data row is present", prompt.count("TXN"), len(rows))

    body = app.build_review_body("Joe's Cafe", totals, 40000.00, "Looks fine.")
    check("review draft names the client", "Joe's Cafe" in body, True)
    check("review draft carries the do-not-send warning",
          "NOT been sent to the client" in body, True)

    held = app.build_held_notice([{
        "client_name": "Bay Bakery", "sender": "hello@baybakery.example",
        "reason": "status is 'unpaid'", "bank_deposit": 980.10,
        "transactions": rows,
    }])
    check("held notice names the client", "Bay Bakery" in held, True)
    check("held notice states the reason", "unpaid" in held, True)
    check("held notice says no credit was spent", "no API credit" in held, True)

    print(f"\n  (prompt is {len(prompt):,} chars / roughly "
          f"{len(prompt) // 4:,} tokens for {len(rows):,} rows)")


def run() -> int:
    print("=" * 66)
    print("Offline checks — check_and_reconcile.py")
    print("=" * 66)

    test_billing_gate()
    test_wildcard_matching()
    test_sender_extraction()
    test_parse_money()
    test_subject_parsing()
    test_mime_extraction()
    test_math()
    test_prompt_and_review()

    print("\n" + "=" * 66)
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED:")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("All offline checks passed.")
    print("Still untested: the live Gmail IMAP/SMTP connection and the real")
    print("Claude API call — both need credentials.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
