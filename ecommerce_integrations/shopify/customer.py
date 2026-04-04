from typing import Any

import frappe
from frappe import _
from frappe.utils import cstr, validate_phone_number

from ecommerce_integrations.controllers.customer import EcommerceCustomer
from ecommerce_integrations.shopify.constants import (
	ADDRESS_ID_FIELD,
	CUSTOMER_ID_FIELD,
	MODULE_NAME,
	SETTING_DOCTYPE,
)


class ShopifyCustomer(EcommerceCustomer):
	def __init__(self, customer_id: str):
		self.setting = frappe.get_doc(SETTING_DOCTYPE)
		super().__init__(customer_id, CUSTOMER_ID_FIELD, MODULE_NAME)

	def sync_customer(self, customer: dict[str, Any]) -> None:
		"""Create Customer in ERPNext using shopify's Customer dict."""

		customer_name = cstr(customer.get("first_name")) + " " + cstr(customer.get("last_name"))
		if len(customer_name.strip()) == 0:
			customer_name = customer.get("email")

		customer_group = self.setting.customer_group
		super().sync_customer(customer_name, customer_group)

		billing_address = customer.get("billing_address", {}) or customer.get("default_address")
		shipping_address = customer.get("shipping_address", {})

		if billing_address:
			self.create_customer_address(
				customer_name, billing_address, address_type="Billing", email=customer.get("email")
			)
		if shipping_address:
			self.create_customer_address(
				customer_name, shipping_address, address_type="Shipping", email=customer.get("email")
			)

		self.create_customer_contact(customer)

	def create_customer_address(
		self,
		customer_name,
		shopify_address: dict[str, Any],
		address_type: str = "Billing",
		email: str | None = None,
	) -> None:
		"""Create customer address(es) using Customer dict provided by shopify."""
		address_fields = _map_address_fields(shopify_address, customer_name, address_type, email)
		super().create_customer_address(address_fields)

	def update_existing_addresses(self, customer):
		billing_address = customer.get("billing_address", {}) or customer.get("default_address")
		shipping_address = customer.get("shipping_address", {})

		customer_name = cstr(customer.get("first_name")) + " " + cstr(customer.get("last_name"))
		email = customer.get("email")

		if billing_address:
			self._update_existing_address(customer_name, billing_address, "Billing", email)
		if shipping_address:
			self._update_existing_address(customer_name, shipping_address, "Shipping", email)

	def _update_existing_address(
		self,
		customer_name,
		shopify_address: dict[str, Any],
		address_type: str = "Billing",
		email: str | None = None,
	) -> None:
		old_address = self.get_customer_address_doc(address_type)

		if not old_address:
			self.create_customer_address(customer_name, shopify_address, address_type, email)
		else:
			exclude_in_update = ["address_title", "address_type"]
			new_values = _map_address_fields(shopify_address, customer_name, address_type, email)

			old_address.update({k: v for k, v in new_values.items() if k not in exclude_in_update})
			old_address.flags.ignore_mandatory = True
			old_address.save(ignore_permissions=True)

	def create_customer_contact(self, shopify_customer: dict[str, Any]) -> None:
		if not (shopify_customer.get("first_name") and shopify_customer.get("email")):
			return

		contact_fields = {
			"status": "Passive",
			"first_name": shopify_customer.get("first_name"),
			"last_name": shopify_customer.get("last_name"),
			"unsubscribed": not shopify_customer.get("accepts_marketing"),
		}

		if shopify_customer.get("email"):
			contact_fields["email_ids"] = [{"email_id": shopify_customer.get("email"), "is_primary": True}]

		phone_no = shopify_customer.get("phone") or shopify_customer.get("default_address", {}).get("phone")

		if validate_phone_number(phone_no, throw=False):
			contact_fields["phone_nos"] = [{"phone": phone_no, "is_primary_phone": True}]

		super().create_customer_contact(contact_fields)


def sync_customer_from_webhook(payload, request_id=None):
	"""Handle customers/create webhook from Shopify."""
	from ecommerce_integrations.shopify.utils import create_shopify_log

	try:
		customer_id = str(payload.get("id"))
		shopify_customer = ShopifyCustomer(customer_id)

		if not shopify_customer.is_synced():
			shopify_customer.sync_customer(payload)

		# Sync metafields after customer is created
		_sync_customer_metafields(shopify_customer, payload)

		if request_id:
			log = frappe.get_doc("Ecommerce Integration Log", request_id)
			log.status = "Success"
			log.save(ignore_permissions=True)

	except Exception as e:
		create_shopify_log(status="Error", exception=frappe.get_traceback(), rollback=True)


def update_customer_from_webhook(payload, request_id=None):
	"""Handle customers/update webhook from Shopify."""
	from ecommerce_integrations.shopify.utils import create_shopify_log

	try:
		customer_id = str(payload.get("id"))
		shopify_customer = ShopifyCustomer(customer_id)

		if shopify_customer.is_synced():
			# Update existing customer name
			customer_name = cstr(payload.get("first_name")) + " " + cstr(payload.get("last_name"))
			if len(customer_name.strip()) == 0:
				customer_name = payload.get("email")

			customer_doc = shopify_customer.get_customer_doc()
			customer_doc.customer_name = customer_name
			customer_doc.flags.ignore_mandatory = True
			customer_doc.save(ignore_permissions=True)

			# Update addresses
			shopify_customer.update_existing_addresses(payload)
		else:
			# Customer doesn't exist yet, create it
			shopify_customer.sync_customer(payload)

		# Sync metafields after customer is created/updated
		_sync_customer_metafields(shopify_customer, payload)

		if request_id:
			log = frappe.get_doc("Ecommerce Integration Log", request_id)
			log.status = "Success"
			log.save(ignore_permissions=True)

	except Exception as e:
		create_shopify_log(status="Error", exception=frappe.get_traceback(), rollback=True)


def _sync_customer_metafields(shopify_customer, payload):
	"""Fetch metafields from Shopify API and save on Customer."""
	import json as _json
	import requests

	if not shopify_customer.is_synced():
		return

	customer_id = payload.get("id")
	if not customer_id:
		return

	setting = frappe.get_doc(SETTING_DOCTYPE)
	access_token = setting.get_password("access_token")
	if not access_token:
		return

	shopify_url = setting.shopify_url.rstrip("/")

	# Fetch all metafields for this customer from Shopify API
	from ecommerce_integrations.shopify.constants import API_VERSION

	url = f"https://{shopify_url}/admin/api/{API_VERSION}/customers/{customer_id}/metafields.json"
	headers = {"X-Shopify-Access-Token": access_token}

	response = requests.get(url, headers=headers)
	if response.status_code != 200:
		frappe.logger("shopify_webhook").error(
			f"Failed to fetch metafields for customer {customer_id}: {response.text}"
		)
		return

	metafields = response.json().get("metafields", [])
	if not metafields:
		return

	customer_doc = shopify_customer.get_customer_doc()

	# Store full JSON for medical namespace display in Sales Order
	customer_doc.shopify_metafields_data = _json.dumps(metafields)

	# Set custom namespace values on actual custom fields
	for mf in metafields:
		if mf.get("namespace") != "custom":
			continue

		fieldname = f"shopify_custom_{mf['key']}"
		if not hasattr(customer_doc, fieldname):
			continue

		value = mf.get("value")
		if mf.get("type") == "boolean":
			value = 1 if value in ("true", "True", True) else 0

		customer_doc.set(fieldname, value)

	customer_doc.flags.ignore_mandatory = True
	customer_doc.save(ignore_permissions=True)


@frappe.whitelist()
def get_customer_medical_info(customer):
	"""Get medical namespace metafield values for a customer from stored JSON."""
	import json as _json

	metafields_json = frappe.db.get_value("Customer", customer, "shopify_metafields_data")
	if not metafields_json:
		return []

	metafields = _json.loads(metafields_json)

	# Get label mapping from Shopify Setting
	label_map = {}
	definitions_json = frappe.db.get_single_value(SETTING_DOCTYPE, "shopify_metafield_definitions")
	if definitions_json:
		label_map = _json.loads(definitions_json)

	# Filter medical namespace only
	result = []
	for mf in metafields:
		if mf.get("namespace") != "medical":
			continue

		map_key = f"{mf['namespace']}.{mf['key']}"
		defn = label_map.get(map_key, {})
		label = defn.get("label") or mf["key"].replace("_", " ").title()
		mf_type = mf.get("type", defn.get("type", ""))

		result.append({
			"label": label,
			"value": mf.get("value"),
			"type": mf_type,
		})

	return result


def _map_address_fields(shopify_address, customer_name, address_type, email):
	"""returns dict with shopify address fields mapped to equivalent ERPNext fields"""
	address_fields = {
		"address_title": customer_name,
		"address_type": address_type,
		ADDRESS_ID_FIELD: shopify_address.get("id"),
		"address_line1": shopify_address.get("address1") or "Address 1",
		"address_line2": shopify_address.get("address2"),
		"city": shopify_address.get("city"),
		"state": shopify_address.get("province"),
		"pincode": shopify_address.get("zip"),
		"country": shopify_address.get("country"),
		"email_id": email,
	}

	phone = shopify_address.get("phone")
	if validate_phone_number(phone, throw=False):
		address_fields["phone"] = phone

	return address_fields
