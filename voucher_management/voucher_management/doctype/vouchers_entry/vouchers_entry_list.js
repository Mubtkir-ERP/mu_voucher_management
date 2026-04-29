frappe.listview_settings['Vouchers Entry'] = {
	onload: function(listview) {
		if (!frappe.user_roles.includes('System Manager') && !frappe.user_roles.includes('Administrator')) return;

		listview.page.add_action_item(__('Create GL Entries'), function() {
			const selected = listview.get_checked_items();

			if (!selected.length) {
				frappe.msgprint(__('Please select at least one Vouchers Entry.'));
				return;
			}

			const names = selected.map(r => r.name);

			frappe.confirm(
				__('Create GL Entries for {0} selected voucher(s)?', [names.length]),
				function() {
					frappe.call({
						method: 'voucher_management.voucher_management.doctype.vouchers_entry.vouchers_entry.bulk_create_gl_entries',
						args: { vouchers: names },
						freeze: true,
						freeze_message: __('Creating GL Entries...'),
						callback: function(r) {
							if (!r.exc) {
								const res = r.message;
								frappe.msgprint({
									title: __('Done'),
									indicator: 'green',
									message: `
										✅ ${__('Success')}: <b>${res.success.length}</b><br>
										⏭️ ${__('Skipped (not submitted)')}: <b>${res.skipped.length}</b><br>
										❌ ${__('Failed')}: <b>${res.failed.length}</b>
										${res.failed.length ? '<br><br>' + res.failed.map(f => `${f.name}: ${f.error}`).join('<br>') : ''}
									`
								});
								listview.refresh();
							}
						}
					});
				}
			);
		});
	}
};
