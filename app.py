"""
SettleMod v1 - Flask app with real credit tracking, Stripe payments, and
email-verified access wired in.

New in v1 (vs. the standalone prototype):
  - Email verification: a 6-digit code must be confirmed before an email can
    convert files, check its balance, or buy credits - closes the gap where
    anyone could act as anyone else just by typing their address in a form.
  - Email-gated conversions: users get 1 free trial conversion, then need
    credits or an active subscription
  - Stripe Checkout for single / 10-pack / monthly plans
  - Stripe webhook that credits the account after successful payment
  - SQLite persistence (db.py) so balances survive restarts

Setup:
  pip install -r requirements.txt --break-system-packages
  export SECRET_KEY=...                        # signs verification codes and
                                                 # session tokens - see below
  export STRIPE_SECRET_KEY=sk_test_...
  export STRIPE_WEBHOOK_SECRET=whsec_...       # from `stripe listen` in dev
  export SMTP_HOST=... SMTP_PORT=... SMTP_USER=... SMTP_PASSWORD=... SMTP_FROM=...
  python app.py

Without SMTP_* configured, verification codes are printed to the console
instead of emailed - that's fine for local development, but real users can't
receive a real code without real SMTP settings.

SECRET_KEY: generate one with `python -c "import secrets; print(secrets.token_hex(32))"`
and set it in production. If unset, a random key is generated at startup for
local dev convenience - meaning every restart invalidates every outstanding
code and every logged-in session. Fine on your laptop; never acceptable on a
real deployment, where restarts happen without warning (deploys, crashes) and
would silently log everyone out and break in flight verifications.

Stripe setup checklist (do this before going live):
  1. Create 3 Products in the Stripe dashboard: "Single Conversion" ($9,
     one-time), "10 Conversion Pack" ($19, one-time), "SettleMod Monthly" ($59/mo,
     recurring). This is SettleMod's own standalone subscription, separate from the
     other four apps' shared "All-Access" ($99/mo) bundle - it stacks with
     any or none of them, so it needs its own dedicated Price ID, not a reused one.
  2. Copy each Price ID into PRICE_IDS below (or set the env vars named).
  3. Add a webhook endpoint pointing at /webhook, listening for
     checkout.session.completed, charge.refunded, and customer.subscription.deleted.
     Copy its signing secret into STRIPE_WEBHOOK_SECRET.
  4. For local testing: `stripe listen --forward-to localhost:5000/webhook`
"""

import base64
import io
import os
import secrets
import json
import smtplib
import urllib.request
import urllib.error
import urllib.parse
import time
from collections import defaultdict, deque
from datetime import datetime
from email.message import EmailMessage
from functools import wraps

from flask import Flask, jsonify, render_template, request, send_file, send_from_directory
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

import db
from transform import ConversionError, convert_amazon_settlement_to_qbo

app = Flask(__name__)
# Render/Railway/Fly.io (the hosts this README recommends) all sit one reverse proxy in
# front of the app, setting X-Forwarded-For. Without this, request.remote_addr would be
# the proxy's own IP for every visitor - meaning the rate limiter below would either
# lump every real user together under one "IP", or (worse) not distinguish attackers
# from anyone else at all. x_for=1 trusts exactly one hop, matching that single-proxy
# setup; raise it if you ever add another proxy layer (e.g. a CDN) in front of that.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)
# 2MB ceiling - generous headroom over the ~350KB QBO spec limit documented in
# transform.py, but enough to stop an oversized (or malicious) upload from being
# read fully into memory. Flask rejects anything larger with 413 before the body
# is even read.
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
db.init_db()

SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
if not os.environ.get("SECRET_KEY"):
    print(
        "WARNING: SECRET_KEY is not set - using a random key for this process only. "
        "Every restart will invalidate all pending verification codes and logged-in "
        "sessions. Set SECRET_KEY before deploying anywhere real."
    )
app.secret_key = SECRET_KEY
_serializer = URLSafeTimedSerializer(SECRET_KEY)

CODE_MAX_AGE_SECONDS = 10 * 60  # a requested code is good for 10 minutes
SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60  # a verified session lasts 30 days

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
if STRIPE_SECRET_KEY:
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

# Replace with real Price IDs from the Stripe dashboard before launch.
PRICE_IDS = {
    "single": os.environ.get("STRIPE_PRICE_SINGLE", "price_REPLACE_ME_SINGLE"),
    "pack10": os.environ.get("STRIPE_PRICE_PACK10", "price_REPLACE_ME_PACK10"),
    "monthly_settlemod": os.environ.get("STRIPE_PRICE_MONTHLY_SETTLEMOD", "price_REPLACE_ME_MONTHLY_SETTLEMOD"),
    "monthly_all4": os.environ.get("STRIPE_PRICE_MONTHLY_ALL4", "price_REPLACE_ME_MONTHLY_ALL4"),
}

# Identifies which app a checkout came from. FileMod/ShopMod/TillMod/PayMod/SettleMod
# all share one Stripe account, so Stripe broadcasts every webhook event to every
# app's /webhook endpoint, not just the one that created the checkout - this tag is
# how the webhook below tells "a single/10-pack purchase made on this app" apart
# from "the same kind of purchase made on a sibling app." SettleMod is deliberately
# NOT part of the other four apps' shared "monthly" All-Access bundle by default -
# it has its own standalone paid tier, "monthly_settlemod", priced and purchased
# independently, gated by origin_app the same way single/pack10 are (defense in
# depth - no sibling app ever sends this plan name, but there's no reason not to
# check). "monthly_all4" is the bigger bundle - its plan-key string is a historical
# name from when it covered four apps; it's kept as-is (rather than renamed to
# "monthly_all5") purely because the other four apps' webhook code already matches
# this exact string, and it activates here regardless of which app's checkout the
# customer actually used, same as on the other four.
APP_NAME = "settlemod"

PLAN_CREDITS = {"single": 1, "pack10": 10}  # monthly plans grant a subscription, not credits

FREE_TRIAL_CREDITS = 1

# v1 ships without a subscriber gift workbook - PayMod shipped the same way
# initially ("out of scope for v1") and got one added later once the core
# converter was proven out. Revisit once SettleMod's own categorization
# cheat sheet is built.

SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
SMTP_FROM = os.environ.get("SMTP_FROM", "no-reply@digitalbuilds.org")

# Originals are NOT kept by default (see the "processed and discarded" promise on the
# site). A user can opt in per-conversion via the "keep a copy" checkbox, in which case
# the untouched upload is saved here rather than being processed purely in memory.
ORIGINALS_DIR = os.environ.get(
    "ORIGINALS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads", "originals")
)
os.makedirs(ORIGINALS_DIR, exist_ok=True)
os.chmod(ORIGINALS_DIR, 0o700)


def save_original(email, filename, file_bytes):
    """Persist an opted-in upload's original bytes, untouched, so it can be pulled back
    up if the user isn't happy with a conversion - e.g. re-run it, or look at exactly
    what they sent, without asking them to dig up and re-upload the file themselves.

    `email` here is always the verified email from the caller's session token, never a
    client-supplied form field - see require_verified_email(). That's what makes this
    safe to organize by email at all: only whoever proved control of that inbox (via
    /request-code + /verify-code) can ever cause a file to land in that folder.

    Organized one subfolder per email, so every original a given customer has sent
    lives in one place. Filenames are sanitized so this can't be used to write outside
    ORIGINALS_DIR or collide across uploads.

    Security: this directory is never served by any Flask route (the app only exposes
    /, /request-code, /verify-code, /account, /convert, /create-checkout-session,
    /webhook - nothing maps to ORIGINALS_DIR, unlike Flask's auto-served /static
    folder), so there's no URL that reaches these files. Both the per-user folder and
    the file itself are locked down to the app's own OS user (0700 / 0600) so nothing
    else on the machine - another local user, another process - can read them either.
    That covers "not publicly or locally reachable"; it does NOT cover disk encryption
    or backups. Before this holds real customers' data in production, put
    ORIGINALS_DIR on an encrypted volume (most hosts - Render, Railway, Fly.io -
    encrypt disks at rest by default, but confirm it) and decide how long originals
    should be kept before being purged - see the Deployment section of the README for
    the same caveat about SQLite on ephemeral filesystems, which applies here too.
    """
    safe_name = secure_filename(filename) or "upload.csv"
    # "@" -> "_at_" first so the folder name still reads as an email at a glance
    # (secure_filename would otherwise just drop the "@" outright).
    safe_email = secure_filename(email.replace("@", "_at_")) or "unknown"
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")

    user_dir = os.path.join(ORIGINALS_DIR, safe_email)
    os.makedirs(user_dir, exist_ok=True)
    os.chmod(user_dir, 0o700)

    dest = os.path.join(user_dir, f"{stamp}__{safe_name}")
    with open(dest, "wb") as fh:
        fh.write(file_bytes)
    os.chmod(dest, 0o600)
    return dest


def send_verification_email(email, code, link):
    """Email a verification link (with the code embedded, so clicking it
    auto-verifies with no typing) to `email` via the SendGrid API (HTTPS),
    since this host's outbound SMTP ports are blocked. The raw code is also
    included as a fallback for email clients that strip query strings from
    links, or for verifying on a different device than the link opens on.
    Falls back to printing both to the console when SENDGRID_API_KEY isn't
    configured."""
    api_key = os.environ.get("SENDGRID_API_KEY")
    from_email = os.environ.get("SMTP_FROM", "no-reply@digitalbuilds.org")

    if not api_key:
        print(f"[DEV] verification link for {email}: {link} (code: {code}, SENDGRID_API_KEY not configured)")
        return

    payload = {
        "personalizations": [{"to": [{"email": email}]}],
        "from": {"email": from_email},
        "subject": "Verify your email for SettleMod",
        "content": [{
            "type": "text/plain",
            "value": (
                f"Click to verify your email:\n{link}\n\n"
                f"Or enter this code on the page instead: {code}\n\n"
                "This link/code expires in 10 minutes. If you didn't request this, you can ignore this email."
            ),
        }],
    }

    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"SendGrid API error {e.code}: {e.read().decode('utf-8', 'ignore')}") from e


def send_conversion_email(email, filename, csv_text):
    """Email the converted QBO file to `email` as an attachment, so it's easy to
    find later without digging through browser downloads. Best-effort: a
    failure here must never block the actual conversion response, since the
    file has already downloaded successfully in the browser regardless."""
    api_key = os.environ.get("SENDGRID_API_KEY")
    from_email = os.environ.get("SMTP_FROM", "no-reply@digitalbuilds.org")

    if not api_key:
        print(f"[DEV] would have emailed {filename} to {email} (SENDGRID_API_KEY not configured)")
        return

    payload = {
        "personalizations": [{"to": [{"email": email}]}],
        "from": {"email": from_email},
        "subject": "Your SettleMod file is ready",
        "content": [{
            "type": "text/plain",
            "value": "Your converted QuickBooks-ready file is attached, in case it's easier to find here than in your downloads.",
        }],
        "attachments": [{
            "content": base64.b64encode(csv_text.encode("utf-8")).decode("ascii"),
            "filename": filename,
            "type": "text/csv",
            "disposition": "attachment",
        }],
    }

    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        detail = e.read().decode("utf-8", "ignore") if isinstance(e, urllib.error.HTTPError) else str(e)
        print(f"[WARN] Failed to email converted file to {email}: {detail}")


def require_verified_email():
    """Pull the caller's verified email out of the `Authorization: Bearer <token>`
    header, issued by /verify-code. Returns the email on success, or None if the
    token is missing, malformed, or older than SESSION_MAX_AGE_SECONDS.

    This - not any email field in the request body/query string - is the only source
    of truth for "who is making this request" on every route that spends credits,
    reads balances, or writes files. A client-supplied email alone proves nothing;
    only a token minted after a real code round-trip does.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[len("Bearer "):]
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    return data.get("email")


def verified_email_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        email = require_verified_email()
        if not email:
            return jsonify({"error": "Please verify your email first.", "code": "unverified"}), 401
        return fn(email, *args, **kwargs)

    return wrapped


@app.errorhandler(413)
def file_too_large(_e):
    return jsonify({"error": "File is too large. Max upload size is 2MB."}), 413


# --- Minimal in-memory rate limiting -----------------------------------------------
# Deliberately dependency-free (no Flask-Limiter) so it runs with nothing beyond what's
# already in requirements.txt. This is a mitigation, not a fix on its own: combined
# with email verification below, it means a would-be attacker can no longer act as
# someone else at all (verification), and can't hammer the code-request endpoint to
# spam an inbox or brute-force a 6-digit code within its 10-minute window (rate
# limiting). It's also per-process, per-IP memory: it resets on restart, doesn't share
# state across multiple server instances, and can't tell apart different people behind
# the same NAT/office IP (they'll share a limit). Fine for a single-instance v1;
# revisit (e.g. Redis-backed) if this ever runs on more than one instance.
_rate_limit_hits = defaultdict(deque)


def rate_limit(max_requests, per_seconds):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            key = (fn.__name__, request.remote_addr)
            now = time.monotonic()
            hits = _rate_limit_hits[key]
            while hits and now - hits[0] > per_seconds:
                hits.popleft()
            if len(hits) >= max_requests:
                return jsonify({"error": "Too many requests. Please slow down and try again shortly."}), 429
            hits.append(now)
            return fn(*args, **kwargs)

        return wrapped

    return decorator


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/privacy")
def privacy():
    return send_from_directory(app.static_folder, "privacy.html")


@app.route("/terms")
def terms():
    return send_from_directory(app.static_folder, "terms.html")


@app.route("/request-code", methods=["POST"])
@rate_limit(max_requests=5, per_seconds=300)  # 5 codes per 5 minutes per IP
def request_code():
    data = request.json or {}
    email = (data.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"error": "A valid email is required."}), 400

    code = f"{secrets.randbelow(1_000_000):06d}"
    receipt = _serializer.dumps({"email": email, "code": code})
    link = f"{request.host_url}?verify_receipt={urllib.parse.quote(receipt)}&verify_code={code}"

    try:
        send_verification_email(email, code, link)
    except Exception as e:
        return jsonify({"error": f"Could not send verification email: {e}"}), 502

    return jsonify({"receipt": receipt})


@app.route("/verify-code", methods=["POST"])
@rate_limit(max_requests=10, per_seconds=300)
def verify_code():
    data = request.json or {}
    receipt = data.get("receipt") or ""
    submitted_code = (data.get("code") or "").strip()

    try:
        payload = _serializer.loads(receipt, max_age=CODE_MAX_AGE_SECONDS)
    except SignatureExpired:
        return jsonify({"error": "That code has expired. Request a new one."}), 400
    except BadSignature:
        return jsonify({"error": "Invalid verification request."}), 400

    if not submitted_code or submitted_code != payload.get("code"):
        return jsonify({"error": "Incorrect code."}), 400

    email = payload["email"]
    session_token = _serializer.dumps({"email": email})
    return jsonify({"token": session_token, "email": email})


@app.route("/account")
@verified_email_required
def account(email):
    row = db.get_or_create_user(email)
    return jsonify(
        {
            "email": email,
            "credits": row["credits"],
            "subscription_active": bool(db.has_subscription(email)),
        }
    )


@app.route("/convert", methods=["POST"])
@rate_limit(max_requests=10, per_seconds=60)
@verified_email_required
def convert(email):
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400

    uploaded = request.files["file"]
    if uploaded.filename == "":
        return jsonify({"error": "No file selected."}), 400

    db.get_or_create_user(email)
    used_free_trial = False
    if not db.can_convert(email):
        # Only ever grants once per account, tracked by a persistent flag -
        # not inferred from balance, so it can't be re-triggered by simply
        # running credits back down to zero.
        used_free_trial = db.grant_free_trial_if_eligible(email, FREE_TRIAL_CREDITS)

    if not db.can_convert(email):
        return (
            jsonify({"error": "No conversion credits remaining. Purchase more above."}),
            402,
        )

    file_bytes = uploaded.read()

    keep_original = request.form.get("keep_original", "").strip().lower() in ("1", "true", "on", "yes")
    if keep_original:
        # Saved regardless of whether conversion below succeeds - the point is to
        # preserve exactly what the user uploaded.
        save_original(email, uploaded.filename, file_bytes)

    try:
        csv_text, warnings = convert_amazon_settlement_to_qbo(file_bytes)
    except ConversionError as e:
        if used_free_trial:
            # Nothing was actually delivered - refund the credit but leave
            # free_trial_used set so it can't be claimed again.
            db.remove_credits(email, FREE_TRIAL_CREDITS)
        return jsonify({"error": str(e)}), 422

    db.deduct_credit(email)
    row_count = max(csv_text.count("\n") - 1, 0)  # minus header row
    db.log_conversion(email, uploaded.filename, row_count)
    send_conversion_email(email, "qbo_journal_entries.csv", csv_text)

    buf = io.BytesIO(csv_text.encode("utf-8"))
    buf.seek(0)

    response = send_file(
        buf,
        mimetype="text/csv",
        as_attachment=True,
        download_name="qbo_journal_entries.csv",
    )
    if warnings:
        response.headers["X-Conversion-Warnings"] = " | ".join(warnings)
    response.headers["X-Free-Trial-Used"] = "true" if used_free_trial else "false"
    return response


@app.route("/create-checkout-session", methods=["POST"])
@verified_email_required
def create_checkout_session(email):
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe is not configured on this server yet."}), 501

    data = request.json or {}
    plan = data.get("plan")

    if plan not in PRICE_IDS:
        return jsonify({"error": "Unknown plan."}), 400

    mode = "subscription" if plan in ("monthly_settlemod", "monthly_all4") else "payment"

    session_kwargs = dict(
        line_items=[{"price": PRICE_IDS[plan], "quantity": 1}],
        mode=mode,
        customer_email=email,
        allow_promotion_codes=True,
        metadata={"plan": plan, "email": email, "app": APP_NAME},
        success_url=request.host_url + "?checkout=success",
        cancel_url=request.host_url + "?checkout=cancelled",
    )
    if mode == "payment":
        # Checkout Session metadata does NOT automatically propagate to the Charge
        # object a later charge.refunded event carries - only PaymentIntent metadata
        # does. Set it here too so the refund handler below can attribute a refund
        # back to (plan, email, app) without an extra API round-trip at refund time.
        session_kwargs["payment_intent_data"] = {
            "metadata": {"plan": plan, "email": email, "app": APP_NAME}
        }

    session = stripe.checkout.Session.create(**session_kwargs)
    return jsonify({"url": session.url})


@app.route("/create-billing-portal-session", methods=["POST"])
@verified_email_required
def create_billing_portal_session(email):
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe is not configured on this server yet."}), 501

    customer_id = db.get_stripe_customer_id(email)
    if not customer_id:
        return jsonify({"error": "No subscription found for this account."}), 404

    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=request.host_url,
    )
    return jsonify({"url": session.url})


@app.route("/webhook", methods=["POST"])
def webhook():
    if not STRIPE_SECRET_KEY or not STRIPE_WEBHOOK_SECRET:
        return jsonify({"error": "Webhook not configured."}), 501

    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        return jsonify({"error": "Invalid webhook signature."}), 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        metadata = session.get("metadata") or {}
        plan = metadata.get("plan")
        origin_app = metadata.get("app")
        email = metadata.get("email") or session.get("customer_email")
        customer_id = session.get("customer")

        if email and plan == "monthly_settlemod" and origin_app == APP_NAME:
            # SettleMod's own standalone subscription tier - not part of the
            # other four apps' shared "monthly" All-Access bundle, so this
            # only activates when the purchase actually happened here.
            db.activate_subscription(email, stripe_customer_id=customer_id)
        elif email and plan == "monthly_all4":
            # Bigger bundle: unlocks all five apps, so this activates here
            # regardless of which app's checkout the customer actually used -
            # Stripe delivers this same event to every app's webhook.
            db.activate_subscription(email, stripe_customer_id=customer_id)
        elif email and plan in PLAN_CREDITS and origin_app == APP_NAME:
            # Single/10-pack purchases are per-app. Stripe also delivers this
            # event to the other two apps' webhooks (same Stripe account), so
            # only grant credits here if this purchase was actually made on
            # this app - otherwise a purchase on a sibling app would silently
            # grant free credits here too.
            db.add_credits(email, PLAN_CREDITS[plan])

    elif event["type"] == "charge.refunded":
        charge = event["data"]["object"]
        metadata = charge.get("metadata") or {}
        plan = metadata.get("plan")
        origin_app = metadata.get("app")
        email = metadata.get("email")

        # Only claw back credits on a FULL refund. A partial refund (a support
        # agent knocking a few dollars off, say) doesn't cleanly map to "take back
        # N credits" - handle those manually rather than guess. amount/amount_refunded
        # are both in cents, straight from Stripe.
        is_full_refund = charge.get("amount_refunded") == charge.get("amount")

        if email and plan in PLAN_CREDITS and origin_app == APP_NAME and is_full_refund:
            db.remove_credits(email, PLAN_CREDITS[plan])

    elif event["type"] in ("customer.subscription.deleted", "customer.subscription.paused"):
        sub = event["data"]["object"]
        customer_id = sub.get("customer")
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT email FROM users WHERE stripe_customer_id = ?", (customer_id,)
            ).fetchone()
            if row:
                db.cancel_subscription(row["email"])

    return jsonify({"received": True})


if __name__ == "__main__":
    # Opt-in only: export FLASK_DEBUG=true for local development. Never enable this on
    # anything reachable outside your own machine - see the comment on file_too_large's
    # neighbors above and the README for why.
    debug_mode = os.environ.get("FLASK_DEBUG", "").strip().lower() in ("1", "true", "yes")
    app.run(debug=debug_mode, port=5000)
