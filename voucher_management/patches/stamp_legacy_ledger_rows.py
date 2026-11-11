"""Attach ledger rows posted before stamping to the voucher line behind them.

Until each ledger row carried its source line, a row could only be traced back by its
account and party — which says nothing when several lines of a voucher share one
account, as cash payment vouchers routinely do. Those rows were skipped on every edit:
the cost center written at posting time stayed, while a project filled in afterwards
never reached the general ledger.

Each such row is matched here by account, party, amount and the order it was written
in, stamped with its line, and brought up to date with it. A voucher whose rows cannot
be matched with certainty — a merged ledger, say — is left exactly as it is.
"""

import frappe

from voucher_management.vouchers_entry_gl import (
    ROW_GL_FIELDS,
    _owners_by_ledger_row,
    _header_accounts,
)


def execute():
    vouchers = frappe.get_all("Vouchers Entry", filters={"docstatus": 1}, pluck="name")
    stamped = corrected = skipped = 0

    for name in vouchers:
        doc = frappe.get_doc("Vouchers Entry", name)
        gl_rows = frappe.get_all(
            "GL Entry",
            filters={"voucher_type": doc.doctype, "voucher_no": doc.name, "is_cancelled": 0},
            fields=["name", "voucher_detail_no", "account", "party", "debit", "credit",
                    "cost_center", "project", "remarks"],
            order_by="creation asc, name asc",
        )
        if not gl_rows or all(entry.voucher_detail_no for entry in gl_rows):
            continue

        owners = _owners_by_ledger_row(doc, gl_rows, _header_accounts(doc))
        rows_by_name = {row.name: row for row in doc.references}

        for entry in gl_rows:
            owner = owners.get(entry.name)
            row = rows_by_name.get(owner) if owner and owner != "__header__" else None

            if not row:
                skipped += 1 if not entry.voucher_detail_no and owner != "__header__" else 0
                continue

            values = {}
            if not entry.voucher_detail_no:
                values["voucher_detail_no"] = row.name
                stamped += 1

            for fieldname in ROW_GL_FIELDS:
                if fieldname == "user_remark":
                    wanted = row.user_remark or doc.remarks or ""
                    column = "remarks"
                else:
                    wanted = row.get(fieldname)
                    column = fieldname

                if (wanted or None) != (entry.get(column) or None):
                    values[column] = wanted

            if len(values) > (1 if "voucher_detail_no" in values else 0):
                corrected += 1

            if values:
                frappe.db.set_value("GL Entry", entry.name, values, update_modified=False)

    print(f"stamped {stamped} ledger row(s), corrected {corrected}, left {skipped} unmatched")
