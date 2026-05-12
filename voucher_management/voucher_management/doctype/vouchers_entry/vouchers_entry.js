// Client Script for DocType: Vouchers Entry
// التحديث النهائي: Premium UI + Journal Entry Integration + Smart Auto-Refresh

frappe.ui.form.on('Vouchers Entry', {
    onload: function(frm) {
        // 1. حقن الـ CSS الاحترافي للواجهة والأزرار والتنسيقات المالية
        if (!document.getElementById('vouchers-entry-premium-style')) {
            let style = document.createElement('style');
            style.id = 'vouchers-entry-premium-style';
            style.innerHTML = `
                .btn-gl-ledger {
                    background-color: #2c3e50 !important;
                    color: #ecf0f1 !important;
                    font-weight: bold !important;
                    border: 1px solid #34495e !important;
                    box-shadow: 0 2px 5px rgba(0,0,0,0.2) !important;
                }
                .btn-gl-ledger:hover {
                    background-color: #34495e !important;
                    color: #ffffff !important;
                    transform: translateY(-1px);
                }
                .btn-view-je {
                    background-color: #f39c12 !important;
                    color: white !important;
                    font-weight: bold !important;
                }
                .frappe-control[data-fieldname="amount_after_tax"] .control-value {
                    background-color: #e8f5e9 !important;
                    color: #2e7d32 !important;
                    font-weight: bold !important;
                    border: 1px solid #a5d6a7 !important;
                }
                .frappe-control[data-fieldname="total_taxes"] .control-value {
                    color: #d32f2f !important;
                    font-weight: bold !important;
                }
            `;
            document.head.appendChild(style);
        }
        apply_vouchers_filters(frm);
    },

    refresh: function(frm) {
        // 2. تنسيق زر Get Outstanding Invoices (Gradient Style)
        if (frm.get_field('get_outstanding_invoices')) {
            let btn = frm.get_field('get_outstanding_invoices').$wrapper.find('button');
            btn.removeClass('btn-default').addClass('btn-primary').css({
                'background': 'linear-gradient(135deg, #6e8efb, #a777e3)',
                'border': 'none',
                'border-radius': '10px',
                'padding': '8px 20px',
                'box-shadow': '0 4px 6px rgba(0,0,0,0.1)',
                'color': '#fff',
                'font-weight': 'bold',
                'transition': '0.3s'
            });
            btn.hover(
                function() { $(this).css({'transform': 'translateY(-2px)', 'box-shadow': '0 6px 12px rgba(0,0,0,0.15)'}); },
                function() { $(this).css({'transform': 'translateY(0)', 'box-shadow': '0 4px 6px rgba(0,0,0,0.1)'}); }
            );
        }

        // 3. إدارة أزرار العرض (تظهر فقط إذا تم الترحيل ويوجد قيد مرتبط)
        if (frm.doc.docstatus === 1 && frm.doc.journal_entry) {
            
            // زر فتح مستند قيد اليومية
            frm.add_custom_button(__('View Journal Entry'), function() {
                frappe.set_route("Form", "Journal Entry", frm.doc.journal_entry);
            }, __("View")).addClass('btn-view-je');

            // زر عرض الأستاذ العام بناءً على رقم القيد
            frm.add_custom_button(__('General Ledger'), function() {
                frappe.set_route("query-report", "General Ledger", {
                    "voucher_no": frm.doc.journal_entry,
                    "company": frm.doc.company,
                    "from_date": frm.doc.posting_date,
                    "to_date": frm.doc.posting_date
                });
            }, __("View")).addClass('btn-gl-ledger');
        }

        apply_vouchers_filters(frm);
    },

    // --- التحديث الذكي للإلغاء ---
    after_cancel: function(frm) {
        // بمجرد الإلغاء بنجاح، نقوم بإعادة تحميل البيانات لتصفير حقل Journal Entry في الواجهة
        frappe.msgprint({
            title: __('System Update'),
            indicator: 'blue',
            message: __('Journal Entry has been removed and the voucher is reset.')
        });
        frm.reload_doc();
    },

    // 4. منطق جلب الفواتير المستحقة (Fetch & Allocate)
    get_outstanding_invoices: function(frm) {
        let customer_funds = {};
        let supplier_funds = {};

        (frm.doc.references || []).forEach(row => {
            if (row.party && row.party_type === 'Customer') {
                customer_funds[row.party] = (customer_funds[row.party] || 0) + (row.amount_before_tax || 0);
            } else if (row.party && row.party_type === 'Supplier') {
                supplier_funds[row.party] = (supplier_funds[row.party] || 0) + (row.amount_before_tax || 0);
            }
        });

        if (Object.keys(customer_funds).length === 0 && Object.keys(supplier_funds).length === 0) {
            frappe.msgprint({
                title: __('Notice'),
                indicator: 'orange',
                message: __('Please add Parties in the references table with amounts first.')
            });
            return;
        }

        let d = new frappe.ui.Dialog({
            title: '<span style="color: #6e8efb; font-weight: bold;">🔍 Fetch Outstanding Invoices</span>',
            fields: [
                { label: 'From Date', fieldname: 'from_date', fieldtype: 'Date', reqd: 1, default: frappe.datetime.add_months(frappe.datetime.get_today(), -1), columns: 6 },
                { label: 'To Date', fieldname: 'to_date', fieldtype: 'Date', reqd: 1, default: frappe.datetime.get_today(), columns: 6 },
                { fieldtype: 'Section Break' },
                { label: 'Auto Allocate Amount', fieldname: 'allocate_payment_amount', fieldtype: 'Check', default: 1 }
            ],
            primary_action_label: 'Fetch Invoices',
            primary_action(values) {
                d.hide();
                fetch_and_allocate(frm, values, customer_funds, supplier_funds);
            }
        });
        d.show();
    },

    mode_of_payment: function(frm) {
        if (!frm.doc.mode_of_payment) return;
        frappe.db.get_doc("Mode of Payment", frm.doc.mode_of_payment).then(mop => {
            let account_found = (mop.accounts || []).find(r => r.company === frm.doc.company);
            if (account_found && !frm.doc.account_payment) {
                frm.set_value("account_payment", account_found.account);
            }
        });
    }
});

// Child table events for the accounts grid
frappe.ui.form.on('Voucher Entry Account', {
    amount:           (frm, cdt, cdn) => compute_tax_for_row(frm, cdt, cdn),
    taxes:            (frm, cdt, cdn) => compute_tax_for_row(frm, cdt, cdn),
    party:            (frm, cdt, cdn) => set_party_account_safely(frm, cdt, cdn),
    references_add:   (frm, cdt, cdn) => {
        // Pre-fill party_type based on the payment direction
        let p_type = frm.doc.payment_type === 'Receive' ? 'Customer'
                   : frm.doc.payment_type === 'Pay'     ? 'Supplier' : '';
        frappe.model.set_value(cdt, cdn, 'party_type', p_type);
    },
    references_remove: (frm) => recalc_totals(frm),  // recalculate when a row is deleted
});

// --- وظائف مساعدة معالجة البيانات ---

async function fetch_and_allocate(frm, values, customer_funds, supplier_funds) {
    let existing_sales = (frm.doc.vouchers_payment_references || []).map(d => d.reference_name);
    let existing_purchase = (frm.doc.vouchers_payment_references2 || []).map(d => d.reference_name);

    if (Object.keys(customer_funds).length > 0) {
        let sales_invoices = await frappe.db.get_list('Sales Invoice', {
            filters: [['customer', 'in', Object.keys(customer_funds)], ['docstatus', '=', 1], ['outstanding_amount', '>', 0], ['name', 'not in', existing_sales]],
            fields: ['name', 'customer', 'outstanding_amount', 'grand_total', 'due_date']
        });
        sales_invoices.forEach(inv => {
            let available = customer_funds[inv.customer] || 0;
            let to_allocate = values.allocate_payment_amount ? Math.min(available, inv.outstanding_amount) : 0;
            customer_funds[inv.customer] -= to_allocate;
            let row = frm.add_child('vouchers_payment_references');
            Object.assign(row, { reference_doctype: "Sales Invoice", reference_name: inv.name, due_date: inv.due_date, total_amount: inv.grand_total, outstanding_amount: inv.outstanding_amount, allocated_amount: to_allocate, customer: inv.customer });
        });
    }

    if (Object.keys(supplier_funds).length > 0) {
        let purchase_invoices = await frappe.db.get_list('Purchase Invoice', {
            filters: [['supplier', 'in', Object.keys(supplier_funds)], ['docstatus', '=', 1], ['outstanding_amount', '>', 0], ['name', 'not in', existing_purchase]],
            fields: ['name', 'supplier', 'outstanding_amount', 'grand_total', 'bill_no']
        });
        purchase_invoices.forEach(inv => {
            let available = supplier_funds[inv.supplier] || 0;
            let to_allocate = values.allocate_payment_amount ? Math.min(available, inv.outstanding_amount) : 0;
            supplier_funds[inv.supplier] -= to_allocate;
            let row = frm.add_child('vouchers_payment_references2');
            Object.assign(row, { reference_name: inv.name, reference_doctype: "Purchase Invoice", total_amount: inv.grand_total, outstanding_amount: inv.outstanding_amount, allocated_amount: to_allocate, suppiler: inv.supplier });
        });
    }
    frm.refresh_field('vouchers_payment_references');
    frm.refresh_field('vouchers_payment_references2');
    frappe.show_alert({ message: __('Invoices Linked Successfully'), indicator: 'green' });
}

function set_party_account_safely(frm, cdt, cdn) {
    let row = locals[cdt][cdn];
    if (!row.party || !row.party_type) return;
    frappe.call({
        method: "erpnext.accounts.party.get_party_account",
        args: { company: frm.doc.company, party: row.party, party_type: row.party_type },
        callback: (r) => { if (r.message) frappe.model.set_value(cdt, cdn, 'account', r.message); }
    });
}

function compute_tax_for_row(frm, cdt, cdn) {
    let row = locals[cdt][cdn];
    let amount = flt(row.amount);

    if (!row.taxes || amount === 0) {
        // Update the row model directly (synchronous) then recalculate
        row.tax_amount        = 0;
        row.amount_before_tax = amount;
        row.amount_after_tax  = amount;
        frm.refresh_field('references');
        recalc_totals(frm);
        return;
    }

    frappe.db.get_doc('Purchase Taxes and Charges Template', row.taxes).then(tmpl => {
        let rate = (tmpl.taxes || []).reduce((sum, t) => sum + flt(t.rate), 0);
        let tax  = flt(amount * rate / 100, 2);
        // Update the row model directly (synchronous) then recalculate
        row.tax_amount        = tax;
        row.amount_before_tax = amount;
        row.amount_after_tax  = flt(amount + tax, 2);
        frm.refresh_field('references');
        recalc_totals(frm);
    });
}

function recalc_totals(frm) {
    let total = 0, tax = 0;
    (frm.doc.references || []).forEach(r => {
        total += flt(r.amount_before_tax);
        tax   += flt(r.tax_amount);
    });
    // Write directly to frm.doc then refresh — avoids async frm.set_value race conditions
    frm.doc.total_allocated_amount = total;
    frm.doc.total_taxes            = tax;
    frm.doc.amount_after_tax       = total + tax;
    frm.refresh_field('total_allocated_amount');
    frm.refresh_field('total_taxes');
    frm.refresh_field('amount_after_tax');
}

function apply_vouchers_filters(frm) {
    frm.set_query('account_payment', () => {
        return { filters: { 'company': frm.doc.company, 'is_group': 0 } };
    });
    frm.set_query('party_type', 'references', () => {
        return { filters: [['name', 'in', ['Supplier', 'Customer', 'Employee', 'Shareholder']]] };
    });
}