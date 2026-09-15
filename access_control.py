import os
from datetime import datetime, timedelta

from sqlalchemy import BigInteger, DateTime, Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import ApplicationHandlerStop


def _db_url() -> str:
    url = os.getenv("DATABASE_URL", "sqlite:///asman.db").strip()
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+psycopg" not in url:
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class AccessBase(DeclarativeBase):
    pass


class BotAccessUser(AccessBase):
    __tablename__ = "bot_access_users"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    active: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    added_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BotAccessEvent(AccessBase):
    __tablename__ = "bot_access_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    details: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


engine = create_engine(_db_url(), pool_pre_ping=True)


def init_access_db() -> None:
    AccessBase.metadata.create_all(engine)
    # Existing installations may not yet have last_seen_at.
    try:
        from sqlalchemy import inspect, text
        columns = {c["name"] for c in inspect(engine).get_columns("bot_access_users")}
        if "last_seen_at" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE bot_access_users ADD COLUMN last_seen_at TIMESTAMP"))
    except Exception:
        pass


def _get(user_id: int):
    with Session(engine) as session:
        return session.get(BotAccessUser, int(user_id))


def has_admin() -> bool:
    with Session(engine) as session:
        row = session.scalar(
            select(BotAccessUser).where(
                BotAccessUser.role == "admin",
                BotAccessUser.active == "yes",
            ).limit(1)
        )
        return row is not None


def is_admin(user_id: int) -> bool:
    row = _get(user_id)
    return bool(row and row.active == "yes" and row.role == "admin")


def is_allowed(user_id: int) -> bool:
    row = _get(user_id)
    return bool(row and row.active == "yes")


def _display_name(row: BotAccessUser) -> str:
    if row.full_name:
        return row.full_name
    if row.username:
        return f"@{row.username}"
    return str(row.telegram_user_id)


def _fmt_time(value: datetime | None) -> str:
    if not value:
        return "—"
    # Render stores UTC; show Uzbekistan time (UTC+5).
    return (value + timedelta(hours=5)).strftime("%d.%m %H:%M")


def record_event(user_id: int, event_type: str, details: str | None = None) -> None:
    with Session(engine) as session:
        session.add(
            BotAccessEvent(
                telegram_user_id=int(user_id),
                event_type=(event_type or "activity")[:32],
                details=(details or "")[:512] or None,
            )
        )
        session.commit()


def touch_user(user, *, pending_if_new: bool = False) -> None:
    now = datetime.utcnow()
    with Session(engine) as session:
        row = session.get(BotAccessUser, int(user.id))
        if row is None:
            row = BotAccessUser(
                telegram_user_id=int(user.id),
                role="user",
                active="pending" if pending_if_new else "no",
            )
            session.add(row)
        row.username = user.username
        row.full_name = user.full_name
        row.last_seen_at = now
        session.commit()


def register_access_request(user) -> None:
    touch_user(user, pending_if_new=True)
    row = _get(user.id)
    if row and row.active == "pending":
        record_event(user.id, "access_request", "Bot access requested")


def claim_admin(user_id: int, code: str, username: str | None = None,
                full_name: str | None = None) -> tuple[bool, str]:
    expected = (os.getenv("ADMIN_CLAIM_CODE") or "").strip()
    if not expected:
        return False, "ADMIN_CLAIM_CODE созланмаган."
    if has_admin():
        return False, "Admin аллақачон белгиланган."
    if (code or "").strip() != expected:
        return False, "Admin коди нотўғри."

    with Session(engine) as session:
        row = session.get(BotAccessUser, int(user_id))
        if row is None:
            row = BotAccessUser(telegram_user_id=int(user_id))
            session.add(row)
        row.role = "admin"
        row.active = "yes"
        row.username = username
        row.full_name = full_name
        row.added_by = int(user_id)
        row.last_seen_at = datetime.utcnow()
        session.commit()
    record_event(user_id, "admin_claim", "Admin account activated")
    return True, "👑 Сиз Admin сифатида тасдиқландингиз."


def grant_user(admin_id: int, user_id: int, label: str | None = None) -> tuple[bool, str]:
    if not is_admin(admin_id):
        return False, "Бу амал фақат Admin учун."
    if int(user_id) == int(admin_id):
        return False, "Сиз аллақачон Adminсиз."

    with Session(engine) as session:
        row = session.get(BotAccessUser, int(user_id))
        if row is None:
            row = BotAccessUser(telegram_user_id=int(user_id), role="user", active="yes")
            session.add(row)
        if row.role == "admin":
            return False, "Admin аккаунтини оддий фойдаланувчига ўзгартириб бўлмайди."
        row.role = "user"
        row.active = "yes"
        if label:
            row.full_name = label[:255]
        row.added_by = int(admin_id)
        session.commit()
    record_event(user_id, "access_granted", f"Granted by {admin_id}")
    return True, f"✅ Доступ берилди: {int(user_id)}"


def revoke_user(admin_id: int, user_id: int) -> tuple[bool, str]:
    if not is_admin(admin_id):
        return False, "Бу амал фақат Admin учун."
    if int(user_id) == int(admin_id):
        return False, "Admin ўз доступини ўчира олмайди."

    with Session(engine) as session:
        row = session.get(BotAccessUser, int(user_id))
        if row is None:
            return False, "Бу ID рўйхатда йўқ."
        if row.role == "admin":
            return False, "Admin аккаунтини ўчириб бўлмайди."
        row.active = "no"
        session.commit()
    record_event(user_id, "access_revoked", f"Revoked by {admin_id}")
    return True, f"🚫 Доступ ўчирилди: {int(user_id)}"


def list_access_users() -> list[BotAccessUser]:
    with Session(engine) as session:
        return list(session.scalars(
            select(BotAccessUser).order_by(BotAccessUser.role, BotAccessUser.telegram_user_id)
        ).all())


def list_pending_users() -> list[BotAccessUser]:
    with Session(engine) as session:
        return list(session.scalars(
            select(BotAccessUser)
            .where(BotAccessUser.active == "pending")
            .order_by(BotAccessUser.created_at.desc())
            .limit(30)
        ).all())


def recent_events(event_type: str | None = None, limit: int = 20) -> list[BotAccessEvent]:
    with Session(engine) as session:
        stmt = select(BotAccessEvent)
        if event_type:
            stmt = stmt.where(BotAccessEvent.event_type == event_type)
        stmt = stmt.order_by(BotAccessEvent.id.desc()).limit(limit)
        return list(session.scalars(stmt).all())


def _admin_menu(bot_module):
    try:
        keyboard = [list(row) for row in bot_module.MENU.keyboard]
    except Exception:
        keyboard = []
    if not any("👑 Admin панел" in row for row in keyboard):
        keyboard.append(["👑 Admin панел"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def _panel_markup():
    pending_count = len(list_pending_users())
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Фойдаланувчилар", callback_data="access_users")],
        [InlineKeyboardButton(f"🟡 Доступ сўровлари ({pending_count})", callback_data="access_pending")],
        [
            InlineKeyboardButton("📄 Ҳужжатлар тарихи", callback_data="access_docs"),
            InlineKeyboardButton("🕒 Фаоллик", callback_data="access_activity"),
        ],
        [InlineKeyboardButton("🔄 Янгилаш", callback_data="access_panel")],
    ])


def _panel_text() -> str:
    rows = list_access_users()
    active = sum(1 for r in rows if r.active == "yes")
    pending = sum(1 for r in rows if r.active == "pending")
    blocked = sum(1 for r in rows if r.active == "no")
    return (
        "👑 ADMIN ПАНЕЛ\n\n"
        f"✅ Фаол: {active}\n"
        f"🟡 Кутилаётган сўровлар: {pending}\n"
        f"🚫 Доступ ўчирилган: {blocked}\n\n"
        "Керакли бўлимни танланг:"
    )


def _users_view():
    rows = list_access_users()
    lines = ["👥 ФОЙДАЛАНУВЧИЛАР", ""]
    buttons = []
    for row in rows[:30]:
        status = "👑" if row.role == "admin" else ("✅" if row.active == "yes" else ("🟡" if row.active == "pending" else "🚫"))
        lines.append(
            f"{status} {_display_name(row)} | ID {row.telegram_user_id}\n"
            f"   Охирги фаоллик: {_fmt_time(row.last_seen_at)}"
        )
        if row.role != "admin":
            if row.active == "yes":
                buttons.append([InlineKeyboardButton(
                    f"🚫 {_display_name(row)[:25]}", callback_data=f"access_revoke:{row.telegram_user_id}"
                )])
            else:
                buttons.append([InlineKeyboardButton(
                    f"✅ {_display_name(row)[:25]}", callback_data=f"access_grant:{row.telegram_user_id}"
                )])
    if not rows:
        lines.append("Ҳозирча фойдаланувчилар йўқ.")
    buttons.append([InlineKeyboardButton("⬅️ Admin панел", callback_data="access_panel")])
    return "\n".join(lines)[:3900], InlineKeyboardMarkup(buttons)


def _pending_view():
    rows = list_pending_users()
    lines = ["🟡 ДОСТУП СЎРОВЛАРИ", ""]
    buttons = []
    for row in rows:
        lines.append(
            f"• {_display_name(row)}\n"
            f"  ID: {row.telegram_user_id} | {_fmt_time(row.last_seen_at)}"
        )
        buttons.append([InlineKeyboardButton(
            f"✅ Рухсат: {_display_name(row)[:22]}",
            callback_data=f"access_grant:{row.telegram_user_id}",
        )])
    if not rows:
        lines.append("Янги сўровлар йўқ.")
    buttons.append([InlineKeyboardButton("⬅️ Admin панел", callback_data="access_panel")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _activity_view():
    rows = list_access_users()
    rows.sort(key=lambda r: r.last_seen_at or datetime.min, reverse=True)
    lines = ["🕒 ОХИРГИ ФАОЛЛИК", ""]
    for row in rows[:25]:
        status = "👑" if row.role == "admin" else ("✅" if row.active == "yes" else ("🟡" if row.active == "pending" else "🚫"))
        lines.append(f"{status} {_display_name(row)} — {_fmt_time(row.last_seen_at)}")
    if not rows:
        lines.append("Маълумот йўқ.")
    return "\n".join(lines), InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Admin панел", callback_data="access_panel")]
    ])


def _documents_view():
    events = recent_events("document_upload", 25)
    user_map = {r.telegram_user_id: r for r in list_access_users()}
    lines = ["📄 ҲУЖЖАТЛАР ТАРИХИ", ""]
    for event in events:
        row = user_map.get(event.telegram_user_id)
        name = _display_name(row) if row else str(event.telegram_user_id)
        lines.append(f"• {_fmt_time(event.created_at)} | {name}\n  {event.details or 'Ҳужжат'}")
    if not events:
        lines.append("Ҳозирча юкланган ҳужжатлар тарихи йўқ.")
    return "\n".join(lines)[:3900], InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Admin панел", callback_data="access_panel")]
    ])


async def _safe_edit(query, text: str, reply_markup=None):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except Exception:
        await query.message.reply_text(text, reply_markup=reply_markup)


def install_access_control(bot_module):
    init_access_db()
    original_text_handler = bot_module.text_handler
    original_start = bot_module.start

    async def secure_access_check(update, context):
        user = update.effective_user
        if not user:
            raise ApplicationHandlerStop

        text = ""
        message = update.effective_message
        if message:
            text = (message.text or "").strip()

        claim_code = (os.getenv("ADMIN_CLAIM_CODE") or "").strip()
        is_claim = (
            not has_admin()
            and claim_code
            and (text == claim_code or (text.lower().startswith("admin:") and text.split(":", 1)[1].strip() == claim_code))
        )
        if is_claim:
            return

        allowed = is_allowed(user.id)
        touch_user(user, pending_if_new=not allowed)

        if allowed:
            if message and message.document:
                record_event(user.id, "document_upload", message.document.file_name or "document")
            elif message and message.photo:
                record_event(user.id, "document_upload", "Telegram photo")
            return

        register_access_request(user)
        if update.callback_query:
            await update.callback_query.answer("Киришга рухсат йўқ.", show_alert=True)
        elif message:
            await message.reply_text(
                "🔐 Бу бот ёпиқ. Фақат Admin рухсат берган фойдаланувчилар ишлата олади.\n\n"
                f"Сизнинг Telegram ID: {user.id}\n"
                "Admin панел орқали сизга доступ бериши мумкин."
            )
        raise ApplicationHandlerStop

    async def secure_start(update, context):
        user = update.effective_user
        if user:
            touch_user(user)
            record_event(user.id, "start", "/start")
        await original_start(update, context)
        if user and is_admin(user.id):
            await update.effective_message.reply_text(
                "👑 Admin панел тайёр.",
                reply_markup=_admin_menu(bot_module),
            )

    async def secure_text_handler(update, context):
        text = (update.effective_message.text or "").strip()
        user = update.effective_user
        if not user:
            return

        claim_code = (os.getenv("ADMIN_CLAIM_CODE") or "").strip()
        if not has_admin() and claim_code and (
            text == claim_code or text.lower().startswith("admin:")
        ):
            code = text if text == claim_code else text.split(":", 1)[1].strip()
            ok, message = claim_admin(
                user.id,
                code,
                username=user.username,
                full_name=user.full_name,
            )
            await update.effective_message.reply_text(
                message,
                reply_markup=_admin_menu(bot_module) if ok else None,
            )
            return

        if text in ("👑 Admin панел", "🔐 Доступ"):
            if not is_admin(user.id):
                await update.effective_message.reply_text("⛔ Бу бўлим фақат Admin учун.")
                return
            await update.effective_message.reply_text(_panel_text(), reply_markup=_panel_markup())
            return

        lower = text.lower()
        if lower.startswith("доступ:"):
            if not is_admin(user.id):
                await update.effective_message.reply_text("⛔ Бу амал фақат Admin учун.")
                return
            payload = text.split(":", 1)[1].strip()
            parts = [p.strip() for p in payload.split("|", 1)]
            try:
                target_id = int(parts[0])
            except ValueError:
                await update.effective_message.reply_text("❌ Формат: доступ: 123456789")
                return
            label = parts[1] if len(parts) > 1 else None
            _, message = grant_user(user.id, target_id, label)
            await update.effective_message.reply_text(message)
            return

        revoke_prefixes = ("доступ ўчир:", "доступ учир:", "доступ удалить:")
        if lower.startswith(revoke_prefixes):
            if not is_admin(user.id):
                await update.effective_message.reply_text("⛔ Бу амал фақат Admin учун.")
                return
            try:
                target_id = int(text.split(":", 1)[1].strip())
            except ValueError:
                await update.effective_message.reply_text("❌ Формат: доступ ўчир: 123456789")
                return
            _, message = revoke_user(user.id, target_id)
            await update.effective_message.reply_text(message)
            return

        await original_text_handler(update, context)

    async def admin_callback(update, context):
        query = update.callback_query
        user = update.effective_user
        if not user or not is_admin(user.id):
            await query.answer("⛔ Фақат Admin учун.", show_alert=True)
            return

        data = query.data or ""
        await query.answer()

        if data == "access_panel":
            await _safe_edit(query, _panel_text(), _panel_markup())
            return
        if data == "access_users":
            text, kb = _users_view()
            await _safe_edit(query, text, kb)
            return
        if data == "access_pending":
            text, kb = _pending_view()
            await _safe_edit(query, text, kb)
            return
        if data == "access_activity":
            text, kb = _activity_view()
            await _safe_edit(query, text, kb)
            return
        if data == "access_docs":
            text, kb = _documents_view()
            await _safe_edit(query, text, kb)
            return
        if data.startswith("access_grant:"):
            target_id = int(data.split(":", 1)[1])
            _, message = grant_user(user.id, target_id)
            text, kb = _users_view()
            await _safe_edit(query, f"{message}\n\n{text}", kb)
            return
        if data.startswith("access_revoke:"):
            target_id = int(data.split(":", 1)[1])
            _, message = revoke_user(user.id, target_id)
            text, kb = _users_view()
            await _safe_edit(query, f"{message}\n\n{text}", kb)
            return

    bot_module.access_check = secure_access_check
    bot_module.start = secure_start
    bot_module.text_handler = secure_text_handler

    def secure_main():
        if not bot_module.TOKEN:
            raise RuntimeError("BOT_TOKEN киритилмаган")

        bot_module.init_db()
        init_access_db()
        app = bot_module.Application.builder().token(bot_module.TOKEN).build()
        app.add_handler(bot_module.TypeHandler(bot_module.Update, bot_module.access_check), group=-1)
        app.add_handler(bot_module.CommandHandler("start", bot_module.start))
        app.add_handler(bot_module.CommandHandler("help", bot_module.help_cmd))
        app.add_handler(bot_module.CallbackQueryHandler(
            bot_module.confirm_callback,
            pattern=r"^doc_(confirm|cancel)(?::[0-9a-f]+)?$",
        ))
        app.add_handler(bot_module.CallbackQueryHandler(admin_callback, pattern=r"^access_"))
        app.add_handler(bot_module.MessageHandler(bot_module.filters.PHOTO, bot_module.photo_handler))
        app.add_handler(bot_module.MessageHandler(bot_module.filters.Document.ALL, bot_module.document_handler))
        app.add_handler(bot_module.MessageHandler(
            bot_module.filters.TEXT & ~bot_module.filters.COMMAND,
            bot_module.text_handler,
        ))
        app.add_error_handler(bot_module.error_handler)

        mode = os.getenv("RUN_MODE", "polling").lower()
        if mode == "webhook":
            port = int(os.getenv("PORT", "10000"))
            base_url = (
                os.getenv("WEBHOOK_BASE_URL")
                or os.getenv("RENDER_EXTERNAL_URL")
                or ""
            ).rstrip("/")
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
                drop_pending_updates=False,
            )
        else:
            app.run_polling(drop_pending_updates=False)

    bot_module.main = secure_main
