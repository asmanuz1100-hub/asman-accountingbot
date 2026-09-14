from io import BytesIO

import enhancements
from telegram import InputFile, ReplyKeyboardMarkup

from reconciliation_pdf import build_reconciliation_pdf


ACT_DIRECTION_KB = ReplyKeyboardMarkup(
    [
        ["📤 Чиқим - сотув", "📥 Кирим - харид"],
        ["🗂 Сақланган актлар", "🏠 Асосий меню"],
    ],
    resize_keyboard=True,
)


def _clear_state(context):
    for key in (
        "act_flow",
        "act_partner_map",
        "act_saved_map",
        "awaiting_reconciliation_partner",
        "awaiting_saved_act_search",
    ):
        context.user_data.pop(key, None)


def _identity(party):
    tin = str(party.get("tin") or "").strip()
    account = str(party.get("account") or "").strip()
    name = str(party.get("name") or "").strip()
    return tin or account or name


def _short_name(name, max_len=28):
    text = " ".join(str(name or "Ҳамкор").split()).strip()
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def _partner_keyboard(parties, context, limit=15):
    mapping = {}
    rows = []

    for index, party in enumerate(parties[:limit], 1):
        label = f"{index}. 🏢 {_short_name(party.get('name'))}"
        mapping[label] = _identity(party)
        rows.append([label])

    rows.append(["⬅️ Акт сверка", "🏠 Асосий меню"])
    context.user_data["act_partner_map"] = mapping
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _partner_lines(parties, limit=15):
    lines = []
    for index, party in enumerate(parties[:limit], 1):
        lines += [
            f"{index}) 🏢 {party.get('name') or '—'}",
            f"   🆔 ИНН: {party.get('tin') or '—'}",
            f"   🏦 Ҳисоб рақами: {party.get('account') or '—'}",
        ]
    return "\n".join(lines)


def _saved_keyboard(items, context):
    mapping = {}
    rows = []
    for item in items[:15]:
        data = item["data"]
        partner = data.get("partner") or {}
        label = f"📄 #{item['id']} {_short_name(partner.get('name'), 22)}"
        mapping[label] = item["id"]
        rows.append([label])
    rows.append(["⬅️ Акт сверка", "🏠 Асосий меню"])
    context.user_data["act_saved_map"] = mapping
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def _show_direction(update):
    await update.message.reply_text(
        "🧮 АКТ СВЕРКА\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "Қайси йўналиш бўйича акт керак?\n\n"
        "📤 ЧИҚИМ - СОТУВ\n"
        "Харидорлар: чиқувчи фактуралар ва харидордан тушган тўловлар.\n\n"
        "📥 КИРИМ - ХАРИД\n"
        "Етказиб берувчилар: кирувчи фактуралар ва уларга қилинган тўловлар.\n\n"
        "🗂 САҚЛАНГАН АКТЛАР\n"
        "Олдин базага сақланган актларни қайта очиш ва PDF олиш.\n\n"
        "👇 Керакли бўлимни танланг:",
        reply_markup=ACT_DIRECTION_KB,
    )


async def _show_partners(update, context, flow):
    outgoing, incoming = enhancements.reconciliation_partner_groups()
    parties = outgoing if flow == "outgoing" else incoming

    context.user_data["act_flow"] = flow
    context.user_data["awaiting_reconciliation_partner"] = True
    context.user_data.pop("awaiting_saved_act_search", None)

    title = "📤 ЧИҚИМ - ХАРИДОРЛАР" if flow == "outgoing" else "📥 КИРИМ - ЕТКАЗИБ БЕРУВЧИЛАР"

    if not parties:
        await update.message.reply_text(
            f"{title}\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "Ҳозирча бу йўналишда ҳамкор топилмади.\n"
            "Аввал банк выпискаси ёки фактура реестрини юкланг.",
            reply_markup=ACT_DIRECTION_KB,
        )
        return

    shown = min(len(parties), 15)
    extra = ""
    if len(parties) > shown:
        extra = (
            f"\n\nℹ️ Рўйхатда биринчи {shown} та кўрсатилди. "
            "Қолган ҳамкорни номи, ИНН ёки ҳисоб рақами билан қидиринг."
        )

    await update.message.reply_text(
        f"{title}\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"👥 Жами ҳамкорлар: {len(parties)} та\n\n"
        f"{_partner_lines(parties, shown)}\n\n"
        "🔎 Қидириш мумкин:\n"
        "• компания номи\n"
        "• ИНН рақами\n"
        "• ҳисоб рақами\n\n"
        "👇 Тугмадан танланг ёки қидирув матнини ёзинг:"
        f"{extra}",
        reply_markup=_partner_keyboard(parties, context, shown),
    )


async def _send_pdf(update, data, caption="📄 Акт сверка PDF"):
    pdf_bytes = build_reconciliation_pdf(data)
    partner = data.get("partner") or {}
    safe = "".join(ch if ch.isalnum() else "_" for ch in str(partner.get("name") or "partner"))
    safe = safe[:45].strip("_") or "partner"
    report_id = data.get("report_id") or "new"
    filename = f"akt_sverka_{safe}_{report_id}.pdf"

    stream = BytesIO(pdf_bytes)
    stream.seek(0)
    await update.message.reply_document(
        document=InputFile(stream, filename=filename),
        caption=caption,
    )


async def _prepare_and_send(update, context, identifier):
    flow = context.user_data.get("act_flow")
    result = enhancements.prepare_reconciliation(identifier, flow)
    _clear_state(context)

    await update.message.reply_text(result.get("text") or "Натижа топилмади.", reply_markup=ACT_DIRECTION_KB)
    if not result.get("ok"):
        return

    try:
        await _send_pdf(update, result["data"], caption=f"📄 Акт сверка #{result['doc_id']} - батафсил PDF")
    except Exception as exc:
        await update.message.reply_text(
            f"⚠️ Акт базага сақланди, лекин PDF тайёрлашда хато: {exc}",
            reply_markup=ACT_DIRECTION_KB,
        )


async def _show_saved(update, context, query=None):
    items = enhancements.list_saved_reconciliations(query=query, limit=15)
    context.user_data.pop("awaiting_reconciliation_partner", None)
    context.user_data["awaiting_saved_act_search"] = True

    if not items:
        msg = (
            "🗂 САҚЛАНГАН АКТЛАР\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "Сақланган акт топилмади.\n\n"
            "Компания номи, ИНН, ҳисоб рақами ёки акт ID рақамини ёзиб қидиришингиз мумкин."
        )
        await update.message.reply_text(msg, reply_markup=ACT_DIRECTION_KB)
        return

    lines = ["🗂 САҚЛАНГАН АКТЛАР", "━━━━━━━━━━━━━━━━", ""]
    for item in items:
        data = item["data"]
        partner = data.get("partner") or {}
        period = data.get("statement_period") or {}
        icon = "📤" if data.get("flow") == "outgoing" else "📥" if data.get("flow") == "incoming" else "🔄"
        lines += [
            f"{icon} Акт #{item['id']}",
            f"   🏢 {partner.get('name') or '—'}",
            f"   🆔 ИНН: {partner.get('tin') or '—'}",
            f"   🏦 Ҳисоб рақами: {partner.get('account_number') or '—'}",
            f"   📅 {period.get('from') or '—'} - {period.get('to') or '—'}",
            "",
        ]

    lines += [
        "🔎 Қидириш учун компания номи, ИНН, ҳисоб рақами ёки акт ID ни ёзинг.",
        "👇 PDF олиш учун актни танланг:",
    ]

    await update.message.reply_text("\n".join(lines), reply_markup=_saved_keyboard(items, context))


async def _send_saved_pdf(update, context, doc_id):
    data = enhancements.get_saved_reconciliation(doc_id)
    if not data:
        await update.message.reply_text("❌ Сақланган акт топилмади.", reply_markup=ACT_DIRECTION_KB)
        return

    try:
        await _send_pdf(update, data, caption=f"🗂 Сақланган акт #{doc_id}")
    except Exception as exc:
        await update.message.reply_text(f"❌ PDF тайёрлашда хато: {exc}", reply_markup=ACT_DIRECTION_KB)


def install_reconciliation_ui(bot_module):
    original_text_handler = bot_module.text_handler
    main_labels = {getattr(button, "text", button) for row in bot_module.MENU.keyboard for button in row}

    async def text_handler(update, context):
        text = (update.message.text or "").strip()

        if text == "🧮 Акт сверка":
            _clear_state(context)
            await _show_direction(update)
            return

        if text == "📤 Чиқим - сотув":
            await _show_partners(update, context, "outgoing")
            return

        if text == "📥 Кирим - харид":
            await _show_partners(update, context, "incoming")
            return

        if text == "🗂 Сақланган актлар":
            _clear_state(context)
            await _show_saved(update, context)
            return

        if text == "⬅️ Акт сверка":
            _clear_state(context)
            await _show_direction(update)
            return

        if text == "🏠 Асосий меню":
            _clear_state(context)
            await update.message.reply_text("🏠 Асосий меню", reply_markup=bot_module.MENU)
            return

        saved_map = context.user_data.get("act_saved_map") or {}
        if text in saved_map:
            await _send_saved_pdf(update, context, saved_map[text])
            return

        partner_map = context.user_data.get("act_partner_map") or {}
        if text in partner_map:
            await _prepare_and_send(update, context, partner_map[text])
            return

        if context.user_data.get("awaiting_saved_act_search"):
            if text not in main_labels:
                await _show_saved(update, context, query=text)
                return
            _clear_state(context)

        if context.user_data.get("awaiting_reconciliation_partner"):
            if text not in main_labels:
                await _prepare_and_send(update, context, text)
                return
            _clear_state(context)

        await original_text_handler(update, context)

    bot_module.text_handler = text_handler
