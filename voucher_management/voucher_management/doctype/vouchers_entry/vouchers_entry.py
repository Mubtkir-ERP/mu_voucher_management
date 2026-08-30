# Copyright (c) 2025, Amir and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, getdate

# Charge types that make sense on a voucher. "On Item Quantity" needs item rows,
# which a voucher does not have, so it is skipped rather than silently mis-added.
SUPPORTED_CHARGE_TYPES = (
	"Actual",
	"On Net Total",
	"On Previous Row Amount",
	"On Previous Row Total",
)

PARTY_ACCOUNT_TYPES = ("Receivable", "Payable")

# Money in this app is carried to two decimals. The pin lives on the DocType fields
# themselves (`"precision": "2"`), not in System Settings, so every other screen in
# ERPNext keeps whatever precision the site is configured for — `float_precision` stays
# 3 and only vouchers are narrowed.
#
# This constant is the Python half of that pin. Reading it, instead of asking the meta
# for each field's precision at every call site, is what keeps a row and the total it
# feeds from being rounded to two different widths — which is how a voucher ends up one
# hallala above the amount that was typed. `check_precision_is_pinned` in full_check.py
# fails if the JSON and this constant ever drift apart.
CURRENCY_PRECISION = 2

# Exchange rates are not money. A pegged rate needs its digits (3.6725), so the rate
# fields keep precision 9 and are never rounded to CURRENCY_PRECISION.
EXCHANGE_RATE_PRECISION = 9

# How far the two converted sides of a voucher may drift before the gap stops being a
# rate movement and starts being a typo.
#
# Deliberately tight. Gulf currencies are pegged — SAR, AED and QAR have not moved
# against the dollar in decades — so between invoicing and collection the rate simply
# does not change. A visible gap on a pegged pair is an entry error, and letting a wide
# tolerance wave it through to the exchange account is how those errors become permanent.
# Raise this only if the company starts trading in a currency that genuinely floats.
EXCHANGE_DIFFERENCE_LIMIT = 0.01


class VouchersEntry(Document):
	def validate(self):
		self.set_missing_values()
		self.validate_posting_date()
		self.validate_exchange_rates()
		self.calculate_taxes_and_totals()
		self.validate_accounts()
		self.validate_rows()
		self.set_allocation_rates()
		self.validate_allocations()
		self.set_allocation_gain_loss()
		self.set_payment_amount()

	def set_missing_values(self):
		if not self.company:
			self.company = frappe.defaults.get_user_default("Company")

		if not self.company:
			frappe.throw(_("Company is required."))

		# `currency` on the header is the company currency — the yardstick every leg is
		# converted to. Each account carries its own currency; there is no single
		# "document currency" because one voucher may touch several.
		self.currency = self.company_currency

		# A voucher is identified by its own number, not by its direction — two hundred
		# receipts all titled "Receive" tell the reader nothing. `set_new_name` has
		# already run by the time `validate` fires, so `self.name` holds the series
		# number here; re-syncing it on every save keeps an amended voucher showing its
		# amended number.
		self.title = self.name

		self.set_leg_currencies()
		self.set_exchange_rates()

	def set_leg_currencies(self):
		"""Stamp every leg with its account's currency and neutralise domestic rates."""
		if self.payment_type == "Internal Transfer":
			self.paid_from_currency = self.get_account_currency(self.paid_from)
			self.paid_to_currency = self.get_account_currency(self.paid_to)

			if self.paid_from_currency == self.company_currency:
				self.source_exchange_rate = 1
			if self.paid_to_currency == self.company_currency:
				self.target_exchange_rate = 1

			# Same currency on both sides is a move, not a conversion.
			if self.paid_from_currency and self.paid_from_currency == self.paid_to_currency:
				self.target_exchange_rate = self.source_exchange_rate
				if not flt(self.received_amount):
					self.received_amount = flt(self.paid_amount)

			return

		self.account_payment_currency = self.get_account_currency(self.account_payment)
		if self.account_payment_currency == self.company_currency:
			self.payment_exchange_rate = 1

		for row in self.references:
			row.company = self.company
			row.account_currency = self.get_account_currency(row.account)
			if row.account_currency == self.company_currency:
				row.exchange_rate = 1

	def set_exchange_rates(self):
		"""Fill any blank rate from the stored Currency Exchange table.

		The rate is a constant here, not a daily quote: one Currency Exchange record per pair
		is enough for a pegged currency, and retyping it on every voucher only invites typos.
		The value that lands in the field stays editable — a fetched rate is a default, not a
		lock — and a rate the user already typed is never overwritten.

		This runs on the server rather than only in the form so that Data Import and API
		callers get the same default; the browser is not the only way a voucher is created.
		"""
		if self.payment_type == "Internal Transfer":
			if not flt(self.source_exchange_rate):
				self.source_exchange_rate = self.default_rate_for(self.paid_from_currency)
			if not flt(self.target_exchange_rate):
				self.target_exchange_rate = self.default_rate_for(self.paid_to_currency)
			return

		if not flt(self.payment_exchange_rate):
			self.payment_exchange_rate = self.default_rate_for(self.account_payment_currency)

		for row in self.references:
			if not flt(row.exchange_rate):
				row.exchange_rate = self.default_rate_for(row.account_currency)

	def default_rate_for(self, currency):
		if not currency or currency == self.company_currency:
			return 1.0

		return flt(
			get_stored_exchange_rate(currency, self.company_currency, self.posting_date)
		)

	@property
	def company_currency(self):
		if not getattr(self, "_company_currency", None):
			self._company_currency = frappe.get_cached_value("Company", self.company, "default_currency")
		return self._company_currency

	def get_account_currency(self, account):
		"""A blank currency on an Account means the company currency in ERPNext."""
		if not account:
			return None
		return frappe.get_cached_value("Account", account, "account_currency") or self.company_currency

	def has_foreign_leg(self):
		if self.payment_type == "Internal Transfer":
			currencies = {self.paid_from_currency, self.paid_to_currency}
		else:
			currencies = {self.account_payment_currency}
			currencies |= {row.account_currency for row in self.references}

		return any(c and c != self.company_currency for c in currencies)

	def validate_exchange_rates(self):
		"""Every foreign leg needs a hand-entered rate.

		Nothing is fetched. This site keeps no Currency Exchange records and the rates in
		use are negotiated rather than published, so a rate the user did not type is a rate
		nobody can vouch for. A missing or zero rate is refused instead of quietly becoming
		1 — which would post a foreign amount as though it were company currency, the exact
		defect this feature exists to remove.
		"""
		if self.payment_type == "Internal Transfer":
			legs = [
				(_("Source Exchange Rate"), self.paid_from_currency, self.source_exchange_rate),
				(_("Target Exchange Rate"), self.paid_to_currency, self.target_exchange_rate),
			]
		else:
			legs = [(_("Payment Exchange Rate"), self.account_payment_currency, self.payment_exchange_rate)]
			legs += [
				(_("Row #{0} Exchange Rate").format(row.idx), row.account_currency, row.exchange_rate)
				for row in self.references
			]

		for label, currency, rate in legs:
			if not currency:
				continue

			if currency == self.company_currency:
				continue

			if flt(rate) <= 0:
				frappe.throw(
					_(
						"{0} is required: {1} is not the company currency {2}. "
						"Enter it here, or add a Currency Exchange record for {1} to {2} so it fills in by itself."
					).format(frappe.bold(label), frappe.bold(currency), frappe.bold(self.company_currency))
				)

	def validate_posting_date(self):
		"""Fail at save time, with a readable message, instead of at submit time.

		`make_gl_entries` runs the same three checks, but only once the user has already
		filled the whole voucher and pressed Submit — and it reports them in terms of the
		ledger, not the voucher. Running them here turns a late, cryptic failure into an
		early, obvious one. The submit-time checks stay in place as the real gate.
		"""
		from erpnext.accounts.doctype.accounting_period.accounting_period import ClosedAccountingPeriod
		from erpnext.accounts.general_ledger import check_freezing_date
		from erpnext.accounts.utils import get_fiscal_year

		if not self.posting_date:
			frappe.throw(_("Date is required."))

		# Throws if the date falls outside every configured Fiscal Year.
		get_fiscal_year(self.posting_date, company=self.company, label=_("Date"))

		# Accounts frozen up to a date (Accounts Settings) — the quarterly-return lock.
		# A site can carry the placeholder "0001-01-01" in that setting, which ERPNext's
		# own check cannot compare (`getdate` maps that sentinel to None and the
		# comparison raises TypeError). Treat an unparseable value as "not frozen" so a
		# stale setting cannot break every save.
		frozen_upto = frappe.db.get_value("Accounts Settings", None, "acc_frozen_upto")
		if not frozen_upto or getdate(frozen_upto):
			check_freezing_date(self.posting_date)

		closed = frappe.db.sql(
			"""
			SELECT ap.name
			FROM `tabAccounting Period` ap, `tabClosed Document` cd
			WHERE ap.name = cd.parent
				AND ap.company = %(company)s
				AND cd.closed = 1
				AND cd.document_type = %(voucher_type)s
				AND %(date)s BETWEEN ap.start_date AND ap.end_date
			""",
			{"company": self.company, "voucher_type": self.doctype, "date": self.posting_date},
		)

		if closed:
			frappe.throw(
				_("You cannot create or cancel any accounting entries within the closed Accounting Period {0}").format(
					frappe.bold(closed[0][0])
				),
				ClosedAccountingPeriod,
			)

	def calculate_taxes_and_totals(self):
		"""Re-derive every amount on the server, in both currencies.

		What the user types on a row is in that row's *account* currency. `base_*` is the
		same figure converted at the row's rate, and the three document totals are base
		only — a voucher may touch three currencies at once, so the company currency is the
		one yardstick they can all share.

		`read_only` is a UI-level flag in Frappe: the fields stay writable over the API, so
		nothing in the submitted payload is trusted and everything is recomputed here.

		Rounding happens once per row, to CURRENCY_PRECISION, before anything is added up.
		The totals are then sums of those already-rounded figures — never a sum of raw
		products rounded at the end. Those two orders disagree: three rows of 0.075 sum to
		0.225 and round to 0.23, while rounding each to 0.08 first gives 0.24. The second
		is the one that matches what the rows show on screen and what the ledger posts, so
		it is the one used.
		"""
		precision = CURRENCY_PRECISION

		base_before_tax = 0.0
		base_taxes = 0.0

		for row in self.references:
			amount = flt(row.amount, precision)
			rate = flt(row.exchange_rate, EXCHANGE_RATE_PRECISION) or 1.0
			breakup = get_tax_breakup(row.taxes, amount, precision)

			row.amount = amount
			row.exchange_rate = rate

			# Row figures in the account's own currency, each settled at two decimals.
			row.amount_before_tax = flt(breakup["net_amount"], precision)
			row.tax_amount = flt(breakup["total"], precision)
			row.amount_after_tax = flt(row.amount_before_tax + row.tax_amount, precision)

			# The same figures converted to company currency. Convert the settled row
			# values, not the raw ones, so the two currencies tell the same story.
			row.base_amount = flt(row.amount * rate, precision)
			row.base_amount_before_tax = flt(row.amount_before_tax * rate, precision)
			row.base_tax_amount = flt(row.tax_amount * rate, precision)
			# Add the two converted figures rather than converting the total separately:
			# three independent roundings do not have to agree, and the GL posts the net
			# and the tax as separate lines whose sum must equal this.
			row.base_amount_after_tax = flt(row.base_amount_before_tax + row.base_tax_amount, precision)

			base_before_tax += row.base_amount_before_tax
			base_taxes += row.base_tax_amount

		# Both accumulators only ever received values that were already rounded, so these
		# calls are settling float representation error (0.1 + 0.2), not rounding money.
		self.total_allocated_amount = flt(base_before_tax, precision)
		self.total_taxes = flt(base_taxes, precision)
		self.amount_after_tax = flt(self.total_allocated_amount + self.total_taxes, precision)

	def set_payment_amount(self):
		"""Settle the bank leg and book whatever is left over as an exchange difference.

		When nothing foreign is involved the bank figure is fully determined by the rows, so
		it is filled in and the difference is necessarily zero — a purely domestic voucher
		behaves exactly as it did before this feature existed.

		Once a currency differs, the bank figure can no longer be derived: the rows may be in
		USD while the account paying them is in YER. The user enters what actually moved
		through the account, and the gap between the two converted sides is the realised gain
		or loss.
		"""
		if self.payment_type == "Internal Transfer":
			self.set_transfer_amounts()
			return

		precision = CURRENCY_PRECISION

		if not self.has_foreign_leg():
			self.payment_exchange_rate = 1
			self.payment_amount = self.amount_after_tax
			self.exchange_difference = 0
			return

		if not flt(self.payment_amount):
			# Best guess, so the user has something to correct rather than a blank field.
			self.payment_amount = flt(
				flt(self.amount_after_tax) / (flt(self.payment_exchange_rate) or 1.0), precision
			)

		base_payment = flt(flt(self.payment_amount) * flt(self.payment_exchange_rate), precision)
		self.exchange_difference = flt(base_payment - flt(self.amount_after_tax), precision)

		self.validate_exchange_difference(base_payment)

	def set_transfer_amounts(self):
		"""A transfer between two currencies is a purchase of one with the other."""
		precision = CURRENCY_PRECISION

		if not flt(self.received_amount):
			self.received_amount = flt(self.paid_amount)

		base_paid = flt(flt(self.paid_amount) * (flt(self.source_exchange_rate) or 1.0), precision)
		base_received = flt(flt(self.received_amount) * (flt(self.target_exchange_rate) or 1.0), precision)

		self.total_allocated_amount = base_paid
		self.total_taxes = 0
		self.amount_after_tax = base_paid
		self.exchange_difference = flt(base_received - base_paid, precision)

		self.validate_exchange_difference(base_paid)

	def validate_exchange_difference(self, base_reference):
		"""Keep the exchange line honest.

		The difference is a plug: it absorbs whatever the two converted sides disagree by.
		That is right for a genuine rate movement and disastrous for a typo, so it is capped.
		Beyond the cap the numbers are not an exchange difference, they are a mistake, and
		the voucher is refused rather than quietly writing the error to the exchange account.
		"""
		difference = flt(self.exchange_difference)
		if not difference:
			return

		if not self.has_foreign_leg():
			frappe.throw(
				_("The two sides of this voucher differ by {0}, but no foreign currency is involved.").format(
					frappe.bold(frappe.format_value(difference, {"fieldtype": "Currency"}))
				)
			)

		limit = abs(flt(base_reference)) * EXCHANGE_DIFFERENCE_LIMIT
		if limit and abs(difference) > limit:
			frappe.throw(
				_(
					"Exchange difference of {0} is more than {1}% of the voucher. Check the amounts and "
					"the exchange rates — a gap this size is an entry error, not a rate movement."
				).format(
					frappe.bold(frappe.format_value(difference, {"fieldtype": "Currency"})),
					cint(EXCHANGE_DIFFERENCE_LIMIT * 100),
				)
			)

		if not frappe.get_cached_value("Company", self.company, "exchange_gain_loss_account"):
			frappe.throw(
				_("Set the Exchange Gain / Loss Account on company {0} before posting a voucher that needs one.").format(
					frappe.bold(self.company)
				)
			)

	def validate_accounts(self):
		"""Every account must be a usable ledger of this company.

		Foreign currencies are supported now, so the currency check is no longer a blanket
		refusal — except for tax accounts. A tax head aggregates the tax of every row, and
		those rows may be in different currencies; the only figure they can all be added in
		is the company currency. A foreign tax account would have no well-defined
		account-currency balance, so it is rejected rather than fudged.
		"""
		if self.payment_type == "Internal Transfer":
			accounts = [self.paid_from, self.paid_to]
			tax_accounts = []
		else:
			accounts = [self.account_payment] + [row.account for row in self.references]
			tax_accounts = [tax["account_head"] for tax in self.get_tax_lines()]

		for account in accounts + tax_accounts:
			if not account:
				continue

			company, is_group = frappe.get_cached_value("Account", account, ["company", "is_group"])

			if company != self.company:
				frappe.throw(
					_("Account {0} does not belong to company {1}.").format(
						frappe.bold(account), frappe.bold(self.company)
					)
				)

			if is_group:
				frappe.throw(
					_("Account {0} is a group account and cannot be used in a transaction.").format(
						frappe.bold(account)
					)
				)

		for account in tax_accounts:
			currency = self.get_account_currency(account)
			if currency != self.company_currency:
				frappe.throw(
					_(
						"Tax account {0} is in {1}. Tax accounts must be in the company currency {2}, because "
						"one tax head collects the tax of rows that may be in different currencies."
					).format(frappe.bold(account), frappe.bold(currency), frappe.bold(self.company_currency))
				)

	def validate_rows(self):
		if self.payment_type == "Internal Transfer":
			if self.references:
				frappe.throw(_("Accounts References must be empty for an Internal Transfer."))

			if not (self.paid_from and self.paid_to):
				frappe.throw(_("Account Paid From and Account Paid To are required for an Internal Transfer."))

			if self.paid_from == self.paid_to:
				frappe.throw(_("Account Paid From and Account Paid To cannot be the same."))

			if flt(self.paid_amount) <= 0:
				frappe.throw(_("Amount must be greater than zero."))

			return

		if not self.account_payment:
			frappe.throw(_("Account is required."))

		if not self.references:
			frappe.throw(_("At least one row is required in Accounts References."))

		for row in self.references:
			if not row.account:
				frappe.throw(_("Row #{0}: Account is required.").format(row.idx))

			if flt(row.amount) <= 0:
				frappe.throw(_("Row #{0}: Amount must be greater than zero.").format(row.idx))

			if flt(row.amount_before_tax) <= 0:
				frappe.throw(
					_("Row #{0}: the tax template consumes the whole amount, leaving nothing for {1}.").format(
						row.idx, frappe.bold(row.account)
					)
				)

			account_type = frappe.get_cached_value("Account", row.account, "account_type")
			if account_type in PARTY_ACCOUNT_TYPES and not (row.party_type and row.party):
				frappe.throw(
					_("Row #{0}: Party Type and Party are required for {1} account {2}.").format(
						row.idx, account_type, frappe.bold(row.account)
					)
				)

	def validate_allocations(self):
		"""Allocations against invoices may never exceed what the party row carries.

		Over-allocation used to be dropped silently by the GL builder, which produced
		an unbalanced voucher. It is rejected here instead, at the source. This check is
		also what lets `distribute_allocations` always find room for every allocation.
		"""
		if self.payment_type == "Internal Transfer":
			return

		allocations, party_field = self.get_allocation_table()
		unused_table = (
			"vouchers_payment_references2"
			if self.payment_type == "Receive"
			else "vouchers_payment_references"
		)

		if self.get(unused_table):
			frappe.throw(
				_("Invoice references for the opposite payment direction must be cleared when Type is {0}.").format(
					frappe.bold(self.payment_type)
				)
			)

		if not allocations:
			return

		self.validate_allocation_currency()

		precision = CURRENCY_PRECISION

		available = {}
		for row in self.references:
			if row.party:
				available[row.party] = flt(available.get(row.party, 0) + flt(row.amount_before_tax), precision)

		allocated = {}
		for alloc in allocations:
			party = alloc.get(party_field)

			if not party:
				frappe.throw(_("Row #{0}: Party is required on the invoice reference.").format(alloc.idx))

			if party not in available:
				frappe.throw(
					_("Row #{0}: {1} is not present in Accounts References, so nothing can be allocated to it.").format(
						alloc.idx, frappe.bold(party)
					)
				)

			if flt(alloc.allocated_amount) < 0:
				frappe.throw(_("Row #{0}: Allocated amount cannot be negative.").format(alloc.idx))

			allocated[party] = flt(allocated.get(party, 0) + flt(alloc.allocated_amount), precision)

		for party, amount in allocated.items():
			if amount > available[party]:
				frappe.throw(
					_("Total allocated {0} for {1} exceeds the {2} available in Accounts References.").format(
						frappe.bold(frappe.format_value(amount, {"fieldtype": "Currency"})),
						frappe.bold(party),
						frappe.bold(frappe.format_value(available[party], {"fieldtype": "Currency"})),
					)
				)

	def set_allocation_rates(self):
		"""Stamp every allocation with the rate its own invoice was booked at.

		Read from the invoice, never from the payload: this rate decides how much of the
		receivable an allocation clears, so a client-supplied value would let a caller
		dissolve a balance at a rate of its choosing.
		"""
		allocations, _party_field = self.get_allocation_table()

		for alloc in allocations:
			if not (alloc.reference_doctype and alloc.reference_name):
				continue

			currency, rate = frappe.db.get_value(
				alloc.reference_doctype, alloc.reference_name, ["currency", "conversion_rate"]
			) or (None, None)

			alloc.exchange_rate = flt(rate) or 1.0
			alloc.invoice_currency = currency

	def validate_allocation_currency(self):
		"""An allocation may only land on a row held in the invoice's own currency.

		The allocated figure is expressed in one currency and subtracted from a row expressed
		in another; without this the two would be added as if they were the same money.
		"""
		allocations, party_field = self.get_allocation_table()
		if not allocations:
			return

		row_currencies = {}
		for row in self.references:
			if row.party:
				row_currencies.setdefault(row.party, set()).add(row.account_currency)

		for alloc in allocations:
			invoice_currency = alloc.get("invoice_currency") or self.company_currency
			currencies = row_currencies.get(alloc.get(party_field)) or set()

			if currencies and invoice_currency not in currencies:
				frappe.throw(
					_(
						"Row #{0}: {1} is in {2}, but {3} is held in {4} in Accounts References. "
						"Use a party account in the invoice currency."
					).format(
						alloc.idx,
						frappe.bold(alloc.reference_name),
						frappe.bold(invoice_currency),
						frappe.bold(alloc.get(party_field)),
						frappe.bold(", ".join(sorted(c for c in currencies if c))),
					)
				)

	def set_allocation_gain_loss(self):
		"""Record what each allocation will realise, before the voucher is submitted.

		The ledger relieves an invoice at the rate it was booked at while the voucher values
		the same money at its own rate; the gap is what this payment actually gained or lost.
		It is computed here rather than while the ledger is being built because by then the
		document has already been written, and a figure the user cannot see before submitting
		is a figure they cannot check.
		"""
		from voucher_management.vouchers_entry_gl import distribute_allocations

		allocations, _party_field = self.get_allocation_table()
		for alloc in allocations:
			alloc.exchange_gain_loss = 0

		if not allocations:
			return

		precision = CURRENCY_PRECISION
		rates = {row.name: flt(row.exchange_rate) or 1.0 for row in self.references}

		for row_name, pairs in distribute_allocations(self).items():
			row_rate = rates.get(row_name, 1.0)

			for alloc, amount in pairs:
				invoice_rate = flt(alloc.exchange_rate) or row_rate
				alloc.exchange_gain_loss = flt(
					flt(alloc.exchange_gain_loss) + amount * (row_rate - invoice_rate), precision
				)

	def get_allocation_table(self):
		"""Return (rows, party_fieldname) for the table matching the payment direction."""
		if self.payment_type == "Receive":
			return self.get("vouchers_payment_references") or [], "customer"

		if self.payment_type == "Pay":
			return self.get("vouchers_payment_references2") or [], "supplier"

		return [], None

	def get_tax_lines(self):
		"""Aggregate the per-row tax breakups into one figure per tax account head.

		Every row is priced with its own template and its own currency, so the amounts are
		converted before they are added. Summing per account head — in the company currency,
		and not onto one arbitrary account — is what keeps the tax ledger usable for a return.
		"""
		if self.payment_type == "Internal Transfer":
			return []

		precision = CURRENCY_PRECISION
		totals = {}
		order = []

		for row in self.references:
			rate = flt(row.exchange_rate) or 1.0
			breakup = get_tax_breakup(row.taxes, flt(row.amount, precision), precision)

			for line in breakup["rows"]:
				head = line["account_head"]
				if head not in totals:
					totals[head] = 0.0
					order.append(head)
				totals[head] += flt(flt(line["amount"]) * rate, precision)

		return [
			{"account_head": head, "amount": flt(totals[head], precision)}
			for head in order
			if flt(totals[head], precision)
		]


@frappe.whitelist()
def get_stored_exchange_rate(from_currency, to_currency, posting_date=None):
    """The latest Currency Exchange record for a pair, on or before a date.

    Deliberately does not fall back to the external rate API that ERPNext's own
    `get_exchange_rate` reaches for. These rates are pegs that somebody sets once; a
    voucher must never depend on a network call succeeding, and silently importing a
    live market quote for a pegged pair would be worse than having no rate at all.

    Returns 0 when nothing is on file, which `validate_exchange_rates` turns into a
    readable error naming the leg.
    """
    if not from_currency or from_currency == to_currency:
        return 1.0

    rate = frappe.db.get_value(
        "Currency Exchange",
        {
            "from_currency": from_currency,
            "to_currency": to_currency,
            "date": ("<=", getdate(posting_date) if posting_date else getdate()),
        },
        "exchange_rate",
        order_by="date desc",
    )

    return flt(rate)


def _inclusive_tax_fraction(tax_rows):
	"""Fraction of the net amount that the tax-inclusive rows of a template represent.

	Mirrors ERPNext's `determine_exclusive_rate` / `get_current_tax_fraction`: a row that
	is not flagged `included_in_print_rate` contributes 0, and a "previous row" reference
	follows the template's own `row_id` — falling back to the preceding row only when the
	referenced one was skipped for having an unsupported charge type.
	"""
	cumulated_fraction = 0.0
	previous_fraction = 0.0
	grand_total_fraction = 1.0

	fraction_by_idx = {}
	grand_fraction_by_idx = {}

	for tax in tax_rows:
		fraction = 0.0

		if cint(tax.included_in_print_rate):
			rate = flt(tax.rate) / 100.0

			if tax.charge_type == "On Net Total":
				fraction = rate
			elif tax.charge_type == "On Previous Row Amount":
				fraction = rate * fraction_by_idx.get(cint(tax.row_id), previous_fraction)
			elif tax.charge_type == "On Previous Row Total":
				fraction = rate * grand_fraction_by_idx.get(cint(tax.row_id), grand_total_fraction)
			# "Actual" carries no fraction — ERPNext forbids marking it tax-inclusive.

			if tax.add_deduct_tax == "Deduct":
				fraction = -fraction

		previous_fraction = fraction
		cumulated_fraction += fraction
		grand_total_fraction += fraction

		fraction_by_idx[tax.idx] = fraction
		grand_fraction_by_idx[tax.idx] = grand_total_fraction

	return cumulated_fraction


@frappe.whitelist()
def get_tax_breakup(template, amount, precision=None):
	"""Split a taxes template into per-account amounts for a given entered amount.

	Shared by the server-side recalculation and the client script so both sides always
	produce the same number. Returns:

	    net_amount   — the base the party ledger is charged with
	    total        — total tax (negative for a "Deduct" template)
	    grand_total  — net_amount + total, the figure that reaches the bank
	    rows         — one entry per tax account head

	For a tax-inclusive template the entered amount *is* the grand total, so the net is
	divided out of it; for an exclusive one the tax is added on top and the entered
	amount *is* the net.
	"""
	frappe.has_permission("Purchase Taxes and Charges Template", throw=True)

	amount = flt(amount)
	# The client passes the precision explicitly; the fallback keeps a direct call — from
	# a script or the console — on the same two decimals as everything else.
	precision = cint(precision) or CURRENCY_PRECISION

	untaxed = {
		"net_amount": flt(amount, precision),
		"total": 0.0,
		"grand_total": flt(amount, precision),
		"rows": [],
	}

	if not template or not amount:
		return untaxed

	tax_rows = frappe.get_all(
		"Purchase Taxes and Charges",
		filters={"parent": template, "parenttype": "Purchase Taxes and Charges Template"},
		fields=[
			"idx",
			"charge_type",
			"rate",
			"tax_amount",
			"account_head",
			"add_deduct_tax",
			"included_in_print_rate",
			"row_id",
		],
		order_by="idx asc",
	)

	tax_rows = [t for t in tax_rows if t.charge_type in SUPPORTED_CHARGE_TYPES and t.account_head]
	if not tax_rows:
		return untaxed

	# Back the tax out of the entered amount before anything is computed on it.
	cumulated_fraction = _inclusive_tax_fraction(tax_rows)
	if cumulated_fraction and flt(1.0 + cumulated_fraction, 9) == 0:
		frappe.throw(_("Tax template {0} cancels out the whole amount.").format(frappe.bold(template)))

	net_amount = flt(amount / (1.0 + cumulated_fraction), precision) if cumulated_fraction else flt(amount, precision)

	rows = []
	total = 0.0
	inclusive_total = 0.0
	running_total = net_amount
	previous_row_amount = 0.0

	amount_by_idx = {}
	total_by_idx = {}

	for tax in tax_rows:
		if tax.charge_type == "Actual":
			tax_amount = flt(tax.tax_amount)
		elif tax.charge_type == "On Net Total":
			tax_amount = net_amount * flt(tax.rate) / 100
		elif tax.charge_type == "On Previous Row Amount":
			base = amount_by_idx.get(cint(tax.row_id), previous_row_amount)
			tax_amount = base * flt(tax.rate) / 100
		else:  # On Previous Row Total
			base = total_by_idx.get(cint(tax.row_id), running_total)
			tax_amount = base * flt(tax.rate) / 100

		tax_amount = flt(tax_amount, precision)

		if tax.add_deduct_tax == "Deduct":
			tax_amount = -tax_amount

		previous_row_amount = tax_amount
		running_total = flt(running_total + tax_amount, precision)
		total += tax_amount

		if cint(tax.included_in_print_rate):
			inclusive_total += tax_amount

		amount_by_idx[tax.idx] = tax_amount
		total_by_idx[tax.idx] = running_total

		rows.append({"account_head": tax.account_head, "amount": tax_amount})

	total = flt(total, precision)
	inclusive_total = flt(inclusive_total, precision)

	# Take the net back out of the entered amount by subtraction rather than keeping the
	# divided figure. Dividing rounds once and taxing that result rounds again, and when
	# both round the same way the two half-units surface as a grand total that differs
	# from what the user typed — 1,000 entered on a 15% inclusive template came back as
	# 1,000.01. The tax figures are the ones a return is filed on, so they stand as
	# computed and the net absorbs the remainder; the identity
	# `net + inclusive tax == entered amount` then holds exactly.
	if inclusive_total:
		net_amount = flt(amount - inclusive_total, precision)

	return {
		"net_amount": net_amount,
		"total": total,
		"grand_total": flt(net_amount + total, precision),
		"rows": rows,
	}


@frappe.whitelist()
def bulk_create_gl_entries(vouchers):
	"""Post GL entries for vouchers submitted before the submit hook existed.

	Each voucher runs inside its own savepoint so a failure cannot leave partial
	ledger rows behind for the ones that succeeded.
	"""
	from voucher_management.vouchers_entry_gl import create_gl_entries

	# Posting to the ledger on someone else's behalf is an accounting operation, not a
	# list-view convenience — the per-document submit check below is not enough on its own.
	frappe.only_for(("Accounts Manager", "System Manager"))

	if isinstance(vouchers, str):
		vouchers = json.loads(vouchers)

	if not isinstance(vouchers, list):
		frappe.throw(_("Invalid list of vouchers."))

	results = {"success": [], "failed": [], "skipped": []}

	for name in vouchers:
		savepoint = "vm_bulk_gl"

		try:
			frappe.db.savepoint(savepoint)

			doc = frappe.get_doc("Vouchers Entry", name)
			doc.check_permission("submit")

			if doc.docstatus != 1:
				results["skipped"].append(name)
				continue

			# Any existing GL row — cancelled or not — means this voucher has already
			# been through the ledger, so re-posting would duplicate it.
			if frappe.db.exists("GL Entry", {"voucher_type": doc.doctype, "voucher_no": doc.name}):
				results["skipped"].append(name)
				continue

			create_gl_entries(doc)
			results["success"].append(name)

		except Exception as e:
			frappe.db.rollback(save_point=savepoint)
			frappe.log_error(title=f"Bulk GL creation failed for {name}", message=frappe.get_traceback())
			results["failed"].append({"name": name, "error": str(e)})

	return results
