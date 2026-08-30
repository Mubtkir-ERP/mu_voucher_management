import frappe


def execute():
	"""Rename the misspelled `suppiler` column on Vouchers Ref Child 2 to `supplier`.

	The typo was baked into the schema, so production rows carry their supplier in a
	column nobody can find by name. This runs post model sync: `supplier` already exists
	as an empty column by then and `suppiler` still holds the data, so `rename_field`
	copies it across. Idempotent — once the old column is gone there is nothing to do.
	"""
	from frappe.model.utils.rename_field import rename_field

	if not frappe.db.has_column("Vouchers Ref Child 2", "suppiler"):
		return

	rename_field("Vouchers Ref Child 2", "suppiler", "supplier")

	# `rename_field` copies values but leaves the dead column behind; drop it so the
	# next migrate cannot resurrect stale data into the renamed field.
	frappe.db.sql_ddl("ALTER TABLE `tabVouchers Ref Child 2` DROP COLUMN `suppiler`")
	frappe.clear_cache(doctype="Vouchers Ref Child 2")
