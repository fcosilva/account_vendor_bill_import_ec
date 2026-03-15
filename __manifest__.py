{
    "name": "EC Vendor Bill XML Import",
    "version": "17.0.1.0.0",
    "summary": "Import vendor bills from Ecuador SRI XML files",
    "author": "Openlab Ecuador",
    "license": "AGPL-3",
    "category": "Accounting",
    "depends": [
        "account",
        "product",
        "l10n_ec",
        "l10n_ec_account_edi",
    ],
    "data": [
        "security/ir.model.access.csv",
        "views/vendor_bill_import_wizard_views.xml",
        "views/account_move_views.xml",
        "views/account_move_l10n_override_views.xml",
        "views/res_config_settings_views.xml",
    ],
    "installable": True,
    "application": False,
}
