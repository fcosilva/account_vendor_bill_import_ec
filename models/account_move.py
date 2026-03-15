from odoo import _, models


class AccountMove(models.Model):
    _inherit = "account.move"

    def action_open_vendor_bill_import_wizard(self):
        self.ensure_one()
        if self.move_type not in ("in_invoice", "in_refund", "out_invoice", "out_refund"):
            return False
        return {
            "name": _("Import XML/PDF"),
            "type": "ir.actions.act_window",
            "res_model": "vendor.bill.import.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"import_target_move_id": self.id},
        }
