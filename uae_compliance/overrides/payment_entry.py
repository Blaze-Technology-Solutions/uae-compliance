# Copyright (c) 2024, Blaze Technology Solutions and contributors
# For license information, please see license.txt

import frappe
from erpnext.accounts.general_ledger import make_gl_entries
from erpnext.accounts.utils import create_payment_ledger_entry
from erpnext.controllers.accounts_controller import get_advance_payment_entries
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import flt, getdate

from uae_compliance.utils import get_all_vat_accounts


def on_submit(doc, method=None):
    make_vat_revesal_entry_from_advance_payment(doc)

def on_update_after_submit(doc, method=None):
    make_vat_revesal_entry_from_advance_payment(doc)

def make_vat_revesal_entry_from_advance_payment(doc):
    """
    This functionality aims to create a VAT reversal entry where VAT was paid in advance

    On Submit: Creates GLEs and PLEs for all references.
    On Update after Submit: Creates GLEs for new references. Creates PLEs for all references.
    """
    gl_dict = []
    update_gl_for_advance_vat_reversal(gl_dict, doc)

    if not gl_dict:
        return

    # Creates GLEs and PLEs
    make_gl_entries(gl_dict)

def update_gl_for_advance_vat_reversal(gl_dict, doc):
    if not doc.taxes:
        return

    for row in doc.get("references"):
        if row.reference_doctype not in ("Sales Invoice"):
            continue

        gl_dict.extend(_get_gl_for_advance_vat_reversal(doc, row))

def _get_gl_for_advance_vat_reversal(payment_entry, reference_row):
    gl_dicts = []
    voucher_date = frappe.db.get_value(
        reference_row.reference_doctype, reference_row.reference_name, "posting_date"
    )
    posting_date = (
        payment_entry.posting_date
        if getdate(payment_entry.posting_date) > getdate(voucher_date)
        else voucher_date
    )

    taxes = get_proportionate_taxes_for_reversal(payment_entry, reference_row)

    if not taxes:
        return gl_dicts

    total_amount = sum(taxes.values())

    args = {
        "posting_date": posting_date,
        "voucher_detail_no": reference_row.name,
        "remarks": f"Reversal for VAT on Advance Payment Entry"
        f" {payment_entry.name} against {reference_row.reference_doctype} {reference_row.reference_name}",
    }

    # Reduce receivables
    gl_entry = payment_entry.get_gl_dict(
        {
            "account": reference_row.account,
            "credit": total_amount,
            "credit_in_account_currency": total_amount,
            "party_type": payment_entry.party_type,
            "party": payment_entry.party,
            "against_voucher_type": reference_row.reference_doctype,
            "against_voucher": reference_row.reference_name,
            **args,
        },
        item=reference_row,
    )

    if frappe.db.exists("GL Entry", args):
        if frappe.db.exists("Payment Ledger Entry", {**args, "delinked": 0}):
            return gl_dicts

        # All existing PLE are delinked and new ones are created everytime on update
        # refer: reconcile_against_document in utils.py
        create_payment_ledger_entry(
            [gl_entry], update_outstanding="No", cancel=0, adv_adj=1
        )

        return gl_dicts

    if not frappe.flags.vat_excess_allocation_validated:
        total_allocation = total_amount + reference_row.allocated_amount
        excess_allocation = total_allocation - reference_row.outstanding_amount

        if excess_allocation > 1:
            frappe.throw(
                _(
                    "Outstanding amount {0} is less than the total allocated amount"
                    " with taxes {1} for {2} {3}"
                ).format(
                    reference_row.outstanding_amount,
                    total_allocation,
                    reference_row.reference_doctype,
                    reference_row.reference_name,
                )
            )

    gl_dicts.append(gl_entry)

    # Reverse taxes
    for account, amount in taxes.items():
        gl_dicts.append(
            payment_entry.get_gl_dict(
                {
                    "account": account,
                    "debit": amount,
                    "debit_in_account_currency": amount,
                    "against_voucher_type": payment_entry.doctype,
                    "against_voucher": payment_entry.name,
                    **args,
                },
                item=reference_row,
            )
        )

    return gl_dicts


def get_proportionate_taxes_for_reversal(payment_entry, reference_row):
    """
    This function calculates proportionate taxes for reversal of VAT paid in advance
    """
    # Compile taxes
    vat_accounts = get_all_vat_accounts(payment_entry.company)
    taxes = {}
    for row in payment_entry.taxes:
        if row.account_head not in vat_accounts:
            continue

        taxes.setdefault(row.account_head, 0)
        taxes[row.account_head] += row.base_tax_amount

    if not taxes:
        return

    # Ensure there is no rounding error
    if (
        not payment_entry.unallocated_amount
        and payment_entry.references[-1].idx == reference_row.idx
    ):
        return balance_taxes(payment_entry, reference_row, taxes)

    return get_proportionate_taxes_for_row(payment_entry, reference_row, taxes)

def balance_taxes(payment_entry, reference_row, taxes):
    for account, amount in taxes.items():
        for allocation_row in payment_entry.references:
            if allocation_row.reference_name == reference_row.reference_name:
                continue

            taxes[account] = taxes[account] - flt(
                amount
                * payment_entry.calculate_base_allocated_amount_for_reference(
                    allocation_row
                )
                / payment_entry.base_paid_amount,
                2,
            )

    return taxes

def get_proportionate_taxes_for_row(payment_entry, reference_row, taxes):
    base_allocated_amount = payment_entry.calculate_base_allocated_amount_for_reference(
        reference_row
    )
    for account, amount in taxes.items():
        taxes[account] = flt(
            amount * base_allocated_amount / payment_entry.base_paid_amount, 2
        )

    return taxes

def get_advance_payment_entries_for_regional(
    party_type,
    party,
    party_account,
    order_doctype,
    order_list=None,
    include_unallocated=True,
    against_all_orders=False,
    limit=None,
    condition=None,
):
    """
    Get Advance Payment Entries with VAT Taxes
    """

    payment_entries = get_advance_payment_entries(
        party_type=party_type,
        party=party,
        party_account=party_account,
        order_doctype=order_doctype,
        order_list=order_list,
        include_unallocated=include_unallocated,
        against_all_orders=against_all_orders,
        limit=limit,
        condition=condition,
    )

    # if not Sales Invoice and is Payment Reconciliation
    if not condition or not payment_entries:
        return payment_entries

    company = frappe.db.get_value("Account", party_account, "company")
    taxes = get_taxes_summary(company, payment_entries)

    for pe in payment_entries:
        tax_row = taxes.get(
            pe.reference_name,
            frappe._dict(paid_amount=1, tax_amount=0, tax_amount_reversed=0),
        )
        pe.amount += tax_row.tax_amount - tax_row.tax_amount_reversed

    return payment_entries

def get_taxes_summary(company, payment_entries):
    vat_accounts = get_all_vat_accounts(company)
    references = [
        advance.reference_name
        for advance in payment_entries
        if advance.reference_type == "Payment Entry"
    ]

    if not references:
        return {}

    gl_entry = frappe.qb.DocType("GL Entry")
    pe = frappe.qb.DocType("Payment Entry")
    taxes = (
        frappe.qb.from_(gl_entry)
        .join(pe)
        .on(pe.name == gl_entry.voucher_no)
        .select(
            Sum(gl_entry.credit_in_account_currency).as_("tax_amount"),
            Sum(gl_entry.debit_in_account_currency).as_("tax_amount_reversed"),
            pe.name.as_("payment_entry"),
            pe.paid_amount,
            pe.unallocated_amount,
        )
        .where(gl_entry.is_cancelled == 0)
        .where(gl_entry.voucher_type == "Payment Entry")
        .where(gl_entry.voucher_no.isin(references))
        .where(gl_entry.account.isin(vat_accounts))
        .where(gl_entry.company == company)
        .groupby(gl_entry.voucher_no)
        .run(as_dict=True)
    )

    taxes = {tax.payment_entry: tax for tax in taxes}

    return taxes

def adjust_allocations_for_taxes_in_payment_reconciliation(doc):
    if not doc.allocation:
        return

    taxes = get_taxes_summary(doc.company, doc.allocation)
    taxes = {
        tax.payment_entry: frappe._dict(
            {
                **tax,
                "paid_proportion": tax.paid_amount / (tax.paid_amount + tax.tax_amount),
            }
        )
        for tax in taxes.values()
    }

    for row in doc.allocation:
        tax_row = taxes.get(row.reference_name)
        if not tax_row:
            continue

        row.update(
            {
                "amount": tax_row.unallocated_amount,
                "allocated_amount": flt(
                    row.get("allocated_amount", 0) * tax_row.paid_proportion, 2
                ),
                "unreconciled_amount": tax_row.unallocated_amount,
            }
        )