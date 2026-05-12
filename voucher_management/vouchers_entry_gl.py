import frappe
from frappe.utils import flt
from frappe import _
from erpnext.accounts.general_ledger import make_gl_entries
from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import get_accounting_dimensions


def on_submit(doc, method=None):
    """Create GL entries on submit."""
    create_gl_entries(doc)


def on_cancel(doc, method=None):
    """Delete GL and Payment Ledger entries on cancel."""
    _delete_gl_and_ple(doc)
    frappe.msgprint(_("GL entries cancelled successfully."))


def on_trash(doc, method=None):
    """Delete GL and Payment Ledger entries before permanent deletion."""
    _delete_gl_and_ple(doc)


def on_update_after_submit(doc, method=None):
    """Sync allowed post-submit field changes to GL and Payment Ledger entries.

    Pass 1 — apply header-level values (remarks, header cost_center, header
              dimensions) to every GL line for this voucher.
    Pass 2 — override per-row GL lines (matched via voucher_detail_no) with
              their own cost_center, user_remark, and row-level dimensions.
    This preserves all GL Entry IDs and avoids any delete/re-create cycle.
    """
    active_dims = get_accounting_dimensions()
    voucher_filter = {"voucher_type": doc.doctype, "voucher_no": doc.name}

    # --- Pass 1: header-level defaults applied to all GL lines ---
    header_update = {"remarks": doc.remarks or "", "cost_center": doc.cost_center}
    for fieldname in active_dims:
        header_update[fieldname] = doc.get(fieldname) or None

    frappe.db.set_value("GL Entry", voucher_filter, header_update, update_modified=True)
    frappe.db.set_value("Payment Ledger Entry", voucher_filter, header_update, update_modified=True)

    # --- Pass 2: per-row overrides matched by voucher_detail_no ---
    for row in (doc.references or []):
        row_update = {
            "cost_center": row.cost_center or doc.cost_center,
            "remarks": row.user_remark or doc.remarks or "",
        }
        # Row-level dimensions take priority over header-level values
        for fieldname in active_dims:
            row_update[fieldname] = row.get(fieldname) or doc.get(fieldname) or None

        row_filter = {**voucher_filter, "voucher_detail_no": row.name}
        frappe.db.set_value("GL Entry", row_filter, row_update, update_modified=True)
        frappe.db.set_value("Payment Ledger Entry", row_filter, row_update, update_modified=True)

    frappe.msgprint(_("GL entries updated successfully."))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _delete_gl_and_ple(doc):
    """Remove all GL Entry and Payment Ledger Entry records for this voucher."""
    filters = {"voucher_type": doc.doctype, "voucher_no": doc.name}

    for ple in frappe.get_all("Payment Ledger Entry", filters=filters, pluck="name"):
        frappe.delete_doc("Payment Ledger Entry", ple, force=True, ignore_permissions=True)

    for gle in frappe.get_all("GL Entry", filters=filters, pluck="name"):
        frappe.delete_doc("GL Entry", gle, force=True, ignore_permissions=True)


def _get_dimension_values(doc):
    """Return a dict of {fieldname: value} for every active accounting dimension.

    Frappe stores custom dimensions (e.g. customer, supplier) as fields on both
    the parent doc and the GL Entry table. We read them from the parent doc here
    so they can be injected into every GL line that belongs to the header.
    """
    dimensions = {}
    for fieldname in get_accounting_dimensions():
        value = doc.get(fieldname)
        if value:
            dimensions[fieldname] = value
    return dimensions


def _build_gl_dict(doc, args, header_dimensions):
    """Build a single GL Entry dict, merging header dimensions with row-level overrides.

    Row-level dimensions (passed inside `args`) take priority over header-level ones,
    so a child-table row can override the parent's dimension if needed.
    """
    # Start with base fields common to all entries
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

    # Apply row-specific overrides (higher priority — includes cost_center, party, etc.)
    gl_dict.update(args)

    # Resolve account currency when not supplied
    if not gl_dict.get("account_currency"):
        gl_dict["account_currency"] = frappe.get_cached_value(
            "Account", gl_dict["account"], "account_currency"
        )

    # Mirror account-currency amounts to company-currency fields when not set
    if gl_dict.get("debit_in_account_currency") and not gl_dict.get("debit"):
        gl_dict["debit"] = gl_dict["debit_in_account_currency"]
    if gl_dict.get("credit_in_account_currency") and not gl_dict.get("credit"):
        gl_dict["credit"] = gl_dict["credit_in_account_currency"]

    return gl_dict


def create_gl_entries(doc):
    """Build and post all GL entries for this voucher."""

    # Avoid duplicate entries (e.g. if called twice for the same submit)
    if frappe.db.exists("GL Entry", {"voucher_type": doc.doctype, "voucher_no": doc.name, "is_cancelled": 0}):
        return

    # Collect accounting dimensions set on the parent header
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

    merge_entries = frappe.db.get_single_value("Accounts Settings", "merge_similar_account_heads")
    make_gl_entries(gl_entries, merge_entries=merge_entries)
    frappe.msgprint(_("GL entries created successfully."))


# ---------------------------------------------------------------------------
# Entry builders per payment type
# ---------------------------------------------------------------------------

def _get_tax_account(row):
    """Resolve the tax account head from the Purchase Taxes and Charges Template."""
    if not row.taxes:
        return None
    return frappe.db.get_value(
        "Purchase Taxes and Charges", {"parent": row.taxes}, "account_head"
    )


def _build_receive_entries(doc, gl_entries, header_dimensions):
    """Debit the payment account; credit each party account."""

    # Header GL lines (payment account, tax) use the document general remarks.
    # Per-row GL lines use the row's own remark, falling back to doc.remarks.
    header_remarks = doc.remarks or ""

    # Debit: payment account (e.g. bank/cash)
    gl_entries.append(_build_gl_dict(doc, {
        "account": doc.account_payment,
        "debit": flt(doc.amount_after_tax),
        "debit_in_account_currency": flt(doc.amount_after_tax),
        "credit": 0,
        "credit_in_account_currency": 0,
        "cost_center": doc.cost_center,
        "remarks": header_remarks,
    }, header_dimensions))

    tax_account = None

    for row in doc.references:
        total_allocated = 0
        row_remarks = row.user_remark or header_remarks

        # Credit: allocated amounts linked to specific invoices
        for alloc in (doc.get("vouchers_payment_references") or []):
            if alloc.customer != row.party:
                continue
            gl_entries.append(_build_gl_dict(doc, {
                "account": row.account,
                "party_type": row.party_type,
                "party": row.party,
                "credit": flt(alloc.allocated_amount),
                "credit_in_account_currency": flt(alloc.allocated_amount),
                "debit": 0,
                "debit_in_account_currency": 0,
                "project": row.project,
                "against_voucher_type": alloc.reference_doctype,
                "against_voucher": alloc.reference_name,
                "cost_center": row.cost_center,
                "remarks": row_remarks,
                "voucher_detail_no": row.name,
            }, header_dimensions))
            total_allocated += flt(alloc.allocated_amount)

        # Credit: remaining unallocated amount
        remaining = flt(row.amount_before_tax) - total_allocated
        if remaining > 0:
            gl_entries.append(_build_gl_dict(doc, {
                "account": row.account,
                "party_type": row.party_type,
                "party": row.party,
                "project": row.project,
                "credit": remaining,
                "credit_in_account_currency": remaining,
                "debit": 0,
                "debit_in_account_currency": 0,
                "cost_center": row.cost_center,
                "remarks": row_remarks,
                "voucher_detail_no": row.name,
            }, header_dimensions))

        if not tax_account:
            tax_account = _get_tax_account(row)

    # Credit: tax account if applicable
    if tax_account and flt(doc.total_taxes) > 0:
        gl_entries.append(_build_gl_dict(doc, {
            "account": tax_account,
            "credit": flt(doc.total_taxes),
            "credit_in_account_currency": flt(doc.total_taxes),
            "debit": 0,
            "debit_in_account_currency": 0,
            "cost_center": doc.cost_center,
            "remarks": header_remarks,
        }, header_dimensions))


def _build_pay_entries(doc, gl_entries, header_dimensions):
    """Debit each party account; credit the payment account."""

    # Header GL lines (payment account, tax) use the document general remarks.
    # Per-row GL lines use the row's own remark, falling back to doc.remarks.
    header_remarks = doc.remarks or ""

    tax_account = None

    for row in doc.references:
        total_allocated = 0
        row_remarks = row.user_remark or header_remarks

        # Debit: allocated amounts linked to specific invoices
        for alloc in (doc.get("vouchers_payment_references2") or []):
            if alloc.suppiler != row.party:
                continue
            gl_entries.append(_build_gl_dict(doc, {
                "account": row.account,
                "party_type": row.party_type,
                "party": row.party,
                "project": row.project,
                "debit": flt(alloc.allocated_amount),
                "debit_in_account_currency": flt(alloc.allocated_amount),
                "credit": 0,
                "credit_in_account_currency": 0,
                "against_voucher_type": alloc.reference_doctype,
                "against_voucher": alloc.reference_name,
                "cost_center": row.cost_center,
                "remarks": row_remarks,
                "voucher_detail_no": row.name,
            }, header_dimensions))
            total_allocated += flt(alloc.allocated_amount)

        # Debit: remaining unallocated amount
        remaining = flt(row.amount_before_tax) - total_allocated
        if remaining > 0:
            gl_entries.append(_build_gl_dict(doc, {
                "account": row.account,
                "party_type": row.party_type,
                "party": row.party,
                "project": row.project,
                "debit": remaining,
                "debit_in_account_currency": remaining,
                "credit": 0,
                "credit_in_account_currency": 0,
                "cost_center": row.cost_center,
                "remarks": row_remarks,
                "voucher_detail_no": row.name,
            }, header_dimensions))

        if not tax_account:
            tax_account = _get_tax_account(row)

    # Debit: tax account if applicable
    if tax_account and flt(doc.total_taxes) > 0:
        gl_entries.append(_build_gl_dict(doc, {
            "account": tax_account,
            "debit": flt(doc.total_taxes),
            "debit_in_account_currency": flt(doc.total_taxes),
            "credit": 0,
            "credit_in_account_currency": 0,
            "cost_center": doc.cost_center,
            "remarks": header_remarks,
        }, header_dimensions))

    # Credit: payment account (e.g. bank/cash)
    gl_entries.append(_build_gl_dict(doc, {
        "account": doc.account_payment,
        "credit": flt(doc.amount_after_tax),
        "credit_in_account_currency": flt(doc.amount_after_tax),
        "debit": 0,
        "debit_in_account_currency": 0,
        "cost_center": doc.cost_center,
        "remarks": header_remarks,
    }, header_dimensions))


def _build_transfer_entries(doc, gl_entries, header_dimensions):
    """Credit the source account and debit the destination account."""
    remarks = doc.remarks or ""

    # Credit: source account (paid_from)
    gl_entries.append(_build_gl_dict(doc, {
        "account": doc.paid_from,
        "credit": flt(doc.paid_amount),
        "credit_in_account_currency": flt(doc.paid_amount),
        "debit": 0,
        "debit_in_account_currency": 0,
        "cost_center": doc.cost_center,
        "remarks": remarks,
    }, header_dimensions))

    # Debit: destination account (paid_to)
    gl_entries.append(_build_gl_dict(doc, {
        "account": doc.paid_to,
        "debit": flt(doc.paid_amount),
        "debit_in_account_currency": flt(doc.paid_amount),
        "credit": 0,
        "credit_in_account_currency": 0,
        "cost_center": doc.cost_center,
        "remarks": remarks,
    }, header_dimensions))