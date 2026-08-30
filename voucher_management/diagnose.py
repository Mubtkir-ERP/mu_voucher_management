"""Health check for Voucher Management on a live site.

    bench --site <site_name> execute voucher_management.diagnose.run

Read-only. Sections 1-3 confirm the deployment is wired up; sections 4-7 list the
already-submitted vouchers that the pre-fix code may have posted incorrectly, so they
can be corrected before the fixed version starts producing new entries.
"""

import frappe
from frappe.utils import flt


def run():
	print("\n" + "=" * 72)
	print("VOUCHER MANAGEMENT — DIAGNOSTICS")
	print("=" * 72)

	problems = []

	problems += _check_patches()
	problems += _check_hooks()
	problems += _check_voucher_detail_no()
	problems += _check_unbalanced()
	problems += _check_inclusive_tax_exposure()
	problems += _check_duplicate_allocation_exposure()
	problems += _check_orphan_ledger()

	print("\n" + "-" * 72)
	if problems:
		print(f"{len(problems)} issue(s) need attention:")
		for p in problems:
			print(f"  - {p}")
	else:
		print("No issues found.")
	print("=" * 72)


def _check_patches():
	print("\n[1] Schema patches")
	problems = []

	if frappe.db.has_column("Vouchers Ref Child 2", "suppiler"):
		print("  FAIL  the misspelled `suppiler` column is still present")
		problems.append("run `bench --site <site> migrate` — the suppiler rename patch has not run")
	else:
		print("  OK    `suppiler` renamed to `supplier`")

	dims = frappe.get_all("Accounting Dimension", filters={"disabled": 0}, fields=["fieldname", "label"])
	if not dims:
		print("  --    no active accounting dimensions configured")

	for d in dims:
		value = frappe.db.get_value(
			"Custom Field", {"dt": "Voucher Entry Account", "fieldname": d.fieldname}, "allow_on_submit"
		)
		if value is None:
			print(f"  --    {d.label} ({d.fieldname}) has no field on Voucher Entry Account")
		elif value == 1:
			print(f"  OK    {d.label} ({d.fieldname}) is editable after submit")
		else:
			print(f"  FAIL  {d.label} ({d.fieldname}) is locked after submit")
			problems.append(f"run migrate — allow_on_submit missing on Voucher Entry Account.{d.fieldname}")

	return problems


def _check_hooks():
	print("\n[2] Document hooks")
	problems = []

	registered = (frappe.get_hooks("doc_events") or {}).get("Vouchers Entry", {})

	for event in ("on_submit", "on_cancel", "on_trash", "on_update_after_submit"):
		if registered.get(event):
			print(f"  OK    {event} -> {registered[event]}")
		else:
			print(f"  FAIL  {event} is not registered")
			problems.append(f"`bench restart` (or migrate) — the {event} hook is not loaded")

	return problems


def _check_voucher_detail_no():
	print("\n[3] voucher_detail_no stamped on party lines")
	problems = []

	rows = frappe.db.sql(
		"""
		SELECT voucher_no,
			   COUNT(*) AS total,
			   SUM(CASE WHEN IFNULL(voucher_detail_no, '') != '' THEN 1 ELSE 0 END) AS with_detail
		FROM `tabGL Entry`
		WHERE voucher_type = 'Vouchers Entry' AND is_cancelled = 0
		GROUP BY voucher_no
		HAVING with_detail = 0
		ORDER BY MAX(creation) DESC
		LIMIT 20
		""",
		as_dict=True,
	)

	if not rows:
		print("  OK    every live voucher has its row lines stamped")
	else:
		for r in rows:
			print(f"  FAIL  {r.voucher_no}: 0/{r.total} lines carry voucher_detail_no")
		problems.append(
			f"{len(rows)} voucher(s) submitted before the fix cannot be re-stamped in place "
			"after submit — cancel and amend them, or accept that post-submit edits will not "
			"reach their ledger rows"
		)

	return problems


def _check_unbalanced():
	print("\n[4] Ledger balance per voucher")
	problems = []

	rows = frappe.db.sql(
		"""
		SELECT voucher_no, ROUND(SUM(debit) - SUM(credit), 4) AS diff
		FROM `tabGL Entry`
		WHERE voucher_type = 'Vouchers Entry' AND is_cancelled = 0
		GROUP BY voucher_no
		HAVING ABS(SUM(debit) - SUM(credit)) > 0.005
		ORDER BY ABS(SUM(debit) - SUM(credit)) DESC
		LIMIT 20
		""",
		as_dict=True,
	)

	if not rows:
		print("  OK    every live voucher balances")
	else:
		for r in rows:
			print(f"  FAIL  {r.voucher_no}: debit - credit = {r.diff}")
		problems.append(f"{len(rows)} unbalanced voucher(s) in the ledger — cancel and amend each")

	return problems


def _check_inclusive_tax_exposure():
	print("\n[5] Submitted vouchers priced with a tax-inclusive template")
	problems = []

	rows = frappe.db.sql(
		"""
		SELECT DISTINCT ve.name, ve.posting_date, ve.total_taxes, ve.amount_after_tax
		FROM `tabVouchers Entry` ve
		JOIN `tabVoucher Entry Account` vea ON vea.parent = ve.name
		JOIN `tabPurchase Taxes and Charges` ptc ON ptc.parent = vea.taxes
		WHERE ve.docstatus = 1 AND ptc.included_in_print_rate = 1
		ORDER BY ve.posting_date DESC
		""",
		as_dict=True,
	)

	if not rows:
		print("  OK    none — no submitted voucher used an inclusive template")
		return problems

	print(f"  WARN  {len(rows)} voucher(s) were posted before the inclusive-tax fix.")
	print("        Their tax was added on top of an amount that already contained it,")
	print("        so bank, party and tax balances are all overstated.")
	for r in rows[:20]:
		print(f"          {r.name}  {r.posting_date}  tax={flt(r.total_taxes):,.2f}  total={flt(r.amount_after_tax):,.2f}")
	if len(rows) > 20:
		print(f"          ... and {len(rows) - 20} more")

	problems.append(
		f"{len(rows)} voucher(s) carry an inclusive-tax miscalculation — cancel and amend "
		"each one so it is repriced by the corrected formula"
	)
	return problems


def _check_duplicate_allocation_exposure():
	print("\n[6] Submitted vouchers where one party sits in more than one row")
	problems = []

	rows = frappe.db.sql(
		"""
		SELECT ve.name, vea.party, COUNT(*) AS row_count
		FROM `tabVouchers Entry` ve
		JOIN `tabVoucher Entry Account` vea ON vea.parent = ve.name
		WHERE ve.docstatus = 1 AND IFNULL(vea.party, '') != ''
		  AND (
			EXISTS (SELECT 1 FROM `tabVouchers Ref Child` c
					WHERE c.parent = ve.name AND c.customer = vea.party)
			OR EXISTS (SELECT 1 FROM `tabVouchers Ref Child 2` c2
					   WHERE c2.parent = ve.name AND c2.supplier = vea.party)
		  )
		GROUP BY ve.name, vea.party
		HAVING row_count > 1
		ORDER BY ve.name
		""",
		as_dict=True,
	)

	if not rows:
		print("  OK    none — no submitted voucher repeats an allocated party across rows")
		return problems

	for r in rows:
		print(f"  WARN  {r.name}: {r.party} appears in {r.row_count} rows with invoice allocations")

	problems.append(
		f"{len(rows)} voucher/party pair(s) hit the duplicated-allocation path — check their "
		"invoice outstanding amounts, then cancel and amend"
	)
	return problems


def _check_orphan_ledger():
	print("\n[7] Ledger rows with no voucher behind them")
	problems = []

	rows = frappe.db.sql(
		"""
		SELECT DISTINCT gle.voucher_no
		FROM `tabGL Entry` gle
		LEFT JOIN `tabVouchers Entry` ve ON ve.name = gle.voucher_no
		WHERE gle.voucher_type = 'Vouchers Entry' AND ve.name IS NULL
		LIMIT 20
		""",
		as_dict=True,
	)

	if not rows:
		print("  OK    none")
	else:
		for r in rows:
			print(f"  FAIL  GL rows exist for deleted voucher {r.voucher_no}")
		problems.append(f"{len(rows)} deleted voucher(s) left ledger rows behind — remove them manually")

	return problems
