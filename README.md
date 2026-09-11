# SettleMod — Amazon Settlement Report → QuickBooks Online journal entry CSV

v1: a working converter with real credit tracking, Stripe payments, and
email verification wired in — one free trial conversion per email, then
pay-per-use, a 10-pack, or an unlimited monthly subscription.

## What's here

- `transform.py` — the actual conversion logic. Pure Python, no dependencies
  beyond the standard library. This is the core IP; everything else is
  plumbing around it.
- `db.py` — SQLite-backed user/credit tracking. Email-only accounts (no
  passwords) — enough to track balances and subscription status, not a full
  auth system.
- `app.py` — Flask web app: upload UI, `/request-code` + `/verify-code`
  (email verification), `/convert` (credit-gated), `/account` (balance
  lookup), `/create-checkout-session` (Stripe Checkout), and `/webhook`
  (credits/activates subscriptions after a real payment).
- `templates/index.html` — the upload page: verify your email, then upload,
  with working Buy buttons.
- `samples/sample_amazon_settlement.csv` — a realistic fake Amazon Flat File
  V2 Settlement Report you can test with immediately.

## Email verification

Every identity-sensitive route (`/convert`, `/account`,
`/create-checkout-session`) requires proof that you actually control the
email you're using — not just an email string typed into a form. Without
this, anyone who knew or guessed another person's email could spend their
credits, see their balance, or (once "keep a copy" existed) have a file
planted in their archive folder under that person's name.

- `/request-code` emails (or in dev, prints to the console — see below) a
  6-digit code and returns an opaque signed "receipt" to the browser.
- `/verify-code` checks the submitted code against the receipt. On success it
  issues a signed session token (30-day expiry) that the browser stores in
  `localStorage` and sends as `Authorization: Bearer <token>` on every
  request from then on.
- Every route that touches credits, balances, or files derives "who is this"
  from that token alone — never from a client-supplied email field. See the
  `require_verified_email()` / `verified_email_required` decorator in
  `app.py`.

Required setup:

```
export SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
```

Without `SECRET_KEY` set, a random one is generated each time the process
starts — fine for a quick local test, but it means every restart silently
invalidates every outstanding code and logs everyone out. Never run without
an explicit `SECRET_KEY` anywhere that isn't your own machine.

To actually send codes by email, set:

```
export SMTP_HOST=... SMTP_PORT=587 SMTP_USER=... SMTP_PASSWORD=... SMTP_FROM=no-reply@digitalbuilds.org
```

Without `SMTP_HOST` set, codes print to the server console instead
(`[DEV] verification code for x@y.com: 123456`) — that's how local
development and the tests for this feature work without any real email
setup, but it means real users can't receive a real code until SMTP is
configured. In practice the sibling apps use the SendGrid HTTPS API instead
of SMTP directly (see `send_verification_email()` in `app.py`) since this
host's outbound SMTP ports are blocked — set `SENDGRID_API_KEY` for that path.

## How the credit system works

- First conversion for a brand-new email is free (tracked by a persistent
  `free_trial_used` flag — not inferred from balance, so it can only ever
  fire once per account, even if credits later hit zero again).
- After that, `/convert` returns HTTP 402 if the account has no credits and
  no active subscription.
- A successful Stripe Checkout fires `checkout.session.completed`, which the
  `/webhook` route uses to add credits (single/10-pack) or activate a
  subscription (monthly), keyed by the email passed into Checkout.
- Subscriptions get a 32-day grace window past their last confirmed webhook,
  so a slightly-delayed renewal event doesn't lock out a paying customer.
  Cancellations arrive via `customer.subscription.deleted`.

This was tested end-to-end (free trial grant, exhaustion, and the real
`/convert` HTTP path) with Flask's test client — not just read over.

## Keeping a copy of the original file (opt-in)

By default, uploads are processed in memory and never written to disk — the
"processed and discarded" line on both pages is literally true unless a user
checks the box.

- A "Keep a copy of my original file" checkbox on the upload form is
  unchecked by default. When checked, `/convert` saves the untouched upload
  via `save_original()` in `app.py` before attempting conversion, so it's
  captured even if the conversion itself fails.
- Files land in `uploads/originals/<email>/<timestamp>__<filename>`, one
  subfolder per email — the point is that if a customer says a conversion
  came out wrong, you can go find exactly what they sent without asking them
  to dig it up and re-send it.
- `uploads/` is gitignored — these can be real Amazon settlement exports
  with real customer/financial data, and should never end up in git.
- Not web-reachable: no Flask route serves this directory (unlike Flask's
  auto-served `static/` folder), so there's no URL that reaches these files.
- Locked to the app's own OS user: both the per-email folder (0700) and each
  file (0600) are chmod'd on write, so no other local user or process on the
  same machine can read them either.
- What this does **not** cover: disk encryption. Before this holds real
  customers' data in production, put `uploads/` on an encrypted volume (most
  hosts — Render, Railway, Fly.io — encrypt at rest by default, but confirm
  it).
- Retention: `cleanup_originals.py` deletes originals older than a chosen
  window (default 90 days) and prunes any per-email folder that ends up
  empty. Run it by hand:
  ```
  python3 cleanup_originals.py --dry-run       # preview, deletes nothing
  python3 cleanup_originals.py --days 30       # actually delete, custom window
  ```
  or point a daily cron job at it in production. The Privacy Policy
  (`marketing/privacy.html`) says originals are "retained until you ask us
  to delete them or your account is closed" — if you start running this on
  a schedule, update that page to say so.

## Running it locally

```bash
pip install -r requirements.txt --break-system-packages
python app.py
```

Then open `http://localhost:5000`, drop in `samples/sample_amazon_settlement.csv`
(or a real Amazon Settlement Report export), and it'll download a QBO
journal-entry-ready CSV.

`python app.py` runs Flask's built-in dev server — fine for local testing,
not for production (it's single-threaded and not hardened for real traffic).

### Running it in production

The repo includes a `Procfile` for platforms that use one (Render, Railway,
Heroku-style buildpacks) plus `gunicorn` in `requirements.txt`:

```
web: gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60
```

If your host doesn't read a `Procfile`, use that same command as the start
command. Also set these environment variables before the first deploy:

- `SECRET_KEY` — required. The app refuses to run safely without it.
- `DB_PATH` — set this to a path on **persistent** storage if your host's
  local disk is wiped on redeploy (Render, Railway, and Fly.io all do this
  by default unless you attach a volume). Otherwise credits and conversion
  history reset on every deploy.
- Stripe and SendGrid/SMTP env vars as described above.

## The QBO journal entry format this targets

Targets QuickBooks Online Advanced's native journal-entry CSV importer
(Settings → Import Data → Journal Entries) - the only QBO tier with a bulk
CSV path for journal entries; other tiers need manual entry or a
third-party importer (Transaction Pro, SaasAnt), both of which accept this
same layout:

- Columns: `JournalNo, JournalDate, AccountName, Debits, Credits,
  Description, Name, Class`
- UTF-8 encoding, dates as `MM/DD/YYYY`
- **Debits always equal credits** - every line item is posted against an
  "Amazon Clearing Account" with the opposite entry, so the whole journal
  entry balances by construction rather than by hoping the source file's
  rows happen to add up (see the module docstring in `transform.py` for the
  full reasoning). What IS validated against the file itself: the line
  items must sum to the settlement's own reported `total-amount` (the real
  deposit), or the file is rejected as partial/inconsistent.
- **Verify Amazon's exact column names against a real export before going
  live** - see the docstring at the top of `transform.py` for exactly which
  columns it looks for, how leniently it matches them, and the full
  amount-type/amount-description → account mapping table. Amazon has
  changed this report's format before (V1 → V2 flat file); this parser
  fails loudly rather than silently mis-mapping money if a required column
  goes missing or a category it's never seen shows up (it falls back to a
  clearly-labeled "Amazon Other Adjustments" account with a warning, rather
  than dropping the line or guessing wrong).

## Setup to actually take payments

1. Use the existing DigitalBuilds Stripe account (shared with
   FileMod/ShopMod/TillMod/PayMod - test mode first).
2. Create 3 Products/Prices: "Single Conversion" ($9 one-time), "10
   Conversion Pack" ($19 one-time), "SettleMod Monthly" ($59/mo, recurring).
   Do **not** create a new price for the bundle tier -
   `STRIPE_PRICE_MONTHLY_ALL4` should point at the same shared All-Access
   bundle Price ID the other four apps already use, since Stripe broadcasts
   that plan's webhook event to every app's endpoint.
3. Set environment variables before running:
   ```
   export STRIPE_SECRET_KEY=sk_test_...
   export STRIPE_WEBHOOK_SECRET=whsec_...
   export STRIPE_PRICE_SINGLE=price_...
   export STRIPE_PRICE_PACK10=price_...
   export STRIPE_PRICE_MONTHLY_SETTLEMOD=price_...
   export STRIPE_PRICE_MONTHLY_ALL4=price_...       # the existing shared bundle price
   ```
4. For local webhook testing: `stripe listen --forward-to localhost:5000/webhook`
   (the Stripe CLI prints the `whsec_...` value to use above).
5. In production, point a Stripe webhook endpoint at
   `https://settlemod.digitalbuilds.org/webhook`, listening for
   `checkout.session.completed`, `customer.subscription.deleted`, and
   `charge.refunded`.

Without these env vars set, the app still runs fine for testing the
converter itself — `/create-checkout-session` just returns a clear 501
instead of erroring unpredictably.

## Legal pages

`marketing/privacy.html` and `marketing/terms.html` are a real Privacy
Policy and Terms of Service, styled to match the site, describing what the
app actually does: email verification, credit tracking, the opt-in
"keep a copy of my original file" feature, and Stripe for payments. The app
itself serves the same pages at `/privacy` and `/terms` (via
`static/privacy.html` and `static/terms.html`).

**Before launch:**

- Both pages currently point to `privacy@digitalbuilds.org` and
  `support@digitalbuilds.org` - real, monitored inboxes, already set up for
  the sibling apps.
- These are a solid starting draft, not a substitute for a lawyer - worth a
  quick review once you're taking real payments.

## What's still not done

1. **Verify against a real Amazon export.** `transform.py`'s column-matching
   and the entire amount-type/amount-description → account mapping table
   were written from Amazon's publicly documented Flat File V2 Settlement
   Report conventions, not a live sample file — pull a real settlement
   report from a Seller Central account and confirm actual header names and
   category values match before trusting this with real customer data.
   This is the single most important thing to do before launch, same
   caveat PayMod carried for Gusto before it had a real sample.
2. **A `.amazon-to-quickbooks-guide.html` page** doesn't exist yet on the
   marketing site — the "Free guide" box on this app's landing page links to
   it already, so it 404s until that page is built (same pattern as the
   existing platform guides).
3. **GitHub repo + Render deployment.** Not created yet - needs a new
   private repo and a new Render web service, same as the other four apps.
4. **Stripe products + DNS.** Create the 3 new Test-mode Products/Prices
   (see "Setup to actually take payments" above), wire a webhook endpoint,
   then repeat in Live mode when ready to accept real payments. Add the
   `settlemod.digitalbuilds.org` CNAME record on Wix once the Render service
   exists.
5. **No subscriber gift workbook yet.** The other four apps each ship a
   two-tab categorization cheat sheet for active monthly subscribers;
   SettleMod launches without one, same as PayMod did initially - can be
   added later once the core converter is proven out.
6. **Homepage storefront card** on `digitalbuilds-site/index.html` needs a
   5th product card added, and the other four apps' own "All-Access" bundle
   footnote copy should mention Amazon alongside PayPal/Shopify/Square/
   Payroll once this ships (same update each app got when PayMod joined).

## Legal/ToS check: Amazon and Intuit

Following the same reasoning already applied to FileMod/ShopMod/TillMod/PayMod
for PayPal/Shopify/Square/Gusto - neither Amazon's Business Solutions
Agreement nor Intuit's App Center terms specifically address a tool like
SettleMod, but the relevant pieces:

- **Amazon's Business Solutions Agreement / Seller Central Terms** restrict
  automated/programmatic access to Seller Central and the Selling Partner
  API without proper authorization (an SP-API developer account, OAuth,
  etc.). SettleMod does neither - it never touches Amazon's site or API at
  all. The user exports their own Settlement Report from their own Seller
  Central account by hand (Reports → Payments → Date Range Reports) and
  uploads it here; that's the same category of action as opening the file
  in Excel. No API access, no scraping, no stored Amazon credentials
  anywhere in this codebase.
- **Amazon's Data Protection Policy** is more detailed than Gusto's or the
  other platforms' about how a seller's data may be used by third-party
  tools, but it specifically governs apps that connect via SP-API/OAuth and
  ingest a seller's data programmatically - it doesn't reach a tool the
  seller manually uploads their own already-exported file to. Worth
  revisiting if SettleMod ever adds direct SP-API integration (auto-pulling
  settlement reports instead of a manual upload) - that would put it
  squarely under this policy and require actual developer registration.
- **Intuit's App Center terms** govern apps that integrate with the
  QuickBooks Online *API* (OAuth connections, listed in the App Center).
  SettleMod isn't one of those - it produces a plain CSV, imported through
  QuickBooks Online Advanced's own journal-entry importer (or a third-party
  importer app for other QBO tiers) - worth calling out explicitly in
  marketing copy so customers on lower QBO tiers know they'll need one of
  those to actually use the output file.
- **Trademark usage** is the one place real rules exist and apply directly.
  Amazon actively and specifically polices third-party use of "Amazon,"
  "FBA," and its other marks in product/company names and logos - stricter
  in practice than Gusto's enforcement. SettleMod's name, domain, and logo
  deliberately don't reference "Amazon" or "FBA" at all (same reasoning
  TillMod applied to avoid "Square" in its own name), and a "not affiliated
  with, endorsed by, or sponsored by Amazon or Intuit" disclaimer is in the
  footer of the app, plus spelled out in `marketing/terms.html`. Body copy
  referring to "Amazon Settlement Report" as the file format it reads is
  standard nominative fair use (naming a real, compatible file format) -
  the same pattern already used to say "PayPal," "Shopify," "Square," and
  "Gusto" by name on the sibling apps' own pages.

None of this is a substitute for an actual lawyer if this app starts making
real money — it's a reasonable-effort check, not legal advice.

## Validate before building further

Same approach as the other four apps: get real usage/feedback via the
organic Reddit/community outreach plan (r/FulfillmentByAmazon, r/AmazonSeller,
r/QuickBooks, r/bookkeeping) before sinking more time into additional
marketplaces (Amazon direct-payment settlements for non-FBA sellers, Walmart
Marketplace, etc.) or additional polish.
