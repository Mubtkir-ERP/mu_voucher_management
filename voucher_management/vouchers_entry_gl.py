import frappe
from frappe.utils import flt
from frappe import _

def on_submit(doc, method=None):
    """إنشاء قيد يومية عند الترحيل"""
    make_journal_entry(doc)

def on_cancel(doc, method=None):
    """عند الإلغاء: كسر الرابط في الذاكرة والقاعدة ثم حذف القيد"""
    if doc.journal_entry:
        je_name = doc.journal_entry
        if frappe.db.exists("Journal Entry", je_name):
            # 1. تفريغ الحقل في قاعدة البيانات فوراً (قسرياً)
            frappe.db.set_value("Vouchers Entry", doc.name, "journal_entry", None)
            
            # 2. تفريغ الحقل في كائن المستند الحالي (لتجنب خطأ Validation بعد الحذف)
            doc.journal_entry = None
            
            # 3. جلب مستند القيد والتعامل معه
            je = frappe.get_doc("Journal Entry", je_name)
            
            try:
                if je.docstatus == 1:
                    je.cancel()
                
                # 4. حذف القيد نهائياً من النظام
                frappe.delete_doc("Journal Entry", je_name)
                frappe.msgprint(_("تم حذف قيد اليومية {0} وتصفير الحقل بنجاح").format(je_name))
            except Exception as e:
                # في حال فشل الحذف لأي سبب، نضمن أن الحقل قد تم تصفيره على الأقل
                frappe.msgprint(_("تم فك الارتباط ولكن فشل حذف القيد: {0}").format(str(e)))

def on_trash(doc, method=None):
    """عند حذف السند نهائياً"""
    if doc.journal_entry:
        if frappe.db.exists("Journal Entry", doc.journal_entry):
            je_name = doc.journal_entry
            # كسر الرابط قبل الحذف
            frappe.db.set_value("Vouchers Entry", doc.name, "journal_entry", None)
            frappe.delete_doc("Journal Entry", je_name)

def make_journal_entry(doc):
    # تجنب التكرار
    if doc.journal_entry and frappe.db.exists("Journal Entry", doc.journal_entry):
        return

    je = frappe.new_doc("Journal Entry")
    je.posting_date = doc.posting_date
    je.company = doc.company
    je.cheque_date = doc.posting_date
    je.cheque_no = doc.name
    je.user_remark = doc.remarks
    je.voucher_type = "Journal Entry"

    tax_account = None
    
    # --- منطق الاستلام (Receive) ---
    remarks = doc.references[0].user_remark if doc.references else None
    if doc.payment_type == "Receive":
        je.append("accounts", {
            "account": doc.account_payment,
            "debit_in_account_currency": flt(doc.amount_after_tax),
            "credit_in_account_currency": 0,
            "cost_center": doc.cost_center,
            "user_remark": remarks
        })
        for row in doc.references:
            total_allocated = 0
            if doc.get("vouchers_payment_references"):
                for alloc in doc.get("vouchers_payment_references"):
                    if alloc.customer == row.party:
                        je.append("accounts", {
                            "account": row.account, "party_type": row.party_type, "party": row.party,
                            "credit_in_account_currency": flt(alloc.allocated_amount),
                            "project": row.project,
                            "reference_type": alloc.reference_doctype, "reference_name": alloc.reference_name,
                            "cost_center": row.cost_center,
                            "user_remark": row.user_remark
                        })
                        total_allocated += flt(alloc.allocated_amount)

            remaining = flt(row.amount_before_tax) - total_allocated
            remarks = row.user_remark
            if remaining > 0:
                je.append("accounts", {
                    "account": row.account, "party_type": row.party_type, "party": row.party, "project": row.project,
                    "credit_in_account_currency": remaining, "cost_center": row.cost_center,
                    "user_remark": row.user_remark
                })
            
            if not tax_account and row.taxes:
                tax_account = frappe.db.get_value("Purchase Taxes and Charges", {"parent": row.taxes}, "account_head")

        if tax_account and flt(doc.total_taxes) > 0:
            je.append("accounts", {"account": tax_account, "credit_in_account_currency": flt(doc.total_taxes), "cost_center": doc.cost_center, "user_remark": remarks})

        
    # --- منطق الدفع (Pay) ---
    elif doc.payment_type == "Pay":

        for row in doc.references:
            total_allocated = 0
            if doc.get("vouchers_payment_references2"):
                for alloc in doc.get("vouchers_payment_references2"):
                    if alloc.suppiler == row.party:
                        je.append("accounts", {
                            "account": row.account, "party_type": row.party_type, "party": row.party, "project": row.project,
                            "debit_in_account_currency": flt(alloc.allocated_amount),
                            "reference_type": alloc.reference_doctype, "reference_name": alloc.reference_name,
                            "cost_center": row.cost_center,
                            "user_remark": row.user_remark
                        })
                        total_allocated += flt(alloc.allocated_amount)

            remaining = flt(row.amount_before_tax) - total_allocated
            remarks = row.user_remark
            if remaining > 0:
                je.append("accounts", {
                    "account": row.account, "party_type": row.party_type, "party": row.party, "project": row.project,
                    "debit_in_account_currency": remaining, "cost_center": row.cost_center,
                    "user_remark": row.user_remark
                })

            if not tax_account and row.taxes:
                tax_account = frappe.db.get_value("Purchase Taxes and Charges", {"parent": row.taxes}, "account_head")

        if tax_account and flt(doc.total_taxes) > 0:
            je.append("accounts", {"account": tax_account, "debit_in_account_currency": flt(doc.total_taxes), "cost_center": doc.cost_center, "user_remark": remarks})

        je.append("accounts", {
            "account": doc.account_payment,
            "credit_in_account_currency": flt(doc.amount_after_tax),
            "debit_in_account_currency": 0,
            "cost_center": doc.cost_center,
            "user_remark": remarks
        })

    if je.accounts:
        je.insert(ignore_permissions=True)
        je.submit()
        # تحديث الحقل بعد الترحيل
        doc.db_set("journal_entry", je.name)