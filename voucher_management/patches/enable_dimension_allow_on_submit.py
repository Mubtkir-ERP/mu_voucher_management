import frappe


def execute():
    """Allow editing accounting dimension fields on child table rows after submission.

    ERPNext creates dimension custom fields (e.g. customer, supplier) on the child
    table 'Voucher Entry Account' without allow_on_submit, so the fields are locked
    once the voucher is submitted. This patch enables editing so that changes on row-
    level dimensions are picked up by on_update_after_submit and reflected in GL.
    """
    from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
        get_accounting_dimensions,
    )

    child_doctype = "Voucher Entry Account"
    dimension_fieldnames = get_accounting_dimensions()

    for fieldname in dimension_fieldnames:
        custom_field = frappe.db.get_value(
            "Custom Field", {"dt": child_doctype, "fieldname": fieldname}, "name"
        )
        if custom_field:
            frappe.db.set_value("Custom Field", custom_field, "allow_on_submit", 1)

    if dimension_fieldnames:
        frappe.clear_cache(doctype=child_doctype)
