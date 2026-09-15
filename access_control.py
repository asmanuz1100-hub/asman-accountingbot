import os
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from telegram import ReplyKeyboardMarkup
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
    active: Mapped[str] = mapped_column(String(8), nullable=False, default="yes")
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    added_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


engine = create_engine(_db_url(), pool_pre_ping=True)


def init_access_db() -> None:
    AccessBase.metadata.create_all(engine)


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
        session.commit()
    return True, "👑 Сиз Admin сифатида тасдиқландингиз."


def grant_user(admin_id: int, user_id: int, label: str | None = None) -> tuple[bool, str]:
    if not is_admin(admin_id):
        return False, "Бу амал фақат Admin учун."
    if int(user_id) == int(admin_id):
        return False, "Сиз аллақачон Adminсиз."

    with Session(engine) as session:
        row = session.get(BotAccessUser, int(user_id))
        if row is None:
            row = BotAccessUser(telegram_user_id=int(user_id), role="user")
            session.add(row)
        if row.role == "admin":
            return False, "Admin аккаунтини оддий фойдаланувчига ўзгартириб бўлмайди."
        row.role = "user"
        row.active = "yes"
        if label:
            row.full_name = label[:255]
        row.added_by = int(admin_id)
        session.commit()
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
    return True, f"🚫 Доступ ўчирилди: {int(user_id)}"


def list_access_users() -> list[BotAccessUser]:
    with Session(engine) as session:
        return list(session.scalars(
            select(BotAccessUser).order_by(BotAccessUser.role, BotAccessUser.telegram_user_id)
        ).all())


def _copy_menu_with_access(bot_module):
    try:
        keyboard = [list(row) for row in bot_module.MENU.keyboard]
        if not any("🔐 Доступ" in row for row in keyboard):
            keyboard.append(["🔐 Доступ"])
        bot_module.MENU = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    except Exception:
        pass


def install_access_control(bot_module):
    init_access_db()
    original_text_handler = bot_module.text_handler
    original_start = bot_module.start

    async def secure_access_check(update, context):
        user = update.effective_user
        if not user:
            raise ApplicationHandlerStop

        if is_allowed(user.id):
            return

        text = ""
        if update.effective_message:
            text = (update.effective_message.text or "").strip()

        # One-time bootstrap: before any admin exists, only the secret claim
        # message may pass through. After the first claim this path closes.
        if not has_admin() and text.lower().startswith("admin:"):
            return

        if update.callback_query:
            await update.callback_query.answer("Киришга рухсат йўқ.", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(
                "🔐 Бу бот ёпиқ. Фақат Admin рухсат берган фойдаланувчилар ишлата олади.\n\n"
                f"Сизнинг Telegram ID: {user.id}\n"
                "Шу ID'ни Admin'га юборинг."
            )
        raise ApplicationHandlerStop

    async def secure_start(update, context):
        await original_start(update, context)
        user = update.effective_user
        if user and is_admin(user.id):
            await update.effective_message.reply_text(
                "👑 Сиз Adminсиз.\n"
                "Доступ бошқаруви учун «🔐 Доступ» тугмасини босинг."
            )

    async def secure_text_handler(update, context):
        text = (update.effective_message.text or "").strip()
        user = update.effective_user
        if not user:
            return

        if text.lower().startswith("admin:"):
            code = text.split(":", 1)[1].strip()
            ok, message = claim_admin(
                user.id,
                code,
                username=user.username,
                full_name=user.full_name,
            )
            await update.effective_message.reply_text(message, reply_markup=bot_module.MENU if ok else None)
            return

        if text == "🔐 Доступ":
            if not is_admin(user.id):
                await update.effective_message.reply_text("⛔ Бу бўлим фақат Admin учун.")
                return
            rows = list_access_users()
            body = []
            for row in rows:
                status = "✅" if row.active == "yes" else "🚫"
                role = "👑 Admin" if row.role == "admin" else "👤 User"
                name = f" | {row.full_name}" if row.full_name else ""
                body.append(f"{status} {role} | {row.telegram_user_id}{name}")
            listing = "\n".join(body) if body else "Рўйхат бўш."
            await update.effective_message.reply_text(
                "🔐 ДОСТУП БОШҚАРУВИ\n\n"
                f"{listing}\n\n"
                "Рухсат бериш:\nдоступ: 123456789\n\n"
                "Исм билан:\nдоступ: 123456789 | Бухгалтер\n\n"
                "Рухсатни ўчириш:\nдоступ ўчир: 123456789"
            )
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

    bot_module.access_check = secure_access_check
    bot_module.start = secure_start
    bot_module.text_handler = secure_text_handler
    _copy_menu_with_access(bot_module)
