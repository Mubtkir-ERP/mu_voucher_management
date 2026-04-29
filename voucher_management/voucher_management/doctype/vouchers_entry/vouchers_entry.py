# Copyright (c) 2025, Amir and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class VouchersEntry(Document):
	pass


@frappe.whitelist()
def bulk_create_gl_entries(vouchers):
	import json
	from voucher_management.vouchers_entry_gl import on_submit

	if isinstance(vouchers, str):
		vouchers = json.loads(vouchers)

	results = {"success": [], "failed": [], "skipped": []}

	for name in vouchers:
		try:
			doc = frappe.get_doc("Vouchers Entry", name)

			if doc.docstatus != 1:
				results["skipped"].append(name)
				continue

			on_submit(doc)
			results["success"].append(name)

		except Exception as e:
			results["failed"].append({"name": name, "error": str(e)})

	return results
