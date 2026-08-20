"""
Automated POS Reconciliation Pipeline
---------------------------------------
Checks your Gmail inbox for client submissions, reconciles them, and emails
YOU a review draft — nothing is ever sent to the client automatically.

How a client submits data:
    They email you with subject line:
        Reconciliation: <Client Name> | Bank deposit: <amount>
    ...with their POS export CSV attached.

Where the API call is: exactly one, in ask_claude_to_explain(). Every number
is computed in Python by summarize(); Claude only writes prose about numbers
it was handed. That call is also the only step that costs money, which is why
the billing gate sits immediately before it.

Run it:
    python check_and_reconcile.py                      # process all active clients
    python check_and_reconcile.py --dry-run            # same, but no API call, no email
    python check_and_reconcile.py --client "Joe's Cafe"  # just one client, on demand
    python check_and_reconcile.py --file x.csv --deposit 4182.35 --client "Joe's Cafe"
    python check_and_reconcile.py --clients            # show the roster and exit

Single-pass and idempotent on purpose: the scheduler owns the cadence, and a
crashed run just retries on the next tick.
"""

import os
import re
import io
import csv
import sys
import json
import email
import imaplib
import logging
import smtplib
import argparse
from email.utils import parseaddr
from email.header import decode_header, make_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import anthropic

try:
    from dotenv import load_dotenv
    # override=True matters: without it a stale ANTHROPIC_API_KEY or
    # GMAIL_ADDRESS already in the OS environment silently wins over .env,
    # and you debug a 401 while looking at a correct .env file.
    load_dotenv(override=True)
except ImportError:
    pass  # dotenv is optional — the script still works with env vars set manually

log = logging.getLogger("reconcile")

MODEL = "claude-opus-5"
CLIENT_REGISTRY = "clients.json"

# Only this status opens the gate. Anything else — including a typo'd status,
# or a sender missing from the roster entirely — is held without spending.
BILLABLE_STATUS = "active"

# Accepts "175", "175.00", "$175.00" and "$4,182.35".
# Written permissively on purpose: a client typo in the subject line means a
# silently ignored email, which is the worst failure mode this script has.
SUBJECT_PATTERN = re.compile(
    r"Reconciliation:\s*(.+?)\s*\|\s*Bank\s+deposit:\s*\$?\s*"
    r"(\(?-?[\d,]+(?:\.\d{1,2})?\)?)",
    re.IGNORECASE,
)


def _credentials(need_gmail: bool = True, need_api: bool = True) -> dict:
    """Read credentials at call time, not import time, so the pure-logic
    functions (summarize, parsing, gating) stay testable with no environment
    at all. --dry-run and --file modes need less than a full inbox run."""
    required = []
    if need_gmail:
        required += ["GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"]
    if need_api:
        required += ["ANTHROPIC_API_KEY"]

    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise SystemExit(
            "Missing required environment variable(s): " + ", ".join(missing) +
            "\nSet them in a .env file next to this script (see env.example)."
        )

    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    return {
        "address": address,
        # Google shows app passwords as "abcd efgh ijkl mnop"; pasted verbatim
        # those spaces make IMAP/SMTP login fail with a bare "Invalid
        # credentials", which looks exactly like a wrong password.
        "app_password": "".join(os.environ.get("GMAIL_APP_PASSWORD", "").split()),
        "review_email": (os.environ.get("REVIEW_EMAIL") or address).strip(),
    }


# ── Billing gate ──────────────────────────────────────────────────────

def load_client_registry(path: str = CLIENT_REGISTRY) -> dict:
    """Load clients.json into {lowercased email: client}. A missing or broken
    roster is fatal, not a warning: defaulting to "process everything" would
    bill you for unpaid clients and for anyone who guessed the subject line."""
    if not os.path.exists(path):
        raise SystemExit(
            f"No client roster found at {path}.\n"
            f"Copy clients.example.json to {path} and list your clients.\n"
            "Nothing runs without it — that's what stops unpaid work and "
            "stray email from spending your API credit."
        )

    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}") from exc

    registry = {}
    for entry in data.get("clients", []):
        address = (entry.get("email") or "").strip().lower()
        if address == "*" or address.startswith("*@"):
            log.warning("Roster has a wildcard entry %r (%s) — it will match "
                        "senders you have not individually approved.",
                        address, entry.get("name") or "unnamed")
        if not address:
            log.warning("Roster entry with no email, skipped: %r", entry.get("name"))
            continue
        registry[address] = entry

    if not registry:
        raise SystemExit(f"{path} lists no clients with an email address.")

    return registry


def match_roster(sender: str, registry: dict) -> tuple[dict | None, str]:
    """Find the roster entry for a sender. Three forms, most specific first:

        owner@joescafe.com    exact address
        *@joescafe.com        anyone at that company — a real requirement, since
                              a business often has three people who send
                              receipts and you don't want to chase each one
        *                     any sender at all. Testing only; it removes your
                              only guard against a stray email spending credit.

    Returns (entry, how_it_matched)."""
    address = (sender or "").strip().lower()
    if not address:
        return None, "no sender address"

    if address in registry:
        return registry[address], "exact address"

    _, _, domain = address.partition("@")
    if domain and f"*@{domain}" in registry:
        return registry[f"*@{domain}"], f"domain *@{domain}"

    if "*" in registry:
        return registry["*"], "CATCH-ALL (*) — testing only"

    return None, "sender is not on the roster"


def check_gate(sender: str, registry: dict) -> tuple[bool, str]:
    """Decide whether this sender's work is billable. Pure function — no
    network, no API, no side effects — so it is cheap to test exhaustively.

    Returns (allowed, human-readable reason)."""
    client, how = match_roster(sender, registry)

    if client is None:
        return False, how

    status = (client.get("status") or "").strip().lower()
    if status == BILLABLE_STATUS:
        # Say how it matched when it wasn't an exact address, so a catch-all
        # left in the roster by accident is visible in the logs rather than
        # quietly billing you for every stray email with a photo attached.
        return True, "active" if how == "exact address" else f"active via {how}"

    if not status:
        return False, "roster entry has no status set"

    return False, f"status is '{status}'"


# ── Reconciliation maths (no API involved) ────────────────────────────

def parse_money(value: str | None) -> float:
    """POS exports write money a dozen different ways. Accountants use
    parentheses for negatives, so "($35.42)" has to come out as -35.42."""
    if value is None:
        return 0.0

    cleaned = str(value).strip()
    if not cleaned:
        return 0.0

    cleaned = cleaned.replace("$", "").replace(",", "")
    cleaned = cleaned.replace("(", "-").replace(")", "")
    return float(cleaned)


def fmt_money(value: float) -> str:
    """-$2,027.06 rather than $-2,027.06 — these totals go straight into an
    email a person reads."""
    return f"{'-' if value < 0 else ''}${abs(value):,.2f}"


def summarize(transactions: list[dict]) -> dict:
    """Add up sales, refunds, voids, and fees — the numbers a client actually
    needs, computed here rather than trusting Claude with arithmetic."""
    totals = {"sales": 0.0, "refunds": 0.0, "fees": 0.0, "voided_count": 0}

    for row in transactions:
        status = (row.get("Status") or "").strip().lower()
        amount = parse_money(row.get("Amount"))
        fee = parse_money(row.get("Fee"))

        if status in {"voided", "cancelled", "canceled"}:
            totals["voided_count"] += 1
            continue  # voided or cancelled transactions never actually happened

        if (row.get("Type") or "").strip().lower() == "refund":
            totals["refunds"] += amount  # already negative in the export
        else:
            totals["sales"] += amount

        totals["fees"] += fee

    totals["net_expected"] = round(
        totals["sales"] + totals["refunds"] - totals["fees"], 2
    )
    for key in ("sales", "refunds", "fees"):
        totals[key] = round(totals[key], 2)
    return totals


# ── Inbox reading ─────────────────────────────────────────────────────

def _decode_header_value(raw: str | None) -> str:
    """Gmail RFC2047-encodes any header with non-ASCII in it (accented client
    names, curly apostrophes), which arrives as "=?utf-8?B?...?=" gibberish."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def _decode_csv_bytes(payload: bytes) -> str:
    """Excel-exported CSVs commonly carry a UTF-8 BOM or cp1252 curly quotes."""
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def parse_submission(msg: email.message.Message) -> dict | None:
    """Pull sender, client name, bank deposit, and CSV rows out of one email.
    Returns None if this message isn't a well-formed submission."""
    subject = _decode_header_value(msg["Subject"])
    match = SUBJECT_PATTERN.search(subject)
    if not match:
        return None  # not a reconciliation submission

    client_name = match.group(1).strip()
    bank_deposit = parse_money(match.group(2))
    sender = parseaddr(_decode_header_value(msg["From"]))[1].strip().lower()

    csv_text = None
    for part in msg.walk():
        filename = _decode_header_value(part.get_filename())
        if filename and filename.lower().endswith(".csv"):
            payload = part.get_payload(decode=True)
            if payload:
                csv_text = _decode_csv_bytes(payload)
                break

    if csv_text is None:
        log.warning("Subject matched but no CSV attached: %r", subject)
        return None

    transactions = list(csv.DictReader(io.StringIO(csv_text)))
    if not transactions:
        log.warning("CSV attached but it has no data rows: %r", subject)
        return None

    return {
        "sender": sender,
        "client_name": client_name,
        "bank_deposit": bank_deposit,
        "transactions": transactions,
    }


def fetch_pending_submissions(creds: dict) -> list[dict]:
    """Connect to Gmail over IMAP and return every unread email that looks
    like a submission. Messages are NOT marked read here — that only happens
    once a review email has actually gone out, so a mid-run failure retries."""
    submissions = []

    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        imap.login(creds["address"], creds["app_password"])
        imap.select("INBOX")

        status, message_ids = imap.search(None, "UNSEEN")
        if status != "OK":
            log.error("IMAP search failed: %s", status)
            return []

        ids = message_ids[0].split()
        log.info("%d unread message(s) in the inbox", len(ids))

        for msg_id in ids:
            # BODY.PEEK[] rather than RFC822, which would set \Seen as a side
            # effect of merely reading the message.
            status, msg_data = imap.fetch(msg_id, "(BODY.PEEK[])")
            if status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                log.warning("Could not fetch message %s", msg_id.decode())
                continue

            submission = parse_submission(email.message_from_bytes(msg_data[0][1]))
            if submission:
                submission["msg_id"] = msg_id
                submissions.append(submission)
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    return submissions


def mark_as_read(creds: dict, msg_ids: list[bytes]) -> None:
    """Flag the processed messages so the next run skips them. Called only
    after their review emails have been sent successfully. Held submissions
    are deliberately left unread so a status flip picks them up next run."""
    if not msg_ids:
        return

    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        imap.login(creds["address"], creds["app_password"])
        imap.select("INBOX")
        for msg_id in msg_ids:
            imap.store(msg_id, "+FLAGS", "\\Seen")
    finally:
        try:
            imap.logout()
        except Exception:
            pass


# ── The one API call ──────────────────────────────────────────────────

def _transactions_as_csv(transactions: list[dict]) -> str:
    """Compact CSV beats Python dict repr here — same information, far fewer
    tokens, and it looks like what the client actually exported."""
    if not transactions:
        return "(none)"

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(transactions[0].keys()))
    writer.writeheader()
    writer.writerows(transactions)
    return buffer.getvalue().strip()


def build_prompt(totals: dict, bank_deposit: float, transactions: list[dict]) -> str:
    gap = round(totals["net_expected"] - bank_deposit, 2)
    return f"""You're helping a small business consultant explain a sales reconciliation
to a non-technical business owner. Be direct and plain-English, no jargon.

POS EXPORT SUMMARY:
- Gross sales: {fmt_money(totals['sales'])}
- Refunds: {fmt_money(totals['refunds'])}
- Card processing fees: {fmt_money(totals['fees'])}
- Voided/cancelled transactions (excluded from totals): {totals['voided_count']}
- Net expected deposit: {fmt_money(totals['net_expected'])}

ACTUAL BANK DEPOSIT: {fmt_money(bank_deposit)}
GAP: {fmt_money(gap)}

RAW TRANSACTIONS (for reference, in case something specific explains the gap):
{_transactions_as_csv(transactions)}

Write a short explanation (3-5 sentences) covering:
1. Whether the numbers reconcile cleanly or there's a real gap to investigate
2. The most likely explanation based on the data given
3. Anything genuinely unusual worth the owner double-checking themselves

Every figure above was computed from the attached export — treat them as given.
Do not invent transactions or reasons not supported by the data above.
Reply with the explanation only, no preamble or sign-off.
"""


def ask_claude_to_explain(totals: dict, bank_deposit: float,
                          transactions: list[dict]) -> str:
    """The one and only API call in this script — and the only line that
    spends money. Callers must pass the billing gate before reaching it."""
    client = anthropic.Anthropic()  # picks up ANTHROPIC_API_KEY from the environment
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "medium"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # a safety decline transparently reruns on a fallback model
        messages=[{
            "role": "user",
            "content": build_prompt(totals, bank_deposit, transactions),
        }],
    )

    if response.stop_reason == "refusal":
        raise RuntimeError(
            "Claude declined this request: " +
            str(getattr(response.stop_details, "explanation", "no explanation given"))
        )

    text = "\n".join(
        block.text for block in response.content
        if block.type == "text" and block.text
    ).strip()

    if not text:
        raise RuntimeError(
            f"Claude returned no text (stop_reason={response.stop_reason})"
        )

    log.info("Claude call: %d in / %d out tokens",
             response.usage.input_tokens, response.usage.output_tokens)
    return text


# ── Delivery ──────────────────────────────────────────────────────────

def build_review_body(client_name: str, totals: dict, bank_deposit: float,
                      explanation: str) -> str:
    gap = round(totals["net_expected"] - bank_deposit, 2)
    return f"""REVIEW BEFORE SENDING — Reconciliation for {client_name}

Gross sales:          {fmt_money(totals['sales'])}
Refunds:              {fmt_money(totals['refunds'])}
Card fees:            {fmt_money(totals['fees'])}
Voided/cancelled:     {totals['voided_count']} transaction(s), excluded
Net expected:         {fmt_money(totals['net_expected'])}
Actual bank deposit:  {fmt_money(bank_deposit)}
Gap:                  {fmt_money(gap)}

Claude's explanation:
{explanation}

---
This has NOT been sent to the client. Review the above, then forward it to
{client_name} yourself if it looks right.
"""


def build_held_notice(held: list[dict]) -> str:
    """One digest covering everything the gate stopped, rather than an email
    per held submission — on a weekly schedule that stays readable."""
    lines = [
        "These submissions arrived but were NOT processed, so no API credit",
        "was spent on them. They are still unread in the inbox: fix the",
        "roster entry in clients.json and the next run picks them up.",
        "",
    ]
    for item in held:
        lines.append(f"  {item['client_name']}  <{item['sender']}>")
        lines.append(f"      held because: {item['reason']}")
        lines.append(f"      claimed deposit: {fmt_money(item['bank_deposit'])}"
                     f", {len(item['transactions'])} rows attached")
        lines.append("")
    return "\n".join(lines)


def send_email(creds: dict, subject: str, body: str) -> None:
    msg = MIMEMultipart()
    msg["From"] = creds["address"]
    msg["To"] = creds["review_email"]
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(creds["address"], creds["app_password"])
        server.send_message(msg)


# ── Entry points ──────────────────────────────────────────────────────

def load_local_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def process(submission: dict, creds: dict, dry_run: bool) -> bool:
    """Reconcile one gated-through submission. Returns True on success."""
    name = submission["client_name"]
    totals = summarize(submission["transactions"])

    log.info("  net expected %s vs deposit %s (gap %s)",
             fmt_money(totals["net_expected"]),
             fmt_money(submission["bank_deposit"]),
             fmt_money(round(totals["net_expected"] - submission["bank_deposit"], 2)))

    if dry_run:
        prompt = build_prompt(totals, submission["bank_deposit"],
                             submission["transactions"])
        log.info("  DRY RUN: skipped the API call (~%d input tokens, ~$%.3f) "
                 "and the review email", len(prompt) // 4,
                 (len(prompt) / 4) * 5 / 1_000_000)
        return True

    explanation = ask_claude_to_explain(
        totals, submission["bank_deposit"], submission["transactions"]
    )
    send_email(creds,
               f"[REVIEW] Reconciliation ready — {name}",
               build_review_body(name, totals, submission["bank_deposit"],
                                 explanation))
    log.info("  -> review email sent to %s", creds["review_email"])
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile client POS exports and email yourself a review draft.")
    parser.add_argument("--client", metavar="NAME",
                        help="only process submissions for this client "
                             "(matches the roster name, case-insensitive)")
    parser.add_argument("--file", metavar="CSV",
                        help="skip the inbox and reconcile this CSV directly")
    parser.add_argument("--deposit", type=float, metavar="AMOUNT",
                        help="bank deposit figure, required with --file")
    parser.add_argument("--dry-run", action="store_true",
                        help="do everything except the API call and the email")
    parser.add_argument("--clients", action="store_true",
                        help="print the roster and exit")
    parser.add_argument("--roster", default=CLIENT_REGISTRY, metavar="JSON",
                        help=f"roster path (default: {CLIENT_REGISTRY})")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    registry = load_client_registry(args.roster)

    if args.clients:
        print(f"\n{'CLIENT':<22} {'STATUS':<10} {'BILLABLE':<9} EMAIL")
        print("-" * 78)
        for address, entry in sorted(registry.items(),
                                     key=lambda kv: kv[1].get("name", "")):
            allowed, _ = check_gate(address, registry)
            print(f"{entry.get('name', '?'):<22} "
                  f"{entry.get('status', '(unset)'):<10} "
                  f"{'yes' if allowed else 'NO':<9} {address}")
            if entry.get("notes"):
                print(f"{'':<22} {entry['notes']}")
        print()
        return 0

    # --file mode never touches the inbox, so it needs no Gmail credentials
    # unless it also has to send you the review draft.
    manual = args.file is not None
    creds = _credentials(need_gmail=not (manual and args.dry_run),
                         need_api=not args.dry_run)

    # ---- gather ----
    if manual:
        if args.deposit is None:
            parser.error("--file requires --deposit")
        if not args.client:
            parser.error("--file requires --client, so the gate knows who it's for")

        match = [entry for entry in registry.values()
                 if (entry.get("name") or "").lower() == args.client.lower()]
        if not match:
            raise SystemExit(f"No roster entry named {args.client!r}. "
                             f"Run --clients to see the roster.")

        submissions = [{
            "sender": match[0]["email"],
            "client_name": match[0].get("name", args.client),
            "bank_deposit": args.deposit,
            "transactions": load_local_csv(args.file),
            "msg_id": None,
        }]
        log.info("Manual run: %s, %d rows from %s",
                 submissions[0]["client_name"],
                 len(submissions[0]["transactions"]), args.file)
    else:
        submissions = fetch_pending_submissions(creds)
        if args.client:
            wanted = args.client.lower()
            submissions = [
                sub for sub in submissions
                if wanted in (sub["client_name"] or "").lower()
                or wanted == (registry.get(sub["sender"], {}).get("name") or "").lower()
            ]
            log.info("Filtered to --client %r: %d submission(s)",
                     args.client, len(submissions))

    if not submissions:
        log.info("No reconciliation submissions to process.")
        return 0

    # ---- gate, then process ----
    processed, held, failed = [], [], 0

    for sub in submissions:
        name = sub["client_name"]
        allowed, reason = check_gate(sub["sender"], registry)

        if not allowed:
            # Nothing below this point costs money, and we never got here.
            log.warning("HELD  %s <%s>: %s — no API call made",
                        name, sub["sender"], reason)
            held.append({**sub, "reason": reason})
            continue

        try:
            log.info("Processing %s <%s> (deposit %s, %d rows)",
                     name, sub["sender"], fmt_money(sub["bank_deposit"]),
                     len(sub["transactions"]))
            process(sub, creds, args.dry_run)
            if sub["msg_id"] is not None and not args.dry_run:
                processed.append(sub["msg_id"])
        except Exception:
            failed += 1
            # Left unread on purpose, so the next scheduled run retries it.
            log.exception("  -> FAILED for %s; leaving the email unread", name)

    if not args.dry_run:
        mark_as_read(creds, processed)
        if held:
            send_email(creds,
                       f"[HELD] {len(held)} submission(s) not processed",
                       build_held_notice(held))
            log.info("Held-submission notice sent to %s", creds["review_email"])

    log.info("Done: %d processed, %d held, %d failed",
             len(submissions) - len(held) - failed, len(held), failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
