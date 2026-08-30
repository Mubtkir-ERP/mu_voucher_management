"""Watch the balance fix work, on real data, then roll everything back.

    bench --site mysite.local execute voucher_management.demo_balance.run
"""

import frappe
from frappe.utils import add_days, flt, nowdate

COMPANY = None
ABBR = None


def acc(name):
	return f"{name} - {ABBR}"


def line(char="-", n=74):
	print(char * n)


def show_gl(voucher):
	rows = frappe.get_all(
		"GL Entry",
		filters={"voucher_type": "Vouchers Entry", "voucher_no": voucher, "is_cancelled": 0},
		fields=["account", "party", "debit", "credit", "against_voucher"],
		order_by="creation asc",
	)

	print(f"    {'الحساب':<34}{'مدين':>13}{'دائن':>13}   مقابل")
	line(".")
	debit = credit = 0.0
	for r in rows:
		debit += flt(r.debit)
		credit += flt(r.credit)
		print(f"    {r.account:<34}{flt(r.debit):>13,.2f}{flt(r.credit):>13,.2f}   {r.against_voucher or '-'}")
	line(".")
	print(f"    {'الإجمالي':<34}{debit:>13,.2f}{credit:>13,.2f}")
	print(f"    الفرق (مدين - دائن) = {flt(debit - credit, 2):,.2f}"
		  f"   {'✅ متوازن' if abs(debit - credit) < 0.005 else '❌ غير متوازن'}")
	return debit, credit


def make_invoice(customer, amount):
	item = "_DEMO-SERVICE"
	if not frappe.db.exists("Item", item):
		frappe.get_doc({
			"doctype": "Item", "item_code": item, "item_name": "demo",
			"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
			"stock_uom": "Nos", "is_stock_item": 0,
		}).insert(ignore_permissions=True)

	doc = frappe.get_doc({
		"doctype": "Sales Invoice", "customer": customer, "company": COMPANY,
		"posting_date": nowdate(), "due_date": add_days(nowdate(), 30),
		"debit_to": acc("1310 - Debtors"),
		"items": [{"item_code": item, "qty": 1, "rate": amount,
				   "income_account": acc("4110 - Sales"),
				   "cost_center": frappe.db.get_value("Cost Center", {"company": COMPANY, "is_group": 0}, "name")}],
	})
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc


def build(rows, allocations):
	cc = frappe.db.get_value("Cost Center", {"company": COMPANY, "is_group": 0}, "name")
	return frappe.get_doc({
		"doctype": "Vouchers Entry",
		"payment_type": "Receive",
		"company": COMPANY,
		"posting_date": nowdate(),
		"account_payment": acc("1110 - Cash"),
		"cost_center": cc,
		"remarks": "demo",
		"references": [
			{"account": acc("1310 - Debtors"), "party_type": "Customer", "party": p,
			 "amount": a, "cost_center": cc}
			for p, a in rows
		],
		"vouchers_payment_references": allocations,
	})


def run():
	global COMPANY, ABBR
	COMPANY = frappe.defaults.get_user_default("Company") or frappe.db.get_value("Company", {}, "name")
	ABBR = frappe.get_cached_value("Company", COMPANY, "abbr")
	frappe.flags.ignore_permissions = True

	try:
		line("=")
		print("  السيناريو: العميل «علي» في صفّين — 1,000 و 5,000 — وفاتورة مخصَّص لها 4,000")
		line("=")

		invoice = make_invoice("علي", 4000)
		print(f"\n  الفاتورة {invoice.name}: الإجمالي 4,000 — المستحق {flt(invoice.outstanding_amount):,.2f}")

		alloc = [{
			"reference_doctype": "Sales Invoice", "reference_name": invoice.name,
			"customer": "علي", "total_amount": 4000,
			"outstanding_amount": 4000, "allocated_amount": 4000,
		}]

		# ---------------------------------------------------------------
		print("\n\n[أ] التخصيص الصحيح — 4,000 على صفّين مجموعهما 6,000")
		line()

		doc = build([("علي", 1000), ("علي", 5000)], alloc)
		doc.insert(ignore_permissions=True)
		doc.submit()

		print(f"  السند {doc.name} رُحّل.\n")
		show_gl(doc.name)

		invoice.reload()
		print(f"\n  مستحق الفاتورة بعد الترحيل: {flt(invoice.outstanding_amount):,.2f}"
			  f"   {'✅' if abs(invoice.outstanding_amount) < 0.005 else '❌'}"
			  "  (الكود القديم كان سيجعله -4,000 بسبب التسديد المزدوج)")

		# ---------------------------------------------------------------
		print("\n\n[ب] التخصيص الزائد — 4,000 على صف واحد سعته 1,000")
		line()

		try:
			over = build([("علي", 1000)], [dict(alloc[0])])
			over.insert(ignore_permissions=True)
			print("  ❌ قُبل بلا اعتراض — الحماية لا تعمل")
		except Exception as e:
			print("  ✅ رُفض عند الحفظ، قبل أن يصل الدفتر:")
			print(f"     {frappe.utils.strip_html(str(e)).strip()}")

		# ---------------------------------------------------------------
		print("\n\n[ج] فحص التوازن الصريح — نُجبر القيد على الاختلال")
		line()

		from voucher_management import vouchers_entry_gl

		original = vouchers_entry_gl.distribute_allocations

		def sabotaged(d):
			"""يحاكي الخلل القديم: التخصيص يُرحَّل لكل صف يطابق الطرف."""
			allocations, party_field = d.get_allocation_table()
			return {
				row.name: [(a, flt(a.allocated_amount)) for a in allocations
						   if a.get(party_field) == row.party]
				for row in d.references
			}

		vouchers_entry_gl.distribute_allocations = sabotaged
		try:
			broken = build([("علي", 1000), ("علي", 5000)], [dict(alloc[0])])
			broken.insert(ignore_permissions=True)
			broken.submit()
			print("  ❌ رُحّل قيد غير متوازن — الفحص لا يعمل")
			show_gl(broken.name)
		except Exception as e:
			print("  ✅ أُوقف قبل make_gl_entries:")
			print(f"     {frappe.utils.strip_html(str(e)).strip()}")
			print("\n  وسطور الدفتر التي كانت ستُكتب:")
			print("     نقدية        مدين  6,000")
			print("     ذمم (فاتورة) دائن  4,000")
			print("     ذمم (فاتورة) دائن  4,000   ← مكرر")
			print("     ذمم (متبقّي) دائن  1,000")
			print("     ──────────────────────────  مدين 6,000 مقابل دائن 9,000")
		finally:
			vouchers_entry_gl.distribute_allocations = original

		# ---------------------------------------------------------------
		print("\n\n[د] ميزان المراجعة — أي سند غير متوازن في الموقع كله")
		line()

		unbalanced = frappe.db.sql("""
			SELECT voucher_no, ROUND(SUM(debit) - SUM(credit), 2) AS diff
			FROM `tabGL Entry`
			WHERE voucher_type = 'Vouchers Entry' AND is_cancelled = 0
			GROUP BY voucher_no
			HAVING ABS(SUM(debit) - SUM(credit)) > 0.005
		""", as_dict=True)

		if unbalanced:
			for u in unbalanced:
				print(f"  ❌ {u.voucher_no}: فرق {u.diff:,.2f}")
		else:
			print("  ✅ لا يوجد أي سند غير متوازن")

	finally:
		line("=")
		frappe.db.rollback()
		print("  تم التراجع عن كل شيء — الموقع لم يتغيّر.")
		line("=")
