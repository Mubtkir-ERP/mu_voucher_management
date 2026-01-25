import frappe
from frappe.utils import flt
from frappe import _
from erpnext.accounts.general_ledger import make_gl_entries, make_reverse_gl_entries

def on_submit(doc, method=None):
    """إنشاء قيود دفتر الأستاذ عند الترحيل"""
    create_gl_entries(doc)

def on_cancel(doc, method=None):
    """عند الإلغاء: إلغاء قيود دفتر الأستاذ"""
    make_reverse_gl_entries(voucher_type=doc.doctype, voucher_no=doc.name)
    frappe.msgprint(_("تم إلغاء قيود دفتر الأستاذ بنجاح"))

def on_trash(doc, method=None):
    """عند حذف السند نهائياً"""
    # GL Entries are automatically deleted when the voucher is deleted
    pass

def get_gl_dict(doc, args, account_currency=None):
    """إنشاء قاموس قيد دفتر الأستاذ"""
    gl_dict = frappe._dict({
        "posting_date": doc.posting_date,
        "company": doc.company,
        "voucher_type": doc.doctype,
        "voucher_no": doc.name,
        "remarks": doc.remarks or args.get("remarks", ""),
        "is_opening": "No",
        "is_cancelled": 0,
    })
    
    gl_dict.update(args)
    
    if not gl_dict.get("account_currency"):
        gl_dict["account_currency"] = frappe.get_cached_value("Account", gl_dict["account"], "account_currency")
    
    # Set debit/credit in company currency if not set
    if gl_dict.get("debit_in_account_currency") and not gl_dict.get("debit"):
        gl_dict["debit"] = gl_dict["debit_in_account_currency"]
    if gl_dict.get("credit_in_account_currency") and not gl_dict.get("credit"):
        gl_dict["credit"] = gl_dict["credit_in_account_currency"]
    
    return gl_dict

def create_gl_entries(doc):
    """إنشاء قيود دفتر الأستاذ"""
    # Check if GL entries already exist for this voucher
    existing_entries = frappe.db.exists("GL Entry", {
        "voucher_type": doc.doctype,
        "voucher_no": doc.name,
        "is_cancelled": 0
    })
    if existing_entries:
        return

    gl_entries = []
    tax_account = None
    
    # --- منطق الاستلام (Receive) ---
    remarks = doc.references[0].user_remark if doc.references else None
    if doc.payment_type == "Receive":
        # Debit the payment account
        gl_entries.append(get_gl_dict(doc, {
            "account": doc.account_payment,
            "debit": flt(doc.amount_after_tax),
            "debit_in_account_currency": flt(doc.amount_after_tax),
            "credit": 0,
            "credit_in_account_currency": 0,
            "cost_center": doc.cost_center,
            "remarks": remarks,
        }))
        
        for row in doc.references:
            total_allocated = 0
            if doc.get("vouchers_payment_references"):
                for alloc in doc.get("vouchers_payment_references"):
                    if alloc.customer == row.party:
                        gl_entries.append(get_gl_dict(doc, {
                            "account": row.account,
                            "party_type": row.party_type,
                            "party": row.party,
                            "credit": flt(alloc.allocated_amount),
                            "credit_in_account_currency": flt(alloc.allocated_amount),
                            "debit": 0,
                            "debit_in_account_currency": 0,
                            "project": row.project,
                            "against_voucher_type": alloc.reference_doctype,
                            "against_voucher": alloc.reference_name,
                            "cost_center": row.cost_center,
                            "remarks": row.user_remark,
                        }))
                        total_allocated += flt(alloc.allocated_amount)

            remaining = flt(row.amount_before_tax) - total_allocated
            remarks = row.user_remark
            if remaining > 0:
                gl_entries.append(get_gl_dict(doc, {
                    "account": row.account,
                    "party_type": row.party_type,
                    "party": row.party,
                    "project": row.project,
                    "credit": remaining,
                    "credit_in_account_currency": remaining,
                    "debit": 0,
                    "debit_in_account_currency": 0,
                    "cost_center": row.cost_center,
                    "remarks": row.user_remark,
                }))
            
            if not tax_account and row.taxes:
                tax_account = frappe.db.get_value("Purchase Taxes and Charges", {"parent": row.taxes}, "account_head")

        if tax_account and flt(doc.total_taxes) > 0:
            gl_entries.append(get_gl_dict(doc, {
                "account": tax_account,
                "credit": flt(doc.total_taxes),
                "credit_in_account_currency": flt(doc.total_taxes),
                "debit": 0,
                "debit_in_account_currency": 0,
                "cost_center": doc.cost_center,
                "remarks": remarks,
            }))

        
    # --- منطق الدفع (Pay) ---
    elif doc.payment_type == "Pay":
        for row in doc.references:
            total_allocated = 0
            if doc.get("vouchers_payment_references2"):
                for alloc in doc.get("vouchers_payment_references2"):
                    if alloc.suppiler == row.party:
                        gl_entries.append(get_gl_dict(doc, {
                            "account": row.account,
                            "party_type": row.party_type,
                            "party": row.party,
                            "project": row.project,
                            "debit": flt(alloc.allocated_amount),
                            "debit_in_account_currency": flt(alloc.allocated_amount),
                            "credit": 0,
                            "credit_in_account_currency": 0,
                            "against_voucher_type": alloc.reference_doctype,
                            "against_voucher": alloc.reference_name,
                            "cost_center": row.cost_center,
                            "remarks": row.user_remark,
                        }))
                        total_allocated += flt(alloc.allocated_amount)

            remaining = flt(row.amount_before_tax) - total_allocated
            remarks = row.user_remark
            if remaining > 0:
                gl_entries.append(get_gl_dict(doc, {
                    "account": row.account,
                    "party_type": row.party_type,
                    "party": row.party,
                    "project": row.project,
                    "debit": remaining,
                    "debit_in_account_currency": remaining,
                    "credit": 0,
                    "credit_in_account_currency": 0,
                    "cost_center": row.cost_center,
                    "remarks": row.user_remark,
                }))

            if not tax_account and row.taxes:
                tax_account = frappe.db.get_value("Purchase Taxes and Charges", {"parent": row.taxes}, "account_head")

        if tax_account and flt(doc.total_taxes) > 0:
            gl_entries.append(get_gl_dict(doc, {
                "account": tax_account,
                "debit": flt(doc.total_taxes),
                "debit_in_account_currency": flt(doc.total_taxes),
                "credit": 0,
                "credit_in_account_currency": 0,
                "cost_center": doc.cost_center,
                "remarks": remarks,
            }))

        # Credit the payment account
        gl_entries.append(get_gl_dict(doc, {
            "account": doc.account_payment,
            "credit": flt(doc.amount_after_tax),
            "credit_in_account_currency": flt(doc.amount_after_tax),
            "debit": 0,
            "debit_in_account_currency": 0,
            "cost_center": doc.cost_center,
            "remarks": remarks,
        }))

    if gl_entries:
        make_gl_entries(gl_entries)
        frappe.msgprint(_("تم إنشاء قيود دفتر الأستاذ بنجاح"))