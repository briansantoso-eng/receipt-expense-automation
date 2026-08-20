"""
STAGE 4 — it stops being a script you run.
---------------------------------------------
Watches your Gmail inbox for receipts forwarded by clients, saves the
attachments, extracts and validates them, and files the results per client.
Runs unattended on a schedule.

How a client submits:
    They just forward the receipt. No subject convention, no template —
    any image or PDF attached to an email from a roster address counts.
    That's deliberate: every rule you impose is a rule a client will break.

Run it:
    python check_receipts.py                  # one pass over the inbox
    python check_receipts.py --dry-run        # fetch and gate, but no API, no marking
    python check_receipts.py --client "Joe's Cafe"
    python check_receipts.py --self-test      # offline checks, no credentials

Scheduled, via run_receipts.bat.

What this stage adds beyond Stage 3:

  * THE PAYMENT GATE RUNS BEFORE THE API CALL. An unpaid client's receipts are
    saved but never extracted, so they cost nothing. Same clients.json and
    same check_gate() as the POS project.

  * PER-CLIENT ISOLATION. Each client gets receipts/<slug>/ and output/<slug>/
    and their own CSV. One client's spend can never leak into another's books,
    which is the sort of mistake you don't recover from commercially.

  * ATTACHMENTS ARE SAVED BEFORE ANYTHING ELSE. The original image is the
    evidence. If extraction is wrong you need the picture, and re-downloading
    from an email you've already marked read is not a thing you want to rely on.

  * EMAILS ARE MARKED READ ONLY AFTER their attachments are safely on disk.
"""

import os
import re
import sys
import email
import imaplib
import logging
import argparse
import datetime
import unicodedata
from email.utils import parseaddr
from email.header import decode_header, make_header

# Everything below is reused, not rewritten. The POS project supplied the
# inbox reader and the billing gate; Stages 1-3 supplied the extraction.
from check_and_reconcile import (
    CLIENT_REGISTRY, check_gate, load_client_registry, send_email,
)
from extract_receipt import MEDIA_TYPES, check_key
from batch_receipts import (
    load_records, print_summary, process_folder, write_csv,
)

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

log = logging.getLogger("receipts")

INBOX_ROOT = "receipts"
OUTPUT_ROOT = "output"

# Attachments Gmail adds to almost every message, which are never receipts.
IGNORED_NAMES = {"image001.png", "image002.png", "image003.png", "smime.p7s"}

# A phone photo can be several MB; anything past this is not a receipt.
MAX_ATTACHMENT_BYTES = 12 * 1024 * 1024

# How far back to look. A scheduled weekly run only needs a couple of weeks of
# slack; searching all history on every run is wasted work.
DEFAULT_SINCE_DAYS = 30

# Safety rail. If a client dumps 400 receipts in one go, process a sane batch
# and let the next run take the rest, rather than spending $5 unannounced.
DEFAULT_MAX_MESSAGES = 50


def slugify(name: str) -> str:
    """Turn "Joe's Cafe" into "joes-cafe" for use as a folder name. Stripping
    accents matters because a folder called "Café" behaves differently across
    Windows, OneDrive and zip files."""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    # Drop apostrophes rather than treating them as separators, so "Joe's Cafe"
    # becomes "joes-cafe" and not "joe-s-cafe".
    ascii_only = re.sub(r"['’ʼ`]", "", ascii_only)
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
    return slug or "unknown-client"


def decode_header_value(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def safe_filename(name: str, fallback: str) -> str:
    """Attachment filenames come from the sender, so they are hostile input:
    "../../.env" or "C:\\Windows\\x.png" would otherwise write outside the
    folder. Keep the basename only, and strip anything exotic."""
    name = decode_header_value(name)
    name = name.replace("\\", "/").split("/")[-1]     # defeat path traversal
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if not name or not os.path.splitext(name)[1]:
        return fallback
    return name[:120]


def find_attachments(msg: email.message.Message) -> list[tuple[str, bytes]]:
    """Every image or PDF in the message, as (filename, bytes)."""
    found = []
    for index, part in enumerate(msg.walk()):
        if part.get_content_maintype() == "multipart":
            continue

        raw_name = part.get_filename()
        content_type = (part.get_content_type() or "").lower()

        # Accept on either the extension or the MIME type — phones often send
        # an image with no filename at all.
        extension = os.path.splitext(decode_header_value(raw_name) or "")[1].lower()
        looks_right = extension in MEDIA_TYPES or content_type in set(
            MEDIA_TYPES.values())
        if not looks_right:
            continue

        name = safe_filename(raw_name or "", fallback=f"attachment{index}"
                             f"{extension or '.png'}")
        if name.lower() in IGNORED_NAMES:
            continue

        payload = part.get_payload(decode=True)
        if not payload:
            continue
        if len(payload) > MAX_ATTACHMENT_BYTES:
            log.warning("Skipping %s: %.1f MB is too large to be a receipt",
                        name, len(payload) / 1024 / 1024)
            continue

        found.append((name, payload))

    return found


def unique_path(folder: str, name: str) -> str:
    """Never overwrite. Two clients both sending "IMG_1234.jpg" in the same
    week is normal, and silently replacing the first one loses evidence."""
    base, extension = os.path.splitext(name)
    candidate = os.path.join(folder, name)
    counter = 2
    while os.path.exists(candidate):
        candidate = os.path.join(folder, f"{base}-{counter}{extension}")
        counter += 1
    return candidate


def fetch_receipt_emails(creds: dict, registry: dict,
                         client_filter: str | None,
                         since_days: int = DEFAULT_SINCE_DAYS,
                         max_messages: int = DEFAULT_MAX_MESSAGES) -> dict:
    """Walk unread mail, gate each sender, and save attachments from the ones
    that pass. Returns {slug: {"name":..., "saved":[paths]}} plus held info.

    Messages are not marked read here — the caller does that only once the
    files are on disk."""
    submissions: dict[str, dict] = {}
    held: list[dict] = []
    to_mark: list[bytes] = []

    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        imap.login(creds["address"], creds["app_password"])
        imap.select("INBOX")

        # Search per roster address rather than scanning UNSEEN wholesale.
        # A real personal inbox has thousands of unread messages — the first
        # live run here hit 10,593 — and fetching all of them to find three
        # receipts is both unusably slow and a way to accidentally process
        # mail that has nothing to do with any client.
        #
        # Only addresses on the roster are searched, at any status: active
        # ones get processed, unpaid/paused ones still need reporting as held.
        # Anything from anyone else is never even downloaded.
        since = (datetime.date.today()
                 - datetime.timedelta(days=since_days)).strftime("%d-%b-%Y")

        ids: list[bytes] = []
        seen_ids: set[bytes] = set()
        for address in sorted(registry):
            entry = registry[address]
            display = entry.get("name") or address
            if client_filter and client_filter.lower() not in display.lower():
                continue

            query = f'(UNSEEN SINCE {since} FROM "{address}")'
            status, ids_raw = imap.search(None, query)
            if status != "OK":
                log.error("IMAP search failed for %s: %s", address, status)
                continue

            found = [i for i in ids_raw[0].split() if i not in seen_ids]
            seen_ids.update(found)
            ids.extend(found)
            if found:
                log.info("%s <%s>: %d unread message(s) in the last %d days",
                         display, address, len(found), since_days)

        if not ids:
            log.info("No unread mail from any roster address in the last %d days.",
                     since_days)
        elif len(ids) > max_messages:
            log.warning("%d messages found; capping this run at %d. "
                        "The rest will be picked up next run.",
                        len(ids), max_messages)
            ids = ids[:max_messages]

        for msg_id in ids:
            status, data = imap.fetch(msg_id, "(BODY.PEEK[])")
            if status != "OK" or not data or not isinstance(data[0], tuple):
                log.warning("Could not fetch message %s", msg_id.decode())
                continue

            msg = email.message_from_bytes(data[0][1])
            sender = parseaddr(decode_header_value(msg["From"]))[1].lower()
            subject = decode_header_value(msg["Subject"])[:60]

            attachments = find_attachments(msg)
            if not attachments:
                continue  # not a receipt email — leave it alone entirely

            allowed, reason = check_gate(sender, registry)
            entry = registry.get(sender, {})
            display = entry.get("name") or sender

            if client_filter and client_filter.lower() not in display.lower():
                continue

            if not allowed:
                # The gate is BEFORE any API call, so this costs nothing.
                log.warning("HELD  %s <%s>: %s — %d attachment(s) not processed",
                            display, sender, reason, len(attachments))
                held.append({"name": display, "sender": sender,
                             "reason": reason, "count": len(attachments),
                             "subject": subject})
                continue

            slug = slugify(display)
            folder = os.path.join(INBOX_ROOT, slug)
            os.makedirs(folder, exist_ok=True)

            saved = []
            for name, payload in attachments:
                path = unique_path(folder, name)
                with open(path, "wb") as handle:
                    handle.write(payload)
                saved.append(path)

            bucket = submissions.setdefault(
                slug, {"name": display, "sender": sender, "saved": []})
            bucket["saved"].extend(saved)
            to_mark.append(msg_id)

            log.info("%s <%s>: saved %d attachment(s) to %s/",
                     display, sender, len(saved), folder)
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    return {"clients": submissions, "held": held, "to_mark": to_mark}


def mark_read(creds: dict, msg_ids: list[bytes]) -> None:
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


def process_client(slug: str, info: dict, dry_run: bool) -> dict:
    """Run Stages 1-3 for one client, inside their own folders."""
    folder = os.path.join(INBOX_ROOT, slug)
    output_dir = os.path.join(OUTPUT_ROOT, slug)
    csv_path = f"expenses_{slug}.csv"

    log.info("Processing %s (%d new file(s))", info["name"], len(info["saved"]))

    if dry_run:
        log.info("  DRY RUN: would extract %d receipt(s) from %s/ into %s/",
                 len(info["saved"]), folder, output_dir)
        return {"csv": csv_path, "records": [], "stats": None}

    stats = process_folder(folder, force=False, limit=None,
                           output_dir=output_dir)
    records = load_records(output_dir)
    write_csv(records, csv_path)
    print_summary(records, csv_path, stats, output_dir)

    return {"csv": csv_path, "records": records, "stats": stats}


def build_held_notice(held: list[dict]) -> str:
    lines = [
        "These receipts arrived but were NOT processed, so no API credit was",
        "spent on them. The emails are still unread: fix the roster entry in",
        f"{CLIENT_REGISTRY} and the next run picks them up.",
        "",
    ]
    for item in held:
        lines.append(f"  {item['name']}  <{item['sender']}>")
        lines.append(f"      held because: {item['reason']}")
        lines.append(f"      {item['count']} attachment(s), subject: "
                     f"{item['subject']!r}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Watch Gmail for forwarded receipts and process them.")
    parser.add_argument("--client", metavar="NAME",
                        help="only process this client's receipts")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and gate, but make no API calls and mark "
                             "nothing as read")
    parser.add_argument("--roster", default=CLIENT_REGISTRY,
                        help=f"client roster (default: {CLIENT_REGISTRY})")
    parser.add_argument("--since-days", type=int, default=DEFAULT_SINCE_DAYS,
                        help=f"how far back to search "
                             f"(default: {DEFAULT_SINCE_DAYS})")
    parser.add_argument("--max-messages", type=int, default=DEFAULT_MAX_MESSAGES,
                        help=f"most messages to process in one run "
                             f"(default: {DEFAULT_MAX_MESSAGES})")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    registry = load_client_registry(args.roster)

    missing = [k for k in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")
               if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            "Missing " + ", ".join(missing) + " in .env (see env.example).\n"
            "Gmail needs an App Password, not your account password:\n"
            "  myaccount.google.com/apppasswords")

    if not args.dry_run:
        log.info("Using API key %s", check_key())

    creds = {
        "address": os.environ["GMAIL_ADDRESS"].strip(),
        "app_password": "".join(os.environ["GMAIL_APP_PASSWORD"].split()),
        "review_email": (os.environ.get("REVIEW_EMAIL")
                         or os.environ["GMAIL_ADDRESS"]).strip(),
    }

    result = fetch_receipt_emails(creds, registry, args.client,
                                  args.since_days, args.max_messages)

    if not result["clients"] and not result["held"]:
        log.info("No new receipt emails found.")
        return 0

    # Attachments are on disk now, so it's safe to stop re-reading these.
    if not args.dry_run:
        mark_read(creds, result["to_mark"])

    failures = 0
    for slug, info in result["clients"].items():
        try:
            process_client(slug, info, args.dry_run)
        except Exception:
            failures += 1
            log.exception("Failed while processing %s", info["name"])

    if result["held"] and not args.dry_run:
        send_email(creds,
                   f"[HELD] {len(result['held'])} receipt submission(s) not processed",
                   build_held_notice(result["held"]))
        log.info("Held notice sent to %s", creds["review_email"])

    log.info("Done: %d client(s) processed, %d held, %d failed",
             len(result["clients"]), len(result["held"]), failures)
    return 1 if failures else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        from test_receipts_inbox import run
        raise SystemExit(run())
    raise SystemExit(main())
