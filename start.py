import os

# Old Render Blueprint deployments may retain an invalid Telegram webhook secret.
os.environ.pop("WEBHOOK_SECRET", None)

# Patch spreadsheet analysis before bot import.
import excel_docs
import enhancements
import reconciliation_ui
from asaka_excel import try_analyze_asaka
from enhancements import enrich_bank_data, install_bot_enhancements
from invoice_registry import try_analyze_invoice_registry
from menu_customization import install_menu_customization
from reconciliation_pdf_v2 import build_reconciliation_pdf_v2
from reconciliation_persistence import install_reconciliation_persistence
from reconciliation_saldo import install_saldo_fix
from reconciliation_ui import install_reconciliation_ui

_original_analyze_spreadsheet = excel_docs.analyze_spreadsheet_bytes


def _analyze_spreadsheet(data: bytes, filename: str = "statement.xlsx") -> dict:
    registry_result = try_analyze_invoice_registry(data, filename)
    if registry_result is not None:
        return registry_result

    result = try_analyze_asaka(data, filename)
    if result is None:
        result = _original_analyze_spreadsheet(data, filename)
    return enrich_bank_data(data, filename, result)


excel_docs.analyze_spreadsheet_bytes = _analyze_spreadsheet

# Import bot after all parser patches, then install UI/partner/reconciliation enhancements.
import bot as bot_module

install_bot_enhancements(bot_module)
install_menu_customization(bot_module)
install_reconciliation_persistence(bot_module, enhancements)
install_saldo_fix(enhancements)
reconciliation_ui.build_reconciliation_pdf = build_reconciliation_pdf_v2
install_reconciliation_ui(bot_module)

main = bot_module.main

if __name__ == "__main__":
    main()
