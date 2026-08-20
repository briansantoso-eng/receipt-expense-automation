"""
Tests for Stage 4's email handling. No credentials, no network, no cost.

The attachment-handling tests matter more than they look. Filenames arrive
from whoever sent the email, so they are hostile input: a name like
"../../.env" would otherwise write outside the receipts folder.

Run it:
    python test_receipts_inbox.py
    python check_receipts.py --self-test
"""

import sys
from email import encoders, message_from_bytes
from email.header import Header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import check_receipts as app

failures = []


def check(label: str, got, expected):
    ok = got == expected
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"         expected {expected!r}")
        print(f"         got      {got!r}")
        failures.append(label)


def build_email(sender="owner@joescafe.example", subject="Receipt",
                attachments=(("receipt.png", b"\x89PNG fake", "image", "png"),),
                encode_subject=False):
    """Build an email the way a phone or mail client would."""
    msg = MIMEMultipart()
    msg["Subject"] = Header(subject, "utf-8") if encode_subject else subject
    msg["From"] = sender
    msg["To"] = "books@example.com"
    msg.attach(MIMEText("Here's the receipt, thanks!", "plain"))

    for name, data, maintype, subtype in attachments:
        part = MIMEBase(maintype, subtype)
        part.set_payload(data)
        encoders.encode_base64(part)
        if name is not None:
            part.add_header("Content-Disposition", "attachment", filename=name)
        msg.attach(part)

    return message_from_bytes(msg.as_bytes())


def test_slugify():
    print("\nslugify — client name to a safe folder name")
    check("apostrophe and space", app.slugify("Joe's Cafe"), "joes-cafe")
    check("accents are stripped", app.slugify("Café Niño"), "cafe-nino")
    check("punctuation collapses", app.slugify("Smith & Sons, Pty Ltd."),
          "smith-sons-pty-ltd")
    check("already clean", app.slugify("bunnings"), "bunnings")
    check("leading/trailing junk", app.slugify("  --Acme--  "), "acme")
    check("empty falls back", app.slugify(""), "unknown-client")
    check("all-punctuation falls back", app.slugify("!!!"), "unknown-client")


def test_safe_filename():
    print("\nsafe_filename — attachment names are hostile input")
    # "../../.env" reduces to ".env", which has no extension once the leading
    # dot is stripped, so it falls back. Asserting the security property
    # rather than the exact string: whatever comes out must not escape the
    # folder and must not be a dotfile we'd then try to write.
    traversal = app.safe_filename("../../.env", "fb.png")
    check("traversal produces no path separators",
          "/" in traversal or "\\" in traversal, False)
    check("traversal does not yield a dotfile", traversal.startswith("."), False)
    check("traversal falls back to the safe name", traversal, "fb.png")
    check("deep traversal is defeated",
          app.safe_filename("../../../etc/passwd.png", "fb.png"), "passwd.png")
    check("windows path is stripped",
          app.safe_filename(r"C:\Windows\System32\evil.png", "fb.png"),
          "evil.png")
    check("spaces become underscores",
          app.safe_filename("my receipt.jpg", "fb.png"), "my_receipt.jpg")
    check("no extension falls back",
          app.safe_filename("receipt", "fb.png"), "fb.png")
    check("empty falls back", app.safe_filename("", "fb.png"), "fb.png")
    check("normal name survives",
          app.safe_filename("IMG_2841.jpg", "fb.png"), "IMG_2841.jpg")
    check("very long name is truncated",
          len(app.safe_filename("a" * 300 + ".png", "fb.png")) <= 120, True)


def test_find_attachments():
    print("\nfind_attachments — what counts as a receipt")
    msg = build_email()
    found = app.find_attachments(msg)
    check("one PNG is found", len(found), 1)
    check("filename preserved", found[0][0], "receipt.png")

    msg = build_email(attachments=(
        ("a.png", b"png", "image", "png"),
        ("b.pdf", b"%PDF-1.4", "application", "pdf"),
        ("c.jpg", b"jpg", "image", "jpeg"),
    ))
    check("png + pdf + jpg all found", len(app.find_attachments(msg)), 3)

    print("\nfind_attachments — what must be ignored")
    msg = build_email(attachments=(("notes.txt", b"hello", "text", "plain"),))
    check("a text file is not a receipt", app.find_attachments(msg), [])

    msg = build_email(attachments=(("sheet.xlsx", b"xx", "application",
                                    "vnd.ms-excel"),))
    check("a spreadsheet is not a receipt", app.find_attachments(msg), [])

    msg = build_email(attachments=())
    check("no attachments at all", app.find_attachments(msg), [])

    # Gmail attaches signature images to almost every message.
    msg = build_email(attachments=(("image001.png", b"sig", "image", "png"),))
    check("Gmail signature image is skipped", app.find_attachments(msg), [])

    # A phone can send a photo with no filename header.
    msg = build_email(attachments=((None, b"png-bytes", "image", "png"),))
    found = app.find_attachments(msg)
    check("unnamed image is still picked up by MIME type", len(found), 1)

    print("\nfind_attachments — size limit")
    big = b"x" * (app.MAX_ATTACHMENT_BYTES + 1)
    msg = build_email(attachments=(("huge.png", big, "image", "png"),))
    check("oversized attachment is skipped", app.find_attachments(msg), [])


def test_unique_path(tmp="__test_unique__"):
    import os
    import shutil
    print("\nunique_path — never overwrite evidence")
    os.makedirs(tmp, exist_ok=True)
    try:
        first = app.unique_path(tmp, "IMG_1234.jpg")
        check("first use keeps the name",
              os.path.basename(first), "IMG_1234.jpg")
        open(first, "wb").close()

        second = app.unique_path(tmp, "IMG_1234.jpg")
        check("second use is suffixed",
              os.path.basename(second), "IMG_1234-2.jpg")
        open(second, "wb").close()

        third = app.unique_path(tmp, "IMG_1234.jpg")
        check("third use increments again",
              os.path.basename(third), "IMG_1234-3.jpg")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_gate_still_guards():
    print("\nthe payment gate still applies to receipts")
    registry = {
        "owner@joescafe.example": {"email": "owner@joescafe.example",
                                   "name": "Joe's Cafe", "status": "active"},
        "hello@baybakery.example": {"email": "hello@baybakery.example",
                                    "name": "Bay Bakery", "status": "unpaid"},
    }
    check("paid client passes",
          app.check_gate("owner@joescafe.example", registry)[0], True)
    check("unpaid client is held",
          app.check_gate("hello@baybakery.example", registry)[0], False)
    check("stranger is held",
          app.check_gate("random@internet.example", registry)[0], False)

    notice = app.build_held_notice([{
        "name": "Bay Bakery", "sender": "hello@baybakery.example",
        "reason": "status is 'unpaid'", "count": 3, "subject": "receipts"}])
    check("held notice names the client", "Bay Bakery" in notice, True)
    check("held notice says nothing was spent", "no API credit" in notice, True)
    check("held notice gives the count", "3 attachment(s)" in notice, True)


def run() -> int:
    print("=" * 62)
    print("Stage 4 — inbox handling tests (no credentials, no cost)")
    print("=" * 62)

    test_slugify()
    test_safe_filename()
    test_find_attachments()
    test_unique_path()
    test_gate_still_guards()

    print("\n" + "=" * 62)
    if failures:
        print(f"{len(failures)} FAILED:")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("All Stage 4 offline checks passed.")
    print("Still untested: the live Gmail connection (needs an App Password).")
    return 0


if __name__ == "__main__":
    sys.exit(run())
