"""
Diagnostic script for accounting dimension sync issues on production.
Run: bench --site <site_name> execute voucher_management.diagnose.run
"""
import frappe


def run():
    print("\n" + "=" * 60)
    print("DIAGNOSTIC: Accounting Dimension Sync")
    print("=" * 60)

    # 1. Did the patch run? Check allow_on_submit on child table dimension fields
    print("\n[1] allow_on_submit on child table dimension custom fields:")
    dims = frappe.get_all("Accounting Dimension", filters={"disabled": 0},
                          fields=["fieldname", "label"])
    if not dims:
        print("  ⚠️  No active accounting dimensions found")
    for d in dims:
        val = frappe.db.get_value(
            "Custom Field",
            {"dt": "Voucher Entry Account", "fieldname": d.fieldname},
            "allow_on_submit"
        )
        ok = val == 1
        print(f"  {'✅' if ok else '❌ PATCH NOT RUN'}  {d.label} ({d.fieldname}) — allow_on_submit={val}")

    # 2. Is on_update_after_submit registered in hooks?
    print("\n[2] on_update_after_submit registered in hooks:")
    ve_hooks = (frappe.get_hooks("doc_events") or {}).get("Vouchers Entry", {})
    if "on_update_after_submit" in ve_hooks:
        print(f"  ✅  Hook found: {ve_hooks['on_update_after_submit']}")
    else:
        print("  ❌  Hook NOT found — needs bench restart or migrate")

    # 3. Do recent GL entries have voucher_detail_no stamped?
    print("\n[3] voucher_detail_no on recent GL entries:")
    recent = frappe.db.sql("""
        SELECT voucher_no,
               COUNT(*) as total,
               SUM(CASE WHEN IFNULL(voucher_detail_no,'') != '' THEN 1 ELSE 0 END) as with_detail
        FROM `tabGL Entry`
        WHERE voucher_type = 'Vouchers Entry' AND is_cancelled = 0
        GROUP BY voucher_no
        ORDER BY MAX(creation) DESC
        LIMIT 5
    """, as_dict=True)

    if not recent:
        print("  ⚠️  No GL entries for Vouchers Entry found")
    for r in recent:
        ok = r.with_detail > 0
        note = "" if ok else "  ← submitted BEFORE fix, re-submit to stamp voucher_detail_no"
        print(f"  {'✅' if ok else '❌'}  {r.voucher_no}: {r.with_detail}/{r.total} lines have voucher_detail_no{note}")

    # 4. Simulate: can on_update_after_submit find a row's GL lines?
    print("\n[4] Simulation — matching GL lines by voucher_detail_no:")
    voucher = frappe.db.get_value("Vouchers Entry", {"docstatus": 1}, "name",
                                  order_by="modified desc")
    if voucher:
        doc = frappe.get_doc("Vouchers Entry", voucher)
        for row in (doc.references or [])[:2]:
            count = frappe.db.count("GL Entry", {
                "voucher_type": "Vouchers Entry",
                "voucher_no": voucher,
                "voucher_detail_no": row.name,
                "is_cancelled": 0,
            })
            ok = count > 0
            print(f"  {'✅' if ok else '❌'}  {voucher} / row {row.name} → {count} GL line(s) matched")
            if not ok:
                print("      → Cancel and re-submit this voucher to stamp voucher_detail_no")
    else:
        print("  ⚠️  No submitted Vouchers Entry found")

    # Summary
    print("\n" + "-" * 60)
    print("Fix actions:")
    print("  ❌ [1] → bench --site <site> migrate")
    print("  ❌ [2] → bench restart")
    print("  ❌ [3/4] → cancel & re-submit old vouchers (or run a one-time backfill patch)")
    print("=" * 60)
