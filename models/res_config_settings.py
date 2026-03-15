from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    customer_invoice_import_journal_id = fields.Many2one(
        related="company_id.customer_invoice_import_journal_id",
        readonly=False,
        string="Customer Invoice Import Journal",
        domain="[('type', '=', 'sale'), ('company_id', '=', company_id)]",
    )
