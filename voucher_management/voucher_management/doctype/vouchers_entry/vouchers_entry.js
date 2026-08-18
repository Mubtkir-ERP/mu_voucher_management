// Client Script for DocType: Vouchers Entry
// الواجهة تعرض فقط — كل الأرقام تُعاد من السيرفر عبر get_tax_breakup

const METHOD_ROOT = 'voucher_management.voucher_management.doctype.vouchers_entry.vouchers_entry';
const TAX_BREAKUP_METHOD = METHOD_ROOT + '.get_tax_breakup';
const EXCHANGE_RATE_METHOD = METHOD_ROOT + '.get_stored_exchange_rate';

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

        // The server fills `company` in set_missing_values, but the account filters run
        // long before the first save. With company empty they filter on `undefined` and
        // match nothing, so picking a Mode of Payment fetched an account the field then
        // rejected — the red border. Seed it here, the same way the server does.
        if (frm.is_new() && !frm.doc.company) {
            let company = voucher_company(frm);
            if (company) frm.set_value('company', company);
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

        // 3. الأستاذ العام — يُفلتر برقم السند نفسه، فالتطبيق يرحّل إلى GL مباشرة
        //    ولا ينشئ قيد يومية وسيط.
        if (frm.doc.docstatus === 1) {
            frm.add_custom_button(__('General Ledger'), function() {
                frappe.set_route("query-report", "General Ledger", {
                    "voucher_no": frm.doc.name,
                    "company": frm.doc.company,
                    "from_date": frm.doc.posting_date,
                    "to_date": frm.doc.posting_date,
                    "group_by": "Group by Voucher (Consolidated)"
                });
            }, __("View")).addClass('btn-gl-ledger');
        }

        apply_vouchers_filters(frm);
        show_exchange_hint(frm);
    },

    after_cancel: function(frm) {
        frappe.msgprint({
            title: __('System Update'),
            indicator: 'blue',
            message: __('GL entries were reversed and the invoices outstanding amounts restored.')
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

    payment_amount:        (frm) => recalc_totals(frm),
    payment_exchange_rate: (frm) => recalc_totals(frm),
    account_payment:       (frm) => set_payment_currency(frm),
    paid_from:             (frm) => set_transfer_currency(frm, 'paid_from'),
    paid_to:               (frm) => set_transfer_currency(frm, 'paid_to'),

    paid_amount:           (frm) => recalc_totals(frm),
    received_amount:       (frm) => recalc_totals(frm),
    source_exchange_rate:  (frm) => recalc_totals(frm),
    target_exchange_rate:  (frm) => recalc_totals(frm),

    mode_of_payment: function(frm) {
        if (!frm.doc.mode_of_payment) return;

        let company = voucher_company(frm);
        if (!company) return;

        frappe.db.get_doc("Mode of Payment", frm.doc.mode_of_payment).then(mop => {
            let mapped = (mop.accounts || []).find(r => r.company === company);

            if (!mapped) {
                // Say so instead of leaving the account blank with no explanation.
                frappe.show_alert({
                    message: __('{0} has no account mapped for {1}.',
                                [frm.doc.mode_of_payment, company]),
                    indicator: 'orange'
                });
                return;
            }

            // The child field is `default_account`; the old code read `account`, which
            // does not exist on Mode of Payment Account and always came back undefined.
            if (!mapped.default_account) return;

            // The doctype used to carry `fetch_from: mode_of_payment.custom_default_account`
            // on these two fields. That field does not exist on Mode of Payment, so the
            // fetch produced nothing usable and the account came back rejected. Filling
            // it here, from the company's own mapping, is the whole of that behaviour.
            let target = frm.doc.payment_type === 'Internal Transfer'
                       ? 'paid_from' : 'account_payment';

            if (!frm.doc[target]) frm.set_value(target, mapped.default_account);
        });
    }
});

// Child table events for the accounts grid
frappe.ui.form.on('Voucher Entry Account', {
    amount:           (frm, cdt, cdn) => compute_tax_for_row(frm, cdt, cdn),
    taxes:            (frm, cdt, cdn) => compute_tax_for_row(frm, cdt, cdn),
    exchange_rate:    (frm)           => recalc_totals(frm),
    account:          (frm, cdt, cdn) => set_row_currency(frm, cdt, cdn),
    party:            (frm, cdt, cdn) => set_party_account_safely(frm, cdt, cdn),
    references_add:   (frm, cdt, cdn) => {
        // Pre-fill party_type based on the payment direction, then seed the party from
        // the header default. These header fields are a typing shortcut only — they are
        // deliberately NOT accounting dimensions, because a dimension set on the header
        // is stamped on every GL line including the bank and tax lines, which would tag
        // a multi-party voucher with a single party.
        let is_receive = frm.doc.payment_type === 'Receive';
        let is_pay     = frm.doc.payment_type === 'Pay';

        let p_type = is_receive ? 'Customer' : is_pay ? 'Supplier' : '';
        frappe.model.set_value(cdt, cdn, 'party_type', p_type);

        let party = is_receive ? frm.doc.default_customer
                  : is_pay     ? frm.doc.default_supplier : null;

        // set_value on `party` fires the row's own handler, which fetches the account.
        if (party) frappe.model.set_value(cdt, cdn, 'party', party);
    },
    references_remove: (frm) => recalc_totals(frm),  // recalculate when a row is deleted
});

// --- وظائف مساعدة معالجة البيانات ---

async function fetch_and_allocate(frm, values, customer_funds, supplier_funds) {
    let existing_sales = (frm.doc.vouchers_payment_references || []).map(d => d.reference_name);
    let existing_purchase = (frm.doc.vouchers_payment_references2 || []).map(d => d.reference_name);

    // limit_page_length: 0 — otherwise only the first 20 invoices are ever offered.
    if (Object.keys(customer_funds).length > 0) {
        let sales_invoices = await frappe.db.get_list('Sales Invoice', {
            filters: [
                ['customer', 'in', Object.keys(customer_funds)],
                ['docstatus', '=', 1],
                ['outstanding_amount', '>', 0],
                ['company', '=', frm.doc.company],
                ['posting_date', 'between', [values.from_date, values.to_date]],
                ['name', 'not in', existing_sales]
            ],
            fields: ['name', 'customer', 'outstanding_amount', 'grand_total', 'due_date'],
            order_by: 'due_date asc',
            limit_page_length: 0
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
            filters: [
                ['supplier', 'in', Object.keys(supplier_funds)],
                ['docstatus', '=', 1],
                ['outstanding_amount', '>', 0],
                ['company', '=', frm.doc.company],
                ['posting_date', 'between', [values.from_date, values.to_date]],
                ['name', 'not in', existing_purchase]
            ],
            fields: ['name', 'supplier', 'outstanding_amount', 'grand_total', 'bill_no', 'due_date'],
            order_by: 'due_date asc',
            limit_page_length: 0
        });
        purchase_invoices.forEach(inv => {
            let available = supplier_funds[inv.supplier] || 0;
            let to_allocate = values.allocate_payment_amount ? Math.min(available, inv.outstanding_amount) : 0;
            supplier_funds[inv.supplier] -= to_allocate;
            let row = frm.add_child('vouchers_payment_references2');
            Object.assign(row, { reference_name: inv.name, reference_doctype: "Purchase Invoice", due_date: inv.due_date, bill_no: inv.bill_no, total_amount: inv.grand_total, outstanding_amount: inv.outstanding_amount, allocated_amount: to_allocate, supplier: inv.supplier });
        });
    }
    frm.refresh_field('vouchers_payment_references');
    frm.refresh_field('vouchers_payment_references2');
    frm.dirty();
    frappe.show_alert({ message: __('Invoices Linked Successfully'), indicator: 'green' });
}

async function set_payment_currency(frm) {
    if (!frm.doc.account_payment) return;

    let currency = await account_currency_of(frm, frm.doc.account_payment);
    frm.set_value('account_payment_currency', currency);
    frm.set_value('payment_exchange_rate', await default_rate(frm, currency));
    recalc_totals(frm);
    show_exchange_hint(frm);
}

async function set_row_currency(frm, cdt, cdn) {
    let row = locals[cdt][cdn];
    if (!row.account) return;

    let currency = await account_currency_of(frm, row.account);
    frappe.model.set_value(cdt, cdn, 'account_currency', currency);
    frappe.model.set_value(cdt, cdn, 'exchange_rate', await default_rate(frm, currency));
    show_exchange_hint(frm);
}

async function default_rate(frm, currency) {
    // Pegged currencies do not move, so the rate lives in one Currency Exchange record
    // rather than in the accountant's memory. What comes back is a starting value —
    // the field stays editable.
    if (!currency || currency === frm.doc.currency) return 1;

    let r = await frappe.call({
        method: EXCHANGE_RATE_METHOD,
        args: { from_currency: currency, to_currency: frm.doc.currency, posting_date: frm.doc.posting_date }
    });
    return flt(r.message) || 0;
}

async function set_transfer_currency(frm, side) {
    let account = frm.doc[side];
    if (!account) return;

    let currency = await account_currency_of(frm, account);
    let rate_field = side === 'paid_from' ? 'source_exchange_rate' : 'target_exchange_rate';

    frm.set_value(side + '_currency', currency);
    frm.set_value(rate_field, await default_rate(frm, currency));
    recalc_totals(frm);
    show_exchange_hint(frm);
}

async function account_currency_of(frm, account) {
    // A blank currency on an Account means the company currency in ERPNext.
    let value = await frappe.db.get_value('Account', account, 'account_currency');
    return (value && value.message && value.message.account_currency) || frm.doc.currency;
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
        row.tax_amount        = 0;
        row.amount_before_tax = amount;
        row.amount_after_tax  = amount;
        frm.refresh_field('references');
        recalc_totals(frm);
        return;
    }

    // The same server function that `validate` uses, so the preview can never disagree
    // with what is stored — inclusive templates, Actual amounts and Deduct rows included.
    frappe.call({
        method: TAX_BREAKUP_METHOD,
        args: { template: row.taxes, amount: amount },
        callback: (r) => {
            if (!r.message) return;

            // The grid row may have been removed or retyped while the call was in flight.
            let current = locals[cdt] && locals[cdt][cdn];
            if (!current || flt(current.amount) !== amount || current.taxes !== row.taxes) return;

            current.amount_before_tax = flt(r.message.net_amount);
            current.tax_amount        = flt(r.message.total);
            current.amount_after_tax  = flt(r.message.grand_total);
            frm.refresh_field('references');
            recalc_totals(frm);
        }
    });
}

function recalc_totals(frm) {
    // Mirrors set_payment_amount() on the server. The two have to agree: the server is
    // what actually posts, and a preview that disagrees with it is worse than no preview.
    if (frm.doc.payment_type === 'Internal Transfer') {
        recalc_transfer(frm);
        return;
    }

    // Every figure the user typed is in its own account's currency, so the totals can only
    // be added once each row is converted.
    let base_total = 0, base_tax = 0;
    (frm.doc.references || []).forEach(r => {
        let rate = flt(r.exchange_rate) || 1;
        base_total += flt(r.amount_before_tax) * rate;
        base_tax   += flt(r.tax_amount) * rate;
    });

    let grand = base_total + base_tax;
    let values = {
        total_allocated_amount: base_total,
        total_taxes: base_tax,
        amount_after_tax: grand
    };

    if (!has_foreign_leg(frm)) {
        // Nothing foreign in play: the bank figure follows from the rows and there is
        // nothing left to explain.
        values.payment_exchange_rate = 1;
        values.payment_amount = grand;
        values.exchange_difference = 0;
    } else {
        let rate = flt(frm.doc.payment_exchange_rate);
        let paid = flt(frm.doc.payment_amount);

        if (!paid && rate) {
            // The amount has not been entered yet. Derive it, exactly as the server does,
            // instead of treating the blank as a zero — that turned the whole voucher
            // total into a phantom "exchange difference".
            paid = flt(grand / rate, frm.precision('payment_amount'));
            values.payment_amount = paid;
        }

        values.exchange_difference = rate ? (paid * rate) - grand : 0;
    }

    frm.set_value(values);
}

function recalc_transfer(frm) {
    // A transfer has no reference rows at all, so the branch above would have zeroed a
    // genuine conversion cost.
    let source_rate = flt(frm.doc.source_exchange_rate) || 1;
    let target_rate = flt(frm.doc.target_exchange_rate) || 1;

    let base_paid = flt(frm.doc.paid_amount) * source_rate;
    let received  = flt(frm.doc.received_amount) || flt(frm.doc.paid_amount);
    let base_received = received * target_rate;

    frm.set_value({
        received_amount: received,
        total_allocated_amount: base_paid,
        total_taxes: 0,
        amount_after_tax: base_paid,
        exchange_difference: base_received - base_paid
    });
}

function has_foreign_leg(frm) {
    let company_currency = frm.doc.currency;
    if (!company_currency) return false;

    let currencies = frm.doc.payment_type === 'Internal Transfer'
        ? [frm.doc.paid_from_currency, frm.doc.paid_to_currency]
        : [frm.doc.account_payment_currency].concat(
              (frm.doc.references || []).map(r => r.account_currency)
          );

    return currencies.some(c => c && c !== company_currency);
}

function exchange_outcome(frm) {
    // Which way the difference lands depends on the direction of the voucher: money
    // arriving above expectation is a gain, money leaving above expectation is a loss.
    let difference = flt(frm.doc.exchange_difference);
    if (!difference) return null;

    let gain = frm.doc.payment_type === 'Pay' ? difference < 0 : difference > 0;

    return { amount: Math.abs(difference), gain: gain };
}

function show_exchange_hint(frm) {
    // A blank rate on a foreign leg is the single most expensive mistake available here,
    // so say so before the save does.
    let missing = [];

    if (frm.doc.payment_type === 'Internal Transfer') {
        if (frm.doc.paid_from_currency && frm.doc.paid_from_currency !== frm.doc.currency
            && !flt(frm.doc.source_exchange_rate)) missing.push(__('Source Exchange Rate'));
        if (frm.doc.paid_to_currency && frm.doc.paid_to_currency !== frm.doc.currency
            && !flt(frm.doc.target_exchange_rate)) missing.push(__('Target Exchange Rate'));
    } else {
        if (frm.doc.account_payment_currency && frm.doc.account_payment_currency !== frm.doc.currency
            && !flt(frm.doc.payment_exchange_rate)) missing.push(__('Payment Exchange Rate'));
        (frm.doc.references || []).forEach(r => {
            if (r.account_currency && r.account_currency !== frm.doc.currency && !flt(r.exchange_rate)) {
                missing.push(__('Row #{0} Exchange Rate', [r.idx]));
            }
        });
    }

    frm.dashboard.clear_headline();

    if (missing.length) {
        frm.dashboard.set_headline(
            `<span style="color:#c62828">⚠ ${__('Enter the exchange rate for')}: ${missing.join('، ')}</span>`
        );
        return;
    }

    let outcome = exchange_outcome(frm);
    if (!outcome) return;

    let label = outcome.gain ? __('Exchange gain on this voucher') : __('Exchange loss on this voucher');
    let colour = outcome.gain ? '#2e7d32' : '#c62828';
    let money = format_currency(outcome.amount, frm.doc.currency || frappe.defaults.get_default('currency'));

    frm.dashboard.set_headline(
        `<span style="color:${colour}">${label}: <b>${money}</b></span>`
    );
}

// The company the filters should scope to. Falls back to the user default so a link
// field is never filtered on `undefined`, which matches no account at all and makes a
// perfectly valid choice come back rejected.
function voucher_company(frm) {
    return frm.doc.company || frappe.defaults.get_user_default('Company') || '';
}

function apply_vouchers_filters(frm) {
    // Ledger accounts of this company. The callback runs on every click, so it always
    // sees the company as it stands at that moment.
    const ledger_accounts = () => {
        let company = voucher_company(frm);
        let filters = { 'is_group': 0 };
        if (company) filters.company = company;
        return { filters: filters };
    };

    frm.set_query('account_payment', ledger_accounts);
    frm.set_query('paid_from', ledger_accounts);
    frm.set_query('paid_to', ledger_accounts);
    frm.set_query('account', 'references', ledger_accounts);

    frm.set_query('party_type', 'references', () => {
        return { filters: [['name', 'in', ['Supplier', 'Customer', 'Employee', 'Shareholder']]] };
    });

    // `mode_of_payment` is deliberately left unfiltered. It is a global doctype whose
    // company mapping lives in a child table, so there is nothing to filter it by
    // without excluding modes the company legitimately uses.
}
