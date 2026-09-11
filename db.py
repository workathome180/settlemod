"""
Minimal user + credit tracking, backed by SQLite.

Deliberately simple for v1: no passwords, no sessions. A user is identified
by email address alone. This is fine for a low-friction utility tool where
the "account" only needs to remember a credit balance / subscription status
- not for anything requiring real authentication. If this becomes a product
with saved conversion templates or sensitive data, add real auth then.

Schema:
  users(email TEXT PRIMARY KEY, credits INTEGER, subscription_active INTEGER,
        subscription_expires TEXT, stripe_customer_id TEXT)
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

# Configurable so this can point at a persistent volume on a real host - most
# platforms (Render, Railway, Fly.io) wipe local disk on every redeploy, so
# "settlemod.db" living next to the code would silently lose every balance and
# subscription the moment you next ship a change. Point DB_PATH at a mounted
# volume's path (e.g. /data/settlemod.db) once you've attached one. See the
# Deployment section of the README.
DB_PATH = os.environ.get("DB_PATH", "settlemod.db")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                credits INTEGER NOT NULL DEFAULT 0,
                subscription_active INTEGER NOT NULL DEFAULT 0,
                subscription_expires TEXT,
                stripe_customer_id TEXT,
                free_trial_used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                filename TEXT,
                row_count INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_or_create_user(email: str) -> sqlite3.Row:
    email = email.strip().lower()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (email, credits, subscription_active, created_at) "
                "VALUES (?, 0, 0, ?)",
                (email, _now_iso()),
            )
            row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return row


def get_stripe_customer_id(email: str):
    email = email.strip().lower()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT stripe_customer_id FROM users WHERE email = ?", (email,)
        ).fetchone()
        return row["stripe_customer_id"] if row else None


def has_subscription(email: str) -> bool:
    email = email.strip().lower()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT subscription_active, subscription_expires FROM users WHERE email = ?",
            (email,),
        ).fetchone()
        if row is None or not row["subscription_active"]:
            return False
        if row["subscription_expires"]:
            expires = datetime.fromisoformat(row["subscription_expires"])
            if expires < datetime.now(timezone.utc):
                return False
        return True


def get_credits(email: str) -> int:
    row = get_or_create_user(email)
    return row["credits"]


def can_convert(email: str) -> bool:
    return has_subscription(email) or get_credits(email) > 0


def deduct_credit(email: str) -> None:
    """Deduct one credit, unless the user has an active subscription (unlimited)."""
    email = email.strip().lower()
    if has_subscription(email):
        return
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET credits = MAX(credits - 1, 0) WHERE email = ?", (email,)
        )


def grant_free_trial_if_eligible(email: str, n: int) -> bool:
    """Grant the one-time free trial credit if this account hasn't used it
    before. Returns True if the trial was granted just now, False if the
    account already used it (regardless of current balance)."""
    email = email.strip().lower()
    get_or_create_user(email)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT free_trial_used FROM users WHERE email = ?", (email,)
        ).fetchone()
        if row["free_trial_used"]:
            return False
        conn.execute(
            "UPDATE users SET credits = credits + ?, free_trial_used = 1 WHERE email = ?",
            (n, email),
        )
        return True


def add_credits(email: str, n: int) -> None:
    get_or_create_user(email)
    email = email.strip().lower()
    with get_conn() as conn:
        conn.execute("UPDATE users SET credits = credits + ? WHERE email = ?", (n, email))


def remove_credits(email: str, n: int) -> None:
    """Claw back credits (e.g. a refunded purchase, or a free-trial credit that was
    never actually delivered). Clamped at 0 - never goes negative, so an over-eager
    or duplicate refund event can't leave an account owing credits."""
    email = email.strip().lower()
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET credits = MAX(credits - ?, 0) WHERE email = ?", (n, email)
        )


def activate_subscription(email: str, stripe_customer_id: str = None, days: int = 32) -> None:
    """32-day grace window so a slightly-late renewal webhook doesn't lock a
    paying customer out; Stripe's own renewal will refresh this before it
    matters in the normal case."""
    get_or_create_user(email)
    email = email.strip().lower()
    expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET subscription_active = 1, subscription_expires = ?, "
            "stripe_customer_id = COALESCE(?, stripe_customer_id) WHERE email = ?",
            (expires, stripe_customer_id, email),
        )


def cancel_subscription(email: str) -> None:
    email = email.strip().lower()
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET subscription_active = 0 WHERE email = ?", (email,)
        )


def log_conversion(email: str, filename: str, row_count: int) -> None:
    email = email.strip().lower()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO conversions (email, filename, row_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            (email, filename, row_count, _now_iso()),
        )
