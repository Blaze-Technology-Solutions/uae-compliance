# Copyright (c) 2024, Blaze Technology Solutions and contributors
# For license information, please see license.txt

import frappe


def get_all_vat_accounts(company):
    settings = frappe.get_cached_doc("UAE VAT Settings", company)
    accounts_list = []
    for row in settings.uae_vat_accounts:
        if vat_account := row.get("account"):
            accounts_list.append(vat_account)

    return accounts_list
