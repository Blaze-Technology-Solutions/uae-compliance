# Copyright (c) 2024, Blaze Technology Solutions and contributors
# For license information, please see license.txt

import frappe
from erpnext.accounts.doctype.sales_invoice.sales_invoice import SalesInvoice
from erpnext.accounts.general_ledger import make_reverse_gl_entries
from frappe import _, bold
from frappe.utils import flt, fmt_money, getdate

from uae_compliance.overrides.payment_entry import get_taxes_summary


def set_and_validate_advances_with_vat(doc, method=None):
    country = frappe.get_cached_value("Company", doc.company, "country")

    if country != "United Arab Emirates":
        return

    if not doc.advances:
        return

    taxes = get_taxes_summary(doc.company, doc.advances)

    allocated_amount_with_taxes = 0
    tax_amount = 0

    for advance in doc.get("advances"):
        if not advance.allocated_amount:
            continue

        tax_row = taxes.get(
            advance.reference_name, frappe._dict(paid_amount=1, tax_amount=0)
        )

        _tax_amount = flt(
            advance.allocated_amount / tax_row.paid_amount * tax_row.tax_amount, 2
        )
        tax_amount += _tax_amount
        allocated_amount_with_taxes += _tax_amount
        allocated_amount_with_taxes += advance.allocated_amount

    excess_allocation = flt(
        flt(allocated_amount_with_taxes, 2) - (doc.rounded_total or doc.grand_total), 2
    )
    if excess_allocation > 0:
        message = _(
            "Allocated amount with taxes (VAT) in advances table cannot be greater than"
            " outstanding amount of the document. Allocated amount with taxes is greater by {0}."
        ).format(bold(fmt_money(excess_allocation, currency=doc.currency)))

        if excess_allocation < 1:
            message += "<br><br>Is it becasue of Rounding Adjustment? Try disabling Rounded Total in the document."

        frappe.throw(message, title=_("Invalid Allocated Amount"))

    doc.total_advance = allocated_amount_with_taxes
    doc.set_payment_schedule()
    doc.outstanding_amount -= tax_amount
    frappe.flags.vat_excess_allocation_validated = True


def before_cancel(doc, method=None):
    payment_references = frappe.get_all(
        "Payment Entry Reference",
        filters={
            "reference_doctype": doc.doctype,
            "reference_name": doc.name,
            "docstatus": 1,
        },
        fields=["name as voucher_detail_no", "parent as payment_name"],
    )

    if not payment_references:
        return

    for reference in payment_references:
        reverse_vat_adjusted_against_payment_entry(
            reference.voucher_detail_no, reference.payment_name
        )


def reverse_vat_adjusted_against_payment_entry(voucher_detail_no, payment_name):
    filters = {
        "voucher_type": "Payment Entry",
        "voucher_no": payment_name,
        "voucher_detail_no": voucher_detail_no,
    }

    gl_entries = frappe.get_all("GL Entry", filters=filters, fields="*")
    if not gl_entries:
        return

    frappe.db.set_value("GL Entry", filters, "is_cancelled", 1)
    make_reverse_gl_entries(gl_entries, partial_cancel=True)


class CustomSalesInvoice(SalesInvoice):
    def validate_due_date(self):
        if self.get("is_pos") or self.doctype not in ["Sales Invoice"]:
            return

        from erpnext.accounts.party import validate_due_date

        posting_date = self.get("custom_document_date") or self.posting_date

        # skip due date validation for records via Data Import
        if frappe.flags.in_import and getdate(self.due_date) < getdate(posting_date):
            self.due_date = posting_date

        elif self.doctype == "Sales Invoice":
            if not self.due_date:
                frappe.throw(_("Due Date is mandatory"))

            validate_due_date(
                posting_date,
                self.due_date,
                self.payment_terms_template,
            )
