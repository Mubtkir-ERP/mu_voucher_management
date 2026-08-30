import frappe


def execute():
	"""Give every pre-multi-currency voucher the rates and base amounts it now needs.

	Before this feature the app refused any account that was not in the company currency,
	so every stored voucher is domestic by construction: the rate is 1 and each base amount
	equals the amount already there. Filling those in is what keeps old vouchers readable —
	and, for drafts, re-savable — instead of showing a rate of 0 and blank base columns.

	Nothing submitted is re-posted: the ledger those vouchers wrote is already correct.
	"""
	frappe.db.sql(
		"""
		UPDATE `tabVoucher Entry Account`
		SET exchange_rate = 1,
			base_amount = amount,
			base_amount_before_tax = amount_before_tax,
			base_tax_amount = tax_amount,
			base_amount_after_tax = amount_after_tax
		WHERE IFNULL(exchange_rate, 0) = 0
		"""
	)

	frappe.db.sql(
		"""
		UPDATE `tabVouchers Entry`
		SET payment_exchange_rate = 1,
			payment_amount = amount_after_tax,
			exchange_difference = 0
		WHERE payment_type IN ('Receive', 'Pay')
			AND IFNULL(payment_exchange_rate, 0) = 0
		"""
	)

	frappe.db.sql(
		"""
		UPDATE `tabVouchers Entry`
		SET source_exchange_rate = 1,
			target_exchange_rate = 1,
			received_amount = paid_amount,
			exchange_difference = 0
		WHERE payment_type = 'Internal Transfer'
			AND IFNULL(source_exchange_rate, 0) = 0
		"""
	)

	# Allocations were only ever made against company-currency invoices.
	for doctype in ("Vouchers Ref Child", "Vouchers Ref Child 2"):
		frappe.db.sql(
			f"""
			UPDATE `tab{doctype}`
			SET exchange_rate = 1, exchange_gain_loss = 0
			WHERE IFNULL(exchange_rate, 0) = 0
			"""
		)
