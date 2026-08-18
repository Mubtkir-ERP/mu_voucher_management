"""Optional one-off: show older vouchers by their number instead of their payment type.

    bench --site <site> execute voucher_management.scripts.set_title_to_voucher_number.run

NOT wired into patches.txt on purpose. `migrate` will never run it — writing to historical
records is a decision, not a side effect of upgrading.

Background: `title_field` points at `title`, and an earlier version of the controller
filled that field with `payment_type`, so the list view showed every row as "Receive" or
"Pay". The controller now stores the document name, but only documents saved *after* that
change carry it — and a submitted voucher cannot be re-saved without amending it, so the
older rows keep their old label indefinitely.

Running this aligns them. `title` is a display label with no accounting meaning: it appears
in the list view, the form heading and link previews, and in no ledger table. Re-stamping it
changes no balance, no GL Entry and no Payment Ledger Entry.

Pass `dry_run=True` to see the count without writing:

    bench --site <site> execute voucher_management.scripts.set_title_to_voucher_number.run \
        --kwargs "{'dry_run': True}"
"""

import frappe


def run(dry_run=False):
	pending = frappe.db.sql(
		"SELECT COUNT(*) FROM `tabVouchers Entry` WHERE IFNULL(title, '') != name"
	)[0][0]

	if not pending:
		print("Nothing to do — every voucher already shows its own number.")
		return

	if dry_run:
		rows = frappe.db.sql(
			"""
			SELECT name, docstatus, title
			FROM `tabVouchers Entry`
			WHERE IFNULL(title, '') != name
			ORDER BY creation DESC
			LIMIT 20
			""",
			as_dict=True,
		)
		print(f"{pending} voucher(s) would be re-titled. First {len(rows)}:")
		for r in rows:
			state = {0: "draft", 1: "submitted", 2: "cancelled"}.get(r.docstatus, r.docstatus)
			print(f"   {r.name:<28} {state:<10} title={r.title!r}")
		print("\nRe-run without dry_run to apply.")
		return

	frappe.db.sql(
		"""
		UPDATE `tabVouchers Entry`
		SET title = name
		WHERE IFNULL(title, '') != name
		"""
	)
	frappe.db.commit()
	frappe.clear_cache(doctype="Vouchers Entry")

	print(f"Re-titled {pending} voucher(s). No ledger table was touched.")
