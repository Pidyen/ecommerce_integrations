# Copyright (c) 2021, Frappe and contributors
# For license information, please see LICENSE


MODULE_NAME = "shopify"
SETTING_DOCTYPE = "Shopify Setting"
OLD_SETTINGS_DOCTYPE = "Shopify Settings"

API_VERSION = "2026-01"

OAUTH_SCOPES = [
	"read_products",
	"write_products",
	"read_orders",
	"write_orders",
	"write_order_edits",
	"read_customers",
	"write_customers",
	"read_inventory",
	"write_inventory",
	"read_fulfillments",
	"write_fulfillments",
	"read_locations",
	"read_all_orders",
	"read_returns",
	"write_returns",
]

WEBHOOK_EVENTS = [
	"orders/create",
	"orders/paid",
	"orders/cancelled",
	"customers/create",
	"customers/update",
]

EVENT_MAPPER = {
	"orders/create": "ecommerce_integrations.shopify.order.sync_sales_order",
	"orders/paid": "ecommerce_integrations.shopify.invoice.prepare_sales_invoice",
	"orders/cancelled": "ecommerce_integrations.shopify.order.cancel_order",
	"customers/create": "ecommerce_integrations.shopify.customer.sync_customer_from_webhook",
	"customers/update": "ecommerce_integrations.shopify.customer.update_customer_from_webhook",
}

SHOPIFY_VARIANTS_ATTR_LIST = ["option1", "option2", "option3"]

# custom fields

CUSTOMER_ID_FIELD = "shopify_customer_id"
ORDER_ID_FIELD = "shopify_order_id"
ORDER_NUMBER_FIELD = "shopify_order_number"
ORDER_STATUS_FIELD = "shopify_order_status"
FULLFILLMENT_ID_FIELD = "shopify_fulfillment_id"
SUPPLIER_ID_FIELD = "shopify_supplier_id"
ADDRESS_ID_FIELD = "shopify_address_id"
ORDER_ITEM_DISCOUNT_FIELD = "shopify_item_discount"
ITEM_SELLING_RATE_FIELD = "shopify_selling_rate"
SHOPIFY_LINE_ITEM_ID_FIELD = "shopify_line_item_id"
SHOPIFY_VARIANT_ID_FIELD = "shopify_variant_id"

# ERPNext already defines the default UOMs from Shopify but names are different
WEIGHT_TO_ERPNEXT_UOM_MAP = {"kg": "Kg", "g": "Gram", "oz": "Ounce", "lb": "Pound"}
