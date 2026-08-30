import frappe
from frappe import _
from frappe.model.meta import get_field_precision
from frappe.utils import flt

from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import get_accounting_dimensions
from erpnext.accounts.general_ledger import (
    check_freezing_date,
    make_gl_entries,
    make_reverse_gl_entries,
    validate_accounting_period,
)

from voucher_management.voucher_management.doctype.vouchers_entry.vouchers_entry import (
    CURRENCY_PRECISION,
)

# Ledger rows survive cancellation with is_cancelled = 1 instead of being deleted, so
# Frappe's generic "document is linked" guard has to be told to look past them.
LEDGER_DOCTYPES = ("GL Entry", "Payment Ledger Entry")

# Header fields that appear in the ledger but never change an amount. Editing one of
# these after submit updates the existing GL rows in place — the voucher is never
# torn down and rebuilt, so a frozen period can no longer swallow the whole voucher.
HEADER_GL_FIELDS = ("remarks", "cost_center")

# Same idea, per row of Accounts References.
ROW_GL_FIELDS = ("cost_center", "project", "user_remark")


def on_submit(doc, method=None):
    """Create GL entries on submit."""
    create_gl_entries(doc)


def on_cancel(doc, method=None):
    """Reverse the ledger — never delete it.

    `make_reverse_gl_entries` is the standard ERPNext path: it flags the original rows
    `is_cancelled = 1`, writes mirrored rows, and — the part the old delete missed —
    de-links the Payment Ledger Entries so the invoices' `outstanding_amount` goes back
    up. It also runs `validate_accounting_period` and `check_freezing_date`, which a raw
    `frappe.db.delete` bypassed entirely.
    """
    doc.ignore_linked_doctypes = LEDGER_DOCTYPES

    if not frappe.db.exists("GL Entry", {"voucher_type": doc.doctype, "voucher_no": doc.name, "is_cancelled": 0}):
        return

    make_reverse_gl_entries(voucher_type=doc.doctype, voucher_no=doc.name)
    frappe.msgprint(_("GL entries reversed and invoice outstanding amounts restored."), alert=True)


def on_trash(doc, method=None):
    """Refuse to orphan ledger rows on permanent deletion.

    Mirrors ERPNext: the ledger is only purged when `delete_linked_ledger_entries` is
    switched on in Accounts Settings. Otherwise deleting a voucher that still has GL
    rows is blocked, because those rows would survive with no document behind them.
    """
    filters = {"voucher_type": doc.doctype, "voucher_no": doc.name}

    if not frappe.db.exists("GL Entry", filters):
        return

    if frappe.db.get_single_value("Accounts Settings", "delete_linked_ledger_entries"):
        frappe.db.delete("Payment Ledger Entry", filters=filters)
        frappe.db.delete("GL Entry", filters=filters)
        return

    frappe.throw(
        _(
            "{0} still has General Ledger entries. Cancel it first, or enable "
            "'Delete Accounting and Stock Ledger Entries on deletion of Transaction' in Accounts Settings."
        ).format(frappe.bold(doc.name))
    )


def on_update_after_submit(doc, method=None):
    """Update the ledger in place for the fields that are editable after submit.

    Deleting and rebuilding was the previous approach. The delete always succeeded and
    the rebuild could fail (frozen period, closed accounting period, disabled account),
    which left a submitted voucher with no ledger at all. Every field Frappe still lets
    through after submit is descriptive — remarks, cost center, project, accounting
    dimensions — so the matching GL rows can simply be re-stamped. Amounts, accounts,
    parties and dates are not `allow_on_submit` and never reach this function; changing
    them requires Cancel + Amend.
    """
    before = doc.get_doc_before_save()
    if not before:
        return

    changes = _collect_ledger_changes(doc, before)
    if not changes:
        return

    gl_rows = frappe.get_all(
        "GL Entry",
        filters={"voucher_type": doc.doctype, "voucher_no": doc.name, "is_cancelled": 0},
        fields=["name", "voucher_detail_no", "account", "party"],
    )

    if not gl_rows:
        return

    # An in-place edit is still a write to a posted period.
    check_freezing_date(doc.posting_date)
    validate_accounting_period(
        [frappe._dict({"posting_date": doc.posting_date, "company": doc.company, "voucher_type": doc.doctype})]
    )

    header_accounts = _header_accounts(doc)
    row_by_key = _rows_by_account_and_party(doc)

    updated = 0
    unmatched = 0

    for gl in gl_rows:
        owner = _owner_of(gl, header_accounts, row_by_key)

        if owner is None:
            # A ledger row we cannot attribute. Leaving it alone is the only safe move:
            # stamping the header's values on what may be a row line would put one row's
            # cost centre on another's.
            unmatched += 1
            continue

        if owner != "__header__" and not gl.voucher_detail_no:
            # Identified a legacy row line. Stamp it so the next edit is an exact match
            # instead of another guess.
            frappe.db.set_value("GL Entry", gl.name, "voucher_detail_no", owner,
                                update_modified=False)
            gl.voucher_detail_no = owner

        values = changes.get(owner)
        if not values:
            continue

        _apply_ledger_values("GL Entry", gl.name, values)
        _apply_ledger_values(
            "Payment Ledger Entry",
            frappe.db.get_value(
                "Payment Ledger Entry",
                {"voucher_type": doc.doctype, "voucher_no": doc.name, "voucher_detail_no": gl.voucher_detail_no},
                "name",
            ),
            values,
        )
        updated += 1

    # Reports read the voucher through the document cache; drop it so the new values are
    # what the next reader sees.
    frappe.clear_document_cache(doc.doctype, doc.name)

    if updated:
        frappe.msgprint(_("{0} General Ledger row(s) updated in place.").format(updated), alert=True)

    if unmatched:
        frappe.msgprint(
            _(
                "{0} General Ledger row(s) could not be matched to a line of this voucher "
                "and were left unchanged. Cancel and amend the voucher to rebuild them."
            ).format(unmatched),
            indicator="orange",
            title=_("Partially updated"),
        )


def _header_accounts(doc):
    """Accounts whose GL rows belong to the document itself, not to a reference row.

    Needed because a GL row with no `voucher_detail_no` is ambiguous: it is either a
    header line — which never carries one — or a row line posted before the stamp existed.
    Knowing which accounts the header posts to tells the two apart.
    """
    accounts = {doc.account_payment, doc.paid_from, doc.paid_to}
    accounts |= {tax["account_head"] for tax in doc.get_tax_lines()}
    accounts.add(frappe.get_cached_value("Company", doc.company, "exchange_gain_loss_account"))

    return {account for account in accounts if account}


def _rows_by_account_and_party(doc):
    """Map (account, party) -> row name, for keys that belong to exactly one row.

    A key shared by two rows tells us nothing about which of them a ledger row came from,
    so it is dropped rather than guessed at.
    """
    owners = {}

    for row in doc.references:
        key = (row.account, row.party or "")
        owners.setdefault(key, []).append(row.name)

    return {key: names[0] for key, names in owners.items() if len(names) == 1}


def _owner_of(gl, header_accounts, row_by_key):
    """Which part of the voucher a ledger row came from.

    Returns the reference row's name, "__header__", or None when it cannot be told.
    """
    if gl.voucher_detail_no:
        return gl.voucher_detail_no

    # Header lines legitimately carry no stamp.
    if not gl.party and gl.account in header_accounts:
        return "__header__"

    # Otherwise it is a row line from before the stamp existed — match it back by the
    # only identity it still has.
    return row_by_key.get((gl.account, gl.party or ""))


# ---------------------------------------------------------------------------
# Post-submit in-place update helpers
# ---------------------------------------------------------------------------

def _collect_ledger_changes(doc, before):
    """Map {voucher_detail_no | "__header__": {gl_fieldname: new_value}} for what changed."""
    dimension_fields = _dimension_fieldnames(doc.doctype)
    changes = {}

    header = {}
    for fieldname in HEADER_GL_FIELDS + tuple(dimension_fields):
        if doc.get(fieldname) != before.get(fieldname):
            header[fieldname] = doc.get(fieldname)

    if "cost_center" in header and not header["cost_center"]:
        frappe.throw(_("Cost Center cannot be cleared on a submitted voucher."))

    if header:
        changes["__header__"] = header

    row_dimension_fields = _dimension_fieldnames("Voucher Entry Account")
    previous_rows = {row.name: row for row in before.references}

    for row in doc.references:
        old = previous_rows.get(row.name)
        if not old:
            continue

        values = {}
        for fieldname in ROW_GL_FIELDS + tuple(row_dimension_fields):
            if row.get(fieldname) != old.get(fieldname):
                values[fieldname] = row.get(fieldname)

        if "cost_center" in values and not values["cost_center"]:
            frappe.throw(_("Row #{0}: Cost Center cannot be cleared on a submitted voucher.").format(row.idx))

        # A row line inherits the document remarks only while it has none of its own.
        if "user_remark" in values:
            values["remarks"] = values.pop("user_remark") or doc.remarks or ""
        elif "remarks" in header and not row.user_remark:
            values["remarks"] = header["remarks"] or ""

        if values:
            changes[row.name] = values

    return changes


def _apply_ledger_values(doctype, name, values):
    """Write only the fields that actually exist on the target ledger doctype."""
    if not name:
        return

    meta = frappe.get_meta(doctype)
    payload = {k: v for k, v in values.items() if meta.has_field(k)}

    if payload:
        frappe.db.set_value(doctype, name, payload, update_modified=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _dimension_fieldnames(doctype):
    """Active accounting dimensions that really exist as fields on `doctype`.

    `customer` and `supplier` are appended because they are commonly configured as
    dimensions without being cached in the standard list — but only if the field is
    actually present, so a doctype that never had them is not probed for nothing.
    """
    meta = frappe.get_meta(doctype)
    fieldnames = list(get_accounting_dimensions()) + ["customer", "supplier", "project"]

    seen = []
    for fieldname in fieldnames:
        for candidate in (fieldname, f"custom_{fieldname}"):
            if meta.has_field(candidate) and candidate not in seen:
                seen.append(candidate)

    return seen


def _get_dimension_values(doc):
    """Return a dict of {gl_fieldname: value} for every dimension set on the header."""
    dimensions = {}

    for fieldname in _dimension_fieldnames(doc.doctype):
        value = doc.get(fieldname)
        if value:
            # `custom_project` and `project` are the same GL column.
            dimensions[fieldname.removeprefix("custom_")] = value

    return dimensions


def _build_gl_dict(doc, args, header_dimensions, row=None):
    """Build a single GL Entry dict, merging header dimensions with row-level overrides.

    Row-level dimensions (passed inside `args` or extracted from `row`) take priority over
    header-level ones, so a child-table row can override the parent's dimension.

    Amounts arrive as a pair: `debit`/`credit` are already in the company currency and
    `*_in_account_currency` in the account's own. Nothing is mirrored between the two any
    more — that shortcut was what posted a foreign figure as if it were company currency.
    """
    gl_dict = frappe._dict({
        "posting_date": doc.posting_date,
        "company": doc.company,
        "voucher_type": doc.doctype,
        "voucher_no": doc.name,
        "remarks": doc.remarks or "",
        "is_opening": "No",
        "is_cancelled": 0,
    })

    # Apply header-level accounting dimensions first (lower priority)
    gl_dict.update(header_dimensions)

    # Apply row-level accounting dimensions if row object is provided
    if row:
        for fieldname in _dimension_fieldnames(row.doctype):
            val = row.get(fieldname)
            if val:
                gl_dict[fieldname.removeprefix("custom_")] = val

    # Apply row-specific overrides (higher priority — includes cost_center, party, etc.)
    gl_dict.update(args)

    if not gl_dict.get("account_currency"):
        gl_dict["account_currency"] = frappe.get_cached_value(
            "Account", gl_dict["account"], "account_currency"
        ) or frappe.get_cached_value("Company", doc.company, "default_currency")

    return gl_dict


def _amounts(side, account_amount, base_amount):
    """Return the four GL amount fields for one side of an entry.

    `side` is "debit" or "credit". A negative figure is flipped to the other side rather
    than posted as a negative, which is what the ledger expects.
    """
    if flt(base_amount) < 0 or flt(account_amount) < 0:
        side = "credit" if side == "debit" else "debit"
        account_amount = -flt(account_amount)
        base_amount = -flt(base_amount)

    other = "credit" if side == "debit" else "debit"

    return {
        side: flt(base_amount),
        f"{side}_in_account_currency": flt(account_amount),
        other: 0,
        f"{other}_in_account_currency": 0,
    }


def distribute_allocations(doc):
    """Assign each invoice allocation to the reference rows that can carry it.

    Matching was previously done inside the per-row loop with `alloc.party == row.party`.
    When the same party appeared in two rows, every allocation matched both and was
    posted twice, while the row whose amount was smaller than the allocation had its
    remainder silently dropped for being negative — an unbalanced voucher.

    Here each allocation is consumed exactly once: it walks the rows of its party in
    order, taking what is left of each row's capacity, so the total posted per row can
    never exceed that row's net amount and the total posted per allocation is exactly
    `allocated_amount`. `validate_allocations` guarantees the capacity exists.
    """
    allocations, party_field = doc.get_allocation_table()
    assignments = {row.name: [] for row in doc.references}

    if not allocations:
        return assignments

    precision = CURRENCY_PRECISION
    capacity = {row.name: flt(row.amount_before_tax, precision) for row in doc.references}

    for alloc in allocations:
        outstanding = flt(alloc.allocated_amount, precision)
        if outstanding <= 0:
            continue

        for row in doc.references:
            if outstanding <= 0:
                break

            if row.party != alloc.get(party_field) or capacity[row.name] <= 0:
                continue

            taken = min(outstanding, capacity[row.name])
            capacity[row.name] = flt(capacity[row.name] - taken, precision)
            outstanding = flt(outstanding - taken, precision)
            assignments[row.name].append((alloc, taken))

        if outstanding > 0:
            frappe.throw(
                _("Row #{0}: {1} could not be allocated to {2} — no reference row has room left.").format(
                    alloc.idx,
                    frappe.bold(frappe.format_value(outstanding, {"fieldtype": "Currency"})),
                    frappe.bold(alloc.get(party_field)),
                )
            )

    return assignments


def _validate_balance(doc, gl_entries):
    """Refuse to hand an unbalanced map to the ledger.

    `make_gl_entries` does check the difference, but it tolerates up to 0.5 for a custom
    voucher type and quietly books anything smaller to the Round Off account — and when it
    does throw, the message talks about GL entries rather than about the voucher. This
    check runs first, at zero tolerance, so the user sees which voucher is wrong and by
    how much. Any genuine currency gap has already been booked to the exchange account by
    the time this runs, so what is left here is an error, not a rate.
    """
    # GL Entry's own precision, not the voucher's. This measures the ledger rows we are
    # about to hand over, and they are stored at whatever width GL Entry uses. Our figures
    # arrive already settled at CURRENCY_PRECISION, so a wider ledger precision only makes
    # this check stricter — never looser.
    precision = get_field_precision(
        frappe.get_meta("GL Entry").get_field("debit"),
        currency=frappe.get_cached_value("Company", doc.company, "default_currency"),
    )

    difference = flt(sum(flt(e.debit) - flt(e.credit) for e in gl_entries), precision)

    if difference:
        frappe.throw(
            _("Voucher {0} is not balanced: debit minus credit is {1}. No ledger entry was made.").format(
                frappe.bold(doc.name),
                frappe.bold(frappe.format_value(difference, {"fieldtype": "Currency"})),
            )
        )


def create_gl_entries(doc):
    """Build and post all GL entries for this voucher."""

    # Avoid duplicate entries (e.g. if called twice for the same submit)
    if frappe.db.exists("GL Entry", {"voucher_type": doc.doctype, "voucher_no": doc.name, "is_cancelled": 0}):
        frappe.msgprint(
            _("GL entries already exist for {0}; nothing was posted.").format(frappe.bold(doc.name)),
            alert=True,
        )
        return

    header_dimensions = _get_dimension_values(doc)

    gl_entries = []

    if doc.payment_type == "Receive":
        _build_receive_entries(doc, gl_entries, header_dimensions)
    elif doc.payment_type == "Pay":
        _build_pay_entries(doc, gl_entries, header_dimensions)
    elif doc.payment_type == "Internal Transfer":
        _build_transfer_entries(doc, gl_entries, header_dimensions)

    if not gl_entries:
        return

    _validate_balance(doc, gl_entries)

    merge_entries = frappe.db.get_single_value("Accounts Settings", "merge_similar_account_heads")
    make_gl_entries(gl_entries, merge_entries=merge_entries)
    frappe.msgprint(_("GL entries created successfully."), alert=True)


# ---------------------------------------------------------------------------
# Entry builders per payment type
# ---------------------------------------------------------------------------

def _build_party_lines(doc, gl_entries, header_dimensions, direction):
    """One set of party lines, in the direction the payment type calls for.

    `direction` is "credit" for a receipt (the party account is relieved) and "debit" for a
    payment.

    An allocated portion is converted at the *invoice's* rate, not the voucher's. That is
    what makes the invoice clear to exactly zero: the receivable was debited at the invoice
    rate, so relieving it at any other rate would leave a residue on the party account that
    no one could explain. The gap between the two rates is real money, and it reaches the
    exchange account through the difference line instead.
    """
    header_remarks = doc.remarks or ""
    assignments = distribute_allocations(doc)
    precision = CURRENCY_PRECISION

    for row in doc.references:
        row_remarks = row.user_remark or header_remarks
        row_rate = flt(row.exchange_rate) or 1.0
        allocated = 0.0

        for alloc, amount in assignments[row.name]:
            invoice_rate = flt(alloc.exchange_rate) or row_rate
            base_amount = flt(amount * invoice_rate, precision)

            gl_entries.append(_build_gl_dict(doc, dict({
                "account": row.account,
                "account_currency": row.account_currency,
                "party_type": row.party_type,
                "party": row.party,
                "against_voucher_type": alloc.reference_doctype,
                "against_voucher": alloc.reference_name,
                "cost_center": row.cost_center,
                "remarks": row_remarks,
                "voucher_detail_no": row.name,
            }, **_amounts(direction, amount, base_amount)), header_dimensions, row=row))

            allocated += amount

        remaining = flt(flt(row.amount_before_tax) - allocated, precision)
        if remaining > 0:
            gl_entries.append(_build_gl_dict(doc, dict({
                "account": row.account,
                "account_currency": row.account_currency,
                "party_type": row.party_type,
                "party": row.party,
                "cost_center": row.cost_center,
                "remarks": row_remarks,
                "voucher_detail_no": row.name,
            }, **_amounts(direction, remaining, flt(remaining * row_rate, precision))),
                header_dimensions, row=row))




def _build_tax_lines(doc, gl_entries, header_dimensions, direction):
    """One line per tax account head, on the same side as the party lines.

    Tax accounts are company currency by rule (see `validate_accounts`), so the two amount
    columns carry the same figure. A "Deduct" template produces a negative one, which
    `_amounts` flips to the opposite side instead of dropping — the old `if total_taxes > 0`
    guard left the voucher short by exactly the withholding.
    """
    header_remarks = doc.remarks or ""

    for tax in doc.get_tax_lines():
        amount = flt(tax["amount"])

        gl_entries.append(_build_gl_dict(doc, dict({
            "account": tax["account_head"],
            "cost_center": doc.cost_center,
            "remarks": header_remarks,
        }, **_amounts(direction, amount, amount)), header_dimensions))


def _build_payment_line(doc, gl_entries, header_dimensions, direction):
    """The bank/cash side, carrying the gross amount in its own currency."""
    precision = CURRENCY_PRECISION
    amount = flt(doc.payment_amount)
    base_amount = flt(amount * (flt(doc.payment_exchange_rate) or 1.0), precision)

    gl_entries.append(_build_gl_dict(doc, dict({
        "account": doc.account_payment,
        "account_currency": doc.account_payment_currency,
        "cost_center": doc.cost_center,
        "remarks": doc.remarks or "",
    }, **_amounts(direction, amount, base_amount)), header_dimensions))


def _build_exchange_difference_line(doc, gl_entries, header_dimensions):
    """Book the gap between the two converted sides to the exchange account.

    The gap has two sources and one line absorbs both: the payment leg converted at its own
    rate against rows converted at theirs, and allocations relieved at their invoice rate
    rather than the voucher rate. Whatever the two sides disagree by after every other line
    is written is, by definition, the realised gain or loss.
    """
    # GL Entry's own precision, not the voucher's. This measures the ledger rows we are
    # about to hand over, and they are stored at whatever width GL Entry uses. Our figures
    # arrive already settled at CURRENCY_PRECISION, so a wider ledger precision only makes
    # this check stricter — never looser.
    precision = get_field_precision(
        frappe.get_meta("GL Entry").get_field("debit"),
        currency=frappe.get_cached_value("Company", doc.company, "default_currency"),
    )

    difference = flt(sum(flt(e.credit) - flt(e.debit) for e in gl_entries), precision)
    if not difference:
        return

    account = frappe.get_cached_value("Company", doc.company, "exchange_gain_loss_account")
    if not account:
        frappe.throw(
            _("Set the Exchange Gain / Loss Account on company {0}: this voucher needs {1} booked to it.").format(
                frappe.bold(doc.company),
                frappe.bold(frappe.format_value(difference, {"fieldtype": "Currency"})),
            )
        )

    gl_entries.append(_build_gl_dict(doc, dict({
        "account": account,
        "cost_center": doc.cost_center
        or frappe.get_cached_value("Company", doc.company, "cost_center"),
        "remarks": _("Exchange gain or loss on {0}").format(doc.name),
    }, **_amounts("debit", difference, difference)), header_dimensions))


def _build_receive_entries(doc, gl_entries, header_dimensions):
    """Debit the payment account; credit each party account and the tax accounts."""
    _build_payment_line(doc, gl_entries, header_dimensions, "debit")
    _build_party_lines(doc, gl_entries, header_dimensions, "credit")
    _build_tax_lines(doc, gl_entries, header_dimensions, "credit")
    _build_exchange_difference_line(doc, gl_entries, header_dimensions)


def _build_pay_entries(doc, gl_entries, header_dimensions):
    """Debit each party account and the tax accounts; credit the payment account."""
    _build_party_lines(doc, gl_entries, header_dimensions, "debit")
    _build_tax_lines(doc, gl_entries, header_dimensions, "debit")
    _build_payment_line(doc, gl_entries, header_dimensions, "credit")
    _build_exchange_difference_line(doc, gl_entries, header_dimensions)


def _build_transfer_entries(doc, gl_entries, header_dimensions):
    """Credit the source account and debit the destination account.

    Across two currencies the two legs are different numbers: what left the source and what
    arrived at the destination, each converted at its own rate. The exchange line closes the
    gap, which is the cost of the conversion itself.
    """
    remarks = doc.remarks or ""
    precision = CURRENCY_PRECISION

    paid = flt(doc.paid_amount)
    received = flt(doc.received_amount) or paid
    base_paid = flt(paid * (flt(doc.source_exchange_rate) or 1.0), precision)
    base_received = flt(received * (flt(doc.target_exchange_rate) or 1.0), precision)

    gl_entries.append(_build_gl_dict(doc, dict({
        "account": doc.paid_from,
        "account_currency": doc.paid_from_currency,
        "cost_center": doc.cost_center,
        "remarks": remarks,
    }, **_amounts("credit", paid, base_paid)), header_dimensions))

    gl_entries.append(_build_gl_dict(doc, dict({
        "account": doc.paid_to,
        "account_currency": doc.paid_to_currency,
        "cost_center": doc.cost_center,
        "remarks": remarks,
    }, **_amounts("debit", received, base_received)), header_dimensions))

    _build_exchange_difference_line(doc, gl_entries, header_dimensions)
