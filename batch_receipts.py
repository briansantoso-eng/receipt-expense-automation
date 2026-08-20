"""
STAGE 3 — a folder of receipts becomes one spreadsheet.
----------------------------------------------------------
Stage 1 handles one receipt. A real client hands you 200 a quarter. This runs
Stage 1 + Stage 2 over a whole folder and writes the CSV you'd actually send.

Run it:
    python batch_receipts.py                    # process receipts/, write expenses.csv
    python batch_receipts.py --folder client_a  # a different folder
    python batch_receipts.py --limit 3          # only 3 files, to test cheaply
    python batch_receipts.py --csv-only         # rebuild the CSV, no API calls (free)
    python batch_receipts.py --force            # re-extract even if already done

Three things here matter more than the loop itself:

  1. ALREADY-PROCESSED FILES ARE SKIPPED. Each receipt's result lives in
     output/<name>.json. If a run dies on receipt 180 of 200, the next run
     costs you 20 receipts, not 200. Without this, every crash costs money
     and you eventually stop running it.

  2. ONE BAD FILE CANNOT KILL THE BATCH. A corrupt image, a HEIC someone
     renamed to .jpg, a 401 halfway through — each is caught per-file and
     reported at the end. 199 good rows still land.

  3. THE CSV IS BUILT FROM THE JSON FILES, NOT FROM MEMORY. So --csv-only
     rebuilds it for free, and you can change the CSV layout without
     re-paying for a single extraction.
"""

import os
import csv
import glob
import argparse
import datetime
import collections

import anthropic

from extract_receipt import (
    COST_IN, COST_OUT, DEFAULT_BATCH_SIZE, MEDIA_TYPES, MODEL,
    check_key, extract, extract_batch,
)
from validate import annotate, validate

OUTPUT_DIR = "output"
DEFAULT_FOLDER = "receipts"
DEFAULT_CSV = "expenses.csv"

# Column order for the CSV. Date first because that's how a bookkeeper reads
# it, and status/issues last because they're for you, not the client.
CSV_COLUMNS = [
    "date", "vendor", "category", "subtotal", "tax", "total", "currency",
    "tax_included_in_total", "payment_method", "status", "issues", "source_file",
]


def find_receipts(folder: str, limit: int | None = None) -> list[str]:
    """Every supported image or PDF in the folder, oldest name first so runs
    are reproducible."""
    paths = []
    for path in sorted(glob.glob(os.path.join(folder, "*"))):
        if os.path.isfile(path) and os.path.splitext(path)[1].lower() in MEDIA_TYPES:
            paths.append(path)
    return paths[:limit] if limit else paths


def result_path(receipt_path: str, output_dir: str = OUTPUT_DIR) -> str:
    stem = os.path.splitext(os.path.basename(receipt_path))[0]
    return os.path.join(output_dir, f"{stem}.json")


# The Messages API rejects requests over 32 MB. base64 inflates a file by ~33%,
# so ten 4 MB phone photos is 53 MB encoded — over the limit. The batch would
# fail, fall back to individual calls, and end up costing MORE than not batching
# at all. So group by BYTES as well as count, and stay well under the ceiling.
MAX_REQUEST_BYTES = 20 * 1024 * 1024      # 20 MB encoded, leaving headroom
BASE64_OVERHEAD = 4 / 3


def chunk_receipts(paths: list[str], batch_size: int) -> list[list[str]]:
    """Group receipts into batches bounded by count AND encoded size.

    A single file bigger than the budget still gets its own batch rather than
    being dropped — it will fail on its own and be reported, which is far
    better than silently vanishing from a client's expense report."""
    groups: list[list[str]] = []
    current: list[str] = []
    current_bytes = 0

    for path in paths:
        try:
            encoded = os.path.getsize(path) * BASE64_OVERHEAD
        except OSError:
            encoded = 0

        too_many = len(current) >= batch_size
        too_big = current and (current_bytes + encoded) > MAX_REQUEST_BYTES
        if too_many or too_big:
            groups.append(current)
            current, current_bytes = [], 0

        current.append(path)
        current_bytes += encoded

    if current:
        groups.append(current)
    return groups


def save_record(record: dict, target: str) -> None:
    import json
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, ensure_ascii=False)


def process_folder(folder: str, force: bool, limit: int | None,
                   output_dir: str = OUTPUT_DIR,
                   batch_size: int = DEFAULT_BATCH_SIZE) -> dict:
    """Extract and validate every receipt. Returns a run summary.

    Receipts are sent in groups of batch_size, which amortises the ~1,289-token
    schema across the group (about 60% cheaper at 10 per call). A batch that
    fails for any reason falls back to individual calls for that group only, so
    resilience is unchanged — see extract_batch() for the reasoning."""
    paths = find_receipts(folder, limit)
    if not paths:
        raise SystemExit(
            f"No receipts found in {folder}/\n"
            f"Supported types: {', '.join(sorted(MEDIA_TYPES))}\n"
            f"Run 'python make_sample_receipt.py' to generate a test set.")

    os.makedirs(output_dir, exist_ok=True)

    stats = {"processed": 0, "skipped": 0, "failed": 0,
             "tokens_in": 0, "tokens_out": 0, "errors": []}

    print(f"{len(paths)} receipt(s) in {folder}/")

    todo = []
    for path in paths:
        target = result_path(path, output_dir)
        if os.path.exists(target) and not force:
            print(f"  SKIP    {os.path.basename(path)} "
                  f"(already in {output_dir}/, use --force to redo)")
            stats["skipped"] += 1
        else:
            todo.append(path)

    if not todo:
        print()
        return stats

    groups = chunk_receipts(todo, batch_size)
    sizes = [len(g) for g in groups]
    print(f"{len(todo)} to extract, in {len(groups)} call(s) "
          f"(sizes: {sizes}, capped at {batch_size} or "
          f"{MAX_REQUEST_BYTES // 1024 // 1024} MB per call)")
    print()

    for number, group in enumerate(groups, 1):
        label = f"[batch {number}/{len(groups)}]"
        try:
            if len(group) == 1:
                receipt, response = extract(group[0])
                results = {os.path.basename(group[0]): receipt}
            else:
                results, response = extract_batch(group)

            stats["tokens_in"] += response.usage.input_tokens
            stats["tokens_out"] += response.usage.output_tokens

            for path in group:
                name = os.path.basename(path)
                record = annotate(results[name].model_dump(mode="json"))
                record["source_file"] = name
                save_record(record, result_path(path, output_dir))
                stats["processed"] += 1

                flag = {"ok": "OK", "check": "CHECK",
                        "needs_review": "REVIEW"}[record["status"]]
                print(f"  {label} {flag:<7} {name:<22} "
                      f"{record['currency']} {record['total']:>8,.2f}  "
                      f"{record['vendor'][:24]}")
                for issue in record["issues"]:
                    print(f"                    {issue}")

        except anthropic.APIStatusError as exc:
            stats["failed"] += len(group)
            stats["errors"].append(f"{label}: API {exc.status_code} {exc.message}")
            print(f"  {label} FAILED: API {exc.status_code}")
            if exc.status_code in (401, 403, 429):
                print(f"          {exc.status_code} affects every remaining "
                      f"call — stopping here.")
                break

        except Exception as exc:
            # A batch failure must not lose the whole group. Retry it one at a
            # time: costs more for this group only, and the good receipts land.
            print(f"  {label} batch failed ({type(exc).__name__}: {exc})")
            if len(group) == 1:
                stats["failed"] += 1
                stats["errors"].append(
                    f"{os.path.basename(group[0])}: {type(exc).__name__}: {exc}")
                continue

            print(f"  {label} falling back to {len(group)} individual call(s)")
            for path in group:
                name = os.path.basename(path)
                try:
                    receipt, response = extract(path)
                    stats["tokens_in"] += response.usage.input_tokens
                    stats["tokens_out"] += response.usage.output_tokens
                    record = annotate(receipt.model_dump(mode="json"))
                    record["source_file"] = name
                    save_record(record, result_path(path, output_dir))
                    stats["processed"] += 1
                    print(f"           retry OK    {name}")
                except Exception as inner:
                    stats["failed"] += 1
                    stats["errors"].append(
                        f"{name}: {type(inner).__name__}: {inner}")
                    print(f"           retry FAILED {name}: "
                          f"{type(inner).__name__}")

    return stats


def load_records(output_dir: str = OUTPUT_DIR) -> list[dict]:
    """Read every saved result. Re-validates as it goes, so a change to the
    rules in validate.py takes effect on old extractions for free."""
    import json

    records = []
    for path in sorted(glob.glob(os.path.join(output_dir, "*.json"))):
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  ! couldn't read {path}: {exc}")
            continue
        records.append(annotate(record))

    # Duplicate detection needs to see every record at once, which is why it
    # lives here rather than in validate.py's per-record checks.
    return flag_duplicates(records)


def flag_duplicates(records: list[dict]) -> list[dict]:
    """Same vendor, same date, same total — almost certainly the same receipt
    submitted twice. Clients forward things twice constantly, and a duplicate
    silently inflates a category total, which is the kind of error that costs
    you the client rather than just an afternoon.

    Deliberately FLAGGED, not deleted. Two flat whites at the same cafe on the
    same day for the same price is a real thing that happens, and a tool that
    silently drops legitimate rows is worse than one that asks."""
    groups = collections.defaultdict(list)
    for record in records:
        key = (
            str(record.get("vendor") or "").strip().lower(),
            str(record.get("date")),
            round(float(record.get("total") or 0), 2),
        )
        groups[key].append(record)

    for group in groups.values():
        if len(group) < 2:
            continue
        names = [str(r.get("source_file") or "?") for r in group]
        for record in group:
            others = [n for n in names if n != record.get("source_file")] or names
            record["issues"] = list(record.get("issues") or []) + [
                f"[warning] possible duplicate: same vendor, date and total as "
                f"{', '.join(sorted(set(others)))}"]
            record["duplicate_group"] = True
            if record.get("status") == "ok":
                record["status"] = "check"

    return records


def duplicate_exposure(records: list[dict]) -> tuple[int, dict[str, float]]:
    """How much of the headline total is at risk from suspected duplicates:
    every copy after the first in each group."""
    groups = collections.defaultdict(list)
    for record in records:
        if not record.get("duplicate_group"):
            continue
        key = (str(record.get("vendor") or "").lower(), str(record.get("date")),
               round(float(record.get("total") or 0), 2))
        groups[key].append(record)

    count = 0
    by_currency: dict[str, float] = collections.defaultdict(float)
    for group in groups.values():
        for record in group[1:]:          # the first copy is presumed genuine
            count += 1
            by_currency[record.get("currency", "?")] += float(record.get("total") or 0)
    return count, dict(by_currency)


def write_csv(records: list[dict], out_path: str) -> None:
    """One row per receipt, sorted by date — the order a bookkeeper expects."""
    def sort_key(record):
        try:
            return datetime.date.fromisoformat(str(record.get("date")))
        except (TypeError, ValueError):
            return datetime.date.min   # undated rows float to the top to be seen

    with open(out_path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS,
                                extrasaction="ignore")
        writer.writeheader()
        for record in sorted(records, key=sort_key):
            row = dict(record)
            # A list of issues can't go in a CSV cell as-is.
            row["issues"] = " | ".join(record.get("issues") or [])
            writer.writerow(row)


def print_summary(records: list[dict], out_path: str, stats: dict | None,
                  output_dir: str = OUTPUT_DIR) -> None:
    by_status = collections.Counter(r.get("status", "?") for r in records)

    print()
    print("=" * 64)
    print(f"WROTE {out_path}  —  {len(records)} row(s)")
    print("=" * 64)
    print(f"  ok            {by_status['ok']:>4}   straight to the books")
    print(f"  check         {by_status['check']:>4}   usable, worth a glance")
    print(f"  needs review  {by_status['needs_review']:>4}   don't trust yet")

    # Totals per category, per currency — the numbers a client actually asks
    # for. Rows needing review are excluded, because including a number you
    # don't trust in a headline total is how you lose a client.
    trusted = [r for r in records if r.get("status") != "needs_review"]
    buckets = collections.defaultdict(float)
    for record in trusted:
        key = (record.get("currency", "?"), record.get("category", "other"))
        buckets[key] += float(record.get("total") or 0)

    if buckets:
        print()
        print(f"  Totals by category (excluding {len(records) - len(trusted)} "
              f"row(s) needing review):")
        for (currency, category), amount in sorted(buckets.items()):
            print(f"    {currency}  {category:<24} {amount:>10,.2f}")

        print()
        for currency in sorted({c for c, _ in buckets}):
            total = sum(v for (c, _), v in buckets.items() if c == currency)
            tax = sum(float(r.get("tax") or 0) for r in trusted
                      if r.get("currency") == currency)
            print(f"    {currency}  {'TOTAL':<24} {total:>10,.2f}"
                  f"   (tax {tax:,.2f})")

        dupe_count, dupe_value = duplicate_exposure(trusted)
        if dupe_count:
            print()
            print(f"  !! {dupe_count} suspected duplicate(s) ARE included above:")
            for currency, amount in sorted(dupe_value.items()):
                print(f"     {currency} {amount:,.2f} of the total may be "
                      f"double-counted")
            print(f"     They're flagged, not dropped — two identical purchases "
                  f"on one day")
            print(f"     is legitimate. Delete the real duplicates from "
                  f"{output_dir}/ to correct it.")

    if stats:
        cost = (stats["tokens_in"] * COST_IN
                + stats["tokens_out"] * COST_OUT) / 1_000_000
        print()
        print(f"  API: {stats['processed']} extracted, {stats['skipped']} skipped, "
              f"{stats['failed']} failed")
        print(f"       {stats['tokens_in']:,} in / {stats['tokens_out']:,} out "
              f"= ${cost:.4f} this run")
        if stats["processed"]:
            print(f"       ${cost / stats['processed']:.4f} per receipt "
                  f"({MODEL})")
        for error in stats["errors"]:
            print(f"  ! {error}")

    flagged = [r for r in records if r.get("status") == "needs_review"]
    if flagged:
        print()
        print("  Look at these before sending anything to a client:")
        for record in flagged:
            print(f"    {record.get('source_file', '?')}  —  "
                  f"{'; '.join(record.get('issues') or [])}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract, validate and tabulate a folder of receipts.")
    parser.add_argument("--folder", default=DEFAULT_FOLDER,
                        help=f"folder of receipts (default: {DEFAULT_FOLDER})")
    parser.add_argument("--out", default=DEFAULT_CSV,
                        help=f"CSV to write (default: {DEFAULT_CSV})")
    parser.add_argument("--limit", type=int,
                        help="only process the first N receipts")
    parser.add_argument("--force", action="store_true",
                        help="re-extract receipts already in output/")
    parser.add_argument("--csv-only", action="store_true",
                        help="rebuild the CSV from saved results, no API calls")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"receipts per API call (default: "
                             f"{DEFAULT_BATCH_SIZE}). 1 = one call each, which "
                             f"costs ~2.5x more but isolates every failure.")
    parser.add_argument("--output-dir", default=OUTPUT_DIR,
                        help=f"where results are kept (default: {OUTPUT_DIR}). "
                             f"Stage 4 gives each client their own, so one "
                             f"client's receipts never reach another's CSV.")
    args = parser.parse_args(argv)

    stats = None
    if not args.csv_only:
        print(f"Using API key {check_key()}")
        print()
        stats = process_folder(args.folder, args.force, args.limit,
                               args.output_dir, args.batch_size)

    records = load_records(args.output_dir)
    if not records:
        raise SystemExit(f"Nothing in {args.output_dir}/ to tabulate.")

    write_csv(records, args.out)
    print_summary(records, args.out, stats, args.output_dir)

    # Non-zero if anything needs a human, so Stage 4's scheduled run can react.
    return 1 if any(r.get("status") == "needs_review" for r in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
