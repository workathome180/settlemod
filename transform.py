"""
Amazon Settlement Report (Flat File V2) -> QuickBooks Online (QBO) Journal
Entry CSV converter.

Like PayMod's Gusto payroll journal, this becomes a *balanced double-entry
journal entry*, not a flat list of bank-matching rows - a settlement report
bundles dozens of transaction types (sales, referral fees, FBA fees, ad
spend, refunds, reserve holdbacks...) into the one lump sum that actually
hits the seller's bank account, and booking that lump sum as plain "Sales
income" (a very common seller mistake) overstates revenue and hides every
fee as if it never happened.

Output targets QuickBooks Online Advanced's native journal-entry CSV
importer (Settings -> Import Data -> Journal Entries) - same column layout
already used by PayMod, confirmed against Intuit's own journal-entry import
spec: JournalNo, JournalDate, AccountName, Debits, Credits, Description,
Name, Class.

**Not yet verified against a real Amazon export** - built from Amazon's
publicly documented Flat File V2 Settlement Report column/value conventions
(settlement-id, transaction-type, amount-type, amount-description, amount,
etc.), the same way PayMod was built from Gusto's documented format before a
real sample file was available. Verify column names and the amount-type/
amount-description values below against a real downloaded settlement report
before trusting this with real customer data - Amazon has changed this
format before (V1 -> V2), and this parser fails loudly (ConversionError)
rather than silently mis-mapping money if a required column is missing.

The Flat File V2 report is a **tab-delimited** text file by default when
downloaded from Seller Central (Reports -> Payments -> Date Range Reports),
though some tools re-export it as comma-delimited - this module sniffs the
delimiter rather than assuming one.

Columns we look for (case-insensitive, matched by "contains" so small
Amazon wording drift doesn't break it):
  settlement-id                          -> one journal entry per file; a
                                             file spanning more than one
                                             settlement is rejected, same
                                             restriction PayMod applies to
                                             pay dates
  deposit-date (falls back to
    settlement-end-date)                 -> JournalDate
  total-amount                           -> the actual deposit; every line
                                             item's amount must sum to this,
                                             or the file is rejected as
                                             inconsistent/partial
  transaction-type                       -> informational only (Order,
                                             Refund, Transfer, Adjustment,
                                             ServiceFee, Liquidations) - not
                                             used for account mapping,
                                             amount-type/amount-description
                                             already identifies that
  amount-type / amount-description       -> looked up in _ACCOUNT_MAP below
                                             to decide which QBO account a
                                             line belongs to and which side
                                             (debit/credit) is its normal
                                             balance
  amount                                 -> signed dollar value (positive =
                                             toward the seller, negative =
                                             away from the seller, per
                                             Amazon's documented convention)
  marketplace-name                       -> optional; if more than one
                                             marketplace appears (e.g. US +
                                             CA), splits lines by marketplace
                                             into the JE's Class field,
                                             mirroring PayMod's department
                                             split

Every dollar posted to a revenue/expense/liability account is posted with
an equal, opposite entry to "Amazon Clearing Account" (an asset account
representing money Amazon owes the seller but hasn't paid out yet) - this
makes every individual line self-balancing, so the whole journal entry
balances by construction rather than by hoping the input file's rows happen
to add up. What IS checked against the file itself: that the sum of all
line-item amounts for the settlement equals the file's own reported
total-amount (the real deposit) - if they disagree, the file is likely
partial or edited, and this raises ConversionError rather than producing a
journal entry that doesn't reconcile to the real bank deposit.
"""

from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from datetime import datetime

MAX_UPLOAD_BYTES = 500_000
MAX_ROWS_PER_FILE = 5000
BALANCE_TOLERANCE_CENTS = 1  # rounding slack, in cents, before we call it "unbalanced"

_SPECIAL_CHARS_RE = re.compile(r"[^A-Za-z0-9 ,.\-/#&]")
_AMAZON_DATE_FORMATS = ["%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%d.%m.%Y"]

ACCOUNT_CLEARING = "Amazon Clearing Account"

# (normalized amount-type, normalized amount-description) -> (AccountName, normal-balance side)
# "normal balance side" is which side of the ledger this account increases
# on. Deliberately just an AccountName, not a per-account "normal balance
# side" - Amazon's amount sign already means the same thing on every line
# regardless of account type ("positive = adds to what the seller is owed,
# negative = subtracts from it"), which maps directly onto credit/debit: a
# positive amount credits its account (revenue increasing, or a fee being
# refunded back), a negative amount debits its account (a fee being charged,
# or a refund reducing revenue). See add_line() below - an earlier version
# of this tracked normal-balance side per account and inverted debit/credit
# for expense accounts, which was wrong: it posted fees as credits instead
# of debits.
_ACCOUNT_MAP: dict[tuple[str, str], str] = {
    ("itemprice", "principal"): "Sales Income:Amazon Sales",
    ("itemprice", "shipping"): "Sales Income:Shipping Income",
    ("itemprice", "giftwrap"): "Sales Income:Gift Wrap Income",
    ("itemprice", "goodwill"): "Sales Income:Amazon Sales",
    ("itemprice", "restockingfee"): "Sales Income:Amazon Sales",
    ("itemwithheldtax", "marketplacefacilitatortax"): "Sales Tax Collected (Liability)",
    ("itemfees", "commission"): "Amazon Selling Fees:Referral Fees",
    ("itemfees", "variableclosingfee"): "Amazon Selling Fees:Referral Fees",
    ("itemfees", "refundcommission"): "Amazon Selling Fees:Referral Fees",
    ("itemfees", "giftwrapchargeback"): "Amazon Selling Fees:Referral Fees",
    ("itemfees", "shippingchargeback"): "Amazon Selling Fees:Referral Fees",
    ("itemfees", "fbaperunitfulfillmentfee"): "Amazon Selling Fees:FBA Fulfillment Fees",
    ("itemfees", "fbaweightbasedfee"): "Amazon Selling Fees:FBA Fulfillment Fees",
    ("shipmentfees", "fbaperorderfulfillmentfee"): "Amazon Selling Fees:FBA Fulfillment Fees",
    ("shipmentfees", "fbatransportationfee"): "Amazon Selling Fees:FBA Fulfillment Fees",
    ("promotion", "shipping"): "Amazon Selling Fees:Promotions & Discounts",
    ("servicefee", "subscription"): "Amazon Selling Fees:Subscription & Storage Fees",
    ("servicefee", "storagefee"): "Amazon Selling Fees:Subscription & Storage Fees",
    ("othertransaction", "storagefee"): "Amazon Selling Fees:Subscription & Storage Fees",
    ("othertransaction", "refundreimbursal"): "Other Income:Amazon Reimbursements",
    ("othertransaction", "currentreserveamount"): "Amazon Reserve (Asset)",
    ("othertransaction", "previousreserveamountbalance"): "Amazon Reserve (Asset)",
}

_FALLBACK_ACCOUNT = "Amazon Other Adjustments"


class ConversionError(Exception):
    """Raised when the input file can't be safely converted into a balanced journal entry."""


def _normalize_header(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _find_column(fieldnames: list[str], *candidates: str) -> str | None:
    normalized = {_normalize_header(f): f for f in fieldnames}
    for field_norm, field_orig in normalized.items():
        for candidate in candidates:
            if candidate in field_norm:
                return field_orig
    return None


def _find_exact_column(fieldnames: list[str], name: str) -> str | None:
    """Like _find_column but requires an exact normalized match - needed for
    'amount', since 'contains' matching would also hit 'total-amount',
    'amount-type', and 'amount-description', all of which contain the
    substring 'amount'."""
    for f in fieldnames:
        if _normalize_header(f) == name:
            return f
    return None


def _sniff_dialect(text: str) -> csv.Dialect:
    sample = text[:4096]
    try:
        return csv.Sniffer().sniff(sample, delimiters="\t,")
    except csv.Error:
        return csv.excel_tab if "\t" in sample.splitlines()[0] else csv.excel


def _parse_amazon_date(raw: str) -> str:
    raw = raw.strip()
    for fmt in _AMAZON_DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    raise ConversionError(f"Unrecognized date format: '{raw}'")


def _sanitize_text(raw: str, fallback: str) -> str:
    cleaned = _SPECIAL_CHARS_RE.sub("", raw.strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip() or fallback
    if cleaned and cleaned[0] in "=+-@":
        cleaned = "'" + cleaned
    return cleaned


def _parse_money(raw: str) -> float:
    cleaned = (raw or "").replace("$", "").replace(",", "").strip()
    if not cleaned:
        return 0.0
    cleaned = cleaned.replace("(", "-").replace(")", "")
    try:
        return float(cleaned)
    except ValueError:
        raise ConversionError(f"Unparseable dollar amount: '{raw}'")


def convert_amazon_settlement_to_qbo(file_bytes: bytes) -> tuple[str, list[str]]:
    """
    Convert an Amazon Flat File V2 Settlement Report (as raw bytes) into a
    QBO journal-entry-ready CSV: JournalNo, JournalDate, AccountName,
    Debits, Credits, Description, Name, Class.

    Returns (csv_text, warnings). Raises ConversionError on fatal problems -
    missing required columns, more than one settlement in the file, or the
    line items not summing to the file's own reported deposit amount.
    """
    warnings: list[str] = []

    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = file_bytes.decode("latin-1")
            warnings.append(
                "File was not UTF-8; re-encoded from Latin-1. Verify special "
                "characters in item descriptions look correct."
            )
        except UnicodeDecodeError as e:
            raise ConversionError(f"Could not decode file as text: {e}")

    if not text.strip():
        raise ConversionError("File appears to be empty.")

    dialect = _sniff_dialect(text)
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if reader.fieldnames is None:
        raise ConversionError("File appears to be empty or not a valid settlement report.")

    fieldnames = reader.fieldnames
    col_settlement_id = _find_column(fieldnames, "settlementid")
    col_amount_type = _find_column(fieldnames, "amounttype")
    col_amount_desc = _find_column(fieldnames, "amountdescription")
    col_amount = _find_exact_column(fieldnames, "amount")
    col_total_amount = _find_column(fieldnames, "totalamount")

    missing_labels = {
        "settlement-id": col_settlement_id,
        "amount-type": col_amount_type,
        "amount-description": col_amount_desc,
        "amount": col_amount,
    }
    missing = [label for label, col in missing_labels.items() if col is None]
    if missing:
        raise ConversionError(
            f"Missing required settlement report columns: {', '.join(missing)}. "
            f"Found columns: {', '.join(fieldnames)}. If Amazon has changed "
            "this report's format, this converter needs updating to match - "
            "contact support rather than importing partial data."
        )
    if col_amount == col_total_amount:
        # "amount" matched the same column as "total-amount" (e.g. a file with
        # only a total-amount column and no per-line amount) - can't proceed.
        raise ConversionError(
            "Could not find a per-line 'amount' column distinct from "
            "'total-amount' - this doesn't look like a line-item settlement "
            "report."
        )

    col_date = _find_column(fieldnames, "depositdate") or _find_column(fieldnames, "settlementenddate")
    col_marketplace = _find_column(fieldnames, "marketplacename")
    col_order_id = _find_column(fieldnames, "orderid")

    rows = list(reader)
    if not rows:
        raise ConversionError("No transaction rows found after the header.")
    if len(rows) > MAX_ROWS_PER_FILE:
        warnings.append(
            f"This file has {len(rows)} rows, above the recommended "
            f"{MAX_ROWS_PER_FILE}-row limit for one journal entry. Consider "
            "splitting by settlement period before converting."
        )

    settlement_ids: set[str] = set()
    dates: set[str] = set()
    reported_total: float | None = None
    line_amount_sum = 0.0

    # keyed by (account, marketplace) -> signed running total, so multi-
    # marketplace sellers get one JE line per account per marketplace
    # instead of everything flattened into one undifferentiated line.
    totals: dict[tuple[str, str], float] = defaultdict(float)
    unmapped_seen: set[tuple[str, str]] = set()

    for i, row in enumerate(rows, start=2):
        sid = (row.get(col_settlement_id) or "").strip()
        if sid:
            settlement_ids.add(sid)

        if col_total_amount:
            raw_total = (row.get(col_total_amount) or "").strip()
            if raw_total:
                try:
                    reported_total = _parse_money(raw_total)
                except ConversionError:
                    pass  # metadata-only rows sometimes repeat this oddly; ignore, we cross-check the sum instead

        raw_amount_type = (row.get(col_amount_type) or "").strip()
        raw_amount_desc = (row.get(col_amount_desc) or "").strip()
        if not raw_amount_type and not raw_amount_desc:
            continue  # settlement-metadata-only row (e.g. transaction-type "Transfer"), no line item to book

        try:
            amount = _parse_money(row.get(col_amount, ""))
        except ConversionError as e:
            warnings.append(f"Row {i}: skipped ({e})")
            continue
        if amount == 0.0:
            continue

        if col_date:
            raw_date = (row.get(col_date) or "").strip()
            if raw_date:
                try:
                    dates.add(_parse_amazon_date(raw_date))
                except ConversionError as e:
                    warnings.append(f"Row {i}: {e}")

        key = (_normalize_header(raw_amount_type), _normalize_header(raw_amount_desc))
        account = _ACCOUNT_MAP.get(key)
        if account is None:
            account = _FALLBACK_ACCOUNT
            if key not in unmapped_seen:
                unmapped_seen.add(key)
                warnings.append(
                    f"Unrecognized amount-type/amount-description pair "
                    f"'{raw_amount_type}' / '{raw_amount_desc}' - booked to "
                    f"'{_FALLBACK_ACCOUNT}' instead of a specific account. "
                    "Review this line in QuickBooks and recategorize if needed."
                )

        marketplace = _sanitize_text(row.get(col_marketplace, ""), "") if col_marketplace else ""
        totals[(account, marketplace)] += amount
        line_amount_sum += amount

    if not settlement_ids:
        raise ConversionError("No rows had a settlement-id - this doesn't look like a settlement report.")
    if len(settlement_ids) > 1:
        raise ConversionError(
            f"This file spans {len(settlement_ids)} different settlements "
            f"({', '.join(sorted(settlement_ids))}). Export and convert one "
            "settlement period at a time - a single journal entry needs a "
            "single settlement."
        )
    settlement_id = next(iter(settlement_ids))

    if not dates:
        raise ConversionError("No rows had a parseable deposit/settlement date.")
    if len(dates) > 1:
        raise ConversionError(
            f"This file has {len(dates)} different dates ({', '.join(sorted(dates))}) "
            "for a single settlement - export a clean date-range report and try again."
        )
    journal_date = next(iter(dates))
    journal_no = f"AMZ-{settlement_id}"

    if reported_total is not None:
        discrepancy = round(line_amount_sum - reported_total, 2)
        if abs(discrepancy) > (BALANCE_TOLERANCE_CENTS / 100):
            raise ConversionError(
                f"This settlement's line items sum to {line_amount_sum:.2f}, but the "
                f"file's own reported total-amount (the actual deposit) is "
                f"{reported_total:.2f} - off by {discrepancy:.2f}. Refusing to book a "
                "journal entry that doesn't reconcile to the real deposit; re-export "
                "the full settlement report and try again."
            )

    if not totals:
        raise ConversionError("No valid line-item transactions found after parsing.")

    je_rows: list[list[str]] = []
    total_debits = 0.0
    total_credits = 0.0

    def add_line(account: str, signed_amount: float, marketplace: str):
        nonlocal total_debits, total_credits
        if abs(signed_amount) < 0.005:
            return
        desc = f"Amazon settlement {settlement_id} ({journal_date})"
        # Positive = adds to what the seller is owed -> credit this account
        # (revenue increasing, or a fee being refunded back). Negative =
        # subtracts from it -> debit this account (a fee being charged, or a
        # refund reducing revenue). Same rule for every account regardless
        # of type - see the _ACCOUNT_MAP comment above for why.
        debit, credit = (0.0, signed_amount) if signed_amount > 0 else (-signed_amount, 0.0)
        total_debits += debit
        total_credits += credit
        je_rows.append(
            [journal_no, journal_date, account, f"{debit:.2f}" if debit else "", f"{credit:.2f}" if credit else "",
             _sanitize_text(desc, "Amazon settlement"), "", _sanitize_text(marketplace, "")]
        )
        # Equal-and-opposite clearing line so this row (and therefore the
        # whole entry) balances by construction.
        clear_debit, clear_credit = credit, debit
        total_debits += clear_debit
        total_credits += clear_credit
        je_rows.append(
            [journal_no, journal_date, ACCOUNT_CLEARING, f"{clear_debit:.2f}" if clear_debit else "",
             f"{clear_credit:.2f}" if clear_credit else "", _sanitize_text(desc, "Amazon settlement"), "",
             _sanitize_text(marketplace, "")]
        )

    for (account, marketplace), signed_amount in sorted(totals.items()):
        add_line(account, signed_amount, marketplace)

    if not je_rows:
        raise ConversionError("No valid settlement data found after parsing.")

    imbalance = round(total_debits - total_credits, 2)
    if abs(imbalance) > (BALANCE_TOLERANCE_CENTS / 100):
        raise ConversionError(
            f"This journal entry doesn't balance: total debits {total_debits:.2f} vs. "
            f"total credits {total_credits:.2f} (off by {imbalance:.2f}). This "
            "shouldn't be possible - please report this file as a bug rather than "
            "importing it."
        )

    out_buf = io.StringIO()
    writer = csv.writer(out_buf, lineterminator="\r\n")
    writer.writerow(["JournalNo", "JournalDate", "AccountName", "Debits", "Credits", "Description", "Name", "Class"])
    writer.writerows(je_rows)

    csv_text = out_buf.getvalue()
    size = len(csv_text.encode("utf-8"))
    if size > MAX_UPLOAD_BYTES:
        warnings.append(
            f"Output file is {size:,} bytes - large, but QBO's journal-entry "
            "importer has no hard size cap like the bank-CSV importer does."
        )

    return csv_text, warnings
