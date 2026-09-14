import enhancements
from telegram import ReplyKeyboardMarkup


ACT_DIRECTION_KB = ReplyKeyboardMarkup(
    [
        ["📤 Чиқим", "📥 Кирим"],
        ["🏠 Асосий меню"],
    ],
    resize_keyboard=True,
)


def _clear_state(context):
    for key in (
        "act_flow",
        "act_partner_map",
        "awaiting_reconciliation_partner",
    ):
        context.user_data.pop(key, None)


def _identity(party):
    tin = str(party.get("tin") or "").strip()
    account = str(party.get("account") or "").strip()
    name = str(party.get("name") or "").strip()
    return tin or account or name


def _button_label(party, index):
    name = " ".join(str(party.get("name") or "Ҳамкор").split()).strip()
    if len(name) > 34:
        name = name[:31] + "..."

    tin = str(party.get("tin") or "").strip()
    account = str(party.get("account") or "").strip()

    if tin:
        suffix = f"ИНН {tin}"
    elif account:
        suffix = f"с/р …{account[-8:]}"
    else:
        suffix = "ID йўқ"

    return f"{index}. 🏢 {name} · {suffix}"


def _partner_keyboard(parties, context, limit=40):
    mapping = {}
    rows = []

    for index, party in enumerate(parties[:limit], 1):
        label = _button_label(party, index)
        mapping[label] = _identity(party)
        rows.append([label])

    rows.append(["⬅️ Акт сверка", "🏠 Асосий меню"])
    context.user_data["act_partner_map"] = mapping
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def _show_direction(update):
    await update.message.reply_text(
        "🧮 АКТ СВЕРКА\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "Қайси йўналиш бўйича текширмоқчисиз?\n\n"
        "📤 ЧИҚИМ\n"
        "Сотув / харидорлар — чиқувчи фактура ва харидордан тушган тўловлар.\n\n"
        "📥 КИРИМ\n"
        "Харид / етказиб берувчилар — кирувчи фактура ва етказиб берувчига тўловлар.\n\n"
        "👇 Йўналишни танланг:",
        reply_markup=ACT_DIRECTION_KB,
    )


async def _show_partners(update, context, flow):
    outgoing, incoming = enhancements.reconciliation_partner_groups()
    parties = outgoing if flow == "outgoing" else incoming

    context.user_data["act_flow"] = flow
    context.user_data["awaiting_reconciliation_partner"] = True

    if not parties:
        title = "📤 ЧИҚИМ — ХАРИДОРЛАР" if flow == "outgoing" else "📥 КИРИМ — ЕТКАЗИБ БЕРУВЧИЛАР"
        await update.message.reply_text(
            f"{title}\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "Ҳозирча бу йўналишда ҳамкор топилмади.\n"
            "Аввал банк выпискаси ёки фактура реестрини юкланг.",
            reply_markup=ACT_DIRECTION_KB,
        )
        return

    title = "📤 ЧИҚИМ — ХАРИДОРЛАР" if flow == "outgoing" else "📥 КИРИМ — ЕТКАЗИБ БЕРУВЧИЛАР"
    subtitle = (
        "Сотув бўйича акт сверка"
        if flow == "outgoing"
        else "Харид бўйича акт сверка"
    )

    shown = min(len(parties), 40)
    extra = "" if len(parties) <= 40 else f"\n\nℹ️ Биринчи {shown} та кўрсатилди. Қолгани учун ИНН ёки ҳисоб рақамини ёзинг."

    await update.message.reply_text(
        f"{title}\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"{subtitle}\n"
        f"👥 Ҳамкорлар: {len(parties)} та\n\n"
        "Ҳамкорлар ИНН ва ҳисоб рақами бўйича бирлаштирилган.\n"
        "👇 Керакли ҳамкорни танланг:"
        f"{extra}",
        reply_markup=_partner_keyboard(parties, context),
    )


def install_reconciliation_ui(bot_module):
    original_text_handler = bot_module.text_handler
    main_labels = {button for row in bot_module.MENU.keyboard for button in row}

    async def text_handler(update, context):
        text = (update.message.text or "").strip()

        if text == "🧮 Акт сверка":
            _clear_state(context)
            await _show_direction(update)
            return

        if text == "📤 Чиқим":
            await _show_partners(update, context, "outgoing")
            return

        if text == "📥 Кирим":
            await _show_partners(update, context, "incoming")
            return

        if text == "⬅️ Акт сверка":
            _clear_state(context)
            await _show_direction(update)
            return

        if text == "🏠 Асосий меню":
            _clear_state(context)
            await update.message.reply_text(
                "🏠 Асосий меню",
                reply_markup=bot_module.MENU,
            )
            return

        partner_map = context.user_data.get("act_partner_map") or {}
        if text in partner_map:
            identifier = partner_map[text]
            result = enhancements.reconciliation_for_partner(identifier)
            _clear_state(context)
            await update.message.reply_text(result, reply_markup=bot_module.MENU)
            return

        if context.user_data.get("awaiting_reconciliation_partner"):
            # User may type a full TIN or bank account instead of tapping a button.
            if text not in main_labels:
                result = enhancements.reconciliation_for_partner(text)
                _clear_state(context)
                await update.message.reply_text(result, reply_markup=bot_module.MENU)
                return
            _clear_state(context)

        await original_text_handler(update, context)

    bot_module.text_handler = text_handler
