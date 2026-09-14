import os

# Old Render Blueprint deployments may retain an invalid Telegram webhook secret.
# Remove it before starting the bot so python-telegram-bot does not send it.
os.environ.pop("WEBHOOK_SECRET", None)

# Asakabank exports an HTML table with an .xls extension and without a normal
# Excel header row. Patch the spreadsheet analyzer before importing bot.py so
# these statements are parsed with the dedicated Asakabank layout handler.
import excel_docs
from asaka_excel import try_analyze_asaka

_original_analyze_spreadsheet = excel_docs.analyze_spreadsheet_bytes


def _analyze_spreadsheet(data: bytes, filename: str = "statement.xlsx") -> dict:
    asaka_result = try_analyze_asaka(data, filename)
    if asaka_result is not None:
        return asaka_result
    return _original_analyze_spreadsheet(data, filename)


excel_docs.analyze_spreadsheet_bytes = _analyze_spreadsheet

# Import the bot only after the spreadsheet analyzer has been patched.
import bot as bot_module


# Make partner creation conversational. Previously the user had to type the
# exact technical format "ҳамкор: Компания номи". Now pressing the Partners
# button puts the bot into partner-name entry mode and the next plain text is
# saved as the partner name.
_original_text_handler = bot_module.text_handler

_MENU_TEXTS = {
    "📎 Ҳужжат юклаш",
    "🤖 AI таҳлил",
    "👥 Ҳамкорлар",
    "📄 Шартномалар",
    "📦 Хом ашё омбори",
    "📥 Кирим",
    "🧾 Чиқиш фактура",
    "📊 Ҳисоботлар",
    "ℹ️ Ёрдам",
}


def _strip_partner_prefix(text: str) -> str:
    value = (text or "").strip()
    lower = value.lower()
    for prefix in ("ҳамкор:", "хамкор:", "hamkor:"):
        if lower.startswith(prefix):
            return value.split(":", 1)[1].strip()
    return value


async def _partner_text_handler(update, context):
    text = (update.message.text or "").strip()
    lower = text.lower()

    if text == "👥 Ҳамкорлар":
        rows = bot_module.list_partners()
        body = "\n".join(
            f"• {p.name}" + (f" | СТИР {p.tin}" if p.tin else "") for p in rows
        ) if rows else "Ҳозирча ҳамкорлар йўқ."

        context.user_data["awaiting_partner_name"] = True
        await update.message.reply_text(
            f"👥 Ҳамкорлар:\n\n{body}\n\n"
            "➕ Янги ҳамкор қўшиш учун компания номини ёзинг.\n"
            "Масалан: GRAND BUILD\n\n"
            "Бекор қилиш учун бошқа меню тугмасини босинг.",
            reply_markup=bot_module.MENU,
        )
        return

    # Keep accepting the old command too, including common spelling variants.
    explicit_partner = any(lower.startswith(p) for p in ("ҳамкор:", "хамкор:", "hamkor:"))
    awaiting = context.user_data.get("awaiting_partner_name")

    if awaiting and text in _MENU_TEXTS:
        context.user_data.pop("awaiting_partner_name", None)
        return await _original_text_handler(update, context)

    if awaiting or explicit_partner:
        name = _strip_partner_prefix(text)
        if not name:
            await update.message.reply_text("⚠️ Компания номини ёзинг. Масалан: GRAND BUILD")
            return
        try:
            partner = bot_module.upsert_partner(name)
            context.user_data.pop("awaiting_partner_name", None)
            await update.message.reply_text(
                f"✅ Ҳамкор сақланди: {partner.name}",
                reply_markup=bot_module.MENU,
            )
        except Exception as exc:
            await update.message.reply_text(f"❌ Ҳамкорни сақлашда хато: {exc}")
        return

    return await _original_text_handler(update, context)


bot_module.text_handler = _partner_text_handler

main = bot_module.main

if __name__ == "__main__":
    main()
