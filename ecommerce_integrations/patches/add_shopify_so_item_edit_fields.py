import frappe

from ecommerce_integrations.shopify.constants import SETTING_DOCTYPE
from ecommerce_integrations.shopify.doctype.shopify_setting.shopify_setting import setup_custom_fields


def execute():
	if not frappe.db.exists("DocType", SETTING_DOCTYPE):
		return
	# always (re)run field setup; create_custom_fields is idempotent and the new
	# shopify_line_item_id / shopify_variant_id fields are required for the Sales
	# Order edit-sync flow regardless of whether shopify is currently enabled.
	setup_custom_fields()
	frappe.db.commit()
