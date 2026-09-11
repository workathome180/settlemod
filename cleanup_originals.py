#!/usr/bin/env python3
"""
Delete stored "original file" copies older than a retention window.

SettleMod's opt-in "keep a copy of my original file" feature (see
save_original() in app.py) writes uploads to:

    ORIGINALS_DIR/<sanitized-email>/<UTC-timestamp>__<filename>

...and never deletes them on its own. That's fine right up until it isn't:
left alone forever, this directory grows without bound, and every day that
passes is another day of holding onto a customer's raw PayPal export they
may not even remember opting to keep. This script is the other half of that
feature - something that actually enforces a retention window - since the
Privacy Policy at marketing/privacy.html promises originals are "retained
until you ask us to delete them or your account is closed," and this is
what makes that true in practice rather than by accident.

This is a plain script, not a Flask route or a background thread in the app
itself, on purpose: deleting customer files is exactly the kind of action
that should require a deliberate, visible step (a person or a cron job
running this file) rather than happening silently inside a web request.

Usage:
    # Preview what would be deleted (default: files older than 90 days)
    python3 cleanup_originals.py --dry-run

    # Actually delete files older than 90 days
    python3 cleanup_originals.py

    # Use a different retention window
    python3 cleanup_originals.py --days 30

    # Point at a non-default originals directory (matches ORIGINALS_DIR env
    # var the app itself uses - set this the same way in both places)
    ORIGINALS_DIR=/data/uploads/originals python3 cleanup_originals.py

Suggested production setup: run this on a daily cron / scheduled job on
whatever host runs the Flask app, e.g.:

    0 3 * * * cd /path/to/app && ORIGINALS_DIR=/data/uploads/originals \\
        python3 cleanup_originals.py --days 90 >> /var/log/settlemod-cleanup.log 2>&1

Exit code is 0 on success (including "nothing to delete"), 1 if any file or
directory couldn't be removed - so a cron job's own failure emails/alerts
will actually fire if something's wrong (e.g. a permissions problem).
"""

import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone

# Matches the "%Y%m%d_%H%M%S_%f" prefix that save_original() in app.py
# stamps onto every filename, e.g. "20260826_140512_123456__export.csv".
TIMESTAMP_RE = re.compile(r"^(\d{8}_\d{6}_\d{6})__")


def default_originals_dir() -> str:
    """Mirrors ORIGINALS_DIR's resolution in app.py exactly, so running this
    script with no arguments and no env var targets the same directory the
    app itself would use by default."""
    return os.environ.get(
        "ORIGINALS_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads", "originals"),
    )


def file_age_cutoff_name(filename: str):
    """Parse the UTC timestamp out of a save_original()-style filename.
    Returns a timezone-aware datetime, or None if the filename doesn't match
    the expected pattern (in which case we fall back to mtime instead of
    silently skipping the file - see below)."""
    m = TIMESTAMP_RE.match(filename)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d_%H%M%S_%f").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def iter_stale_files(originals_dir: str, cutoff: datetime):
    """Yield (path, age_source) for every file under originals_dir whose
    effective age is older than `cutoff`. Prefers the timestamp encoded in
    the filename (authoritative - it's when the upload actually happened);
    falls back to the filesystem mtime for any file that doesn't match the
    expected naming pattern, so a stray or manually-added file still gets
    swept up by policy rather than silently living there forever."""
    if not os.path.isdir(originals_dir):
        return
    for entry in sorted(os.listdir(originals_dir)):
        user_dir = os.path.join(originals_dir, entry)
        if not os.path.isdir(user_dir):
            continue
        for fname in sorted(os.listdir(user_dir)):
            fpath = os.path.join(user_dir, fname)
            if not os.path.isfile(fpath):
                continue
            ts = file_age_cutoff_name(fname)
            if ts is None:
                mtime = os.path.getmtime(fpath)
                ts = datetime.fromtimestamp(mtime, tz=timezone.utc)
                source = "mtime (unrecognized filename pattern)"
            else:
                source = "filename timestamp"
            if ts < cutoff:
                yield fpath, source


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--days", type=int, default=90,
        help="Delete originals older than this many days (default: 90).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be deleted without deleting anything.",
    )
    parser.add_argument(
        "--originals-dir", default=None,
        help="Override the originals directory (defaults to ORIGINALS_DIR env "
             "var, then uploads/originals next to this script - same logic app.py uses).",
    )
    args = parser.parse_args()

    originals_dir = args.originals_dir or default_originals_dir()
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)

    print(f"Originals directory: {originals_dir}")
    print(f"Retention window:    {args.days} days (cutoff: {cutoff.isoformat()})")
    print(f"Mode:                {'DRY RUN - nothing will be deleted' if args.dry_run else 'LIVE - files will be deleted'}")
    print()

    if not os.path.isdir(originals_dir):
        print("Originals directory doesn't exist yet - nothing to do.")
        return 0

    deleted = 0
    errors = 0
    stale = list(iter_stale_files(originals_dir, cutoff))

    if not stale:
        print("No stale files found - nothing to delete.")
    else:
        for fpath, source in stale:
            rel = os.path.relpath(fpath, originals_dir)
            if args.dry_run:
                print(f"  would delete: {rel}  (age source: {source})")
                continue
            try:
                os.remove(fpath)
                print(f"  deleted: {rel}  (age source: {source})")
                deleted += 1
            except OSError as e:
                print(f"  ERROR deleting {rel}: {e}", file=sys.stderr)
                errors += 1

    # Clean up now-empty per-email directories (skip in dry-run - nothing
    # was actually removed above, so directories would still be non-empty).
    if not args.dry_run:
        for entry in sorted(os.listdir(originals_dir)):
            user_dir = os.path.join(originals_dir, entry)
            if os.path.isdir(user_dir) and not os.listdir(user_dir):
                try:
                    os.rmdir(user_dir)
                    print(f"  removed empty directory: {entry}/")
                except OSError as e:
                    print(f"  ERROR removing directory {entry}/: {e}", file=sys.stderr)
                    errors += 1

    print()
    if args.dry_run:
        print(f"Dry run complete. {len(stale)} file(s) would be deleted.")
    else:
        print(f"Done. {deleted} file(s) deleted, {errors} error(s).")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
