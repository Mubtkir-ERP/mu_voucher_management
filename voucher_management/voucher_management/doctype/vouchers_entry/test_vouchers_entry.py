# Copyright (c) 2025, Amir and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from voucher_management.voucher_management.doctype.vouchers_entry.vouchers_entry import get_tax_breakup

TEST_PREFIX = "_Test VM"


def _account(company, abbr, name, parent_filters, account_type, root_type):
	full = f"{name} - {abbr}"
	if frappe.db.exists("Account", full):
		return full

	parent = frappe.db.get_value("Account", dict(parent_filters, company=company, is_group=1), "name")
	return frappe.get_doc({
		"doctype": "Account",
		"account_name": name,
		"parent_account": parent,
		"company": company,
		"account_type": account_type,
		"root_type": root_type,
		"is_group": 0,
	}).insert(ignore_permissions=True).name


def _template(company, title, rows):
	name = f"{title} - {frappe.get_cached_value('Company', company, 'abbr')}"
	if frappe.db.exists("Purchase Taxes and Charges Template", name):
		return name

	return frappe.get_doc({
		"doctype": "Purchase Taxes and Charges Template",
		"title": title,
		"company": company,
		"taxes": [dict({"category": "Total", "description": title}, **r) for r in rows],
	}).insert(ignore_permissions=True).name


class TestVouchersEntry(FrappeTestCase):
	"""The tax arithmetic that decides every amount in the ledger.

	These run against templates rather than vouchers on purpose: `get_tax_breakup` is the
	single source of truth for `validate`, for the GL builder and for the client script,
	so pinning it down pins down all three.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()

		cls.company = frappe.defaults.get_user_default("Company") or frappe.db.get_value("Company", {}, "name")
		abbr = frappe.get_cached_value("Company", cls.company, "abbr")

		vat = _account(cls.company, abbr, f"{TEST_PREFIX} VAT", {"root_type": "Liability"}, "Tax", "Liability")
		wht = _account(cls.company, abbr, f"{TEST_PREFIX} WHT", {"root_type": "Liability"}, "Tax", "Liability")

		cls.vat, cls.wht = vat, wht
		cls.inclusive = _template(cls.company, f"{TEST_PREFIX} incl 15", [
			{"charge_type": "On Net Total", "account_head": vat, "rate": 15, "included_in_print_rate": 1}])
		cls.exclusive = _template(cls.company, f"{TEST_PREFIX} excl 15", [
			{"charge_type": "On Net Total", "account_head": vat, "rate": 15}])
		cls.actual = _template(cls.company, f"{TEST_PREFIX} actual 50", [
			{"charge_type": "Actual", "account_head": vat, "tax_amount": 50}])
		cls.deduct = _template(cls.company, f"{TEST_PREFIX} deduct 5", [
			{"charge_type": "On Net Total", "account_head": wht, "rate": 5, "add_deduct_tax": "Deduct"}])
		cls.multi = _template(cls.company, f"{TEST_PREFIX} vat and wht", [
			{"charge_type": "On Net Total", "account_head": vat, "rate": 15},
			{"charge_type": "On Net Total", "account_head": wht, "rate": 5, "add_deduct_tax": "Deduct"}])
		cls.mixed = _template(cls.company, f"{TEST_PREFIX} incl 15 excl 5", [
			{"charge_type": "On Net Total", "account_head": vat, "rate": 15, "included_in_print_rate": 1},
			{"charge_type": "On Net Total", "account_head": wht, "rate": 5}])
		cls.cascading = _template(cls.company, f"{TEST_PREFIX} 10 then 5 on prev total", [
			{"charge_type": "On Net Total", "account_head": vat, "rate": 10},
			{"charge_type": "On Previous Row Total", "account_head": wht, "rate": 5, "row_id": 1}])
		cls.inclusive_cascading = _template(cls.company, f"{TEST_PREFIX} incl 10 then incl 5", [
			{"charge_type": "On Net Total", "account_head": vat, "rate": 10,
			 "included_in_print_rate": 1},
			{"charge_type": "On Previous Row Total", "account_head": wht, "rate": 5, "row_id": 1,
			 "included_in_print_rate": 1}])

	def assertBreakup(self, template, amount, net, tax, grand):
		result = get_tax_breakup(template, amount, 2)
		self.assertAlmostEqual(result["net_amount"], net, places=2)
		self.assertAlmostEqual(result["total"], tax, places=2)
		self.assertAlmostEqual(result["grand_total"], grand, places=2)
		return result

	def test_no_template_leaves_the_amount_alone(self):
		self.assertBreakup(None, 1000, 1000, 0, 1000)

	def test_exclusive_tax_is_added_on_top(self):
		self.assertBreakup(self.exclusive, 1000, 1000, 150, 1150)

	def test_inclusive_tax_is_backed_out_of_the_amount(self):
		# The entered figure already contains the tax: 1,150 -> 1,000 net + 150 tax.
		self.assertBreakup(self.inclusive, 1150, 1000, 150, 1150)

	def test_inclusive_tax_grand_total_equals_what_was_typed(self):
		# 1,000 is the case that used to come back as 1,000.01: dividing by 1.15 rounds up
		# to 869.57 and taxing that rounds up again to 130.44. Amounts that divide cleanly
		# (1150, 2300) never showed it, which is why it survived so long — so sweep a range
		# wide enough that the awkward ones cannot be avoided.
		amounts = [1000, 1150, 2300, 575, 99.99, 333.33, 1234.56, 7, 0.05]
		amounts += [round(100 + n * 13.37, 2) for n in range(40)]

		for amount in amounts:
			result = get_tax_breakup(self.inclusive, amount, 2)
			self.assertAlmostEqual(
				result["grand_total"], amount, places=2,
				msg=f"a fully inclusive template changed the entered {amount}",
			)

	def test_inclusive_net_plus_tax_is_the_entered_amount(self):
		for amount in (1000, 333.33, 1234.56, 87.65):
			result = get_tax_breakup(self.inclusive, amount, 2)
			self.assertAlmostEqual(
				result["net_amount"] + result["total"], amount, places=2,
				msg=f"net + tax drifted away from the entered {amount}",
			)

	def test_mixed_template_only_removes_the_inclusive_part(self):
		# 15% inclusive + 5% exclusive: the inclusive half comes out of the entered figure,
		# the exclusive half is added on top, so the grand total is above what was typed.
		for amount in (1150, 1000, 777.77):
			result = get_tax_breakup(self.mixed, amount, 2)
			exclusive = result["total"] - (amount - result["net_amount"])
			self.assertAlmostEqual(
				result["grand_total"], amount + exclusive, places=2,
				msg=f"the exclusive part was not added cleanly on {amount}",
			)

	def test_actual_charge_type_is_not_treated_as_a_zero_rate(self):
		self.assertBreakup(self.actual, 1000, 1000, 50, 1050)

	def test_deduct_produces_a_negative_tax(self):
		self.assertBreakup(self.deduct, 1000, 1000, -50, 950)

	def test_each_account_head_keeps_its_own_figure(self):
		result = self.assertBreakup(self.multi, 1000, 1000, 100, 1100)
		self.assertEqual(len(result["rows"]), 2)
		self.assertEqual(result["rows"][0]["account_head"], self.vat)
		self.assertAlmostEqual(result["rows"][0]["amount"], 150, places=2)
		self.assertEqual(result["rows"][1]["account_head"], self.wht)
		self.assertAlmostEqual(result["rows"][1]["amount"], -50, places=2)

	def test_inclusive_and_exclusive_rows_in_one_template(self):
		# 1,150 contains the 15%; the 5% is then added to the resulting net.
		self.assertBreakup(self.mixed, 1150, 1000, 200, 1200)

	def test_on_previous_row_total_compounds(self):
		# 1,000 + 10% = 1,100, then 5% of 1,100 = 55.
		self.assertBreakup(self.cascading, 1000, 1000, 155, 1155)

	def test_inclusive_cascade_is_backed_out_at_the_compounded_rate(self):
		# 1,155 contains both layers: net 1,000 + 100 + 55.
		self.assertBreakup(self.inclusive_cascading, 1155, 1000, 155, 1155)

	def test_row_amounts_always_sum_to_the_total(self):
		for template in (self.exclusive, self.inclusive, self.multi, self.mixed, self.cascading,
						 self.inclusive_cascading, self.actual):
			result = get_tax_breakup(template, 1234.56, 2)
			self.assertAlmostEqual(
				sum(r["amount"] for r in result["rows"]), result["total"], places=2,
				msg=f"per-account amounts do not add up to the total for {template}",
			)

	def test_zero_amount_short_circuits(self):
		self.assertBreakup(self.inclusive, 0, 0, 0, 0)
