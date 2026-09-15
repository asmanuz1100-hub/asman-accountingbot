import os

# Old Render Blueprint deployments may retain an invalid Telegram webhook secret.
os.environ.pop("WEBHOOK_SECRET", None)

# Create schema and wipe old business data exactly once for the requested clean start.
from reset_once import reset_business_data_once

reset_business_data_once()

# Patch spreadsheet analysis before bot import.
import business_controls
import excel_docs
import enhancements
import menu_customization
import reconciliation_persistence
import reconciliation_ui
import stable_reconciliation
from asaka_excel import try_analyze_asaka
from business_controls import (
    enrich_registry_accounts,
    install_business_core,
    install_business_ui,
)
from document_guard import install_document_guard
from enhancements import enrich_bank_data, install_bot_enhancements
from financial_pdf_ui import install_financial_pdf_ui
from incoming_invoice_support import (
    install_incoming_invoice_support,
    try_analyze_invoice_registry_v2,
)
from invoice_registry import try_analyze_invoice_registry
from menu_customization import install_menu_customization
from reconciliation_pdf_v2 import build_reconciliation_pdf_v2
from reconciliation_persistence import install_reconciliation_persistence
from reconciliation_saldo import install_saldo_fix
from reconciliation_ui import install_reconciliation_ui
from stable_reconciliation import install_stable_reconciliation

_original_analyze_spreadsheet = excel_docs.analyze_spreadsheet_bytes


def _analyze_spreadsheet(data: bytes, filename: str = "statement.xlsx") -> dict:
    registry_result = try_analyze_invoice_registry_v2(data, filename)
    if registry_result is not None:
        return enrich_registry_accounts(data, filename, registry_result)

    # Backward-compatible fallback for other electronic-document registry variants.
    registry_result = try_analyze_invoice_registry(data, filename)
    if registry_result is not None:
        return enrich_registry_accounts(data, filename, registry_result)

    result = try_analyze_asaka(data, filename)
    if result is None:
        result = _original_analyze_spreadsheet(data, filename)
    return enrich_bank_data(data, filename, result)


excel_docs.analyze_spreadsheet_bytes = _analyze_spreadsheet

# Import bot after parser patches, then install a single deterministic stack.
import bot as bot_module

install_bot_enhancements(bot_module)
install_menu_customization(bot_module)
install_incoming_invoice_support(bot_module)
install_reconciliation_persistence(bot_module, enhancements)
install_stable_reconciliation(reconciliation_persistence, enhancements)
install_business_core(
    stable_reconciliation,
    reconciliation_persistence,
    enhancements,
    menu_customization,
)
install_saldo_fix(enhancements)
reconciliation_ui.build_reconciliation_pdf = build_reconciliation_pdf_v2
install_reconciliation_ui(bot_module)
install_business_ui(bot_module, reconciliation_ui, enhancements)
install_financial_pdf_ui(bot_module, business_controls)
# Install last: previews stay read-only and duplicate checks cover the final handler stack.
install_document_guard(bot_module, enhancements)

main = bot_module.main

if __name__ == "__main__":
    main()
