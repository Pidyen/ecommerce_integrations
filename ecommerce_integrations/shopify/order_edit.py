"""Sync ERPNext Sales Order item changes back to the linked Shopify order.

Why child-level tracking:
ERPNext's "Update Items" on a submitted Sales Order goes through
erpnext.controllers.accounts_controller.update_child_qty_rate, which writes child
rows to the DB *before* the parent doc is saved. By the time the parent's
on_update_after_submit fires, doc.get_doc_before_save() already reflects the new
rows, so a parent-only diff is always empty. So we listen to Sales Order Item
events directly (after_insert / on_update_after_submit / on_trash), accumulate
the deltas in frappe.flags, and replay them when the parent finishes saving.

Note on event choice: submitted child docs' run_post_save_methods only fires
on_update_after_submit (not on_update), so qty edits must be hooked there.

Flow:
- Sales Order Item events accumulate adds / qty changes / removals per parent
- Sales Order on_update_after_submit reads the bucket and drives Shopify mutations:
    * new ERPNext line          -> orderEditAddVariant + orderEditCommit(notifyCustomer)
    * unfulfilled qty change    -> orderEditSetQuantity + orderEditCommit
    * unfulfilled item removed  -> orderEditSetQuantity(0) + orderEditCommit
    * fulfilled qty decrease    -> refundCreate (refund + restock + notify)

Re-entry from Shopify webhooks is guarded by frappe.flags.shopify_syncing_order.
"""

import json
import uuid

import frappe
import requests
from frappe import _
from frappe.utils import cint, cstr, flt

from ecommerce_integrations.shopify.constants import (
	API_VERSION,
	ORDER_ID_FIELD,
	SETTING_DOCTYPE,
	SHOPIFY_LINE_ITEM_ID_FIELD,
	SHOPIFY_VARIANT_ID_FIELD,
)
from ecommerce_integrations.shopify.utils import create_shopify_log

_BUCKET_FLAG = "shopify_so_item_changes"


def _is_submitted_shopify_sales_order(parent_name):
	"""Cheap DB lookup: parent SO is submitted AND has a shopify_order_id."""
	if not parent_name:
		return False
	row = frappe.db.get_value(
		"Sales Order", parent_name, ["docstatus", ORDER_ID_FIELD], as_dict=True
	)
	if not row:
		return False
	return cint(row.docstatus) == 1 and bool(row.get(ORDER_ID_FIELD))


def _bucket_for(parent_name):
	bucket = frappe.flags.get(_BUCKET_FLAG)
	if bucket is None:
		bucket = {}
		frappe.flags[_BUCKET_FLAG] = bucket
	return bucket.setdefault(parent_name, {"added": [], "qty_changed": [], "removed": []})


def track_so_item_added(doc, method=None):
	"""Sales Order Item after_insert hook -- record additions on submitted SOs."""
	if frappe.flags.shopify_syncing_order:
		return
	if doc.parenttype != "Sales Order":
		return
	if not _is_submitted_shopify_sales_order(doc.parent):
		return
	_bucket_for(doc.parent)["added"].append(
		{
			"row_name": doc.name,
			"item_code": doc.item_code,
			"qty": flt(doc.qty),
			SHOPIFY_VARIANT_ID_FIELD: doc.get(SHOPIFY_VARIANT_ID_FIELD),
		}
	)


def track_so_item_changed(doc, method=None):
	"""Sales Order Item on_update_after_submit hook -- record qty changes on submitted SOs."""
	if frappe.flags.shopify_syncing_order:
		return
	if doc.parenttype != "Sales Order":
		return
	if not _is_submitted_shopify_sales_order(doc.parent):
		return
	prev = doc.get_doc_before_save()
	if not prev:
		return
	if flt(prev.qty) == flt(doc.qty):
		return
	_bucket_for(doc.parent)["qty_changed"].append(
		{
			"row_name": doc.name,
			"item_code": doc.item_code,
			"old_qty": flt(prev.qty),
			"new_qty": flt(doc.qty),
			SHOPIFY_LINE_ITEM_ID_FIELD: doc.get(SHOPIFY_LINE_ITEM_ID_FIELD),
		}
	)


def track_so_item_removed(doc, method=None):
	"""Sales Order Item on_trash hook -- record removals on submitted SOs."""
	if frappe.flags.shopify_syncing_order:
		return
	if doc.parenttype != "Sales Order":
		return
	if not _is_submitted_shopify_sales_order(doc.parent):
		return
	_bucket_for(doc.parent)["removed"].append(
		{
			"row_name": doc.name,
			"item_code": doc.item_code,
			"qty": flt(doc.qty),
			SHOPIFY_LINE_ITEM_ID_FIELD: doc.get(SHOPIFY_LINE_ITEM_ID_FIELD),
		}
	)


def sync_so_changes_to_shopify(doc, method=None):
	"""Hook handler for Sales Order on_update_after_submit.

	Transactional behavior: NO logs are written to the DB before the Shopify call
	completes -- create_shopify_log internally calls frappe.db.commit(), which would
	prematurely commit the ERPNext SO changes and defeat rollback. On Shopify failure
	we frappe.throw() so that the entire SO update transaction (child inserts /
	deletes / qty changes) is rolled back by Frappe and the SO returns to its
	pre-edit state.
	"""
	method_name = "shopify.order_edit.sync_so_changes_to_shopify"

	if frappe.flags.shopify_syncing_order:
		return

	shopify_order_id = doc.get(ORDER_ID_FIELD)
	if not shopify_order_id:
		return

	setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	if not setting.is_enabled():
		return

	bucket = (frappe.flags.get(_BUCKET_FLAG) or {}).pop(doc.name, None)
	if not bucket or not (bucket["added"] or bucket["qty_changed"] or bucket["removed"]):
		return

	diff_summary = {
		"so_name": doc.name,
		"shopify_order_id": shopify_order_id,
		"added": bucket["added"],
		"qty_changed": bucket["qty_changed"],
		"removed": bucket["removed"],
	}

	frappe.flags.shopify_syncing_order = True
	try:
		_apply_edits_from_bucket(shopify_order_id, bucket, setting, doc.name)
	except Exception as e:
		frappe.flags.shopify_syncing_order = False
		# Queue a failure log to redis so it survives the upcoming rollback.
		# enqueue_after_commit=False ensures it runs in a fresh transaction even if
		# the current one rolls back.
		try:
			frappe.enqueue(
				method=_log_failure_async,
				queue="short",
				enqueue_after_commit=False,
				diff_summary=diff_summary,
				error_message=str(e),
				traceback=frappe.get_traceback(),
			)
		except Exception:
			pass
		# Re-raise so Frappe rolls back ALL the SO/child changes from this request.
		frappe.throw(
			_("Shopify sync failed for SO {0}: {1}. Sales Order changes have been reverted.").format(
				doc.name, str(e)
			),
			title=_("Shopify Sync Failed"),
		)
	frappe.flags.shopify_syncing_order = False

	# Success path: now safe to commit a Success log (SO changes are consistent).
	create_shopify_log(
		status="Success",
		method=method_name,
		message=f"SO {doc.name} edits synced to Shopify order {shopify_order_id}",
		request_data=diff_summary,
		make_new=True,
	)


def _log_failure_async(diff_summary, error_message, traceback):
	"""Run by RQ worker after the failing transaction has rolled back."""
	create_shopify_log(
		status="Error",
		method="shopify.order_edit.sync_so_changes_to_shopify",
		message=f"Shopify sync failed and was reverted: {error_message}",
		request_data=diff_summary,
		response_data={"traceback": traceback},
		make_new=True,
	)


def _apply_edits_from_bucket(order_id, bucket, setting, so_name):
	"""Drive Shopify mutations from a tracked-changes bucket (list of plain dicts).

	- new ERPNext rows -> orderEditAddVariant (allowed even on fulfilled orders)
	- qty change / remove on an unfulfilled line -> orderEditSetQuantity
	- qty decrease / remove on a fulfilled line  -> refundCreate (refund + restock)
	"""
	gid = _to_order_gid(order_id)

	fulfillment_map = _get_line_item_fulfillment_map(gid, setting)

	additions = list(bucket["added"])
	non_fulfilled_qty_changes = []
	non_fulfilled_removals = []
	fulfilled_decreases = []
	skipped_legacy_rows = []

	for entry in bucket["qty_changed"]:
		lid = entry.get(SHOPIFY_LINE_ITEM_ID_FIELD)
		if not lid:
			skipped_legacy_rows.append(("qty_changed", entry.get("item_code")))
			continue
		is_fulfilled = fulfillment_map.get(lid, False)
		if is_fulfilled and flt(entry["new_qty"]) < flt(entry["old_qty"]):
			fulfilled_decreases.append(entry)
		else:
			non_fulfilled_qty_changes.append(entry)

	for entry in bucket["removed"]:
		lid = entry.get(SHOPIFY_LINE_ITEM_ID_FIELD)
		if not lid:
			skipped_legacy_rows.append(("removed", entry.get("item_code")))
			continue
		if fulfillment_map.get(lid, False):
			fulfilled_decreases.append({**entry, "old_qty": entry["qty"], "new_qty": 0})
		else:
			non_fulfilled_removals.append(entry)

	# NOTE: any create_shopify_log() call here would commit the SO changes and
	# defeat the rollback path. Skipped rows are tolerated silently; the final
	# Success log on the parent handler captures the full diff.

	if additions or non_fulfilled_qty_changes or non_fulfilled_removals:
		_run_order_edit_session(
			gid,
			setting,
			additions=additions,
			qty_changes=non_fulfilled_qty_changes,
			removals=non_fulfilled_removals,
		)
		if additions:
			_backfill_added_line_items(so_name, additions, gid, setting)

	if fulfilled_decreases:
		_create_refund_for_fulfilled_decreases(order_id, fulfilled_decreases, setting)


def _run_order_edit_session(order_gid, setting, additions, qty_changes, removals):
	"""Begin -> stage edits -> commit. notifyCustomer=true triggers the unpaid-balance invoice."""
	begin_resp = _graphql(
		setting,
		"""
		mutation orderEditBegin($id: ID!) {
			orderEditBegin(id: $id) {
				calculatedOrder { id }
				userErrors { field message }
			}
		}
		""",
		{"id": order_gid},
	)
	_check_user_errors(begin_resp, "orderEditBegin")
	calculated_order_id = begin_resp["data"]["orderEditBegin"]["calculatedOrder"]["id"]

	for entry in additions:
		variant_id = entry.get(SHOPIFY_VARIANT_ID_FIELD) or _resolve_variant_id(entry.get("item_code"))
		if not variant_id:
			frappe.throw(f"Cannot add item {entry.get('item_code')} to Shopify order: missing variant id")
		variant_gid = _to_variant_gid(variant_id)
		add_resp = _graphql(
			setting,
			"""
			mutation orderEditAddVariant($id: ID!, $variantId: ID!, $quantity: Int!) {
				orderEditAddVariant(id: $id, variantId: $variantId, quantity: $quantity) {
					calculatedOrder { id }
					userErrors { field message }
				}
			}
			""",
			{"id": calculated_order_id, "variantId": variant_gid, "quantity": cint(entry.get("qty"))},
		)
		_check_user_errors(add_resp, "orderEditAddVariant")

	for entry in qty_changes:
		line_gid = _to_calculated_line_item_gid(entry.get(SHOPIFY_LINE_ITEM_ID_FIELD))
		_set_quantity(setting, calculated_order_id, line_gid, cint(entry.get("new_qty")))

	for entry in removals:
		line_gid = _to_calculated_line_item_gid(entry.get(SHOPIFY_LINE_ITEM_ID_FIELD))
		_set_quantity(setting, calculated_order_id, line_gid, 0)

	commit_resp = _graphql(
		setting,
		"""
		mutation orderEditCommit($id: ID!, $notifyCustomer: Boolean, $staffNote: String) {
			orderEditCommit(id: $id, notifyCustomer: $notifyCustomer, staffNote: $staffNote) {
				order { id }
				userErrors { field message }
			}
		}
		""",
		{
			"id": calculated_order_id,
			"notifyCustomer": True,
			"staffNote": "Edited from ERPNext Sales Order",
		},
	)
	_check_user_errors(commit_resp, "orderEditCommit")


def _set_quantity(setting, calculated_order_id, line_gid, new_qty):
	resp = _graphql(
		setting,
		"""
		mutation orderEditSetQuantity($id: ID!, $lineItemId: ID!, $quantity: Int!, $restock: Boolean) {
			orderEditSetQuantity(id: $id, lineItemId: $lineItemId, quantity: $quantity, restock: $restock) {
				calculatedOrder { id }
				userErrors { field message }
			}
		}
		""",
		{"id": calculated_order_id, "lineItemId": line_gid, "quantity": new_qty, "restock": True},
	)
	_check_user_errors(resp, "orderEditSetQuantity")


def _create_refund_for_fulfilled_decreases(order_id, decreases, setting):
	"""Build refundLineInput list and call refundCreate to refund (and optionally restock).

	Caps each line's refund qty at Shopify's reported refundableQuantity to avoid
	"Quantity cannot refund more items than were purchased". If a Shopify location is
	configured in shopify_warehouse_mapping, restocks via RETURN with that location;
	otherwise falls back to NO_RESTOCK (refund money only).
	"""
	order_gid = _to_order_gid(order_id)
	refundable_map = _get_refundable_qty_map(order_gid, setting)
	location_id = _get_default_shopify_location_id(setting)
	restock_type = "RETURN" if location_id else "NO_RESTOCK"

	refund_lines = []
	skipped = []
	for entry in decreases:
		line_id = cstr(entry.get(SHOPIFY_LINE_ITEM_ID_FIELD))
		requested = cint(entry.get("old_qty")) - cint(entry.get("new_qty") or 0)
		if requested <= 0:
			continue
		max_refundable = refundable_map.get(line_id, requested)
		actual_qty = min(requested, max_refundable)
		if actual_qty <= 0:
			skipped.append({"line_id": line_id, "reason": "refundableQuantity is 0"})
			continue
		refund_line = {
			"lineItemId": _to_line_item_gid(line_id),
			"quantity": actual_qty,
			"restockType": restock_type,
		}
		if restock_type == "RETURN":
			refund_line["locationId"] = _to_location_gid(location_id)
		refund_lines.append(refund_line)

	# NOTE: deliberate -- no commit-causing log calls before refundCreate succeeds.
	if not refund_lines:
		return

	resp = _graphql(
		setting,
		"""
		mutation refundCreate($input: RefundInput!) {
			refundCreate(input: $input) {
				refund { id }
				userErrors { field message }
			}
		}
		""",
		{
			"input": {
				"orderId": order_gid,
				"notify": True,
				"note": "Refund created from ERPNext Sales Order edit",
				"refundLineItems": refund_lines,
			}
		},
	)
	_check_user_errors(resp, "refundCreate")


def _get_refundable_qty_map(order_gid, setting):
	resp = _graphql(
		setting,
		"""
		query orderRefundable($id: ID!) {
			order(id: $id) {
				lineItems(first: 250) {
					edges {
						node {
							id
							refundableQuantity
						}
					}
				}
			}
		}
		""",
		{"id": order_gid},
	)
	mapping = {}
	order = (resp.get("data") or {}).get("order") or {}
	for edge in (order.get("lineItems") or {}).get("edges") or []:
		node = edge.get("node") or {}
		line_id = (node.get("id") or "").rsplit("/", 1)[-1]
		mapping[line_id] = cint(node.get("refundableQuantity"))
	return mapping


def _get_default_shopify_location_id(setting):
	"""Pick the first configured Shopify location from the warehouse mapping table."""
	for row in setting.get("shopify_warehouse_mapping") or []:
		if row.get("shopify_location_id"):
			return cstr(row.get("shopify_location_id"))
	return None


def _to_location_gid(location_id):
	location_id = cstr(location_id)
	if location_id.startswith("gid://"):
		return location_id
	return f"gid://shopify/Location/{location_id}"


def _backfill_added_line_items(so_name, additions, order_gid, setting):
	"""After an order edit commit, query the order and assign Shopify line item ids
	to the ERPNext SO Item rows we just added, so the next save's diff stays correct.

	Matching is by variant id; if multiple ERPNext rows target the same variant, the
	largest-qty Shopify line is preferred for each ERPNext row (best-effort).
	"""
	resp = _graphql(
		setting,
		"""
		query orderLineItems($id: ID!) {
			order(id: $id) {
				lineItems(first: 250) {
					edges {
						node {
							id
							quantity
							variant { id }
						}
					}
				}
			}
		}
		""",
		{"id": order_gid},
	)
	order = (resp.get("data") or {}).get("order") or {}
	shopify_lines = []
	for edge in (order.get("lineItems") or {}).get("edges") or []:
		node = edge.get("node") or {}
		variant_gid = (node.get("variant") or {}).get("id") or ""
		shopify_lines.append(
			{
				"line_id": (node.get("id") or "").rsplit("/", 1)[-1],
				"variant_id": variant_gid.rsplit("/", 1)[-1],
				"quantity": cint(node.get("quantity")),
			}
		)

	already_used = {row.get(SHOPIFY_LINE_ITEM_ID_FIELD) for row in frappe.get_all(
		"Sales Order Item",
		filters={"parent": so_name},
		fields=[SHOPIFY_LINE_ITEM_ID_FIELD],
	) if row.get(SHOPIFY_LINE_ITEM_ID_FIELD)}

	for entry in additions:
		variant_id = cstr(entry.get(SHOPIFY_VARIANT_ID_FIELD) or _resolve_variant_id(entry.get("item_code")))
		if not variant_id:
			continue
		candidates = [s for s in shopify_lines if s["variant_id"] == variant_id and s["line_id"] not in already_used]
		if not candidates:
			continue
		match = max(candidates, key=lambda s: s["quantity"])
		frappe.db.set_value(
			"Sales Order Item",
			entry["row_name"],
			{
				SHOPIFY_LINE_ITEM_ID_FIELD: match["line_id"],
				SHOPIFY_VARIANT_ID_FIELD: match["variant_id"],
			},
			update_modified=False,
		)
		already_used.add(match["line_id"])


def _get_line_item_fulfillment_map(order_gid, setting):
	"""Return {line_item_id_str: is_fulfilled_bool} for the given Shopify order."""
	resp = _graphql(
		setting,
		"""
		query orderLineItems($id: ID!) {
			order(id: $id) {
				lineItems(first: 250) {
					edges {
						node {
							id
							quantity
							unfulfilledQuantity
						}
					}
				}
			}
		}
		""",
		{"id": order_gid},
	)
	mapping = {}
	order = (resp.get("data") or {}).get("order") or {}
	for edge in (order.get("lineItems") or {}).get("edges") or []:
		node = edge.get("node") or {}
		line_gid = node.get("id") or ""
		line_id = line_gid.rsplit("/", 1)[-1]
		mapping[line_id] = cint(node.get("unfulfilledQuantity")) == 0 and cint(node.get("quantity")) > 0
	return mapping


def _resolve_variant_id(item_code):
	"""Look up the Shopify variant id for an ERPNext item via Ecommerce Item."""
	from ecommerce_integrations.shopify.constants import MODULE_NAME

	row = frappe.db.get_value(
		"Ecommerce Item",
		{"erpnext_item_code": item_code, "integration": MODULE_NAME},
		["variant_id", "integration_item_code"],
		as_dict=True,
	)
	if not row:
		return None
	return row.variant_id or row.integration_item_code


def _to_order_gid(order_id):
	order_id = cstr(order_id)
	if order_id.startswith("gid://"):
		return order_id
	return f"gid://shopify/Order/{order_id}"


def _to_line_item_gid(line_item_id):
	line_item_id = cstr(line_item_id)
	if line_item_id.startswith("gid://"):
		return line_item_id
	return f"gid://shopify/LineItem/{line_item_id}"


def _to_calculated_line_item_gid(line_item_id):
	"""Inside an order edit session, line items are CalculatedLineItems with the same
	numeric id but a different type prefix. Used by orderEditSetQuantity."""
	line_item_id = cstr(line_item_id)
	if line_item_id.startswith("gid://shopify/CalculatedLineItem/"):
		return line_item_id
	if line_item_id.startswith("gid://shopify/LineItem/"):
		return line_item_id.replace("gid://shopify/LineItem/", "gid://shopify/CalculatedLineItem/")
	if line_item_id.startswith("gid://"):
		return line_item_id
	return f"gid://shopify/CalculatedLineItem/{line_item_id}"


def _to_variant_gid(variant_id):
	variant_id = cstr(variant_id)
	if variant_id.startswith("gid://"):
		return variant_id
	return f"gid://shopify/ProductVariant/{variant_id}"


def _graphql(setting, query, variables):
	shopify_url = setting.shopify_url.rstrip("/")
	url = f"https://{shopify_url}/admin/api/{API_VERSION}/graphql.json"
	headers = {
		"X-Shopify-Access-Token": setting.get_password("access_token"),
		"Content-Type": "application/json",
		"Idempotency-Key": str(uuid.uuid4()),
	}
	response = requests.post(url, headers=headers, json={"query": query, "variables": variables})
	if response.status_code != 200:
		raise Exception(f"Shopify GraphQL HTTP {response.status_code}: {response.text}")
	body = response.json()
	if body.get("errors"):
		raise Exception(f"Shopify GraphQL errors: {json.dumps(body['errors'])}")
	return body


def _check_user_errors(resp, mutation_name):
	payload = (resp.get("data") or {}).get(mutation_name) or {}
	errors = payload.get("userErrors") or []
	if errors:
		raise Exception(f"{mutation_name} userErrors: {json.dumps(errors)}")


@frappe.whitelist()
def get_shopify_order_action_state(sales_order):
	"""Return which Shopify action buttons should be shown for this Sales Order."""
	so = frappe.get_doc("Sales Order", sales_order)
	so.check_permission("read")

	if so.docstatus != 1:
		return {"show_refund": False, "show_resend_invoice": False, "reason": "SO not submitted"}

	order_id = so.get(ORDER_ID_FIELD)
	if not order_id:
		return {"show_refund": False, "show_resend_invoice": False, "reason": "No Shopify order id"}

	setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	if not setting.is_enabled():
		return {"show_refund": False, "show_resend_invoice": False, "reason": "Shopify disabled"}

	order_data = _get_order_action_facts(_to_order_gid(order_id), setting)
	refundable_qty = cint(order_data.get("refundable_qty"))
	outstanding_amount = flt(order_data.get("outstanding_amount"))
	show_resend_invoice = outstanding_amount > 0
	# Keep actions mutually exclusive in UI:
	# if customer owes money, prioritize resend invoice; otherwise show refund.
	show_refund = not show_resend_invoice and refundable_qty > 0

	return {
		"show_refund": show_refund,
		"show_resend_invoice": show_resend_invoice,
		"refundable_qty": refundable_qty,
		"outstanding_amount": outstanding_amount,
		"currency": order_data.get("currency"),
		"display_financial_status": order_data.get("display_financial_status"),
	}


@frappe.whitelist()
def trigger_shopify_resend_invoice(sales_order):
	"""Trigger Shopify order invoice resend for the linked Sales Order."""
	so = frappe.get_doc("Sales Order", sales_order)
	so.check_permission("submit")

	if so.docstatus != 1:
		frappe.throw(_("Only submitted Sales Orders are supported."))

	order_id = so.get(ORDER_ID_FIELD)
	if not order_id:
		frappe.throw(_("Sales Order is not linked with a Shopify order."))

	setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	if not setting.is_enabled():
		frappe.throw(_("Shopify integration is disabled."))

	order_gid = _to_order_gid(order_id)
	order_data = _get_order_action_facts(order_gid, setting)
	if flt(order_data.get("outstanding_amount")) <= 0:
		frappe.throw(_("This Shopify order has no outstanding amount to invoice."))

	resp = _graphql(
		setting,
		"""
		mutation orderInvoiceSend($id: ID!) {
			orderInvoiceSend(id: $id) {
				order {
					id
				}
				userErrors {
					field
					message
				}
			}
		}
		""",
		{"id": order_gid},
	)
	_check_user_errors(resp, "orderInvoiceSend")

	create_shopify_log(
		status="Success",
		method="shopify.order_edit.trigger_shopify_resend_invoice",
		message=f"Invoice resend triggered from SO {so.name} for Shopify order {order_id}",
		request_data={"sales_order": so.name, "shopify_order_id": order_id},
		make_new=True,
	)
	return {"ok": True}


@frappe.whitelist()
def trigger_shopify_refund(sales_order):
	"""Trigger a full refundable Shopify refund for the linked Sales Order."""
	so = frappe.get_doc("Sales Order", sales_order)
	so.check_permission("submit")

	if so.docstatus != 1:
		frappe.throw(_("Only submitted Sales Orders are supported."))

	order_id = so.get(ORDER_ID_FIELD)
	if not order_id:
		frappe.throw(_("Sales Order is not linked with a Shopify order."))

	setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	if not setting.is_enabled():
		frappe.throw(_("Shopify integration is disabled."))

	order_gid = _to_order_gid(order_id)
	refundable_lines = _get_refundable_lines(order_gid, setting)
	if not refundable_lines:
		frappe.throw(_("No refundable quantity found on Shopify order."))

	location_id = _get_default_shopify_location_id(setting)
	restock_type = "RETURN" if location_id else "NO_RESTOCK"
	refund_lines = []
	for line in refundable_lines:
		payload = {
			"lineItemId": _to_line_item_gid(line["line_id"]),
			"quantity": line["refundable_qty"],
			"restockType": restock_type,
		}
		if location_id:
			payload["locationId"] = _to_location_gid(location_id)
		refund_lines.append(payload)

	resp = _graphql(
		setting,
		"""
		mutation refundCreate($input: RefundInput!) {
			refundCreate(input: $input) {
				refund {
					id
				}
				userErrors {
					field
					message
				}
			}
		}
		""",
		{
			"input": {
				"orderId": order_gid,
				"notify": True,
				"note": "Manual refund triggered from ERPNext Sales Order",
				"refundLineItems": refund_lines,
			}
		},
	)
	_check_user_errors(resp, "refundCreate")

	create_shopify_log(
		status="Success",
		method="shopify.order_edit.trigger_shopify_refund",
		message=f"Refund triggered from SO {so.name} for Shopify order {order_id}",
		request_data={"sales_order": so.name, "shopify_order_id": order_id, "refund_lines": refund_lines},
		make_new=True,
	)
	return {"ok": True}


def _get_order_action_facts(order_gid, setting):
	resp = _graphql(
		setting,
		"""
		query orderActionState($id: ID!) {
			order(id: $id) {
				id
				displayFinancialStatus
				totalOutstandingSet {
					shopMoney {
						amount
						currencyCode
					}
				}
				lineItems(first: 250) {
					edges {
						node {
							refundableQuantity
						}
					}
				}
			}
		}
		""",
		{"id": order_gid},
	)
	order = (resp.get("data") or {}).get("order") or {}
	outstanding_shop_money = (order.get("totalOutstandingSet") or {}).get("shopMoney") or {}
	refundable_qty = 0
	for edge in (order.get("lineItems") or {}).get("edges") or []:
		node = edge.get("node") or {}
		refundable_qty += cint(node.get("refundableQuantity"))

	return {
		"refundable_qty": refundable_qty,
		"outstanding_amount": flt(outstanding_shop_money.get("amount")),
		"currency": outstanding_shop_money.get("currencyCode"),
		"display_financial_status": order.get("displayFinancialStatus"),
	}


def _get_refundable_lines(order_gid, setting):
	resp = _graphql(
		setting,
		"""
		query orderRefundableLines($id: ID!) {
			order(id: $id) {
				lineItems(first: 250) {
					edges {
						node {
							id
							refundableQuantity
						}
					}
				}
			}
		}
		""",
		{"id": order_gid},
	)
	lines = []
	order = (resp.get("data") or {}).get("order") or {}
	for edge in (order.get("lineItems") or {}).get("edges") or []:
		node = edge.get("node") or {}
		qty = cint(node.get("refundableQuantity"))
		if qty <= 0:
			continue
		line_gid = cstr(node.get("id"))
		line_id = line_gid.rsplit("/", 1)[-1]
		if not line_id:
			continue
		lines.append({"line_id": line_id, "refundable_qty": qty})
	return lines
