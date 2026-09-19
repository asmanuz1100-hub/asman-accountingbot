import os

# Force a fresh webhook registration after deployment. This avoids stale Telegram
# webhook URLs surviving across Render redeploys.
os.environ["WEBHOOK_PATH"] = "telegram-v2"

# Never delete business data during startup.
from database import init_db
init_db()

# XISOB_CLEAN_20260920: one-time user-approved clean start of business data.
# This runs against public only, leaving access control and agentbot intact.
from sqlalchemy import text as _clean_sql
from database import engine as _clean_engine

def _one_time_business_clean():
    if _clean_engine.dialect.name != "postgresql":
        raise RuntimeError("Production clean start only supports PostgreSQL")
    with _clean_engine.begin() as conn:
        dbname = conn.execute(_clean_sql("SELECT current_database()")).scalar_one()
        if dbname != "asman_accounting_db_v2":
            raise RuntimeError("Unexpected database; clean start stopped")
        marker = conn.execute(_clean_sql(
            "SELECT meta_value FROM public.app_meta WHERE meta_key = 'xisob_clean_20260920_v2'"
        )).scalar_one_or_none()
        if marker == "done":
            return
        conn.execute(_clean_sql(
            "TRUNCATE public.contracts, public.materials, "
            "public.documents, public.partners RESTART IDENTITY RESTRICT"
        ))
        conn.execute(_clean_sql(
            "INSERT INTO public.app_meta(meta_key,meta_value) "
            "VALUES ('xisob_clean_20260920_v2','done') "
            "ON CONFLICT(meta_key) DO UPDATE SET meta_value='done'"
        ))

_one_time_business_clean()

# Patch spreadsheet analysis before bot import.
import business_controls
import excel_docs
import enhancements
import menu_customization
import reconciliation_persistence
import reconciliation_ui
import stable_reconciliation
from access_control import install_access_control
from permissions_control import install_permissions_control
from trial_control import install_trial_control
from tenant_control import install_tenant_control
from runtime_compat import install_runtime_compat
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
from report_design import build_reconciliation_pdf as build_reconciliation_pdf_v2
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

from ledger import install_ledger
from ledger_ui import install_review_ui, install_confirmation_safety
import financial_report_pdf
install_ledger(enhancements, business_controls, financial_report_pdf)
install_review_ui(bot_module)
install_confirmation_safety(bot_module)

# Access layers are installed last. Admin controls general access, section rights,
# pilot/test subscriptions, and strict per-customer database isolation.
install_access_control(bot_module)
install_permissions_control(bot_module)
install_trial_control(bot_module)
install_tenant_control(bot_module)
# Preserve compatibility with the original explicit ALLOWED_USER_IDS deployment
# mode and lightweight Telegram update wrappers.
install_runtime_compat(bot_module)

# Persist runtime incidents so the separate Admin Bot can diagnose them without
# exposing customer document contents.
from admin_monitor import capture_error
_original_error_handler = bot_module.error_handler


async def _monitored_error_handler(update, context):
    capture_error(update, context.error)
    await _original_error_handler(update, context)


bot_module.error_handler = _monitored_error_handler

main = bot_module.main

if __name__ == "__main__":
    main()
