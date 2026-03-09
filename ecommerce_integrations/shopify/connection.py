import base64
import functools
import hashlib
import hmac
import json

import frappe
from frappe import _
from shopify.resources import Webhook
from shopify.session import Session

from ecommerce_integrations.shopify.constants import (
	API_VERSION,
	EVENT_MAPPER,
	OAUTH_SCOPES,
	SETTING_DOCTYPE,
	WEBHOOK_EVENTS,
)
from ecommerce_integrations.shopify.utils import create_shopify_log


def temp_shopify_session(func):
	"""Any function that needs to access shopify api needs this decorator. The decorator starts a temp session that's destroyed when function returns."""

	@functools.wraps(func)
	def wrapper(*args, **kwargs):
		# no auth in testing
		if frappe.flags.in_test:
			return func(*args, **kwargs)

		setting = frappe.get_doc(SETTING_DOCTYPE)
		if setting.is_enabled():
			auth_details = (setting.shopify_url, API_VERSION, setting.get_password("access_token"))

			with Session.temp(*auth_details):
				return func(*args, **kwargs)

	return wrapper


def register_webhooks(shopify_url: str, access_token: str) -> list[Webhook]:
	"""Register required webhooks with shopify and return registered webhooks."""
	new_webhooks = []

	# clear all stale webhooks matching current site url before registering new ones
	unregister_webhooks(shopify_url, access_token)

	with Session.temp(shopify_url, API_VERSION, access_token):
		for topic in WEBHOOK_EVENTS:
			webhook = Webhook.create({"topic": topic, "address": get_callback_url(), "format": "json"})

			if webhook.is_valid():
				new_webhooks.append(webhook)
			else:
				create_shopify_log(
					status="Error",
					response_data=webhook.to_dict(),
					exception=webhook.errors.full_messages(),
				)

	return new_webhooks


def unregister_webhooks(shopify_url: str, access_token: str) -> None:
	"""Unregister all webhooks from shopify that correspond to current site url."""
	url = get_current_domain_name()

	with Session.temp(shopify_url, API_VERSION, access_token):
		for webhook in Webhook.find():
			if url in webhook.address:
				webhook.destroy()


def get_current_domain_name() -> str:
	"""Get current site domain name. E.g. test.erpnext.com

	If developer_mode is enabled and localtunnel_url is set in site config then domain  is set to localtunnel_url.
	"""
	if frappe.conf.developer_mode and frappe.conf.localtunnel_url:
		return frappe.conf.localtunnel_url
	else:
		return frappe.request.host


def get_callback_url() -> str:
	"""Shopify calls this url when new events occur to subscribed webhooks.

	If developer_mode is enabled and localtunnel_url is set in site config then callback url is set to localtunnel_url.
	"""
	url = get_current_domain_name()

	return f"https://{url}/api/method/ecommerce_integrations.shopify.connection.store_request_data"


def get_oauth_redirect_uri() -> str:
	"""Build the OAuth redirect URI pointing back to this site."""
	url = get_current_domain_name()
	return f"https://{url}/api/method/ecommerce_integrations.shopify.connection.oauth_callback"


@frappe.whitelist()
def initiate_oauth():
	"""Start OAuth flow by redirecting user to Shopify authorization page."""
	setting = frappe.get_doc(SETTING_DOCTYPE)

	if not setting.shopify_url or not setting.api_key:
		frappe.throw(_("Shop URL and API Key are required to start OAuth."))

	api_key = setting.api_key
	client_secret = setting.get_password("client_secret")

	if not client_secret:
		frappe.throw(_("Client Secret is required to start OAuth."))

	Session.setup(api_key=api_key, secret=client_secret)

	shopify_url = setting.shopify_url.rstrip("/")
	session = Session(f"https://{shopify_url}", API_VERSION)

	redirect_uri = get_oauth_redirect_uri()
	permission_url = session.create_permission_url(OAUTH_SCOPES, redirect_uri)

	frappe.response["type"] = "redirect"
	frappe.response["location"] = permission_url


@frappe.whitelist(allow_guest=True)
def oauth_callback():
	"""Handle OAuth callback from Shopify after merchant approves the app."""
	params = frappe.request.args

	setting = frappe.get_doc(SETTING_DOCTYPE)

	Session.setup(api_key=setting.api_key, secret=setting.get_password("client_secret"))

	shopify_url = setting.shopify_url.rstrip("/")
	session = Session(f"https://{shopify_url}", API_VERSION)

	# request_token validates HMAC and exchanges the authorization code for a permanent offline token
	access_token = session.request_token(params)

	# Save the token
	setting.access_token = access_token
	setting.authorization_status = "Connected"
	setting.flags.ignore_validate = True
	setting.save(ignore_permissions=True)

	# Register webhooks now that we have a valid token
	new_webhooks = register_webhooks(shopify_url, access_token)
	if new_webhooks:
		for webhook in new_webhooks:
			setting.append("webhooks", {"webhook_id": webhook.id, "method": webhook.topic})
		setting.flags.ignore_validate = True
		setting.save(ignore_permissions=True)

	frappe.db.commit()

	frappe.response["type"] = "redirect"
	frappe.response["location"] = "/app/shopify-setting"


@frappe.whitelist(allow_guest=True)
def store_request_data() -> None:
	if frappe.request:
		hmac_header = frappe.get_request_header("X-Shopify-Hmac-Sha256")

		_validate_request(frappe.request, hmac_header)

		data = json.loads(frappe.request.data)
		event = frappe.request.headers.get("X-Shopify-Topic")

		process_request(data, event)


def process_request(data, event):
	# create log
	log = create_shopify_log(method=EVENT_MAPPER[event], request_data=data)

	# enqueue backround job
	frappe.enqueue(
		method=EVENT_MAPPER[event],
		queue="short",
		timeout=300,
		is_async=True,
		**{"payload": data, "request_id": log.name},
	)


def _validate_request(req, hmac_header):
	settings = frappe.get_doc(SETTING_DOCTYPE)
	secret_key = settings.get_password("client_secret")

	sig = base64.b64encode(hmac.new(secret_key.encode("utf8"), req.data, hashlib.sha256).digest())

	if sig != bytes(hmac_header.encode()):
		create_shopify_log(status="Error", request_data=req.data)
		frappe.throw(_("Unverified Webhook Data"))
