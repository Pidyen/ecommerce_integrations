import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice
from frappe.utils import cint, cstr, getdate, nowdate

from ecommerce_integrations.shopify.constants import (
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	SETTING_DOCTYPE,
)
from ecommerce_integrations.shopify.utils import create_shopify_log


def prepare_sales_invoice(payload, request_id=None):
	from ecommerce_integrations.shopify.order import get_sales_order

	order = payload

	frappe.set_user("Administrator")
	setting = frappe.get_doc(SETTING_DOCTYPE)
	frappe.flags.request_id = request_id

	try:
		sales_order = get_sales_order(cstr(order["id"]))
		if sales_order:
			create_sales_invoice(order, setting, sales_order)
			create_shopify_log(status="Success")
		else:
			create_shopify_log(status="Invalid", message="Sales Order not found for syncing sales invoice.")
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True)


def create_sales_invoice(shopify_order, setting, so):
	if not cint(setting.sync_sales_invoice):
		return
	if so.docstatus != 1 or so.per_billed:
		return

	if cint(setting.get("create_payment_entry_against_so")):
		_create_payment_entry_against_sales_order(shopify_order, setting, so)
		return

	if frappe.db.get_value("Sales Invoice", {ORDER_ID_FIELD: shopify_order.get("id")}, "name"):
		return

	posting_date = getdate(shopify_order.get("created_at")) or nowdate()

	sales_invoice = make_sales_invoice(so.name, ignore_permissions=True)
	sales_invoice.set(ORDER_ID_FIELD, str(shopify_order.get("id")))
	sales_invoice.set(ORDER_NUMBER_FIELD, shopify_order.get("name"))
	sales_invoice.set_posting_time = 1
	sales_invoice.posting_date = posting_date
	sales_invoice.due_date = posting_date
	sales_invoice.naming_series = setting.sales_invoice_series or "SI-Shopify-"
	sales_invoice.flags.ignore_mandatory = True
	set_cost_center(sales_invoice.items, setting.cost_center)
	sales_invoice.insert(ignore_mandatory=True)
	sales_invoice.submit()
	if sales_invoice.grand_total > 0:
		make_payament_entry_against_sales_invoice(sales_invoice, setting, posting_date)

	if shopify_order.get("note"):
		sales_invoice.add_comment(text=f"Order Note: {shopify_order.get('note')}")


def _create_payment_entry_against_sales_order(shopify_order, setting, so):
	"""Skip Sales Invoice creation and book a Payment Entry directly against the SO.

	Idempotent: returns early if SO is already fully advance-paid or if a Payment
	Entry referencing this Shopify order id already exists.
	"""
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	shopify_order_id = str(shopify_order.get("id"))
	if so.advance_paid and so.advance_paid >= so.grand_total:
		return
	if frappe.db.exists(
		"Payment Entry",
		{"reference_no": shopify_order_id, "docstatus": 1},
	):
		return

	posting_date = getdate(shopify_order.get("created_at")) or nowdate()

	payment_entry = get_payment_entry("Sales Order", so.name, bank_account=setting.cash_bank_account)
	payment_entry.flags.ignore_mandatory = True
	payment_entry.reference_no = shopify_order_id
	payment_entry.reference_date = posting_date
	payment_entry.posting_date = posting_date
	payment_entry.insert(ignore_permissions=True)
	payment_entry.submit()

	if shopify_order.get("note"):
		so.add_comment(text=f"Order Note: {shopify_order.get('note')}")


def set_cost_center(items, cost_center):
	for item in items:
		item.cost_center = cost_center


def make_payament_entry_against_sales_invoice(doc, setting, posting_date=None):
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	payment_entry = get_payment_entry(doc.doctype, doc.name, bank_account=setting.cash_bank_account)
	payment_entry.flags.ignore_mandatory = True
	payment_entry.reference_no = doc.name
	payment_entry.posting_date = posting_date or nowdate()
	payment_entry.reference_date = posting_date or nowdate()
	payment_entry.insert(ignore_permissions=True)
	payment_entry.submit()
