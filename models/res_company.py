from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ResCompany(models.Model):
    _inherit = "res.company"

    customer_invoice_import_journal_id = fields.Many2one(
        "account.journal",
        string="Customer Invoice Import Journal",
        help=(
            "Preferred sales journal used when importing XML/PDF into customer invoices. "
            "It must be a sales journal without EDI formats."
        ),
    )

    @api.constrains("customer_invoice_import_journal_id")
    def _check_customer_invoice_import_journal(self):
        for company in self:
            journal = company.customer_invoice_import_journal_id
            if not journal:
                continue
            if journal.company_id != company:
                raise ValidationError(
                    _(
                        "The customer invoice import journal must belong to company %(company)s.",
                        company=company.display_name,
                    )
                )
            if journal.type != "sale":
                raise ValidationError(
                    _(
                        "The customer invoice import journal must be a sales journal."
                    )
                )
            if "edi_format_ids" in journal._fields and journal.edi_format_ids:
                raise ValidationError(
                    _(
                        "The customer invoice import journal cannot have active EDI formats."
                    )
                )
