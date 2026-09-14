import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ai_docs import analyze_image_bytes, analyze_pdf_bytes
from excel_docs import analyze_spreadsheet_bytes
from database import (
    add_contract,
    add_material,
    init_db,
    list_contracts,
    list_materials,
    list_partners,
    report,
    save_document,
    upsert_partner,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("accounting-sales-ai")

TOKEN = os.getenv("BOT_TOKEN")
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "20"))

MENU = ReplyKeyboardMarkup(
    [
        ["📎 Ҳужжат юклаш", "🤖 AI таҳлил"],
        ["👥 Ҳамкорлар", "📄 Шартномалар"],
        ["📦 Хом ашё омбори", "📥 Кирим"],
        ["🧾 Чиқиш фактура", "📊 Ҳисоботлар"],
        ["ℹ️ Ёрдам"],
    ],
    resize_keyboard=True,
)

CONFIRM_KB = InlineKeyboardMarkup(
    [[
        InlineKeyboardButton("✅ Тасдиқлаш", callback_data="doc_confirm"),
        InlineKeyboardButton("❌ Бекор қилиш", callback_data="doc_cancel"),
    ]]
)


def money(value):
    try:
        return f"{float(value):,.2f}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def analysis_text(data: dict) -> str:
    if data.get("document_type") == "bank_statement":
        period = data.get("statement_period") or {}
        warnings = data.get("warnings") or []
        counterparties = data.get("top_counterparties") or []
        transactions = data.get("transactions_preview") or []

        lines = [
            "🏦 БАНК ВЫПИСКАСИ — AI ТАҲЛИЛ",
            "",
            f"Банк: {data.get('bank_name') or '—'}",
            f"Ҳисоб эгаси: {data.get('account_holder') or '—'}",
            f"Ҳисоб рақами: {data.get('account_number') or '—'}",
            f"Давр: {period.get('from') or '—'} — {period.get('to') or '—'}",
            f"Валюта: {data.get('currency') or '—'}",
            f"Бошланғич қолдиқ: {money(data.get('opening_balance'))}",
            f"📥 Жами кирим: {money(data.get('total_incoming'))}",
            f"📤 Жами чиқим: {money(data.get('total_outgoing'))}",
            f"Якуний қолдиқ: {money(data.get('closing_balance'))}",
            f"Операциялар: {data.get('operations_count') or 0} та",
        ]

        if counterparties:
            lines += ["", "👥 Асосий контрагентлар:"]
            for item in counterparties[:8]:
                lines.append(
                    f"• {item.get('name') or '—'} | "
                    f"кирим {money(item.get('incoming'))} | "
                    f"чиқим {money(item.get('outgoing'))}"
                )

        if transactions:
            lines += ["", "🔎 Операциялардан намуна:"]
            for item in transactions[:6]:
                lines.append(
                    f"• {item.get('date') or '—'} | "
                    f"{item.get('counterparty') or '—'} | "
                    f"+{money(item.get('incoming'))} / -{money(item.get('outgoing'))}"
                )

        if data.get("summary"):
            lines += ["", "📝 Хулоса:", str(data["summary"])]

        if warnings:
            lines += ["", "⚠️ Текшириш керак:"]
            lines += [f"• {w}" for w in warnings[:10]]
        else:
            lines += ["", "✅ AI жиддий огоҳлантириш аниқламади."]

        lines += [
            "",
            "Выписка таҳлили базага ҳали сақланмади.",
            "Текшириб, кейин «✅ Тасдиқлаш»ни босинг.",
        ]
        return "\n".join(lines)

    partner = data.get("partner") or {}
    warnings = data.get("warnings") or []
    items = data.get("items") or []
    confidence = round(float(data.get("confidence") or 0) * 100)

    lines = [
        "🤖 AI ТАҲЛИЛ НАТИЖАСИ",
        "",
        f"Ҳужжат тури: {data.get('document_type') or '—'}",
        f"Ишонч даражаси: {confidence}%",
        f"Ҳамкор: {partner.get('name') or '—'}",
        f"СТИР: {partner.get('tin') or '—'}",
        f"Шартнома №: {data.get('contract_number') or '—'}",
        f"Фактура №: {data.get('invoice_number') or data.get('document_number') or '—'}",
        f"Сана: {data.get('invoice_date') or data.get('document_date') or data.get('contract_date') or '—'}",
        f"Валюта: {data.get('currency') or '—'}",
        f"ҚҚС / НДС: {money(data.get('vat'))}",
        f"Жами: {money(data.get('total'))}",
        "",
        f"Товар позициялари: {len(items)} та",
    ]

    for i, item in enumerate(items[:8], 1):
        lines.append(
            f"{i}) {item.get('name') or '—'} | "
            f"{item.get('quantity') or '—'} {item.get('unit') or ''} | "
            f"{money(item.get('amount'))}"
        )
    if len(items) > 8:
        lines.append(f"... яна {len(items) - 8} та позиция")

    if data.get("summary"):
        lines += ["", "📝 Хулоса:", str(data["summary"])]

    if warnings:
        lines += ["", "⚠️ Текшириш керак:"]
        lines += [f"• {w}" for w in warnings[:10]]
    else:
        lines += ["", "✅ AI жиддий огоҳлантириш аниқламади."]

    lines += [
        "",
        "Маълумот базага ҳали ёзилмади.",
        "Текшириб, кейин «✅ Тасдиқлаш»ни босинг.",
    ]
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 БУХГАЛТЕРИЯ ВА САВДО AI\n\n"
        "PDF, расм ёки Excel (XLS/XLSX) ташласангиз, бот ҳужжатни ўқийди ва таҳлил қилади.\n"
        "Excel банк выпискасини ҳам автоматик таҳлил қилади.\n"
        "Маълумот фақат сиз тасдиқлагандан кейин базага сақланади.",
        reply_markup=MENU,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ ҚИСҚА ЙЎРИҚНОМА\n\n"
        "📎 PDF/JPG/PNG/XLS/XLSX юборинг — AI ҳужжатни таҳлил қилади.\n"
        "🏦 Банк выпискаси XLS/XLSX бўлса, кирим, чиқим, қолдиқ ва контрагентларни ҳисоблайди.\n\n"
        "📥 Хом ашё кирими:\n+ Акрил | 500 | kg\n\n"
        "👥 Ҳамкор қўшиш:\nҳамкор: Компания номи\n\n"
        "📄 Шартнома қўшиш:\n"
        "шартнома: Компания | 37/2026 | 10000000 | UZS\n\n"
        "⚠️ AI натижасини тасдиқлашдан олдин текширинг."
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text in ("📎 Ҳужжат юклаш", "🤖 AI таҳлил"):
        await update.message.reply_text(
            "📎 PDF, JPG/JPEG, PNG ёки Excel XLS/XLSX файлни шу чатга юборинг.\n"
            "🏦 Банк выпискаси Excel бўлса, AI кирим-чиқим ва қолдиқни ҳам таҳлил қилади."
        )
        return

    if text == "👥 Ҳамкорлар":
        rows = list_partners()
        body = "\n".join(
            f"• {p.name}" + (f" | СТИР {p.tin}" if p.tin else "") for p in rows
        ) if rows else "Ҳозирча ҳамкорлар йўқ."
        await update.message.reply_text(f"👥 Ҳамкорлар:\n\n{body}\n\nҚўшиш: ҳамкор: Компания номи")
        return

    if text.lower().startswith("ҳамкор:"):
        name = text.split(":", 1)[1].strip()
        try:
            partner = upsert_partner(name)
            await update.message.reply_text(f"✅ Ҳамкор сақланди: {partner.name}")
        except Exception as exc:
            await update.message.reply_text(f"❌ Хато: {exc}")
        return

    if text == "📄 Шартномалар":
        rows = list_contracts()
        if not rows:
            body = "Ҳозирча шартномалар йўқ."
        else:
            parts = []
            for contract, partner in rows:
                balance = contract.total_amount - contract.used_amount
                parts.append(
                    f"• {partner.name} | №{contract.number}\n"
                    f"  {money(contract.total_amount)} {contract.currency} | "
                    f"қолдиқ: {money(balance)}"
                )
            body = "\n\n".join(parts)
        await update.message.reply_text(
            f"📄 Шартномалар:\n\n{body}\n\n"
            "Қўшиш:\nшартнома: Компания | 37/2026 | 10000000 | UZS"
        )
        return

    if text.lower().startswith("шартнома:"):
        try:
            parts = [x.strip() for x in text.split(":", 1)[1].split("|")]
            partner_name, number = parts[0], parts[1]
            amount = float(parts[2].replace(" ", "").replace(",", "."))
            currency = parts[3].upper() if len(parts) > 3 else "UZS"
            add_contract(partner_name, number, amount, currency=currency)
            await update.message.reply_text(
                f"✅ Шартнома сақланди\n{partner_name}\n№{number}\n{money(amount)} {currency}"
            )
        except Exception:
            await update.message.reply_text(
                "❌ Формат:\nшартнома: Компания | 37/2026 | 10000000 | UZS"
            )
        return

    if text in ("📦 Хом ашё омбори", "📥 Кирим"):
        rows = list_materials()
        body = "\n".join(f"• {m.name}: {m.qty:g} {m.unit}" for m in rows) if rows else "Омбор ҳозирча бўш."
        await update.message.reply_text(f"📦 Хом ашё қолдиғи:\n\n{body}\n\nКирим:\n+ Акрил | 500 | kg")
        return

    if text.startswith("+"):
        try:
            parts = [x.strip() for x in text[1:].split("|")]
            name = parts[0]
            qty = float(parts[1].replace(",", "."))
            unit = parts[2] if len(parts) > 2 else "kg"
            add_material(name, qty, unit)
            await update.message.reply_text(f"✅ Кирим сақланди: {name} +{qty:g} {unit}")
        except Exception:
            await update.message.reply_text("❌ Формат: + Акрил | 500 | kg")
        return

    if text == "🧾 Чиқиш фактура":
        await update.message.reply_text(
            "🧾 Чиқиш фактура модули кейинги босқичда тўлиқ қилинади.\n"
            "Фактура ёзиш пайтида шартнома қолдиғи автоматик кўрсатилади."
        )
        return

    if text == "📊 Ҳисоботлар":
        data = report()
        await update.message.reply_text(
            "📊 ҲИСОБОТ\n\n"
            f"👥 Ҳамкорлар: {data['partners']}\n"
            f"📄 Шартномалар: {data['contracts']}\n"
            f"📦 Хом ашё турлари: {data['materials']}\n"
            f"📎 Сақланган ҳужжатлар: {data['documents']}\n"
            f"💰 Шартнома умумий қолдиғи: {money(data['contract_balance'])}"
        )
        return

    if text == "ℹ️ Ёрдам":
        await help_cmd(update, context)
        return

    await update.message.reply_text("Менюдан керакли бўлимни танланг.", reply_markup=MENU)


async def show_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict,
                        telegram_file_id: str, filename: str, mime_type: str):
    context.user_data["pending_doc"] = {
        "data": data,
        "telegram_file_id": telegram_file_id,
        "filename": filename,
        "mime_type": mime_type,
    }
    await update.effective_message.reply_text(analysis_text(data), reply_markup=CONFIRM_KB)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = await update.message.reply_text("⏳ Расмни ўқияпман ва таҳлил қиляпман...")
    try:
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        raw = bytes(await tg_file.download_as_bytearray())
        data = analyze_image_bytes(raw, "image/jpeg")
        await status.edit_text("✅ Таҳлил тайёр.")
        await show_analysis(update, context, data, photo.file_id, "telegram_photo.jpg", "image/jpeg")
    except Exception as exc:
        log.exception("Photo analysis failed")
        await status.edit_text(f"❌ Расмни таҳлил қилишда хато:\n{exc}")


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    mime = (doc.mime_type or "").lower()
    filename = doc.file_name or "document"
    lower_name = filename.lower()

    if doc.file_size and doc.file_size > MAX_FILE_MB * 1024 * 1024:
        await update.message.reply_text(f"❌ Файл жуда катта. Лимит: {MAX_FILE_MB} MB.")
        return

    is_pdf = mime == "application/pdf" or lower_name.endswith(".pdf")
    is_image = mime.startswith("image/") or lower_name.endswith((".jpg", ".jpeg", ".png", ".webp"))
    is_excel = lower_name.endswith((".xls", ".xlsx", ".xlsm", ".xlsb", ".ods")) or mime in {
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel.sheet.macroenabled.12",
        "application/vnd.ms-excel.sheet.binary.macroenabled.12",
        "application/vnd.oasis.opendocument.spreadsheet",
    }

    if not (is_pdf or is_image or is_excel):
        await update.message.reply_text(
            "❌ Ҳозирча PDF, JPG/JPEG, PNG, WEBP ва Excel "
            "XLS/XLSX/XLSM/XLSB/ODS қабул қиламан."
        )
        return

    status_text = (
        "⏳ Excel банк выпискасини ўқиб, кирим-чиқимни таҳлил қиляпман..."
        if is_excel
        else "⏳ Ҳужжатни юклаб, AI билан таҳлил қиляпман..."
    )
    status = await update.message.reply_text(status_text)

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        raw = bytes(await tg_file.download_as_bytearray())

        if is_excel:
            data = analyze_spreadsheet_bytes(raw, filename)
        elif is_pdf:
            data = analyze_pdf_bytes(raw, filename)
        else:
            data = analyze_image_bytes(raw, mime or "image/jpeg")

        await status.edit_text("✅ Таҳлил тайёр.")
        await show_analysis(update, context, data, doc.file_id, filename, mime)
    except Exception as exc:
        log.exception("Document analysis failed")
        await status.edit_text(f"❌ Ҳужжатни таҳлил қилишда хато:\n{exc}")


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "doc_cancel":
        context.user_data.pop("pending_doc", None)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("❌ Ҳужжат базага сақланмади.")
        return

    pending = context.user_data.get("pending_doc")
    if not pending:
        await query.message.reply_text("⚠️ Тасдиқланадиган ҳужжат топилмади.")
        return

    data = pending["data"]
    try:
        doc_id = save_document(
            data,
            pending.get("telegram_file_id"),
            pending.get("filename"),
            pending.get("mime_type"),
        )

        if data.get("document_type") == "contract":
            partner = data.get("partner") or {}
            if partner.get("name") and data.get("contract_number") and data.get("total") is not None:
                add_contract(
                    partner_name=partner["name"],
                    number=data["contract_number"],
                    total_amount=float(data["total"]),
                    contract_date=data.get("contract_date"),
                    currency=data.get("currency") or "UZS",
                    tin=partner.get("tin"),
                )

        context.user_data.pop("pending_doc", None)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"✅ Тасдиқланди ва базага сақланди. ID: {doc_id}")
    except Exception as exc:
        log.exception("Saving confirmed document failed")
        await query.message.reply_text(f"❌ Сақлашда хато: {exc}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled bot error", exc_info=context.error)


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN киритилмаган")

    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(confirm_callback, pattern=r"^doc_(confirm|cancel)$"))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(error_handler)

    mode = os.getenv("RUN_MODE", "polling").lower()
    if mode == "webhook":
        port = int(os.getenv("PORT", "10000"))
        base_url = (os.getenv("WEBHOOK_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
        if not base_url:
            raise RuntimeError("Webhook режимда WEBHOOK_BASE_URL ёки RENDER_EXTERNAL_URL керак")
        path = os.getenv("WEBHOOK_PATH", "telegram")
        secret = os.getenv("WEBHOOK_SECRET") or None
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=path,
            webhook_url=f"{base_url}/{path}",
            secret_token=secret,
            drop_pending_updates=True,
        )
    else:
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
