import json
import re
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session
from telegram import ReplyKeyboardMarkup

from asaka_excel import _Parser, _counterparty, _date, _decode, _num
from database import Document, engine, list_partners, upsert_partner


def _norm_name(value):
    text = " ".join(str(value or "").split()).strip()
    return text.casefold()


def enrich_bank_data(raw: bytes, filename: str, data: dict) -> dict:
    """Attach full bank transactions so partners and reconciliation can use them."""
    if not isinstance(data, dict) or data.get("document_type") != "bank_statement":
        return data
    if data.get("transactions"):
        return data

    transactions = []
    try:
        head = raw[:4096].lstrip().lower()
        if b"<html" in head or b"<table" in head:
            parser = _Parser()
            parser.feed(_decode(raw))
            for row in parser.rows:
                if len(row) < 8:
                    continue
                tx_date = _date(row[0])
                if not tx_date:
                    continue
                outgoing = round(_num(row[5]), 2)
                incoming = round(_num(row[6]), 2)
                if incoming == 0 and outgoing == 0:
                    continue
                transactions.append({
                    "date": tx_date,
                    "counterparty": _counterparty(row[1]),
                    "purpose": str(row[7] or "").strip()[:1000] or None,
                    "incoming": incoming,
                    "outgoing": outgoing,
                })
    except Exception:
        transactions = []

    if not transactions:
        transactions = list(data.get("transactions_preview") or [])

    data["transactions"] = transactions
    return data


def _partner_names_from_data(data: dict) -> list[str]:
    names = set()
    own_name = _norm_name(data.get("account_holder"))

    partner = data.get("partner") or {}
    if isinstance(partner, dict) and partner.get("name"):
        names.add(" ".join(str(partner["name"]).split()).strip())

    if data.get("document_type") == "bank_statement":
        for tx in data.get("transactions") or []:
            name = " ".join(str(tx.get("counterparty") or "").split()).strip()
            if not name:
                continue
            norm = _norm_name(name)
            if norm == own_name:
                continue
            if len(norm) < 3:
                continue
            if any(bad in norm for bad in ("итоговый оборот", "остаток на", "оборот дебет", "оборот кредит")):
                continue
            names.add(name[:255])

        if not names:
            for item in data.get("top_counterparties") or []:
                name = " ".join(str(item.get("name") or "").split()).strip()
                if name and _norm_name(name) != own_name:
                    names.add(name[:255])

    return sorted(names, key=str.casefold)


def sync_partners_from_data(data: dict) -> int:
    names = _partner_names_from_data(data)
    for name in names:
        try:
            upsert_partner(name, partner_type="bank_counterparty" if data.get("document_type") == "bank_statement" else "customer")
        except Exception:
            continue
    return len(names)


def _matches_partner(candidate: str, requested: str) -> bool:
    a = _norm_name(candidate)
    b = _norm_name(requested)
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) >= 5 and (a in b or b in a):
        return True
    return False


def reconciliation_for_partner(partner_name: str) -> str:
    found = []
    seen = set()

    with Session(engine) as session:
        docs = session.scalars(
            select(Document).where(Document.document_type == "bank_statement").order_by(Document.id)
        ).all()

    for doc in docs:
        try:
            data = json.loads(doc.raw_json or "{}")
        except Exception:
            continue
        transactions = data.get("transactions") or data.get("transactions_preview") or []
        for tx in transactions:
            cp = str(tx.get("counterparty") or "").strip()
            if not _matches_partner(cp, partner_name):
                continue
            key = (
                str(tx.get("date") or ""),
                _norm_name(cp),
                str(tx.get("purpose") or "").strip(),
                round(float(tx.get("incoming") or 0), 2),
                round(float(tx.get("outgoing") or 0), 2),
            )
            if key in seen:
                continue
            seen.add(key)
            found.append({
                "date": key[0],
                "counterparty": cp,
                "purpose": key[2],
                "incoming": key[3],
                "outgoing": key[4],
            })

    if not found:
        return (
            f"🧮 АКТ СВЕРКА\n\nҲамкор: {partner_name}\n\n"
            "Бу ҳамкор бўйича сақланган банк операциялари топилмади.\n"
            "Аввал банк выпискасини юбориб, «✅ Тасдиқлаш»ни босинг."
        )

    found.sort(key=lambda x: x.get("date") or "")
    incoming = round(sum(x["incoming"] for x in found), 2)
    outgoing = round(sum(x["outgoing"] for x in found), 2)
    net = round(incoming - outgoing, 2)
    dates = [x["date"] for x in found if x.get("date")]
    period_from = min(dates) if dates else "—"
    period_to = max(dates) if dates else "—"

    def money(v):
        return f"{float(v):,.2f}".replace(",", " ")

    lines = [
        "🧮 АКТ СВЕРКА — БАНК БЎЙИЧА",
        "",
        f"Ҳамкор: {partner_name}",
        f"Давр: {period_from} — {period_to}",
        f"Операциялар: {len(found)} та",
        f"📥 Ҳамкордан тушган: {money(incoming)}",
        f"📤 Ҳамкорга тўланган: {money(outgoing)}",
        f"⚖️ Соф банк фарқи: {money(net)}",
        "",
        "Сўнгги операциялар:",
    ]
    for tx in found[-10:]:
        purpose = (tx.get("purpose") or "").strip()
        if len(purpose) > 70:
            purpose = purpose[:67] + "..."
        lines.append(
            f"• {tx.get('date') or '—'} | +{money(tx['incoming'])} / -{money(tx['outgoing'])}"
            + (f"\n  {purpose}" if purpose else "")
        )

    lines += [
        "",
        "ℹ️ Ҳозирги акт банк ҳаракатлари асосида. Чиқиш фактура модули тўлиқ улангач, "
        "сотув + тўлов + шартнома қолдиғи билан тўлиқ бухгалтерия акт сверкаси чиқади.",
    ]
    return "\n".join(lines)


def install_bot_enhancements(bot_module):
    menu_labels = {
        "📎 Ҳужжат юклаш", "🤖 AI таҳлил", "👥 Ҳамкорлар", "📄 Шартномалар",
        "📦 Хом ашё омбори", "📥 Кирим", "🧾 Чиқиш фактура", "📊 Ҳисоботлар",
        "🧮 Акт сверка", "ℹ️ Ёрдам",
    }

    bot_module.MENU = ReplyKeyboardMarkup(
        [
            ["📎 Ҳужжат юклаш", "🤖 AI таҳлил"],
            ["👥 Ҳамкорлар", "📄 Шартномалар"],
            ["📦 Хом ашё омбори", "📥 Кирим"],
            ["🧾 Чиқиш фактура", "🧮 Акт сверка"],
            ["📊 Ҳисоботлар", "ℹ️ Ёрдам"],
        ],
        resize_keyboard=True,
    )

    original_text_handler = bot_module.text_handler
    original_confirm_callback = bot_module.confirm_callback

    async def enhanced_text_handler(update, context):
        text = (update.message.text or "").strip()

        if text == "👥 Ҳамкорлар":
            rows = list_partners(limit=100)
            body = "\n".join(
                f"• {p.name}" + (f" | СТИР {p.tin}" if p.tin else "") for p in rows
            ) if rows else "Ҳозирча ҳамкорлар йўқ."
            await update.message.reply_text(
                f"👥 Ҳамкорлар:\n\n{body}\n\n"
                "✅ Ҳамкорлар банк выпискаси ва тасдиқланган ҳужжатлардан автоматик қўшилади."
            )
            return

        if text == "🧮 Акт сверка":
            rows = list_partners(limit=30)
            body = "\n".join(f"• {p.name}" for p in rows) if rows else "Ҳозирча ҳамкорлар йўқ."
            context.user_data["awaiting_reconciliation_partner"] = True
            await update.message.reply_text(
                "🧮 АКТ СВЕРКА\n\n"
                f"Ҳамкорлар:\n{body}\n\n"
                "Акт чиқариш учун ҳамкор номини ёзинг."
            )
            return

        if text.lower().startswith("акт:"):
            name = text.split(":", 1)[1].strip()
            context.user_data.pop("awaiting_reconciliation_partner", None)
            await update.message.reply_text(reconciliation_for_partner(name))
            return

        if context.user_data.get("awaiting_reconciliation_partner") and text not in menu_labels:
            context.user_data.pop("awaiting_reconciliation_partner", None)
            await update.message.reply_text(reconciliation_for_partner(text))
            return

        await original_text_handler(update, context)

    async def enhanced_confirm_callback(update, context):
        pending = context.user_data.get("pending_doc")
        data = dict(pending.get("data") or {}) if pending else None
        is_confirm = bool(update.callback_query and update.callback_query.data == "doc_confirm")

        await original_confirm_callback(update, context)

        if is_confirm and data:
            count = sync_partners_from_data(data)
            if count:
                await update.callback_query.message.reply_text(
                    f"👥 Ҳамкорлар автоматик синхронланди: {count} та."
                )

    bot_module.text_handler = enhanced_text_handler
    bot_module.confirm_callback = enhanced_confirm_callback
