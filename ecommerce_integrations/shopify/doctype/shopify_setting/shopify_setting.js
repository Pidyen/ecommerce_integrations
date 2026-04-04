// Copyright (c) 2021, Frappe and contributors
// For license information, please see LICENSE

frappe.provide("ecommerce_integrations.shopify.shopify_setting");

frappe.ui.form.on("Shopify Setting", {
	onload: function (frm) {
		frappe.call({
			method: "ecommerce_integrations.utils.naming_series.get_series",
			callback: function (r) {
				$.each(r.message, (key, value) => {
					set_field_options(key, value);
				});
			},
		});
	},

	fetch_shopify_locations: function (frm) {
		frappe.call({
			doc: frm.doc,
			method: "update_location_table",
			callback: (r) => {
				if (!r.exc) refresh_field("shopify_warehouse_mapping");
			},
		});
	},

	refresh: function (frm) {
		// Connection status indicator
		if (frm.doc.authorization_status === "Connected") {
			frm.dashboard.set_headline(__("Connected to Shopify"), "green");
		}

		// OAuth Connect button — shown when not yet connected
		if (frm.doc.authorization_status !== "Connected") {
			frm.add_custom_button(
				__("Connect to Shopify"),
				function () {
					if (!frm.doc.shopify_url || !frm.doc.api_key) {
						frappe.msgprint(
							__("Please fill in Shop URL, API Key, and Client Secret first, then save.")
						);
						return;
					}
					const redirect = () => {
						window.location.href =
							"/api/method/ecommerce_integrations.shopify.connection.initiate_oauth";
					};
					if (frm.dirty()) {
						frm.save().then(redirect);
					} else {
						redirect();
					}
				},
				__("Shopify")
			);
		}

		// Register Webhooks button
		if (frm.doc.authorization_status === "Connected" || frm.doc.access_token) {
			frm.add_custom_button(
				__("Register Webhooks"),
				function () {
					frappe.call({
						doc: frm.doc,
						method: "register_webhooks_manual",
						freeze: true,
						freeze_message: __("Registering webhooks..."),
						callback: (r) => {
							if (!r.exc) {
								frm.reload_doc();
							}
						},
					});
				},
				__("Shopify")
			);
		}

		// Fetch All Webhooks button
	if (frm.doc.enable_shopify && frm.doc.access_token) {
		frm.add_custom_button(
			__("Fetch All Webhooks"),
			function () {
				frappe.call({
					doc: frm.doc,
					method: "fetch_all_webhooks",
					freeze: true,
					freeze_message: __("Fetching webhooks from Shopify..."),
					callback: (r) => {
						if (r.message) {
							show_webhooks_dialog(r.message);
						}
					},
				});
			},
			__("Shopify")
		);
	}

	// Sync Customer Metafield Definitions button
	if (frm.doc.enable_shopify && frm.doc.access_token) {
		frm.add_custom_button(
			__("Sync Customer Metafields"),
			function () {
				frappe.call({
					doc: frm.doc,
					method: "fetch_customer_metafields",
					freeze: true,
					freeze_message: __(
						"Fetching metafield definitions from Shopify..."
					),
					callback: (r) => {
						if (r.message && r.message.length) {
							show_metafields_dialog(r.message);
						}
					},
				});
			},
			__("Shopify")
		);
	}

	frm.add_custom_button(__("Import Products"), function () {
			frappe.set_route("shopify-import-products");
		});
		frm.add_custom_button(__("View Logs"), () => {
			frappe.set_route("List", "Ecommerce Integration Log", {
				integration: "Shopify",
			});
		});
		frm.trigger("setup_queries");
	},

	setup_queries: function (frm) {
		const warehouse_query = () => {
			return {
				filters: {
					company: frm.doc.company,
					is_group: 0,
					disabled: 0,
				},
			};
		};
		frm.set_query("warehouse", warehouse_query);
		frm.set_query(
			"erpnext_warehouse",
			"shopify_warehouse_mapping",
			warehouse_query,
		);

		frm.set_query("price_list", () => {
			return {
				filters: {
					selling: 1,
				},
			};
		});

		frm.set_query("cost_center", () => {
			return {
				filters: {
					company: frm.doc.company,
					is_group: "No",
				},
			};
		});

		frm.set_query("cash_bank_account", () => {
			return {
				filters: [
					["Account", "account_type", "in", ["Cash", "Bank"]],
					["Account", "root_type", "=", "Asset"],
					["Account", "is_group", "=", 0],
					["Account", "company", "=", frm.doc.company],
				],
			};
		});

		const tax_query = () => {
			return {
				query: "erpnext.controllers.queries.tax_account_query",
				filters: {
					account_type: ["Tax", "Chargeable", "Expense Account"],
					company: frm.doc.company,
				},
			};
		};

		frm.set_query("tax_account", "taxes", tax_query);
		frm.set_query("default_sales_tax_account", tax_query);
		frm.set_query("default_shipping_charges_account", tax_query);
	},
});

function show_webhooks_dialog(data) {
	let html = `<div style="margin-bottom:10px;">
		<strong>${__("Current Site")}:</strong> <code>${data.current_site}</code><br>
		<strong>${__("Total Webhooks on Shopify")}:</strong> ${data.total_webhooks}
	</div>`;

	if (!data.webhooks.length) {
		html += `<div class="text-muted">${__("No webhooks found on Shopify store.")}</div>`;
	} else {
		html += `<table class="table table-bordered table-sm" style="font-size:12px;">
			<thead><tr>
				<th>${__("Topic")}</th>
				<th>${__("Callback URL")}</th>
				<th>${__("This Site?")}</th>
				<th>${__("Webhook ID")}</th>
				<th>${__("Created")}</th>
			</tr></thead><tbody>`;

		data.webhooks.forEach((wh) => {
			const is_current = wh.address.includes(data.current_site);
			const badge = is_current
				? `<span class="badge badge-success" style="background:green;color:white;">Yes</span>`
				: `<span class="badge badge-danger" style="background:red;color:white;">No - Other Site</span>`;

			html += `<tr style="${!is_current ? 'background:#fff3cd;' : ''}">
				<td><strong>${wh.topic}</strong></td>
				<td style="word-break:break-all;">${wh.address}</td>
				<td>${badge}</td>
				<td>${wh.id}</td>
				<td>${wh.created_at || ""}</td>
			</tr>`;
		});

		html += `</tbody></table>`;

		// Summary
		const other_site_hooks = data.webhooks.filter(
			(wh) => !wh.address.includes(data.current_site)
		);
		if (other_site_hooks.length) {
			html += `<div class="alert alert-warning" style="margin-top:10px;">
				<strong>${__("Conflict Detected!")}</strong> ${other_site_hooks.length}
				${__("webhook(s) pointing to other site(s). This may cause issues.")}
			</div>`;
		}
	}

	let d = new frappe.ui.Dialog({
		title: __("Shopify Webhooks ({0})", [data.total_webhooks]),
		size: "extra-large",
	});

	d.$body.html(html);
	d.show();
}

function show_metafields_dialog(fields) {
	let html = `<table class="table table-bordered table-sm" style="font-size:12px;">
		<thead><tr>
			<th>${__("Label")}</th>
			<th>${__("Namespace")}</th>
			<th>${__("Key")}</th>
			<th>${__("Shopify Type")}</th>
		</tr></thead><tbody>`;

	fields.forEach((f) => {
		html += `<tr>
			<td><strong>${f.label}</strong></td>
			<td><code>${f.namespace}</code></td>
			<td><code>${f.key}</code></td>
			<td>${f.type || ""}</td>
		</tr>`;
	});

	html += `</tbody></table>`;

	let d = new frappe.ui.Dialog({
		title: __("Shopify Metafield Definitions ({0})", [fields.length]),
		size: "large",
	});

	d.$body.html(html);
	d.show();
}
