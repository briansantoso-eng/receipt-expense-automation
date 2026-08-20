"""
STAGE 5 — the Monday email.
------------------------------
Turns everything the pipeline has processed into one digest, emailed to YOU
with each client's spreadsheet attached. Nothing goes to a client from here;
you read it, spot-check what's flagged, and forward it yourself.

Run it:
    python weekly_summary.py --print     # preview in the terminal, sends nothing
    python weekly_summary.py             # send it to REVIEW_EMAIL
    python weekly_summary.py --days 7    # only receipts dated in the last 7 days

Two design points worth knowing:

  * NO API CALLS. Everything here is arithmetic over JSON files that Stage 1
    already paid for. Running the summary ten times costs nothing, which
    means you can preview it freely until the wording is right.

  * IT SENDS NOTHING ON A QUIET WEEK. An automation that emails you "nothing
    happened" every Monday is an automation you start ignoring, and then you
    miss the week it mattered. Silence is the correct output for no activity.

Schedule this separately from check_receipts.py — process hourly, summarise
weekly:
    schtasks /create /tn "Receipt Summary" /sc weekly /d MON /st 08:00 ^
      /tr "...\\run_summary.bat"
"""

import os
import csv
import glob
import json
import argparse
import datetime
import collections

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication

from check_and_reconcile import CLIENT_REGISTRY, load_client_registry
from batch_receipts import duplicate_exposure, flag_duplicates
from validate import annotate

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

OUTPUT_ROOT = "output"

STATUS_LABEL = {
    "ok": "clean",
    "check": "worth a glance",
    "needs_review": "NEEDS REVIEW",
}


def client_names(roster_path: str = CLIENT_REGISTRY) -> dict:
    """slug -> display name, so the email says "Joe's Cafe" not "joes-cafe"."""
    from check_receipts import slugify
    try:
        registry = load_client_registry(roster_path)
    except SystemExit:
        return {}
    return {slugify(e.get("name") or addr): (e.get("name") or addr)
            for addr, e in registry.items()}


def discover_clients() -> dict:
    """Find every client folder under output/. Loose JSON at the top level is
    from manual --file runs, and gets grouped separately rather than being
    silently attributed to a client."""
    groups: dict[str, list[str]] = {}

    for entry in sorted(glob.glob(os.path.join(OUTPUT_ROOT, "*"))):
        if os.path.isdir(entry):
            files = sorted(glob.glob(os.path.join(entry, "*.json")))
            if files:
                groups[os.path.basename(entry)] = files

    loose = sorted(glob.glob(os.path.join(OUTPUT_ROOT, "*.json")))
    if loose:
        groups["(manual runs)"] = loose

    return groups


def load_group(paths: list[str], days: int | None,
               today: datetime.date | None = None) -> list[dict]:
    """Load and re-validate a client's records, optionally windowed by the
    receipt's own date (not the file's timestamp — a receipt emailed in
    August can be dated July)."""
    today = today or datetime.date.today()
    cutoff = today - datetime.timedelta(days=days) if days else None

    records = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue

        if cutoff:
            try:
                when = datetime.date.fromisoformat(str(record.get("date")))
            except (TypeError, ValueError):
                when = None
            # Undated records are kept, not dropped — they need review, and
            # a date filter silently hiding broken rows is how things get
            # missed for a quarter.
            if when and when < cutoff:
                continue

        records.append(annotate(record))

    # Per client, not across clients: two different businesses buying the
    # same thing on the same day is a coincidence, not a double-count.
    return flag_duplicates(records)


def money(value: float) -> str:
    return f"{'-' if value < 0 else ''}${abs(value):,.2f}"


def summarise_group(name: str, records: list[dict]) -> list[str]:
    """The per-client block of the email."""
    lines = [name, "-" * len(name)]

    counts = collections.Counter(r.get("status", "?") for r in records)
    lines.append(f"  {len(records)} receipt(s): "
                 f"{counts['ok']} clean, {counts['check']} worth a glance, "
                 f"{counts['needs_review']} needing review")

    trusted = [r for r in records if r.get("status") != "needs_review"]
    if len(trusted) < len(records):
        lines.append(f"  (totals below exclude the "
                     f"{len(records) - len(trusted)} unreviewed row(s))")

    buckets: dict[tuple, float] = collections.defaultdict(float)
    for record in trusted:
        buckets[(record.get("currency", "?"),
                 record.get("category", "other"))] += float(record.get("total") or 0)

    if buckets:
        lines.append("")
        for (currency, category), amount in sorted(buckets.items()):
            lines.append(f"    {currency}  {category:<24} {amount:>10,.2f}")
        lines.append("")
        for currency in sorted({c for c, _ in buckets}):
            total = sum(v for (c, _), v in buckets.items() if c == currency)
            tax = sum(float(r.get("tax") or 0) for r in trusted
                      if r.get("currency") == currency)
            lines.append(f"    {currency}  {'TOTAL':<24} {total:>10,.2f}"
                         f"   (tax {tax:,.2f})")

    dupe_count, dupe_value = duplicate_exposure(trusted)
    if dupe_count:
        lines.append("")
        for currency, amount in sorted(dupe_value.items()):
            lines.append(f"    !! {currency} {amount:,.2f} of that total may be "
                         f"double-counted ({dupe_count} suspected duplicate(s))")

    flagged = [r for r in records if r.get("status") in ("check", "needs_review")]
    if flagged:
        lines.append("")
        lines.append("  Flagged:")
        for record in flagged:
            lines.append(f"    {record.get('source_file', '?')} — "
                         f"{STATUS_LABEL.get(record.get('status'), '?')}")
            for issue in record.get("issues") or []:
                lines.append(f"        {issue}")

    lines.append("")
    return lines


def build_summary(groups: dict, days: int | None,
                  today: datetime.date | None = None) -> tuple[str, str, dict]:
    """Returns (subject, body, {name: records}). Pure — no email, no files
    written — so the tests and --print use exactly what gets sent."""
    today = today or datetime.date.today()
    loaded = {name: load_group(paths, days, today)
              for name, paths in groups.items()}
    loaded = {name: records for name, records in loaded.items() if records}

    total_receipts = sum(len(r) for r in loaded.values())
    needs_review = sum(1 for records in loaded.values() for r in records
                       if r.get("status") == "needs_review")

    window = f"last {days} days" if days else "all time"
    subject = (f"[REVIEW] Receipts — {total_receipts} processed"
               + (f", {needs_review} need review" if needs_review else "")
               + f" — {today:%d %b %Y}")

    lines = [
        f"Receipt summary — {today:%A %d %B %Y}",
        f"Covering: {window}",
        "",
        f"{total_receipts} receipt(s) across {len(loaded)} client(s).",
    ]
    if needs_review:
        lines.append(f"{needs_review} row(s) need your eyes before anything "
                     f"is forwarded.")
    else:
        lines.append("Nothing is blocking — every row passed validation.")
    lines += ["", "=" * 58, ""]

    for name, records in sorted(loaded.items()):
        lines += summarise_group(name, records)

    lines += [
        "=" * 58,
        "",
        "The spreadsheets are attached. NOTHING has been sent to any client.",
        "Check anything flagged above, then forward the CSV yourself.",
        "",
        "Reply-to-self note: this email is generated by weekly_summary.py and",
        "costs nothing to re-run — it does no API calls.",
    ]

    return subject, "\n".join(lines), loaded


def write_group_csv(name: str, records: list[dict]) -> str:
    """Rebuild each client's CSV fresh so the attachment always matches the
    numbers quoted in the email body."""
    from batch_receipts import write_csv
    from check_receipts import slugify
    # Use the tested slugify rather than ad-hoc munging. Hand-rolling it here
    # produced "expenses_brian-(test.csv" — strip("()") only removes parens at
    # the ends, so the one in the middle of "Brian (test)" survived.
    path = f"expenses_{slugify(name)}.csv"
    write_csv(records, path)
    return path


def send_summary(subject: str, body: str, attachments: list[str]) -> str:
    import smtplib

    address = os.environ["GMAIL_ADDRESS"].strip()
    password = "".join(os.environ["GMAIL_APP_PASSWORD"].split())
    to = (os.environ.get("REVIEW_EMAIL") or address).strip()

    msg = MIMEMultipart()
    msg["From"] = address
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    for path in attachments:
        with open(path, "rb") as handle:
            part = MIMEApplication(handle.read(), _subtype="csv")
        part.add_header("Content-Disposition", "attachment",
                        filename=os.path.basename(path))
        msg.attach(part)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(address, password)
        server.send_message(msg)

    return to


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Email yourself a digest of processed receipts.")
    parser.add_argument("--print", action="store_true", dest="preview",
                        help="print the email instead of sending it")
    parser.add_argument("--days", type=int,
                        help="only include receipts dated in the last N days")
    parser.add_argument("--roster", default=CLIENT_REGISTRY)
    args = parser.parse_args(argv)

    groups = discover_clients()
    if not groups:
        print(f"Nothing in {OUTPUT_ROOT}/ yet — no summary to send.")
        return 0

    # Swap folder slugs for real client names where we can.
    names = client_names(args.roster)
    groups = {names.get(slug, slug): paths for slug, paths in groups.items()}

    subject, body, loaded = build_summary(groups, args.days)

    if not loaded:
        # Deliberately silent. See the note at the top of this file.
        print("No receipts in the reporting window — no email sent.")
        return 0

    if args.preview:
        print(f"Subject: {subject}")
        print()
        print(body)
        print()
        print("(--print: nothing was sent, no CSVs were rewritten)")
        return 0

    attachments = [write_group_csv(name, records)
                   for name, records in sorted(loaded.items())]
    to = send_summary(subject, body, attachments)
    print(f"Summary sent to {to} with {len(attachments)} attachment(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
