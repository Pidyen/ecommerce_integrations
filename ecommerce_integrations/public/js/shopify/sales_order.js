frappe.ui.form.on("Sales Order", {
	refresh: function (frm) {
		render_medical_info(frm);
		render_shopify_order_action_buttons(frm);
	},
	customer: function (frm) {
		render_medical_info(frm);
	},
});

function render_medical_info(frm) {
	const wrapper = frm.fields_dict.shopify_medical_info_html;
	if (!wrapper) return;

	if (!frm.doc.customer) {
		wrapper.$wrapper.html("");
		return;
	}

	frappe.call({
		method: "ecommerce_integrations.shopify.customer.get_customer_medical_info",
		args: { customer: frm.doc.customer },
		callback: function (r) {
			if (!r.message || !r.message.length) {
				wrapper.$wrapper.html(
					`<div class="text-muted" style="padding:15px;">
						${__("No medical information available for this customer.")}
					</div>`
				);
				return;
			}

			let html = `
				<div style="padding:15px;">
					<div style="display:grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap:12px;">`;

			r.message.forEach((field) => {
				let display_value;
				let value_style = "";

				if (field.type === "boolean") {
					if (
						field.value === "true" ||
						field.value === true ||
						field.value === "True"
					) {
						display_value = `<span style="color:#38a169;font-weight:600;">&#10003; Yes</span>`;
					} else {
						display_value = `<span style="color:#e53e3e;font-weight:600;">&#10005; No</span>`;
					}
				} else if (
					field.value === null ||
					field.value === undefined ||
					field.value === ""
				) {
					display_value = `<span class="text-muted">—</span>`;
				} else {
					display_value = frappe.utils.escape_html(String(field.value));
					value_style = "font-weight:600;";
				}

				html += `
					<div style="background:#f8f9fa; border:1px solid #e9ecef; border-radius:8px; padding:12px 15px;">
						<div style="font-size:11px; color:#6c757d; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:4px;">
							${frappe.utils.escape_html(field.label)}
						</div>
						<div style="font-size:14px; ${value_style}">
							${display_value}
						</div>
					</div>`;
			});

			html += `
					</div>
				</div>`;

			wrapper.$wrapper.html(html);
		},
	});
}

function render_shopify_order_action_buttons(frm) {
	if (frm.doc.docstatus !== 1 || !frm.doc.shopify_order_id) return;

	frappe.call({
		method: "ecommerce_integrations.shopify.order_edit.get_shopify_order_action_state",
		args: { sales_order: frm.doc.name },
		callback: function (r) {
			const state = r.message || {};

			if (state.show_refund) {
				frm.add_custom_button(__("Trigger Shopify Refund"), function () {
					frappe.confirm(
						__(
							"This will create a Shopify refund for current refundable quantities. Continue?"
						),
						function () {
							frappe.call({
								method: "ecommerce_integrations.shopify.order_edit.trigger_shopify_refund",
								args: { sales_order: frm.doc.name },
								callback: function () {
									frappe.show_alert({
										message: __("Shopify refund triggered."),
										indicator: "green",
									});
									frm.reload_doc();
								},
							});
						}
					);
				});
			}

			if (state.show_resend_invoice) {
				frm.add_custom_button(__("Resend Shopify Invoice"), function () {
					frappe.confirm(
						__("Resend Shopify invoice for this order?"),
						function () {
							frappe.call({
								method: "ecommerce_integrations.shopify.order_edit.trigger_shopify_resend_invoice",
								args: { sales_order: frm.doc.name },
								callback: function () {
									frappe.show_alert({
										message: __("Shopify invoice resend triggered."),
										indicator: "green",
									});
									frm.reload_doc();
								},
							});
						}
					);
				});
			}
		},
	});
}
