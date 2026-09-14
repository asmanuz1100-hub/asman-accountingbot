def _money(value):
    try:
        return f"{float(value):,.2f}".replace(",", " ")
    except Exception:
        return "0.00"


def add_closing_saldo(data: dict) -> dict:
    if not isinstance(data, dict):
        return data

    opening_debit = float(data.get("opening_debit") or 0)
    opening_credit = float(data.get("opening_credit") or 0)
    debit_total = float(data.get("debit_total") or 0)
    credit_total = float(data.get("credit_total") or 0)

    closing_net = round((opening_debit - opening_credit) + (debit_total - credit_total), 2)
    closing_debit = closing_net if closing_net > 0 else 0.0
    closing_credit = abs(closing_net) if closing_net < 0 else 0.0

    data["closing_balance"] = abs(closing_net)
    data["closing_side"] = "debit" if closing_net > 0 else "credit" if closing_net < 0 else "zero"
    data["closing_debit"] = round(closing_debit, 2)
    data["closing_credit"] = round(closing_credit, 2)

    flow = data.get("flow")
    if abs(closing_net) < 0.01:
        data["closing_status"] = "settled"
        data["closing_result"] = "Қарздорлик йўқ"
    elif flow == "outgoing":
        if closing_debit > 0:
            data["closing_status"] = "debt"
            data["closing_result"] = f"Ҳамкор қарзи: {_money(closing_debit)} UZS"
        else:
            data["closing_status"] = "advance"
            data["closing_result"] = f"Ҳамкор аванси: {_money(closing_credit)} UZS"
    elif flow == "incoming":
        if closing_credit > 0:
            data["closing_status"] = "debt"
            data["closing_result"] = f"Бизнинг ҳамкор олдидаги қарзимиз: {_money(closing_credit)} UZS"
        else:
            data["closing_status"] = "advance"
            data["closing_result"] = f"Етказиб берувчига аванс: {_money(closing_debit)} UZS"
    else:
        data["closing_status"] = "debt" if closing_debit > 0 else "advance"
        side = "ДЕБЕТ" if closing_debit > 0 else "КРЕДИТ"
        data["closing_result"] = f"{side} сальдо: {_money(abs(closing_net))} UZS"

    return data


def install_saldo_fix(enhancements_module):
    original_prepare = enhancements_module.prepare_reconciliation
    original_get_saved = enhancements_module.get_saved_reconciliation

    def prepare_reconciliation(identifier, flow=None):
        result = original_prepare(identifier, flow)
        if not result.get("ok"):
            return result

        data = add_closing_saldo(result.get("data") or {})
        period = data.get("statement_period") or {}
        closing_date = period.get("to") or data.get("document_date") or "—"
        status = data.get("closing_status")
        icon = "🔴" if status == "debt" else "🟢" if status == "advance" else "⚪"
        status_text = "ҚАРЗДОРЛИК" if status == "debt" else "АВАНС" if status == "advance" else "ҚАРЗ ЙЎҚ"
        saldo_text = (
            "\n\n📌 ЯКУНИЙ САЛЬДО"
            f"\n📅 {closing_date} ҳолатига"
            f"\nДебет: {_money(data.get('closing_debit'))} UZS"
            f"\nКредит: {_money(data.get('closing_credit'))} UZS"
            f"\n{icon} {status_text}: {data.get('closing_result')}"
        )
        if "📌 ЯКУНИЙ САЛЬДО" not in (result.get("text") or ""):
            result["text"] = (result.get("text") or "") + saldo_text
        result["data"] = data
        return result

    def get_saved_reconciliation(doc_id):
        data = original_get_saved(doc_id)
        return add_closing_saldo(data) if data else data

    enhancements_module.prepare_reconciliation = prepare_reconciliation
    enhancements_module.get_saved_reconciliation = get_saved_reconciliation
