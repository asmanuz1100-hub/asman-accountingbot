import json
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session
from telegram import ReplyKeyboardMarkup

from database import Document, engine, save_document


MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["📎 Ҳужжат юклаш", "🤖 AI таҳлил"],
        ["👥 Ҳамкорлар", "📄 Шартномалар"],
        ["📦 Хом ашё омбори", "🧮 Акт сверка"],
        ["💸 Чиқимлар", "🧾 Чиқиш фактура"],
        ["📊 Ҳисоботлар", "ℹ️ Ёрдам"],
    ],
    resize_keyboard=True,
)

EXPENSE_MENU = ReplyKeyboardMarkup(
    [
        ["🧱 Хом ашё харажати", "💡 Коммунал харажат"],
        ["🧾 Солиқ тўловлари", "📦 Бошқа харажат"],
        ["🏠 Асосий меню"],
    ],
    resize_keyboard=True,
)

CATEGORY_LABELS = {
    "raw_material": "🧱 Хом ашё",
    "utilities": "💡 Коммунал",
    "tax": "🧾 Солиқ",
    "other": "📦 Бошқа",
}

BUTTON_TO_CATEGORY = {
    "🧱 Хом ашё харажати": "raw_material",
    "💡 Коммунал харажат": "utilities",
    "🧾 Солиқ тўловлари": "tax",
    "📦 Бошқа харажат": "other",
}


def _money(value):
    try:
        return f"{float(value or 0):,.2f}".replace(",", " ")
    except Exception:
        return "0.00"


def _number(value):
    text = str(value or "").strip().replace(" ", "")
    if not text:
        return 0.0
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    else:
        text = text.replace(",", ".")
    try:
        return float(text)
    except Exception:
        return 0.0


def _normalize_date(value):
    raw = str(value or "").strip()
    if not raw:
        return datetime.now().strftime("%Y-%m-%d")
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return raw


def _classify_expense(purpose, counterparty=""):
    text = f"{purpose or ''} {counterparty or ''}".casefold()

    tax_words = (
        "солиқ", "soliq", "налог", "бюджет", "казнач", "ғазнач", "ндс", "qqs",
        "ижтимоий солиқ", "social tax", "пеня", "пенсион", "инпс",
    )
    utility_words = (
        "электр", "энерг", "electric", "газ", "сув", "вод", "коммун", "тепло",
        "иссиқлик", "мусор", "чиқинди", "интернет", "телефон", "aloqa", "связь",
    )
    raw_words = (
        "хом ашё", "хомашё", "сырье", "материал", "песок", "қум", "кум", "акрил",
        "цемент", "пигмент", "упаков", "мешок", "ведро", "тара", "қадоқ", "кадок",
    )

    if any(word in text for word in tax_words):
        return "tax"
    if any(word in text for word in utility_words):
        return "utilities"
    if any(word in text for word in raw_words):
        return "raw_material"
    return "other"


def _expense_docs(limit=10000):
    with Session(engine) as session:
        return list(
            session.scalars(
                select(Document)
                .where(Document.document_type == "expense_entry")
                .order_by(Document.id.desc())
                .limit(limit)
            ).all()
        )


def _expense_summary():
    totals = {key: 0.0 for key in CATEGORY_LABELS}
    count = 0
    for doc in _expense_docs():
        try:
            data = json.loads(doc.raw_json or "{}")
        except Exception:
            continue
        category = data.get("expense_category") or "other"
        if category not in totals:
            category = "other"
        totals[category] += _number(data.get("total"))
        count += 1
    return totals, count


def _known_signatures():
    result = set()
    for doc in _expense_docs():
        try:
            data = json.loads(doc.raw_json or "{}")
        except Exception:
            continue
        signature = str(data.get("source_signature") or "").strip()
        if signature:
            result.add(signature)
    return result


def _save_expense(category, amount, date=None, purpose=None, partner_name=None,
                  tin=None, account=None, source="manual", source_signature=None):
    data = {
        "document_type": "expense_entry",
        "expense_category": category if category in CATEGORY_LABELS else "other",
        "document_date": _normalize_date(date),
        "currency": "UZS",
        "total": round(float(amount or 0), 2),
        "summary": str(purpose or "").strip(),
        "partner": {
            "name": str(partner_name or "").strip() or None,
            "tin": str(tin or "").strip() or None,
            "account": str(account or "").strip() or None,
        },
        "source": source,
        "source_signature": source_signature,
    }
    return save_document(
        data,
        telegram_file_id=None,
        filename=f"expense-{data['document_date']}.json",
        mime_type="application/json",
    )


def _sync_bank_expenses(data):
    if not isinstance(data, dict) or data.get("document_type") != "bank_statement":
        return 0, {key: 0.0 for key in CATEGORY_LABELS}

    known = _known_signatures()
    added = 0
    amounts = {key: 0.0 for key in CATEGORY_LABELS}
    transactions = data.get("transactions") or data.get("transactions_preview") or []

    for tx in transactions:
        outgoing = round(_number(tx.get("outgoing")), 2)
        if outgoing <= 0:
            continue

        date = str(tx.get("date") or "")
        doc_no = str(tx.get("document_number") or "")
        account = str(tx.get("counterparty_account") or "")
        tin = str(tx.get("counterparty_tin") or "")
        purpose = str(tx.get("purpose") or "")
        counterparty = str(tx.get("counterparty") or "")
        signature = "|".join((date, doc_no, account, tin, f"{outgoing:.2f}", purpose.strip()))
        if signature in known:
            continue

        category = _classify_expense(purpose, counterparty)
        _save_expense(
            category=category,
            amount=outgoing,
            date=date,
            purpose=purpose,
            partner_name=counterparty,
            tin=tin,
            account=account,
            source="bank_statement",
            source_signature=signature,
        )
        known.add(signature)
        added += 1
        amounts[category] += outgoing

    return added, amounts


def _expense_screen_text():
    totals, count = _expense_summary()
    total = sum(totals.values())
    return (
        "💸 ЧИҚИМЛАР\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"🧱 Хом ашё: {_money(totals['raw_material'])} UZS\n"
        f"💡 Коммунал: {_money(totals['utilities'])} UZS\n"
        f"🧾 Солиқ: {_money(totals['tax'])} UZS\n"
        f"📦 Бошқа: {_money(totals['other'])} UZS\n"
        "━━━━━━━━━━━━━━━━\n"
        f"💰 Жами чиқим: {_money(total)} UZS\n"
        f"📑 Операциялар: {count} та\n\n"
        "🏦 Банк выпискаси тасдиқланганда чиқимлар автоматик ажратилади.\n"
        "Қўлда киритиш учун керакли турини танланг."
    )


def _manual_help(category):
    label = CATEGORY_LABELS.get(category, "📦 Бошқа")
    return (
        f"{label} харажати\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "Қуйидаги форматда ёзинг:\n"
        "1250000 | 14.09.2026 | Электр энергияси | Контрагент номи\n\n"
        "Тартиб: сумма | сана | изоҳ | ҳамкор\n"
        "Сана ва ҳамкорни ёзмасангиз ҳам бўлади."
    )


def install_menu_customization(bot_module):
    bot_module.MENU = MAIN_MENU
    original_text_handler = bot_module.text_handler
    original_confirm_callback = bot_module.confirm_callback

    async def text_handler(update, context):
        text = (update.message.text or "").strip()

        if text == "💸 Чиқимлар":
            context.user_data.pop("expense_category", None)
            await update.message.reply_text(_expense_screen_text(), reply_markup=EXPENSE_MENU)
            return

        if text in BUTTON_TO_CATEGORY:
            category = BUTTON_TO_CATEGORY[text]
            context.user_data["expense_category"] = category
            await update.message.reply_text(_manual_help(category), reply_markup=EXPENSE_MENU)
            return

        if text == "🏠 Асосий меню":
            context.user_data.pop("expense_category", None)
            await update.message.reply_text("🏠 Асосий меню", reply_markup=bot_module.MENU)
            return

        category = context.user_data.get("expense_category")
        if category and "|" in text:
            parts = [part.strip() for part in text.split("|")]
            amount = _number(parts[0] if parts else 0)
            if amount <= 0:
                await update.message.reply_text("❌ Сумма нотўғри. Масалан: 1250000 | 14.09.2026 | Электр энергияси")
                return
            date = parts[1] if len(parts) > 1 else None
            purpose = parts[2] if len(parts) > 2 else ""
            partner_name = parts[3] if len(parts) > 3 else ""
            doc_id = _save_expense(category, amount, date, purpose, partner_name)
            context.user_data.pop("expense_category", None)
            await update.message.reply_text(
                f"✅ Харажат сақланди. ID: {doc_id}\n"
                f"{CATEGORY_LABELS.get(category)}: {_money(amount)} UZS",
                reply_markup=EXPENSE_MENU,
            )
            return

        await original_text_handler(update, context)

    async def confirm_callback(update, context):
        pending = context.user_data.get("pending_doc") or {}
        pending_data = pending.get("data") if isinstance(pending, dict) else None
        is_confirm = bool(update.callback_query and update.callback_query.data == "doc_confirm")

        await original_confirm_callback(update, context)

        if is_confirm and not context.user_data.get("pending_doc") and isinstance(pending_data, dict) and pending_data.get("document_type") == "bank_statement":
            try:
                added, amounts = _sync_bank_expenses(pending_data)
                if added:
                    details = []
                    for key in ("raw_material", "utilities", "tax", "other"):
                        if amounts[key] > 0:
                            details.append(f"{CATEGORY_LABELS[key]}: {_money(amounts[key])} UZS")
                    await update.callback_query.message.reply_text(
                        "💸 Банк чиқимлари автоматик ажратилди.\n"
                        f"Янги операциялар: {added} та\n" + "\n".join(details),
                        reply_markup=bot_module.MENU,
                    )
            except Exception:
                # The bank statement itself is already safely stored by the original handler.
                pass

    bot_module.text_handler = text_handler
    bot_module.confirm_callback = confirm_callback
