from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationHandlerStop

from access_control import engine, is_admin, is_allowed, list_access_users, record_event


class PermissionBase(DeclarativeBase):
    pass


class BotUserPermission(PermissionBase):
    __tablename__ = "bot_user_permissions"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    permission: Mapped[str] = mapped_column(String(40), primary_key=True)
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


PERMISSIONS = {
    "documents": "📎 Ҳужжат / AI таҳлил",
    "confirm": "✅ Ҳужжатни тасдиқлаш",
    "reconciliation": "🧮 Акт сверка",
    "reports": "📊 Ҳисобот / PDF",
    "review": "🔎 Топилмаган / хатоли",
    "expenses": "💸 Чиқимлар",
    "partners": "👥 Ҳамкорлар",
    "contracts": "📄 Шартномалар",
    "warehouse": "📦 Хом ашё омбори",
    "sales": "🧾 Чиқиш фактура",
}

BASIC_TEXTS = {
    "🏠 Асосий меню",
    "ℹ️ Ёрдам",
}

TEXT_PERMISSION = {
    "📎 Ҳужжат юклаш": "documents",
    "🤖 AI таҳлил": "documents",
    "🧮 Акт сверка": "reconciliation",
    "📤 Чиқим - сотув": "reconciliation",
    "📥 Кирим - харид": "reconciliation",
    "🗂 Сақланган актлар": "reconciliation",
    "⬅️ Акт сверка": "reconciliation",
    "📊 Ҳисоботлар": "reports",
    "📈 Молиявий таҳлил": "reports",
    "🔎 Топилмаган / хатоли": "review",
    "❗ Хатоли ёзувлар": "review",
    "🧾 Тўловсиз фактуралар": "review",
    "🏦 Фактурасиз тўловлар": "review",
    "📋 Барча ёзувлар": "review",
    "⬅️ Текширув олдинги": "review",
    "➡️ Текширув кейинги": "review",
    "📥 Текширув PDF": "review",
    "🔗 Ҳамкорни боғлаш": "review",
    "✏️ Майдонни тузатиш": "review",
    "📤 Сотув йўналиши": "review",
    "📥 Харид йўналиши": "review",
    "🧾 Фактурага боғлаш": "review",
    "🏦 Савдога тегишли эмас": "review",
    "🚫 Ҳисобдан чиқариш": "review",
    "↩️ Ҳисобга қайтариш": "review",
    "✅ Тузатишни сақлаш": "review",
    "❌ Тузатишни бекор қилиш": "review",
    "💸 Чиқимлар": "expenses",
    "🧱 Хом ашё харажати": "expenses",
    "💡 Коммунал харажат": "expenses",
    "🧾 Солиқ тўловлари": "expenses",
    "📦 Бошқа харажат": "expenses",
    "👥 Ҳамкорлар": "partners",
    "📄 Шартномалар": "contracts",
    "📦 Хом ашё омбори": "warehouse",
    "📥 Кирим": "warehouse",
    "🧾 Чиқиш фактура": "sales",
}


def init_permissions_db() -> None:
    PermissionBase.metadata.create_all(engine)


def _display_name(row) -> str:
    if getattr(row, "full_name", None):
        return row.full_name
    if getattr(row, "username", None):
        return f"@{row.username}"
    return str(row.telegram_user_id)


def has_permission(user_id: int, permission: str) -> bool:
    if is_admin(user_id):
        return True
    if not is_allowed(user_id):
        return False
    if permission not in PERMISSIONS:
        return False

    with Session(engine) as session:
        row = session.get(BotUserPermission, (int(user_id), permission))
        # General access is granted only by Admin. Keep existing behaviour unless
        # Admin explicitly disables a section for that user.
        return True if row is None else bool(row.allowed)


def set_permission(admin_id: int, user_id: int, permission: str, allowed: bool) -> tuple[bool, str]:
    if not is_admin(admin_id):
        return False, "Бу амал фақат Admin учун."
    if permission not in PERMISSIONS:
        return False, "Номаълум ҳуқуқ."
    if is_admin(user_id):
        return False, "Admin ҳуқуқларини чеклаб бўлмайди."

    with Session(engine) as session:
        row = session.get(BotUserPermission, (int(user_id), permission))
        if row is None:
            row = BotUserPermission(
                telegram_user_id=int(user_id),
                permission=permission,
            )
            session.add(row)
        row.allowed = bool(allowed)
        row.updated_by = int(admin_id)
        row.updated_at = datetime.utcnow()
        session.commit()

    state = "ON" if allowed else "OFF"
    record_event(user_id, "permission_changed", f"{permission}={state} by {admin_id}")
    return True, f"{'✅' if allowed else '🚫'} {PERMISSIONS[permission]}"


def set_all_permissions(admin_id: int, user_id: int, allowed: bool) -> tuple[bool, str]:
    if not is_admin(admin_id):
        return False, "Бу амал фақат Admin учун."
    if is_admin(user_id):
        return False, "Admin ҳуқуқларини чеклаб бўлмайди."
    for key in PERMISSIONS:
        set_permission(admin_id, user_id, key, allowed)
    return True, "✅ Барча ҳуқуқлар ёқилди." if allowed else "🚫 Барча ҳуқуқлар ўчирилди."


def _active_users():
    return [
        row for row in list_access_users()
        if row.role != "admin" and row.active == "yes"
    ]


def _permission_users_view():
    rows = _active_users()
    text = [
        "🔑 ФОЙДАЛАНУВЧИ ҲУҚУҚЛАРИ",
        "",
        "Ҳуқуқларини бошқариш учун фойдаланувчини танланг:",
    ]
    buttons = []
    for row in rows[:30]:
        text.append(f"• {_display_name(row)} | ID {row.telegram_user_id}")
        buttons.append([
            InlineKeyboardButton(
                f"🔑 {_display_name(row)[:28]}",
                callback_data=f"perm_user:{row.telegram_user_id}",
            )
        ])
    if not rows:
        text.append("Ҳозирча актив оддий фойдаланувчи йўқ.")
    buttons.append([InlineKeyboardButton("⬅️ Admin панел", callback_data="access_panel")])
    return "\n".join(text), InlineKeyboardMarkup(buttons)


def _user_permission_view(user_id: int):
    row = next((r for r in list_access_users() if int(r.telegram_user_id) == int(user_id)), None)
    name = _display_name(row) if row else str(user_id)
    lines = [
        f"🔑 ҲУҚУҚЛАР — {name}",
        f"ID: {user_id}",
        "",
        "Тугмани босиб ҳуқуқни ёқинг/ўчиринг:",
    ]
    buttons = []
    for key, label in PERMISSIONS.items():
        enabled = has_permission(user_id, key)
        lines.append(f"{'✅' if enabled else '🚫'} {label}")
        buttons.append([
            InlineKeyboardButton(
                f"{'✅' if enabled else '🚫'} {label}",
                callback_data=f"perm_toggle:{user_id}:{key}",
            )
        ])
    buttons.extend([
        [
            InlineKeyboardButton("✅ Ҳаммасини ёқиш", callback_data=f"perm_all:{user_id}:on"),
            InlineKeyboardButton("🚫 Ҳаммасини ўчириш", callback_data=f"perm_all:{user_id}:off"),
        ],
        [InlineKeyboardButton("⬅️ Фойдаланувчилар", callback_data="perm_panel")],
        [InlineKeyboardButton("⬅️ Admin панел", callback_data="access_panel")],
    ])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def _safe_edit(query, text: str, reply_markup=None):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except Exception:
        await query.message.reply_text(text, reply_markup=reply_markup)


def _required_permission(update, context) -> str | None:
    query = update.callback_query
    if query:
        data = query.data or ""
        if data.startswith("doc_confirm"):
            return "confirm"
        if data.startswith("doc_cancel"):
            return None
        return None

    message = update.effective_message
    if not message:
        return None
    if message.document or message.photo:
        return "documents"

    text = (message.text or "").strip()
    if not text or text.startswith("/") or text in BASIC_TEXTS:
        return None

    direct = TEXT_PERMISSION.get(text)
    if direct:
        return direct

    lower = text.casefold()
    if lower.startswith("ҳамкор:"):
        return "partners"
    if lower.startswith("шартнома:"):
        return "contracts"
    if text.startswith("+"):
        return "warehouse"

    data = context.user_data
    if data.get("expense_category"):
        return "expenses"
    if any(key in data for key in (
        "act_flow", "act_partner_map", "act_saved_map",
        "awaiting_reconciliation_partner", "awaiting_saved_act_search",
    )):
        return "reconciliation"
    if any(key.startswith("review_") for key in data):
        return "review"
    return None


async def permission_callback(update, context):
    query = update.callback_query
    user = update.effective_user
    if not user or not is_admin(user.id):
        await query.answer("⛔ Фақат Admin учун.", show_alert=True)
        return

    data = query.data or ""
    await query.answer()

    if data == "perm_panel":
        text, kb = _permission_users_view()
        await _safe_edit(query, text, kb)
        return

    if data.startswith("perm_user:"):
        target_id = int(data.split(":", 1)[1])
        text, kb = _user_permission_view(target_id)
        await _safe_edit(query, text, kb)
        return

    if data.startswith("perm_toggle:"):
        _, raw_id, permission = data.split(":", 2)
        target_id = int(raw_id)
        current = has_permission(target_id, permission)
        set_permission(user.id, target_id, permission, not current)
        text, kb = _user_permission_view(target_id)
        await _safe_edit(query, text, kb)
        return

    if data.startswith("perm_all:"):
        _, raw_id, state = data.split(":", 2)
        target_id = int(raw_id)
        set_all_permissions(user.id, target_id, state == "on")
        text, kb = _user_permission_view(target_id)
        await _safe_edit(query, text, kb)
        return


def install_permissions_control(bot_module) -> None:
    init_permissions_db()

    original_access_check = bot_module.access_check
    original_text_handler = bot_module.text_handler
    real_callback_handler = bot_module.CallbackQueryHandler

    async def permission_access_check(update, context):
        await original_access_check(update, context)
        user = update.effective_user
        if not user or is_admin(user.id):
            return

        permission = _required_permission(update, context)
        if not permission or has_permission(user.id, permission):
            return

        label = PERMISSIONS.get(permission, permission)
        if update.callback_query:
            await update.callback_query.answer(
                f"⛔ Сизда «{label}» ҳуқуқи йўқ.",
                show_alert=True,
            )
        elif update.effective_message:
            await update.effective_message.reply_text(
                f"⛔ Сизда «{label}» бўлимига рухсат йўқ.\n"
                "Admin панел орқали ҳуқуқ бериши мумкин."
            )
        raise ApplicationHandlerStop

    async def permission_text_handler(update, context):
        text = (update.effective_message.text or "").strip()
        await original_text_handler(update, context)
        user = update.effective_user
        if user and is_admin(user.id) and text in ("👑 Admin панел", "🔐 Доступ"):
            await update.effective_message.reply_text(
                "🔑 Фойдаланувчиларга бўлимлар бўйича ҳуқуқ бериш:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔑 Ҳуқуқларни бошқариш", callback_data="perm_panel")
                ]]),
            )

    def callback_handler_factory(callback, *args, **kwargs):
        pattern = kwargs.get("pattern")
        if pattern == r"^access_":
            async def combined_callback(update, context):
                data = update.callback_query.data or ""
                if data.startswith("perm_"):
                    return await permission_callback(update, context)
                return await callback(update, context)

            kwargs["pattern"] = r"^(?:access_|perm_)"
            return real_callback_handler(combined_callback, *args, **kwargs)
        return real_callback_handler(callback, *args, **kwargs)

    bot_module.access_check = permission_access_check
    bot_module.text_handler = permission_text_handler
    bot_module.CallbackQueryHandler = callback_handler_factory
