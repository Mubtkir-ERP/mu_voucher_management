"""End-to-end verification of the Voucher Management accounting fixes.

Run with:

    bench --site <site> execute voucher_management.full_check.run

Everything is created inside one transaction and rolled back at the end, so the site is
left exactly as it was found. Pass `keep=True` to commit the fixtures instead, e.g.

    bench --site <site> execute voucher_management.full_check.run --kwargs "{'keep': True}"
"""

import frappe
from frappe.utils import add_days, cint, flt, getdate, nowdate

from voucher_management.voucher_management.doctype.vouchers_entry.vouchers_entry import get_tax_breakup

COMPANY = None
ABBR = None
ORIGINAL_FROZEN_UPTO = None
# Set in run(). Never rebuild this name by hand — see ensure_account.
WHT_ACCOUNT = None
PREFIX = "VMCHK"

RESULTS = []


# ---------------------------------------------------------------------------
# tiny assertion helpers
# ---------------------------------------------------------------------------

def check(name, condition, detail=""):
	RESULTS.append((bool(condition), name, detail))
	print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))
	return bool(condition)


def check_eq(name, actual, expected, tol=0.005):
	if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
		ok = abs(flt(actual) - flt(expected)) <= tol
	else:
		ok = actual == expected
	return check(name, ok, f"expected={expected} actual={actual}")


def expect_throw(name, fn, contains=None):
	try:
		fn()
	except Exception as e:
		message = str(e)
		if contains and contains not in message:
			return check(name, False, f"wrong error: {message[:160]}")
		return check(name, True, message.split("\n")[0][:90])
	return check(name, False, "no exception raised")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def acc(number_and_name):
	return f"{number_and_name} - {ABBR}"


def ensure_account(name, parent, account_type=None, root_type=None, currency=None):
	# Never assume the document name is "<account_name> - <abbr>". On a chart that uses
	# account numbers ERPNext prefixes one automatically, so a freshly created
	# "VMCHK WHT" comes back as "2301 - VMCHK WHT - K". Look it up by account_name and
	# hand back whatever name the site actually gave it.
	existing = frappe.db.get_value(
		"Account", {"company": COMPANY, "account_name": name}, "name")
	if existing:
		return existing

	doc = frappe.get_doc({
		"doctype": "Account",
		"account_name": name,
		"parent_account": parent,
		"company": COMPANY,
		"account_type": account_type,
		"root_type": root_type,
		"is_group": 0,
		"account_currency": currency or frappe.get_cached_value("Company", COMPANY, "default_currency"),
	}).insert(ignore_permissions=True)
	return doc.name


def ensure_cost_center(name):
	full = f"{name} - {ABBR}"
	if frappe.db.exists("Cost Center", full):
		return full

	root = frappe.db.get_value("Cost Center", {"company": COMPANY, "is_group": 1}, "name")
	return frappe.get_doc({
		"doctype": "Cost Center",
		"cost_center_name": name,
		"parent_cost_center": root,
		"company": COMPANY,
		"is_group": 0,
	}).insert(ignore_permissions=True).name


def ensure_tax_template(title, rows):
	name = f"{title} - {ABBR}"
	if frappe.db.exists("Purchase Taxes and Charges Template", name):
		frappe.delete_doc("Purchase Taxes and Charges Template", name, force=True, ignore_permissions=True)

	doc = frappe.get_doc({
		"doctype": "Purchase Taxes and Charges Template",
		"title": title,
		"company": COMPANY,
		"taxes": [
			{
				"category": "Total",
				"charge_type": r["charge_type"],
				"account_head": r["account_head"],
				"rate": r.get("rate", 0),
				"tax_amount": r.get("tax_amount", 0),
				"add_deduct_tax": r.get("add_deduct_tax", "Add"),
				"included_in_print_rate": r.get("included_in_print_rate", 0),
				"description": r.get("description", title),
				"cost_center": r.get("cost_center"),
			}
			for r in rows
		],
	})
	doc.insert(ignore_permissions=True)
	return doc.name


def ensure_item():
	code = f"{PREFIX}-SERVICE"
	if frappe.db.exists("Item", code):
		return code

	group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
	return frappe.get_doc({
		"doctype": "Item",
		"item_code": code,
		"item_name": "Voucher check service",
		"item_group": group,
		"stock_uom": frappe.db.get_value("UOM", {}, "name") or "Nos",
		"is_stock_item": 0,
		"is_purchase_item": 1,
		"is_sales_item": 1,
	}).insert(ignore_permissions=True).name


def make_sales_invoice(customer, rate, cost_center):
	doc = frappe.get_doc({
		"doctype": "Sales Invoice",
		"customer": customer,
		"company": COMPANY,
		"posting_date": nowdate(),
		"due_date": add_days(nowdate(), 30),
		"debit_to": acc("1310 - Debtors"),
		"items": [{
			"item_code": ensure_item(),
			"qty": 1,
			"rate": rate,
			"income_account": acc("4110 - Sales"),
			"cost_center": cost_center,
		}],
	})
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc


BILL_SEQ = 0


def make_purchase_invoice(supplier, rate, cost_center):
	# A supplier invoice number has to be unique per supplier when
	# `check_supplier_invoice_uniqueness` is on, and this run creates several for the same
	# supplier. Number them rather than reusing one literal.
	global BILL_SEQ
	BILL_SEQ += 1

	doc = frappe.get_doc({
		"doctype": "Purchase Invoice",
		"supplier": supplier,
		"company": COMPANY,
		"posting_date": nowdate(),
		"due_date": add_days(nowdate(), 30),
		"bill_no": f"{PREFIX}-BILL-{BILL_SEQ:03d}",
		"credit_to": acc("2110 - Creditors"),
		"items": [{
			"item_code": ensure_item(),
			"qty": 1,
			"rate": rate,
			"expense_account": acc("5201 - Administrative Expenses"),
			"cost_center": cost_center,
		}],
	})
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc


def make_voucher(payment_type, rows=None, submit=False, **header):
	payload = {
		"doctype": "Vouchers Entry",
		"payment_type": payment_type,
		"company": COMPANY,
		"posting_date": nowdate(),
		"references": rows or [],
	}
	payload.update(header)

	doc = frappe.get_doc(payload)
	doc.insert(ignore_permissions=True)
	if submit:
		doc.submit()
	return doc


# ---------------------------------------------------------------------------
# ledger helpers
# ---------------------------------------------------------------------------

def gl_of(doc, include_cancelled=False):
	filters = {"voucher_type": doc.doctype, "voucher_no": doc.name}
	if not include_cancelled:
		filters["is_cancelled"] = 0

	return frappe.get_all(
		"GL Entry",
		filters=filters,
		fields=["account", "party", "debit", "credit", "against_voucher", "cost_center",
				"project", "remarks", "voucher_detail_no", "is_cancelled", "name"],
		order_by="creation asc",
	)


def totals(entries):
	return flt(sum(flt(e.debit) for e in entries), 2), flt(sum(flt(e.credit) for e in entries), 2)


def dump(label, entries):
	print(f"    -- {label}")
	for e in entries:
		print(f"       {e.account:<42} D:{flt(e.debit):>12,.2f} C:{flt(e.credit):>12,.2f} "
			  f"party={e.party or '-'} against={e.against_voucher or '-'}")


# ---------------------------------------------------------------------------
# the checks
# ---------------------------------------------------------------------------

def check_tax_math(templates):
	print("\n[1] Tax breakup — the arithmetic that reaches the ledger")

	# Item 1 of the review: a 15% tax-inclusive template on 1,150.
	b = get_tax_breakup(templates["inclusive"], 1150, 2)
	check_eq("inclusive 15% on 1150 -> net 1000", b["net_amount"], 1000)
	check_eq("inclusive 15% on 1150 -> tax 150", b["total"], 150)
	check_eq("inclusive 15% on 1150 -> grand 1150", b["grand_total"], 1150)

	b = get_tax_breakup(templates["exclusive"], 1000, 2)
	check_eq("exclusive 15% on 1000 -> net 1000", b["net_amount"], 1000)
	check_eq("exclusive 15% on 1000 -> tax 150", b["total"], 150)
	check_eq("exclusive 15% on 1000 -> grand 1150", b["grand_total"], 1150)

	# "Actual" rows used to be treated as a 0% rate and vanish.
	b = get_tax_breakup(templates["actual"], 1000, 2)
	check_eq("Actual 50 on 1000 -> tax 50", b["total"], 50)
	check_eq("Actual 50 on 1000 -> grand 1050", b["grand_total"], 1050)

	# A "Deduct" row produces a negative tax (withholding).
	b = get_tax_breakup(templates["deduct"], 1000, 2)
	check_eq("Deduct 5% on 1000 -> tax -50", b["total"], -50)
	check_eq("Deduct 5% on 1000 -> grand 950", b["grand_total"], 950)

	# Two account heads in one template must stay separate.
	b = get_tax_breakup(templates["multi"], 1000, 2)
	check_eq("multi template -> 2 tax lines", len(b["rows"]), 2)
	check_eq("multi template -> VAT 150", b["rows"][0]["amount"], 150)
	check_eq("multi template -> WHT -50", b["rows"][1]["amount"], -50)
	check_eq("multi template -> net tax 100", b["total"], 100)

	# Mixed inclusive + exclusive, the formula given in the review document.
	b = get_tax_breakup(templates["mixed"], 1150, 2)
	check_eq("mixed 15% incl + 5% excl on 1150 -> net 1000", b["net_amount"], 1000)
	check_eq("mixed 15% incl + 5% excl on 1150 -> tax 200", b["total"], 200)
	check_eq("mixed 15% incl + 5% excl on 1150 -> grand 1200", b["grand_total"], 1200)


def check_inclusive_voucher(templates, cc):
	print("\n[2] Inclusive tax on a submitted receipt — ledger amounts")

	doc = make_voucher(
		"Receive",
		rows=[{
			"account": acc("1310 - Debtors"), "party_type": "Customer", "party": "خالد",
			"amount": 1150, "taxes": templates["inclusive"], "cost_center": cc,
		}],
		account_payment=acc("1110 - Cash"), cost_center=cc, remarks="inclusive receipt",
		submit=True,
	)
	doc.reload()

	check_eq("row net (amount_before_tax)", doc.references[0].amount_before_tax, 1000)
	check_eq("row tax", doc.references[0].tax_amount, 150)
	check_eq("document grand total", doc.amount_after_tax, 1150)

	entries = gl_of(doc)
	dump("GL", entries)
	debit, credit = totals(entries)
	check_eq("GL balanced", debit, credit)
	check_eq("cash debited with the gross", next(e.debit for e in entries if e.account == acc("1110 - Cash")), 1150)
	check_eq("customer credited with the net", next(e.credit for e in entries if e.party == "خالد"), 1000)
	check_eq("VAT credited with the tax", next(e.credit for e in entries if e.account == acc("VAT")), 150)

	return doc


def check_negative_tax(templates, cc):
	print("\n[3] Withholding (negative tax) — the line the old code dropped")

	doc = make_voucher(
		"Pay",
		rows=[{
			"account": acc("2110 - Creditors"), "party_type": "Supplier", "party": "شركة الخالد",
			"amount": 1000, "taxes": templates["deduct"], "cost_center": cc,
		}],
		account_payment=acc("1110 - Cash"), cost_center=cc, remarks="withholding payment",
		submit=True,
	)
	doc.reload()

	check_eq("document total taxes is negative", doc.total_taxes, -50)
	check_eq("cash paid out is net of withholding", doc.amount_after_tax, 950)

	entries = gl_of(doc)
	dump("GL", entries)
	debit, credit = totals(entries)
	check_eq("GL balanced", debit, credit)

	wht = [e for e in entries if e.account == WHT_ACCOUNT]
	check("withholding line was posted", len(wht) == 1)
	if wht:
		check_eq("withholding is a credit of 50", wht[0].credit, 50)

	return doc


def check_multi_tax_accounts(templates, cc):
	print("\n[4] One tax account per head — not everything on the first one")

	doc = make_voucher(
		"Pay",
		rows=[
			{"account": acc("5201 - Administrative Expenses"), "amount": 1000,
			 "taxes": templates["exclusive"], "cost_center": cc},
			{"account": acc("5209 - Office Rent"), "amount": 2000,
			 "taxes": templates["deduct"], "cost_center": cc},
		],
		account_payment=acc("1110 - Cash"), cost_center=cc, remarks="two templates",
		submit=True,
	)
	doc.reload()

	entries = gl_of(doc)
	dump("GL", entries)
	debit, credit = totals(entries)
	check_eq("GL balanced", debit, credit)

	vat = [e for e in entries if e.account == acc("VAT")]
	wht = [e for e in entries if e.account == WHT_ACCOUNT]
	check("VAT line posted separately", len(vat) == 1)
	check("WHT line posted separately", len(wht) == 1)
	if vat:
		check_eq("VAT = 15% of 1000", vat[0].debit, 150)
	if wht:
		check_eq("WHT = 5% of 2000 on the opposite side", wht[0].credit, 100)

	return doc


def check_duplicate_allocation(cc):
	print("\n[5] Same party in two rows with one invoice allocation")
	print("     (old code posted the allocation once per row and dropped the negative remainder)")

	invoice = make_sales_invoice("علي", 4000, cc)
	check_eq("invoice outstanding before payment", invoice.outstanding_amount, 4000)

	doc = make_voucher(
		"Receive",
		rows=[
			{"account": acc("1310 - Debtors"), "party_type": "Customer", "party": "علي",
			 "amount": 1000, "cost_center": cc},
			{"account": acc("1310 - Debtors"), "party_type": "Customer", "party": "علي",
			 "amount": 5000, "cost_center": cc},
		],
		account_payment=acc("1110 - Cash"), cost_center=cc, remarks="split allocation",
		vouchers_payment_references=[{
			"reference_doctype": "Sales Invoice",
			"reference_name": invoice.name,
			"customer": "علي",
			"total_amount": 4000,
			"outstanding_amount": 4000,
			"allocated_amount": 4000,
		}],
		submit=True,
	)
	doc.reload()

	entries = gl_of(doc)
	dump("GL", entries)
	debit, credit = totals(entries)
	check_eq("GL balanced", debit, credit)
	check_eq("cash debited once with 6000", debit, 6000)

	against = [e for e in entries if e.against_voucher == invoice.name]
	allocated_total = flt(sum(flt(e.credit) for e in against), 2)
	check_eq("invoice was credited exactly 4000 in total", allocated_total, 4000)
	check_eq("allocation split across the two rows", len(against), 2)

	rows_touched = {e.voucher_detail_no for e in against}
	check_eq("the split hit two distinct reference rows", len(rows_touched), 2)

	invoice.reload()
	check_eq("invoice outstanding after payment", invoice.outstanding_amount, 0)

	return doc, invoice


def check_over_allocation(cc):
	print("\n[6] Over-allocation is refused at save time")

	invoice = make_sales_invoice("احمد", 9000, cc)

	def build():
		make_voucher(
			"Receive",
			rows=[{"account": acc("1310 - Debtors"), "party_type": "Customer", "party": "احمد",
				   "amount": 1000, "cost_center": cc}],
			account_payment=acc("1110 - Cash"), cost_center=cc,
			vouchers_payment_references=[{
				"reference_doctype": "Sales Invoice", "reference_name": invoice.name,
				"customer": "احمد", "total_amount": 9000, "outstanding_amount": 9000,
				"allocated_amount": 9000,
			}],
		)

	expect_throw("allocating 9000 against a 1000 row is blocked", build, contains="exceeds")

	bill = make_purchase_invoice("شركة الخالد", 700, cc)

	def wrong_direction():
		make_voucher(
			"Receive",
			rows=[{"account": acc("1310 - Debtors"), "party_type": "Customer", "party": "احمد",
				   "amount": 1000, "cost_center": cc}],
			account_payment=acc("1110 - Cash"), cost_center=cc,
			vouchers_payment_references2=[{
				"reference_doctype": "Purchase Invoice", "reference_name": bill.name,
				"supplier": "شركة الخالد", "total_amount": 700,
				"outstanding_amount": 700, "allocated_amount": 100,
			}],
		)

	expect_throw("supplier references on a Receive voucher are blocked", wrong_direction,
				 contains="opposite payment direction")


def check_cancel_reverses(cc):
	print("\n[7] Cancel reverses the ledger instead of deleting it")

	invoice = make_purchase_invoice("شركة الخالد", 3000, cc)
	check_eq("purchase invoice outstanding", invoice.outstanding_amount, 3000)

	doc = make_voucher(
		"Pay",
		rows=[{"account": acc("2110 - Creditors"), "party_type": "Supplier", "party": "شركة الخالد",
			   "amount": 3000, "cost_center": cc}],
		account_payment=acc("1110 - Cash"), cost_center=cc, remarks="to be cancelled",
		vouchers_payment_references2=[{
			"reference_doctype": "Purchase Invoice", "reference_name": invoice.name,
			"supplier": "شركة الخالد", "total_amount": 3000, "outstanding_amount": 3000,
			"allocated_amount": 3000,
		}],
		submit=True,
	)

	live_before = len(gl_of(doc))
	invoice.reload()
	check_eq("invoice settled by the payment", invoice.outstanding_amount, 0)

	doc.reload()
	doc.cancel()

	live_after = gl_of(doc)
	all_rows = gl_of(doc, include_cancelled=True)

	check_eq("no live GL rows remain", len(live_after), 0)
	check("original rows kept with is_cancelled = 1", len(all_rows) >= live_before * 2,
		  f"{len(all_rows)} rows kept for {live_before} original")

	invoice.reload()
	check_eq("invoice outstanding restored after cancel", invoice.outstanding_amount, 3000)

	ple = frappe.get_all("Payment Ledger Entry",
						 filters={"voucher_type": doc.doctype, "voucher_no": doc.name},
						 fields=["delinked", "amount"])
	check("payment ledger entries de-linked, not deleted", bool(ple) and all(p.delinked for p in ple),
		  str(ple))

	return doc


def check_in_place_update(templates, cc, cc2):
	print("\n[8] Editing after submit re-stamps the ledger — it does not rebuild it")

	doc = make_voucher(
		"Receive",
		rows=[{"account": acc("1310 - Debtors"), "party_type": "Customer", "party": "خالد",
			   "amount": 2300, "taxes": templates["inclusive"], "cost_center": cc}],
		account_payment=acc("1110 - Cash"), cost_center=cc, remarks="before edit",
		submit=True,
	)

	before = gl_of(doc)
	before_names = sorted(e.name for e in before)
	before_debit, before_credit = totals(before)

	doc.reload()
	doc.remarks = "after edit"
	doc.cost_center = cc2
	doc.references[0].cost_center = cc2
	doc.references[0].user_remark = "row level note"
	doc.save()

	after = gl_of(doc)
	after_names = sorted(e.name for e in after)
	after_debit, after_credit = totals(after)

	check("the very same GL rows survived (no delete + rebuild)", before_names == after_names,
		  f"{len(before_names)} -> {len(after_names)}")
	check_eq("amounts untouched (debit)", after_debit, before_debit)
	check_eq("amounts untouched (credit)", after_credit, before_credit)
	check("cost centre re-stamped on every row",
		  all(e.cost_center == cc2 for e in after), str({e.account: e.cost_center for e in after}))

	party_line = next(e for e in after if e.party == "خالد")
	header_line = next(e for e in after if e.account == acc("1110 - Cash"))
	check_eq("row line carries the row remark", party_line.remarks, "row level note")
	check_eq("header line carries the document remark", header_line.remarks, "after edit")

	# The failure mode the old code had: a frozen period made the rebuild fail *after*
	# the delete had already succeeded, leaving the voucher with no ledger at all.
	frappe.db.set_single_value("Accounts Settings", "acc_frozen_upto", add_days(nowdate(), 1))
	frappe.clear_cache()
	try:
		doc.reload()
		doc.remarks = "edit inside a frozen period"

		expect_throw("editing inside a frozen period is refused", doc.save, contains="not authorized")

		survivors = gl_of(doc)
		check("ledger survived the refused edit", sorted(e.name for e in survivors) == before_names,
			  f"{len(survivors)} rows")
	finally:
		frappe.db.set_single_value("Accounts Settings", "acc_frozen_upto", ORIGINAL_FROZEN_UPTO)
		frappe.clear_cache()

	return doc


def check_period_guards(cc):
	print("\n[9] Frozen period and fiscal year are refused at save time")

	frappe.db.set_single_value("Accounts Settings", "acc_frozen_upto", add_days(nowdate(), 5))
	frappe.clear_cache()
	try:
		def frozen():
			make_voucher(
				"Receive",
				rows=[{"account": acc("1310 - Debtors"), "party_type": "Customer", "party": "خالد",
					   "amount": 100, "cost_center": cc}],
				account_payment=acc("1110 - Cash"), cost_center=cc,
			)

		expect_throw("saving a draft inside the frozen window is blocked", frozen,
					 contains="not authorized")
	finally:
		frappe.db.set_single_value("Accounts Settings", "acc_frozen_upto", ORIGINAL_FROZEN_UPTO)
		frappe.clear_cache()

	def no_fiscal_year():
		make_voucher(
			"Receive",
			rows=[{"account": acc("1310 - Debtors"), "party_type": "Customer", "party": "خالد",
				   "amount": 100, "cost_center": cc}],
			account_payment=acc("1110 - Cash"), cost_center=cc,
			posting_date="1999-05-05",
		)

	expect_throw("a date with no Fiscal Year is blocked at save", no_fiscal_year)


def check_internal_transfer(cc):
	print("\n[10] Internal transfer")

	bank = ensure_account(f"{PREFIX} Bank", frappe.db.get_value(
		"Account", {"company": COMPANY, "account_name": "Bank Accounts"}, "name")
		or frappe.db.get_value("Account", {"company": COMPANY, "root_type": "Asset", "is_group": 1}, "name"),
		account_type="Bank", root_type="Asset")

	doc = make_voucher(
		"Internal Transfer",
		rows=[],
		paid_from=acc("1110 - Cash"), paid_to=bank, paid_amount=5000,
		cost_center=cc, remarks="cash to bank", submit=True,
	)

	entries = gl_of(doc)
	dump("GL", entries)
	debit, credit = totals(entries)
	check_eq("GL balanced", debit, credit)
	check_eq("exactly two lines", len(entries), 2)
	check_eq("source credited", next(e.credit for e in entries if e.account == acc("1110 - Cash")), 5000)
	check_eq("destination debited", next(e.debit for e in entries if e.account == bank), 5000)

	def same_account():
		make_voucher("Internal Transfer", rows=[], paid_from=acc("1110 - Cash"),
					 paid_to=acc("1110 - Cash"), paid_amount=100, cost_center=cc)

	expect_throw("transferring an account to itself is blocked", same_account, contains="cannot be the same")

	return doc


def check_account_guards(cc):
	print("\n[11] Account guards")

	group = frappe.db.get_value("Account", {"company": COMPANY, "is_group": 1}, "name")

	def group_account():
		make_voucher(
			"Receive",
			rows=[{"account": group, "amount": 100, "cost_center": cc}],
			account_payment=acc("1110 - Cash"), cost_center=cc,
		)

	expect_throw("a group account is refused", group_account, contains="group account")

	def party_missing():
		make_voucher(
			"Receive",
			rows=[{"account": acc("1310 - Debtors"), "amount": 100, "cost_center": cc}],
			account_payment=acc("1110 - Cash"), cost_center=cc,
		)

	expect_throw("a receivable row without a party is refused", party_missing, contains="Party Type")

	def zero_amount():
		make_voucher(
			"Receive",
			rows=[{"account": acc("1310 - Debtors"), "party_type": "Customer", "party": "خالد",
				   "amount": 0, "cost_center": cc}],
			account_payment=acc("1110 - Cash"), cost_center=cc,
		)

	expect_throw("a zero amount row is refused", zero_amount, contains="greater than zero")

	# Foreign currencies are supported now. What is still refused is a foreign *tax*
	# account: one tax head collects the tax of rows that may be in different currencies,
	# so its account-currency balance would have no meaning.
	usd_tax = ensure_account(f"{PREFIX} USD Tax", liability_group(), account_type="Tax",
							 root_type="Liability", currency="USD")
	foreign_template = ensure_tax_template(f"{PREFIX} USD VAT", [
		{"charge_type": "On Net Total", "account_head": usd_tax, "rate": 15}])

	def foreign_tax_account():
		make_voucher(
			"Receive",
			rows=[{"account": acc("1310 - Debtors"), "party_type": "Customer", "party": "خالد",
				   "amount": 100, "taxes": foreign_template, "cost_center": cc}],
			account_payment=acc("1110 - Cash"), cost_center=cc,
		)

	expect_throw("a foreign-currency tax account head is refused", foreign_tax_account,
				 contains="Tax accounts must be in the company currency")

	# A NULL account_currency means "company currency" in ERPNext and must still pass.
	blank = frappe.db.get_value("Account", {"company": COMPANY, "is_group": 0,
										   "account_currency": ("is", "not set")}, "name")
	check("an account with a blank currency is treated as company currency",
		  True if not blank else _saves_with_account(blank, cc),
		  blank or "no blank-currency account on this site")


def liability_group():
	return frappe.db.get_value("Account", {"company": COMPANY, "root_type": "Liability", "is_group": 1}, "name")


def asset_group():
	return frappe.db.get_value("Account", {"company": COMPANY, "root_type": "Asset", "is_group": 1}, "name")


def _saves_with_account(account, cc):
	try:
		make_voucher(
			"Pay",
			rows=[{"account": account, "amount": 100, "cost_center": cc}],
			account_payment=acc("1110 - Cash"), cost_center=cc,
		)
		return True
	except Exception as e:
		if "Only accounts in the company currency" in str(e):
			return False
		return True  # rejected for an unrelated reason (party required, etc.)


def check_server_recompute(templates, cc):
	print("\n[12] Client-supplied totals are ignored")

	doc = make_voucher(
		"Receive",
		rows=[{
			"account": acc("1310 - Debtors"), "party_type": "Customer", "party": "خالد",
			"amount": 1150, "taxes": templates["inclusive"], "cost_center": cc,
			# what a tampered API payload would send
			"amount_before_tax": 999999, "tax_amount": 999999, "amount_after_tax": 999999,
		}],
		account_payment=acc("1110 - Cash"), cost_center=cc,
		total_allocated_amount=1, total_taxes=1, amount_after_tax=1,
	)

	check_eq("row net recomputed", doc.references[0].amount_before_tax, 1000)
	check_eq("row tax recomputed", doc.references[0].tax_amount, 150)
	check_eq("document total recomputed", doc.amount_after_tax, 1150)


def check_stale_draft_is_recomputed(templates, cc):
	print("\n[13] A draft carrying stale totals is recomputed before it posts")

	doc = make_voucher(
		"Receive",
		rows=[{"account": acc("1310 - Debtors"), "party_type": "Customer", "party": "خالد",
			   "amount": 1150, "taxes": templates["inclusive"], "cost_center": cc}],
		account_payment=acc("1110 - Cash"), cost_center=cc,
	)

	# Simulate the reported symptom: the browser left stale figures behind and they
	# reached the table. Write them straight to the DB, bypassing every controller.
	frappe.db.set_value("Vouchers Entry", doc.name,
						{"total_allocated_amount": 77, "total_taxes": 77, "amount_after_tax": 77},
						update_modified=False)
	frappe.db.set_value("Voucher Entry Account", doc.references[0].name,
						{"amount_before_tax": 77, "tax_amount": 77, "amount_after_tax": 77},
						update_modified=False)

	fresh = frappe.get_doc("Vouchers Entry", doc.name)
	check_eq("the stale figure really is in the database", fresh.amount_after_tax, 77)

	fresh.submit()
	fresh.reload()

	check_eq("submit recomputed the document total", fresh.amount_after_tax, 1150)
	check_eq("submit recomputed the row net", fresh.references[0].amount_before_tax, 1000)

	entries = gl_of(fresh)
	debit, credit = totals(entries)
	check_eq("ledger balanced on the recomputed figures", debit, credit)
	check_eq("ledger used 1150, not the stale 77", debit, 1150)


def check_bulk_permission():
	print("\n[14] The bulk GL endpoint is role-guarded")

	from voucher_management.voucher_management.doctype.vouchers_entry.vouchers_entry import (
		bulk_create_gl_entries,
	)

	original_user = frappe.session.user
	email = f"{PREFIX.lower()}-outsider@example.com"

	if not frappe.db.exists("User", email):
		frappe.get_doc({
			"doctype": "User",
			"email": email,
			"first_name": "Voucher check outsider",
			"send_welcome_email": 0,
			"roles": [],
		}).insert(ignore_permissions=True)

	try:
		frappe.set_user(email)
		check("the test user really has no accounting role",
			  not {"Accounts Manager", "System Manager"} & set(frappe.get_roles()),
			  str(frappe.get_roles()))
		expect_throw("a user without the accounting roles is refused",
					 lambda: bulk_create_gl_entries(["nonexistent"]))
	finally:
		frappe.set_user(original_user)

	# And it still works for someone who does hold the role.
	result = bulk_create_gl_entries(["nonexistent-voucher"])
	check("privileged callers get a structured result, not an exception",
		  isinstance(result, dict) and set(result) == {"success", "failed", "skipped"}, str(result))


def check_trash_guard(cc):
	print("\n[15] A voucher with ledger rows cannot be silently deleted")

	doc = make_voucher(
		"Receive",
		rows=[{"account": acc("1310 - Debtors"), "party_type": "Customer", "party": "خالد",
			   "amount": 500, "cost_center": cc}],
		account_payment=acc("1110 - Cash"), cost_center=cc, submit=True,
	)
	doc.reload()
	doc.cancel()

	expect_throw("deleting a cancelled voucher keeps its ledger safe",
				 lambda: frappe.delete_doc("Vouchers Entry", doc.name, force=True, ignore_permissions=True),
				 contains="General Ledger entries")


def check_schema():
	print("\n[16] Schema corrections")

	vea = frappe.get_meta("Voucher Entry Account")
	check("account is genuinely mandatory", bool(vea.get_field("account").reqd))
	check("account_currency field now exists", bool(vea.get_field("account_currency")))

	for dt in ("Vouchers Ref Child", "Vouchers Ref Child 2"):
		meta = frappe.get_meta(dt)
		for fieldname in ("total_amount", "outstanding_amount", "allocated_amount"):
			check(f"{dt}.{fieldname} is Currency",
				  meta.get_field(fieldname).fieldtype == "Currency",
				  meta.get_field(fieldname).fieldtype)

	child2 = frappe.get_meta("Vouchers Ref Child 2")
	check("Vouchers Ref Child 2 uses `supplier`", bool(child2.get_field("supplier")))
	check("the misspelled column is gone", not frappe.db.has_column("Vouchers Ref Child 2", "suppiler"))

	ve = frappe.get_meta("Vouchers Entry")
	check("company is mandatory", bool(ve.get_field("company").reqd))
	check("dead journal_entry field removed", ve.get_field("journal_entry") is None)
	check("no depends_on still references the non-existent doc.party",
		  not any("doc.party" in (f.depends_on or "") for f in ve.fields))


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def ensure_party(doctype, name, currency):
	if frappe.db.exists(doctype, name):
		return name

	payload = {"doctype": doctype, "default_currency": currency}
	if doctype == "Customer":
		payload.update({
			"customer_name": name,
			"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
			"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
		})
	else:
		payload.update({
			"supplier_name": name,
			"supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}, "name"),
		})

	return frappe.get_doc(payload).insert(ignore_permissions=True).name


def isolate_currency(currency):
	"""Clear any Currency Exchange rows for a pair so a check starts from a known state.

	The site is a live one: somebody may have set a real rate for the same pair between one
	run and the next, and a check that assumes an empty table would then fail for a reason
	that has nothing to do with the code. Everything here is rolled back, so the site's own
	rates survive the run untouched.
	"""
	company_currency = frappe.get_cached_value("Company", COMPANY, "default_currency")

	for name in frappe.get_all(
		"Currency Exchange",
		filters={"from_currency": currency, "to_currency": company_currency},
		pluck="name",
	):
		frappe.delete_doc("Currency Exchange", name, force=True, ignore_permissions=True)

	return company_currency


def set_rate(currency, rate, days_ago=30):
	company_currency = frappe.get_cached_value("Company", COMPANY, "default_currency")
	return frappe.get_doc({
		"doctype": "Currency Exchange",
		"date": add_days(nowdate(), -days_ago),
		"from_currency": currency,
		"to_currency": company_currency,
		"exchange_rate": rate,
	}).insert(ignore_permissions=True)


def check_multi_currency(cc):
	print("\n[17] Multi-currency")

	usd_bank = ensure_account(f"{PREFIX} USD Bank", asset_group(), account_type="Bank",
							  root_type="Asset", currency="USD")
	usd_debtors = ensure_account(f"{PREFIX} USD Debtors", asset_group(), account_type="Receivable",
								 root_type="Asset", currency="USD")
	usd_creditors = ensure_account(f"{PREFIX} USD Creditors", liability_group(), account_type="Payable",
								   root_type="Liability", currency="USD")
	usd_customer = ensure_party("Customer", f"{PREFIX} USD Customer", "USD")
	usd_supplier = ensure_party("Supplier", f"{PREFIX} USD Supplier", "USD")
	fx = frappe.get_cached_value("Company", COMPANY, "exchange_gain_loss_account")
	check("the company has an Exchange Gain/Loss account", bool(fx), fx or "not set")

	# ---- 17a: one currency end to end, no difference to book -----------------
	print("\n  (a) USD receipt into a USD account — one rate, no difference")
	doc = make_voucher(
		"Receive",
		rows=[{"account": usd_debtors, "party_type": "Customer", "party": usd_customer,
			   "amount": 1000, "exchange_rate": 250, "cost_center": cc}],
		account_payment=usd_bank, payment_exchange_rate=250, payment_amount=1000,
		cost_center=cc, remarks="usd receipt", submit=True,
	)
	doc.reload()

	check_eq("row base amount is 1000 x 250", doc.references[0].base_amount_before_tax, 250000)
	check_eq("document total is in company currency", doc.amount_after_tax, 250000)
	check_eq("no exchange difference", doc.exchange_difference, 0)

	entries = gl_of(doc)
	dump("GL", entries)
	debit, credit = totals(entries)
	check_eq("GL balanced", debit, credit)
	check_eq("both legs converted at 250", debit, 250000)
	check_eq("account-currency column holds USD", _account_currency_amount(doc, usd_bank), 1000)

	# ---- 17b: paying a USD supplier out of a YER account ---------------------
	print("\n  (b) USD supplier paid from a YER account — the rate gap is the loss")
	doc = make_voucher(
		"Pay",
		rows=[{"account": usd_creditors, "party_type": "Supplier", "party": usd_supplier,
			   "amount": 1000, "exchange_rate": 250, "cost_center": cc}],
		account_payment=acc("1110 - Cash"), payment_exchange_rate=1, payment_amount=252000,
		cost_center=cc, remarks="cross currency payment", submit=True,
	)
	doc.reload()

	check_eq("supplier debited 250,000 in company currency", doc.amount_after_tax, 250000)
	check_eq("2,000 booked as an exchange loss", doc.exchange_difference, 2000)

	entries = gl_of(doc)
	dump("GL", entries)
	debit, credit = totals(entries)
	check_eq("GL balanced", debit, credit)
	fx_lines = [e for e in entries if e.account == fx]
	check("an exchange line was posted", len(fx_lines) == 1)
	if fx_lines:
		check_eq("the loss is a debit of 2,000", fx_lines[0].debit, 2000)

	# ---- 17c: settling a USD invoice at a rate it was not booked at ----------
	print("\n  (c) A USD invoice settled at a different rate — realised gain")
	invoice = make_foreign_sales_invoice(usd_customer, 1000, 250, usd_debtors, cc)
	check_eq("invoice outstanding is 1000 USD", invoice.outstanding_amount, 1000)
	check_eq("invoice booked at 250", invoice.conversion_rate, 250)

	doc = make_voucher(
		"Receive",
		rows=[{"account": usd_debtors, "party_type": "Customer", "party": usd_customer,
			   "amount": 1000, "exchange_rate": 260, "cost_center": cc}],
		account_payment=usd_bank, payment_exchange_rate=260, payment_amount=1000,
		cost_center=cc, remarks="settled higher",
		vouchers_payment_references=[{
			"reference_doctype": "Sales Invoice", "reference_name": invoice.name,
			"customer": usd_customer, "total_amount": 1000, "outstanding_amount": 1000,
			"allocated_amount": 1000,
		}],
		submit=True,
	)
	doc.reload()

	check_eq("the allocation carries the invoice rate, not the voucher rate",
			 doc.vouchers_payment_references[0].exchange_rate, 250)
	check_eq("realised gain recorded on the allocation",
			 doc.vouchers_payment_references[0].exchange_gain_loss, 10000)

	entries = gl_of(doc)
	dump("GL", entries)
	debit, credit = totals(entries)
	check_eq("GL balanced", debit, credit)

	party_line = next(e for e in entries if e.party == usd_customer)
	check_eq("the receivable is relieved at the invoice rate", party_line.credit, 250000)

	fx_lines = [e for e in entries if e.account == fx]
	check("an exchange line was posted", len(fx_lines) == 1)
	if fx_lines:
		check_eq("the gain is a credit of 10,000", fx_lines[0].credit, 10000)

	invoice.reload()
	check_eq("the invoice clears to exactly zero", invoice.outstanding_amount, 0)

	# ---- 17d: buying one currency with another ------------------------------
	print("\n  (d) Internal transfer across currencies")
	doc = make_voucher(
		"Internal Transfer", rows=[],
		paid_from=acc("1110 - Cash"), source_exchange_rate=1, paid_amount=260000,
		paid_to=usd_bank, target_exchange_rate=258, received_amount=1000,
		cost_center=cc, remarks="bought dollars", submit=True,
	)
	doc.reload()

	check_eq("cost of the conversion booked", doc.exchange_difference, -2000)

	entries = gl_of(doc)
	dump("GL", entries)
	debit, credit = totals(entries)
	check_eq("GL balanced", debit, credit)
	check_eq("source credited 260,000", next(e.credit for e in entries if e.account == acc("1110 - Cash")), 260000)
	check_eq("destination debited 258,000", next(e.debit for e in entries if e.account == usd_bank), 258000)
	fx_lines = [e for e in entries if e.account == fx]
	if check("an exchange line was posted", len(fx_lines) == 1):
		check_eq("the 2,000 shortfall is a debit", fx_lines[0].debit, 2000)

	# ---- 17e: the guards -----------------------------------------------------
	print("\n  (e) Guards")

	isolate_currency("USD")

	def missing_rate():
		make_voucher(
			"Receive",
			rows=[{"account": usd_debtors, "party_type": "Customer", "party": usd_customer,
				   "amount": 1000, "exchange_rate": 0, "cost_center": cc}],
			account_payment=usd_bank, payment_exchange_rate=250, payment_amount=1000, cost_center=cc,
		)

	expect_throw("a foreign row without a rate is refused", missing_rate,
				 contains="Exchange Rate")

	def absurd_gap():
		make_voucher(
			"Receive",
			rows=[{"account": usd_debtors, "party_type": "Customer", "party": usd_customer,
				   "amount": 1000, "exchange_rate": 250, "cost_center": cc}],
			account_payment=acc("1110 - Cash"), payment_exchange_rate=1, payment_amount=400000,
			cost_center=cc,
		)

	expect_throw("a gap far beyond any rate movement is refused", absurd_gap,
				 contains="entry error, not a rate movement")

	def currency_mismatch():
		make_voucher(
			"Receive",
			rows=[{"account": acc("1310 - Debtors"), "party_type": "Customer", "party": usd_customer,
				   "amount": 1000, "cost_center": cc}],
			account_payment=acc("1110 - Cash"), cost_center=cc,
			vouchers_payment_references=[{
				"reference_doctype": "Sales Invoice", "reference_name": invoice.name,
				"customer": usd_customer, "total_amount": 1000, "outstanding_amount": 1000,
				"allocated_amount": 500,
			}],
		)

	expect_throw("a USD invoice cannot be allocated to a YER row", currency_mismatch,
				 contains="Use a party account in the invoice currency")

	# ---- 17f: tax on a foreign row ------------------------------------------
	print("\n  (f) Tax on a foreign row converts at the row rate")
	vat_excl = ensure_tax_template(f"{PREFIX} VAT 15 excl", [
		{"charge_type": "On Net Total", "account_head": acc("VAT"), "rate": 15}])

	doc = make_voucher(
		"Receive",
		rows=[{"account": usd_debtors, "party_type": "Customer", "party": usd_customer,
			   "amount": 1000, "exchange_rate": 250, "taxes": vat_excl, "cost_center": cc}],
		account_payment=usd_bank, payment_exchange_rate=250, payment_amount=1150,
		cost_center=cc, remarks="usd with vat", submit=True,
	)
	doc.reload()

	check_eq("row tax is 150 USD", doc.references[0].tax_amount, 150)
	check_eq("tax reaches the ledger as 37,500 YER", doc.total_taxes, 37500)

	entries = gl_of(doc)
	dump("GL", entries)
	debit, credit = totals(entries)
	check_eq("GL balanced", debit, credit)
	check_eq("the YER VAT account carries the converted tax",
			 next(e.credit for e in entries if e.account == acc("VAT")), 37500)


def _account_currency_amount(doc, account):
	row = frappe.db.get_value(
		"GL Entry",
		{"voucher_type": doc.doctype, "voucher_no": doc.name, "account": account, "is_cancelled": 0},
		["debit_in_account_currency", "credit_in_account_currency"],
	)
	return flt(row[0]) or flt(row[1]) if row else 0


def make_foreign_sales_invoice(customer, amount, rate, debit_to, cost_center):
	doc = frappe.get_doc({
		"doctype": "Sales Invoice",
		"customer": customer,
		"company": COMPANY,
		"posting_date": nowdate(),
		"due_date": add_days(nowdate(), 30),
		"currency": "USD",
		"conversion_rate": rate,
		"debit_to": debit_to,
		"items": [{
			"item_code": ensure_item(),
			"qty": 1,
			"rate": amount,
			"income_account": acc("4110 - Sales"),
			"cost_center": cost_center,
		}],
	})
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc


def check_pegged_rates(cc):
	"""A pegged pair is set once and then filled in for the accountant."""
	print("\n[18] Stored exchange rates fill themselves in")

	from voucher_management.voucher_management.doctype.vouchers_entry.vouchers_entry import (
		get_stored_exchange_rate,
	)

	usd_bank = ensure_account(f"{PREFIX} Peg Bank", asset_group(), account_type="Bank",
							  root_type="Asset", currency="USD")
	company_currency = isolate_currency("USD")

	# Nothing on file yet: the voucher must refuse rather than guess.
	check_eq("no record on file -> rate 0",
			 get_stored_exchange_rate("USD", company_currency, nowdate()), 0)

	def without_a_record():
		make_voucher(
			"Receive",
			rows=[{"account": acc("1310 - Debtors"), "party_type": "Customer", "party": "خالد",
				   "amount": 100, "cost_center": cc}],
			account_payment=usd_bank, cost_center=cc,
		)

	expect_throw("a pair with no Currency Exchange record is refused", without_a_record,
				 contains="add a Currency Exchange record")

	# Set the peg once...
	set_rate("USD", 3.75)

	check_eq("the stored peg is found", get_stored_exchange_rate("USD", company_currency, nowdate()), 3.75)
	check_eq("same currency is always 1", get_stored_exchange_rate(company_currency, company_currency), 1)

	# ...and every leg picks it up without anyone retyping it.
	doc = make_voucher(
		"Receive",
		rows=[{"account": acc("1310 - Debtors"), "party_type": "Customer", "party": "خالد",
			   "amount": 375, "cost_center": cc}],
		account_payment=usd_bank, payment_amount=100, cost_center=cc,
	)

	check_eq("the payment leg filled itself in", doc.payment_exchange_rate, 3.75)
	check_eq("the company-currency row stays at 1", doc.references[0].exchange_rate, 1)
	check_eq("100 USD at the peg is 375", flt(doc.payment_amount) * flt(doc.payment_exchange_rate), 375)
	check_eq("nothing left over on a pegged pair", doc.exchange_difference, 0)

	# A rate the user typed is never overwritten by the stored one.
	typed = make_voucher(
		"Receive",
		rows=[{"account": acc("1310 - Debtors"), "party_type": "Customer", "party": "خالد",
			   "amount": 380, "cost_center": cc}],
		account_payment=usd_bank, payment_exchange_rate=3.80, payment_amount=100, cost_center=cc,
	)
	check_eq("a hand-entered rate survives", typed.payment_exchange_rate, 3.80)

	# The tolerance is tight now: a peg does not move, so a visible gap is an error.
	def beyond_the_peg():
		make_voucher(
			"Receive",
			rows=[{"account": acc("1310 - Debtors"), "party_type": "Customer", "party": "خالد",
				   "amount": 375, "cost_center": cc}],
			account_payment=usd_bank, payment_exchange_rate=3.75, payment_amount=110, cost_center=cc,
		)

	expect_throw("a gap a pegged rate cannot explain is refused", beyond_the_peg,
				 contains="entry error, not a rate movement")


def check_header_dimensions_and_defaults(cc, cc2):
	print("\n[20] Header project is a dimension; header party fields are not")

	# Project autonames by series, so the document name is not the project name — take
	# the name the insert actually produced.
	def make_project(title):
		existing = frappe.db.get_value("Project", {"project_name": title}, "name")
		if existing:
			return existing
		return frappe.get_doc({
			"doctype": "Project", "project_name": title, "company": COMPANY,
		}).insert(ignore_permissions=True).name

	project = make_project(f"{PREFIX} Project A")
	other = make_project(f"{PREFIX} Project B")

	doc = make_voucher(
		"Receive",
		rows=[
			{"account": acc("1310 - Debtors"), "party_type": "Customer", "party": "خالد",
			 "amount": 1000, "cost_center": cc},
			# a row that names its own project must win over the header
			{"account": acc("1310 - Debtors"), "party_type": "Customer", "party": "علي",
			 "amount": 500, "cost_center": cc, "project": other},
		],
		account_payment=acc("1110 - Cash"), cost_center=cc, project=project,
		remarks="header dimension", submit=True,
	)

	entries = gl_of(doc)
	dump("GL", entries)

	bank = next(e for e in entries if e.account == acc("1110 - Cash"))
	khaled = next(e for e in entries if e.party == "خالد")
	ali = next(e for e in entries if e.party == "علي")

	check_eq("the bank line carries the header project", bank.project, project)
	check_eq("a row without its own project inherits the header", khaled.project, project)
	check_eq("a row with its own project overrides the header", ali.project, other)

	# The party fields on the header are a typing shortcut. If they ever became real
	# accounting dimensions they would be stamped on the bank and tax lines too, tagging
	# a multi-party voucher with one party.
	meta = frappe.get_meta("Vouchers Entry")
	check("default_customer exists as a plain field", bool(meta.get_field("default_customer")))
	check("default_supplier exists as a plain field", bool(meta.get_field("default_supplier")))
	check("neither is named like an accounting dimension",
		  not meta.get_field("customer") and not meta.get_field("supplier"))

	from voucher_management.vouchers_entry_gl import _get_dimension_values

	doc.default_customer = "خالد"
	dimensions = _get_dimension_values(doc)
	check("the header default never reaches the ledger as a dimension",
		  "customer" not in dimensions and "default_customer" not in dimensions,
		  str(dimensions))
	check_eq("but the header project does", dimensions.get("project"), project)

	return doc


def check_rounding(templates, cc):
	print("\n[22] Totals land on the figure that was typed")

	# 1,000 on a 15% inclusive template: dividing rounds up to 869.57, taxing that rounds
	# up again to 130.44, and the two halves used to surface as a grand total of 1,000.01.
	doc = make_voucher(
		"Receive",
		rows=[{"account": acc("1310 - Debtors"), "party_type": "Customer", "party": "خالد",
			   "amount": 1000, "taxes": templates["inclusive"], "cost_center": cc}],
		account_payment=acc("1110 - Cash"), cost_center=cc, remarks="rounding",
	)

	row = doc.references[0]
	print(f"    entered {flt(row.amount):,.2f}  ->  net {flt(row.amount_before_tax):,.2f}"
		  f"  + tax {flt(row.tax_amount):,.2f}  = {flt(row.amount_after_tax):,.2f}")

	check_eq("the row total is exactly what was typed", row.amount_after_tax, 1000)
	check_eq("net + tax equals the entered amount",
			 flt(row.amount_before_tax) + flt(row.tax_amount), 1000)
	check_eq("the document total matches", doc.amount_after_tax, 1000)

	# A whole voucher of awkward amounts must not accumulate a drift either.
	awkward = [333.33, 1234.56, 87.65, 999.99, 7.77]
	doc = make_voucher(
		"Receive",
		rows=[{"account": acc("1310 - Debtors"), "party_type": "Customer", "party": "خالد",
			   "amount": a, "taxes": templates["inclusive"], "cost_center": cc}
			  for a in awkward],
		account_payment=acc("1110 - Cash"), cost_center=cc, remarks="rounding sweep",
		submit=True,
	)
	doc.reload()

	drift = [f"{flt(r.amount):,.2f}->{flt(r.amount_after_tax):,.2f}"
			 for r in doc.references if flt(r.amount_after_tax, 2) != flt(r.amount, 2)]
	check("no row drifted from its entered amount", not drift, str(drift))

	check_eq("the document total is the sum of what was typed",
			 doc.amount_after_tax, flt(sum(awkward), 2))
	check_eq("net total plus tax total equals the grand total",
			 flt(doc.total_allocated_amount) + flt(doc.total_taxes), doc.amount_after_tax)

	entries = gl_of(doc)
	debit, credit = totals(entries)
	check_eq("GL balanced", debit, credit)
	check_eq("the bank was debited with the sum of the entered amounts",
			 debit, flt(sum(awkward), 2))


def check_ledger_follows_the_edit(cc, cc2):
	print("\n[24] Update on an approved voucher reaches tabGL Entry")

	project = frappe.db.get_value("Project", {"project_name": f"{PREFIX} Project A"}, "name") \
		or frappe.get_doc({"doctype": "Project", "project_name": f"{PREFIX} Project A",
						   "company": COMPANY}).insert(ignore_permissions=True).name

	def gl_field(voucher, detail_no, fieldname):
		"""Read the ledger the way a report would — straight from GL Entry."""
		return frappe.db.get_value(
			"GL Entry",
			{"voucher_type": "Vouchers Entry", "voucher_no": voucher,
			 "is_cancelled": 0, "voucher_detail_no": detail_no or ("", "is", "not set")},
			fieldname)

	def build_and_edit(label, strip_detail_no):
		doc = make_voucher(
			"Receive",
			rows=[{"account": acc("1310 - Debtors"), "party_type": "Customer", "party": "خالد",
				   "amount": 400, "cost_center": cc}],
			account_payment=acc("1110 - Cash"), cost_center=cc, remarks="ORIGINAL HEADER",
			submit=True,
		)
		row_name = doc.references[0].name

		if strip_detail_no:
			# Exactly what a voucher submitted before the stamp existed looks like.
			frappe.db.sql(
				"""UPDATE `tabGL Entry` SET voucher_detail_no=NULL
				   WHERE voucher_type='Vouchers Entry' AND voucher_no=%s""", doc.name)

		doc.reload()
		doc.remarks = "EDITED HEADER"
		doc.cost_center = cc2
		doc.project = project
		doc.references[0].cost_center = cc2
		doc.references[0].project = project
		doc.references[0].user_remark = "EDITED ROW"
		doc.save()

		print(f"    -- {label}")
		for r in frappe.db.sql(
			"""SELECT account, party, remarks, cost_center, project, voucher_detail_no
			   FROM `tabGL Entry`
			   WHERE voucher_type='Vouchers Entry' AND voucher_no=%s AND is_cancelled=0
			   ORDER BY creation""", doc.name, as_dict=True):
			print(f"       {r.account:<26} party={r.party or '-':<8} cc={r.cost_center}"
				  f" proj={r.project} remarks={r.remarks!r}")

		return doc, row_name

	# --- a voucher posted by the current code -------------------------------------
	doc, row_name = build_and_edit("stamped voucher", strip_detail_no=False)

	check_eq("row line: cost centre reached the ledger",
			 gl_field(doc.name, row_name, "cost_center"), cc2)
	check_eq("row line: project reached the ledger",
			 gl_field(doc.name, row_name, "project"), project)
	check_eq("row line: its own remark reached the ledger",
			 gl_field(doc.name, row_name, "remarks"), "EDITED ROW")

	header = frappe.db.get_value(
		"GL Entry",
		{"voucher_type": "Vouchers Entry", "voucher_no": doc.name, "is_cancelled": 0,
		 "account": acc("1110 - Cash")},
		["remarks", "cost_center", "project"], as_dict=True)
	check_eq("header line: remark reached the ledger", header.remarks, "EDITED HEADER")
	check_eq("header line: cost centre reached the ledger", header.cost_center, cc2)
	check_eq("header line: project reached the ledger", header.project, project)

	# --- a voucher posted before voucher_detail_no was stamped ---------------------
	# The reported symptom. Every GL row looked like a header row, so a row-level edit
	# went nowhere and the header's value was written over the party line instead.
	legacy, legacy_row = build_and_edit("legacy voucher (no voucher_detail_no)",
										strip_detail_no=True)

	party_line = frappe.db.get_value(
		"GL Entry",
		{"voucher_type": "Vouchers Entry", "voucher_no": legacy.name, "is_cancelled": 0,
		 "party": "خالد"},
		["remarks", "cost_center", "project", "voucher_detail_no"], as_dict=True)

	check_eq("legacy row line: its own remark reached the ledger",
			 party_line.remarks, "EDITED ROW")
	check_eq("legacy row line: cost centre reached the ledger",
			 party_line.cost_center, cc2)
	check_eq("legacy row line: project reached the ledger",
			 party_line.project, project)
	check_eq("legacy row line was stamped so the next edit is exact",
			 party_line.voucher_detail_no, legacy_row)

	legacy_header = frappe.db.get_value(
		"GL Entry",
		{"voucher_type": "Vouchers Entry", "voucher_no": legacy.name, "is_cancelled": 0,
		 "account": acc("1110 - Cash")},
		["remarks", "voucher_detail_no"], as_dict=True)
	check_eq("legacy header line kept the document remark",
			 legacy_header.remarks, "EDITED HEADER")
	check("legacy header line was not mistaken for a row",
		  not legacy_header.voucher_detail_no, str(legacy_header.voucher_detail_no))

	# Amounts are not allow_on_submit and must never come through this path.
	def tamper():
		legacy.reload()
		legacy.references[0].amount = 999
		legacy.save()

	expect_throw("an amount cannot be changed after submit", tamper)


def check_precision_is_pinned():
	print("\n[23] Money is pinned to two decimals inside the app only")

	from frappe.model.meta import get_field_precision

	from voucher_management.voucher_management.doctype.vouchers_entry.vouchers_entry import (
		CURRENCY_PRECISION,
		EXCHANGE_RATE_PRECISION,
	)

	money = {
		"Vouchers Entry": ["paid_amount", "total_allocated_amount", "total_taxes",
						   "amount_after_tax", "payment_amount", "received_amount",
						   "exchange_difference"],
		"Voucher Entry Account": ["amount", "amount_before_tax", "tax_amount",
								  "amount_after_tax", "base_amount", "base_amount_before_tax",
								  "base_tax_amount", "base_amount_after_tax"],
		"Vouchers Ref Child": ["total_amount", "outstanding_amount", "allocated_amount",
							   "exchange_gain_loss"],
		"Vouchers Ref Child 2": ["total_amount", "outstanding_amount", "allocated_amount",
								 "exchange_gain_loss"],
	}

	rates = {
		"Vouchers Entry": ["payment_exchange_rate", "source_exchange_rate", "target_exchange_rate"],
		"Voucher Entry Account": ["exchange_rate"],
		"Vouchers Ref Child": ["exchange_rate"],
		"Vouchers Ref Child 2": ["exchange_rate"],
	}

	drift = []
	for doctype, fieldnames in money.items():
		meta = frappe.get_meta(doctype)
		for fieldname in fieldnames:
			df = meta.get_field(fieldname)
			# The JSON must carry the pin, not inherit it — a site whose System Settings
			# say 3 has to still give this app 2.
			if cint(df.precision) != CURRENCY_PRECISION or get_field_precision(df) != CURRENCY_PRECISION:
				drift.append(f"{doctype}.{fieldname} json={df.precision!r} resolved={get_field_precision(df)}")

	check("every money field is pinned to 2 in the DocType JSON", not drift, str(drift[:4]))

	rate_drift = []
	for doctype, fieldnames in rates.items():
		meta = frappe.get_meta(doctype)
		for fieldname in fieldnames:
			df = meta.get_field(fieldname)
			if cint(df.precision) != EXCHANGE_RATE_PRECISION:
				rate_drift.append(f"{doctype}.{fieldname}={df.precision!r}")

	check("exchange rates keep their 9 digits", not rate_drift, str(rate_drift))

	# The whole point of pinning per field is to leave the rest of ERPNext alone. What
	# makes that true is not the number a foreign field happens to resolve to — a blank
	# `currency_precision` already makes most Currency fields land on 2 by themselves —
	# but that nothing outside this app carries a pin we put there.
	system_float = frappe.db.get_single_value("System Settings", "float_precision")
	check("System Settings.float_precision was not narrowed to 2",
		  cint(system_float) != CURRENCY_PRECISION, f"float_precision={system_float!r}")

	outside = []
	for doctype, fieldname in (("Journal Entry", "total_debit"),
							   ("Payment Entry", "paid_amount"),
							   ("Sales Invoice", "grand_total"),
							   ("GL Entry", "debit")):
		df = frappe.get_meta(doctype).get_field(fieldname)
		if df and df.precision:
			outside.append(f"{doctype}.{fieldname}={df.precision!r}")

	check("no field outside this app was pinned", not outside, str(outside))


def check_grid_budget():
	print("\n[21] The child grid fits inside Frappe's column ceiling")

	meta = frappe.get_meta("Voucher Entry Account")
	shown = [(f.fieldname, f.columns or 0) for f in meta.fields if f.in_list_view]
	total = sum(c for _, c in shown)

	print("    " + " + ".join(f"{n}({c})" for n, c in shown) + f" = {total}")

	# grid.js drops every column once the running total passes 11.
	check("total column width is within the ceiling", total <= 11, f"{total} of 11")
	check("every visible column declares a width", all(c > 0 for _, c in shown), str(shown))

	for fieldname in ("account", "party", "amount", "cost_center", "project"):
		check(f"{fieldname} is a grid column", bool(meta.get_field(fieldname).in_list_view))


def check_exchange_difference_figure(cc):
	"""The number on the voucher must be the difference, never the total.

	A blank payment amount used to be read as a zero, which made the whole voucher total
	look like an exchange difference. It balanced in the ledger — the server always derived
	the figure correctly — but the document itself, and the banner built from it, showed a
	gain or loss that did not exist.
	"""
	print("\n[19] The exchange figure is a difference, not a total")

	usd_bank = ensure_account(f"{PREFIX} Diff Bank", asset_group(), account_type="Bank",
							  root_type="Asset", currency="USD")
	isolate_currency("USD")
	set_rate("USD", 3.75)

	# payment_amount deliberately left out — the exact state that produced the phantom.
	doc = make_voucher(
		"Receive",
		rows=[{"account": acc("1310 - Debtors"), "party_type": "Customer", "party": "خالد",
			   "amount": 750, "cost_center": cc}],
		account_payment=usd_bank, cost_center=cc,
	)

	check_eq("the total is the total", doc.amount_after_tax, 750)
	check_eq("the payment amount was derived, not left blank", doc.payment_amount, 200)
	check_eq("the difference is zero, not the voucher total", doc.exchange_difference, 0)
	check("the difference is not the total",
		  flt(doc.exchange_difference) != flt(doc.amount_after_tax)
		  and flt(doc.exchange_difference) != -flt(doc.amount_after_tax),
		  f"difference={doc.exchange_difference} total={doc.amount_after_tax}")

	# A real gap still reports the gap itself, not either side of it.
	doc = make_voucher(
		"Receive",
		rows=[{"account": acc("1310 - Debtors"), "party_type": "Customer", "party": "خالد",
			   "amount": 750, "cost_center": cc}],
		account_payment=usd_bank, payment_exchange_rate=3.75, payment_amount=201,
		cost_center=cc,
	)
	check_eq("a genuine gap is reported at its own size", doc.exchange_difference, 3.75)

	# And a transfer keeps its conversion cost instead of being flattened to zero.
	transfer = make_voucher(
		"Internal Transfer", rows=[],
		paid_from=acc("1110 - Cash"), source_exchange_rate=1, paid_amount=750,
		paid_to=usd_bank, target_exchange_rate=3.75, received_amount=199,
		cost_center=cc,
	)
	check_eq("a transfer keeps its conversion cost", transfer.exchange_difference, -3.75)
	check_eq("the transfer total is the amount paid out", transfer.amount_after_tax, 750)


def run(keep=False):
	global COMPANY, ABBR, ORIGINAL_FROZEN_UPTO

	COMPANY = frappe.defaults.get_user_default("Company") or frappe.db.get_value("Company", {}, "name")
	ABBR = frappe.get_cached_value("Company", COMPANY, "abbr")
	# Read it the way ERPNext does: `get_single_value` casts a NULL to date(1, 1, 1),
	# and writing that back turns the setting into the "0001-01-01" string that
	# ERPNext's own `check_freezing_date` cannot compare.
	ORIGINAL_FROZEN_UPTO = frappe.db.get_value("Accounts Settings", None, "acc_frozen_upto")

	print("=" * 78)
	print(f"Voucher Management — full check on {frappe.local.site}  (company: {COMPANY})")
	print("=" * 78)

	frappe.flags.ignore_permissions = True

	try:
		duties = frappe.db.get_value("Account", {"company": COMPANY, "account_name": "Duties and Taxes"}, "name") \
			or frappe.db.get_value("Account", {"company": COMPANY, "root_type": "Liability", "is_group": 1}, "name")

		global WHT_ACCOUNT
		wht_account = WHT_ACCOUNT = ensure_account(
			f"{PREFIX} WHT", duties, account_type="Tax", root_type="Liability")
		vat_account = acc("VAT")
		cc = frappe.db.get_value("Cost Center", {"company": COMPANY, "is_group": 0}, "name")
		cc2 = ensure_cost_center(f"{PREFIX} CC")

		templates = {
			"inclusive": ensure_tax_template(f"{PREFIX} VAT 15 incl", [
				{"charge_type": "On Net Total", "account_head": vat_account, "rate": 15,
				 "included_in_print_rate": 1}]),
			"exclusive": ensure_tax_template(f"{PREFIX} VAT 15 excl", [
				{"charge_type": "On Net Total", "account_head": vat_account, "rate": 15}]),
			"actual": ensure_tax_template(f"{PREFIX} Actual 50", [
				{"charge_type": "Actual", "account_head": vat_account, "tax_amount": 50}]),
			"deduct": ensure_tax_template(f"{PREFIX} WHT 5 deduct", [
				{"charge_type": "On Net Total", "account_head": wht_account, "rate": 5,
				 "add_deduct_tax": "Deduct"}]),
			"multi": ensure_tax_template(f"{PREFIX} VAT plus WHT", [
				{"charge_type": "On Net Total", "account_head": vat_account, "rate": 15},
				{"charge_type": "On Net Total", "account_head": wht_account, "rate": 5,
				 "add_deduct_tax": "Deduct"}]),
			"mixed": ensure_tax_template(f"{PREFIX} 15 incl plus 5 excl", [
				{"charge_type": "On Net Total", "account_head": vat_account, "rate": 15,
				 "included_in_print_rate": 1},
				{"charge_type": "On Net Total", "account_head": wht_account, "rate": 5}]),
		}

		check_tax_math(templates)
		check_inclusive_voucher(templates, cc)
		check_negative_tax(templates, cc)
		check_multi_tax_accounts(templates, cc)
		check_duplicate_allocation(cc)
		check_over_allocation(cc)
		check_cancel_reverses(cc)
		check_in_place_update(templates, cc, cc2)
		check_period_guards(cc)
		check_internal_transfer(cc)
		check_account_guards(cc)
		check_server_recompute(templates, cc)
		check_stale_draft_is_recomputed(templates, cc)
		check_bulk_permission()
		check_trash_guard(cc)
		check_schema()
		check_multi_currency(cc)
		check_pegged_rates(cc)
		check_exchange_difference_figure(cc)
		check_header_dimensions_and_defaults(cc, cc2)
		check_rounding(templates, cc)
		check_ledger_follows_the_edit(cc, cc2)
		check_precision_is_pinned()
		check_grid_budget()

	finally:
		passed = sum(1 for ok, _, _ in RESULTS if ok)
		failed = [(name, detail) for ok, name, detail in RESULTS if not ok]

		print("\n" + "=" * 78)
		print(f"  {passed}/{len(RESULTS)} checks passed")
		if failed:
			print("  FAILURES:")
			for name, detail in failed:
				print(f"    - {name}   [{detail}]")
		print("=" * 78)

		if keep:
			frappe.db.commit()
			print("Fixtures committed (keep=True).")
		else:
			frappe.db.rollback()
			print("Rolled back — the site is unchanged.")
