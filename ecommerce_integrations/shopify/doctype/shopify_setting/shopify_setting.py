# Copyright (c) 2021, Frappe and contributors
# For license information, please see LICENSE

import requests

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import get_datetime
from shopify.collection import PaginatedIterator
from shopify.resources import Location

from ecommerce_integrations.controllers.setting import (
	ERPNextWarehouse,
	IntegrationWarehouse,
	SettingController,
)
from ecommerce_integrations.shopify import connection
from ecommerce_integrations.shopify.constants import (
	ADDRESS_ID_FIELD,
	CUSTOMER_ID_FIELD,
	FULLFILLMENT_ID_FIELD,
	ITEM_SELLING_RATE_FIELD,
	ORDER_ID_FIELD,
	ORDER_ITEM_DISCOUNT_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	SUPPLIER_ID_FIELD,
)
from ecommerce_integrations.shopify.utils import (
	ensure_old_connector_is_disabled,
	migrate_from_old_connector,
)
from shopify.resources import Webhook


class ShopifySetting(SettingController):
	def is_enabled(self) -> bool:
		return bool(self.enable_shopify)

	def validate(self):
		ensure_old_connector_is_disabled()

		if self.shopify_url:
			self.shopify_url = self.shopify_url.replace("https://", "")

		# Unregister webhooks when disabling
		if not self.is_enabled() and self.webhooks:
			try:
				access_token = self.get_password("access_token")
				if access_token:
					connection.unregister_webhooks(self.shopify_url, access_token)
			except Exception:
				pass  # token might be invalid, just clear local records
			self.webhooks = []

		self._validate_warehouse_links()
		self._initalize_default_values()

		if self.is_enabled():
			setup_custom_fields()

	def on_update(self):
		if self.is_enabled() and not self.is_old_data_migrated:
			migrate_from_old_connector()

	@frappe.whitelist()
	def register_webhooks_manual(self):
		"""Manually register webhooks. Used when access token is pasted directly
		or to re-register webhooks after OAuth."""
		access_token = self.get_password("access_token")
		if not access_token:
			frappe.throw(_("Access Token is required to register webhooks."))

		self.webhooks = []

		new_webhooks = connection.register_webhooks(self.shopify_url, access_token)
		if not new_webhooks:
			frappe.throw(_("Failed to register webhooks. Check credentials and retry."))

		for webhook in new_webhooks:
			self.append("webhooks", {"webhook_id": webhook.id, "method": webhook.topic})

		self.flags.ignore_validate = True
		self.save(ignore_permissions=True)

		frappe.msgprint(_("Webhooks registered successfully."))

	def _validate_warehouse_links(self):
		for wh_map in self.shopify_warehouse_mapping:
			if not wh_map.erpnext_warehouse:
				frappe.throw(_("ERPNext warehouse required in warehouse map table."))

	def _initalize_default_values(self):
		if not self.last_inventory_sync:
			self.last_inventory_sync = get_datetime("1970-01-01")

	@frappe.whitelist()
	@connection.temp_shopify_session
	def update_location_table(self):
		"""Fetch locations from shopify and add it to child table so user can
		map it with correct ERPNext warehouse."""

		self.shopify_warehouse_mapping = []
		for locations in PaginatedIterator(Location.find()):
			for location in locations:
				self.append(
					"shopify_warehouse_mapping",
					{"shopify_location_id": location.id, "shopify_location_name": location.name},
				)

	@frappe.whitelist()
	@connection.temp_shopify_session
	def fetch_all_webhooks(self):
		"""Fetch ALL webhooks registered on Shopify store to check for conflicts."""
		all_webhooks = []
		for webhook in Webhook.find():
			all_webhooks.append({
				"id": webhook.id,
				"topic": webhook.topic,
				"address": webhook.address,
				"format": webhook.format,
				"created_at": webhook.created_at,
				"updated_at": webhook.updated_at,
			})

		current_site = connection.get_current_domain_name()

		result = {
			"current_site": current_site,
			"total_webhooks": len(all_webhooks),
			"webhooks": all_webhooks,
		}

		frappe.logger("shopify_webhook").info(
			f"Fetched {len(all_webhooks)} webhooks from Shopify. Current site: {current_site}"
		)

		return result

	@frappe.whitelist()
	def fetch_customer_metafields(self):
		"""Fetch customer metafield definitions from Shopify and save label mapping."""
		from ecommerce_integrations.shopify.constants import API_VERSION
		import json as _json

		access_token = self.get_password("access_token")
		if not access_token:
			frappe.throw(_("Access Token is required."))

		shopify_url = self.shopify_url.rstrip("/")

		url = f"https://{shopify_url}/admin/api/{API_VERSION}/graphql.json"
		headers = {
			"X-Shopify-Access-Token": access_token,
			"Content-Type": "application/json",
		}

		query = """
		{
			metafieldDefinitions(ownerType: CUSTOMER, first: 250) {
				edges {
					node {
						name
						namespace
						key
						type {
							name
						}
					}
				}
			}
		}
		"""

		response = requests.post(url, headers=headers, json={"query": query})

		if response.status_code != 200:
			frappe.throw(_("Failed to fetch metafield definitions from Shopify: {0}").format(response.text))

		result = response.json()

		if "errors" in result:
			frappe.throw(_("Shopify GraphQL Error: {0}").format(result["errors"]))

		edges = result.get("data", {}).get("metafieldDefinitions", {}).get("edges", [])
		definitions = [edge["node"] for edge in edges]

		if not definitions:
			frappe.msgprint(_("No customer metafield definitions found on Shopify."))
			return []

		# Save definitions as label mapping in Shopify Setting
		label_map = {}
		for defn in definitions:
			map_key = f"{defn['namespace']}.{defn['key']}"
			label_map[map_key] = {
				"label": defn.get("name") or defn["key"],
				"type": defn.get("type", {}).get("name", ""),
				"namespace": defn["namespace"],
				"key": defn["key"],
			}

		self.shopify_metafield_definitions = _json.dumps(label_map)
		self.flags.ignore_validate = True
		self.save(ignore_permissions=True)

		# For "custom" namespace: delete old and create custom fields on Customer
		old_custom_fields = frappe.get_all(
			"Custom Field",
			filters={"dt": "Customer", "fieldname": ["like", "shopify_custom_%"]},
			pluck="name",
		)
		for cf_name in old_custom_fields:
			frappe.delete_doc("Custom Field", cf_name, ignore_permissions=True)

		custom_ns_fields = [d for d in definitions if d["namespace"] == "custom"]
		created_count = 0

		if custom_ns_fields:
			insert_after = CUSTOMER_ID_FIELD
			for defn in custom_ns_fields:
				fieldname = f"shopify_custom_{defn['key']}"
				label = defn.get("name") or defn["key"].replace("_", " ").title()
				shopify_type = defn.get("type", {}).get("name", "")
				fieldtype = _map_shopify_type_to_erpnext(shopify_type)

				cf = frappe.get_doc({
					"doctype": "Custom Field",
					"dt": "Customer",
					"fieldname": fieldname,
					"label": label,
					"fieldtype": fieldtype,
					"insert_after": insert_after,
				})
				cf.insert(ignore_permissions=True)
				insert_after = fieldname
				created_count += 1

		frappe.db.commit()
		frappe.clear_cache(doctype="Customer")

		frappe.msgprint(
			_("Synced {0} metafield definitions. Created {1} custom fields for 'custom' namespace.").format(
				len(label_map), created_count
			)
		)
		return list(label_map.values())

	@frappe.whitelist()
	def setup_custom_fields_manual(self):
		"""Manually create all required custom fields for Shopify integration."""
		setup_custom_fields()
		frappe.msgprint(_("Custom fields created successfully."))

	def get_erpnext_warehouses(self) -> list[ERPNextWarehouse]:
		return [wh_map.erpnext_warehouse for wh_map in self.shopify_warehouse_mapping]

	def get_erpnext_to_integration_wh_mapping(self) -> dict[ERPNextWarehouse, IntegrationWarehouse]:
		return {
			wh_map.erpnext_warehouse: wh_map.shopify_location_id for wh_map in self.shopify_warehouse_mapping
		}

	def get_integration_to_erpnext_wh_mapping(self) -> dict[IntegrationWarehouse, ERPNextWarehouse]:
		return {
			wh_map.shopify_location_id: wh_map.erpnext_warehouse for wh_map in self.shopify_warehouse_mapping
		}


SHOPIFY_TYPE_MAP = {
	"boolean": "Check",
	"single_line_text_field": "Data",
	"multi_line_text_field": "Small Text",
	"rich_text_field": "Text Editor",
	"number_integer": "Int",
	"number_decimal": "Float",
	"date": "Date",
	"date_time": "Datetime",
	"url": "Data",
	"json": "Code",
	"color": "Color",
	"money": "Currency",
	"weight": "Float",
	"dimension": "Float",
	"volume": "Float",
}


def _map_shopify_type_to_erpnext(shopify_type: str) -> str:
	return SHOPIFY_TYPE_MAP.get(shopify_type, "Data")


def setup_custom_fields():
	custom_fields = {
		"Item": [
			dict(
				fieldname=ITEM_SELLING_RATE_FIELD,
				label="Shopify Selling Rate",
				fieldtype="Currency",
				insert_after="standard_rate",
			)
		],
		"Customer": [
			dict(
				fieldname=CUSTOMER_ID_FIELD,
				label="Shopify Customer Id",
				fieldtype="Data",
				insert_after="series",
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname="shopify_metafields_data",
				label="Shopify Metafields",
				fieldtype="Code",
				insert_after=CUSTOMER_ID_FIELD,
				read_only=1,
				hidden=1,
				options="JSON",
			),
			dict(
				fieldname="shopify_custom_verification_status",
				label="Verification Status",
				fieldtype="Check",
				insert_after="shopify_metafields_data",
				read_only=1,
			),
			dict(
				fieldname="shopify_custom_veriification_url",
				label="Verification URL",
				fieldtype="Data",
				insert_after="shopify_custom_verification_status",
				read_only=1,
			),
		],
		"Supplier": [
			dict(
				fieldname=SUPPLIER_ID_FIELD,
				label="Shopify Supplier Id",
				fieldtype="Data",
				insert_after="supplier_name",
				read_only=1,
				print_hide=1,
			)
		],
		"Address": [
			dict(
				fieldname=ADDRESS_ID_FIELD,
				label="Shopify Address Id",
				fieldtype="Data",
				insert_after="fax",
				read_only=1,
				print_hide=1,
			)
		],
		"Sales Order": [
			dict(
				fieldname=ORDER_ID_FIELD,
				label="Shopify Order Id",
				fieldtype="Small Text",
				insert_after="title",
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_NUMBER_FIELD,
				label="Shopify Order Number",
				fieldtype="Small Text",
				insert_after=ORDER_ID_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_STATUS_FIELD,
				label="Shopify Order Status",
				fieldtype="Small Text",
				insert_after=ORDER_NUMBER_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname="shopify_medical_info_tab",
				label="Medical Info",
				fieldtype="Tab Break",
				insert_after=ORDER_STATUS_FIELD,
			),
			dict(
				fieldname="shopify_medical_info_html",
				label="Customer Medical Info",
				fieldtype="HTML",
				insert_after="shopify_medical_info_tab",
			),
		],
		"Sales Order Item": [
			dict(
				fieldname=ORDER_ITEM_DISCOUNT_FIELD,
				label="Shopify Discount per unit",
				fieldtype="Float",
				insert_after="discount_and_margin",
				read_only=1,
			),
		],
		"Delivery Note": [
			dict(
				fieldname=ORDER_ID_FIELD,
				label="Shopify Order Id",
				fieldtype="Small Text",
				insert_after="title",
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_NUMBER_FIELD,
				label="Shopify Order Number",
				fieldtype="Small Text",
				insert_after=ORDER_ID_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_STATUS_FIELD,
				label="Shopify Order Status",
				fieldtype="Small Text",
				insert_after=ORDER_NUMBER_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=FULLFILLMENT_ID_FIELD,
				label="Shopify Fulfillment Id",
				fieldtype="Small Text",
				insert_after="title",
				read_only=1,
				print_hide=1,
			),
		],
		"Sales Invoice": [
			dict(
				fieldname=ORDER_ID_FIELD,
				label="Shopify Order Id",
				fieldtype="Small Text",
				insert_after="title",
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_NUMBER_FIELD,
				label="Shopify Order Number",
				fieldtype="Small Text",
				insert_after=ORDER_ID_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_STATUS_FIELD,
				label="Shopify Order Status",
				fieldtype="Small Text",
				insert_after=ORDER_ID_FIELD,
				read_only=1,
				print_hide=1,
			),
		],
	}

	create_custom_fields(custom_fields)
