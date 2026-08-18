app_name = "voucher_management"
app_title = "Voucher Management"
app_publisher = "Amir"
app_description = "Voucher Management"
app_email = "Amirmasni@gmail.com"
app_license = "mit"

# Accounting Dimensions
accounting_dimension_doctypes = [
    "Vouchers Entry",
    "Voucher Entry Account"
]

# Document Events
doc_events = {
    "Vouchers Entry": {
        "on_submit": "voucher_management.vouchers_entry_gl.on_submit",
        "on_cancel": "voucher_management.vouchers_entry_gl.on_cancel",
        "on_trash": "voucher_management.vouchers_entry_gl.on_trash",
        "on_update_after_submit": "voucher_management.vouchers_entry_gl.on_update_after_submit",
    }
}