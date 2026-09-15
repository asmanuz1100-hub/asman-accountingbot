import os
from datetime import datetime, timedelta

from sqlalchemy import BigInteger, Boolean, DateTime, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from telegram.ext import ApplicationHandlerStop

from access_control import engine, grant_user, is_admin, list_access_users, record_event


class TrialBase(DeclarativeBase):
    pass


class BotTrial(TrialBase):
    __tablename__ = "bot_trials"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="trial")
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


DEFAULT_TRIAL_DAYS = 14
DEFAULT_PILOT_LIMIT = 5


def init_trial_db() -> None:
    TrialBase.metadata.create_all(engine)


def _now() -> datetime:
    return datetime.utcnow()


def _pilot_limit() -> int:
    try:
        return max(1, int(os.getenv("PILOT_TRIAL_LIMIT", str(DEFAULT_PILOT_LIMIT))))
    except Exception:
        return DEFAULT_PILOT_LIMIT


def _trial(user_id: int):
    with Session(engine) as session:
        return session.get(BotTrial, int(user_id))


def _active_trial_count(exclude_user_id: int | None = None) -> int:
    now = _now()
    with Session(engine) as session:
        stmt = select(BotTrial).where(
            BotTrial.status == "trial",
            BotTrial.expires_at > now,
        )
        rows = list(session.scalars(stmt).all())
    if exclude_user_id is not None:
        rows = [r for r in rows if int(r.telegram_user_id) != int(exclude_user_id)]
    return len(rows)


def _display_name(user_id: int) -> str:
    row = next((r for r in list_access_users() if int(r.telegram_user_id) == int(user_id)), None)
    if row is None:
        return str(user_id)
    if getattr(row, "full_name", None):
        return row.full_name
    if getattr(row, "username", None):
        return f"@{row.username}"
    return str(user_id)


def _remaining_text(expires_at: datetime) -> str:
    seconds = int((expires_at - _now()).total_seconds())
    if seconds <= 0:
        return "тугаган"
    days, rest = divmod(seconds, 86400)
    hours = rest // 3600
    if days:
        return f"{days} кун {hours} соат"
    return f"{max(1, hours)} соат"


def start_trial(admin_id: int, user_id: int, days: int = DEFAULT_TRIAL_DAYS, note: str | None = None) -> tuple[bool, str]:
    if not is_admin(admin_id):
        return False, "Бу амал фақат Admin учун."
    if int(user_id) == int(admin_id):
        return False, "Admin аккаунтига TEST режим керак эмас."
    try:
        days = int(days)
    except Exception:
        return False, "Кун сони нотўғри."
    if days < 1 or days > 90:
        return False, "TEST муддати 1–90 кун оралиғида бўлиши керак."

    existing = _trial(user_id)
    if not existing or existing.status != "trial" or existing.expires_at <= _now():
        if _active_trial_count(exclude_user_id=user_id) >= _pilot_limit():
            return False, f"Pilot лимити тўлган: {_pilot_limit()} та актив TEST мижоз."

    ok, msg = grant_user(admin_id, user_id)
    if not ok and "Admin" not in msg:
        return False, msg

    now = _now()
    with Session(engine) as session:
        row = session.get(BotTrial, int(user_id))
        if row is None:
            row = BotTrial(
                telegram_user_id=int(user_id),
                expires_at=now + timedelta(days=days),
            )
            session.add(row)
        row.status = "trial"
        row.started_at = now
        row.expires_at = now + timedelta(days=days)
        row.created_by = int(admin_id)
        row.note = (note or "")[:255] or None
        row.updated_at = now
        session.commit()

    record_event(user_id, "trial_started", f"{days} days by {admin_id}")
    return True, f"🧪 TEST доступ берилди: {_display_name(user_id)}\nМуддат: {days} кун"


def cancel_trial(admin_id: int, user_id: int) -> tuple[bool, str]:
    if not is_admin(admin_id):
        return False, "Бу амал фақат Admin учун."
    with Session(engine) as session:
        row = session.get(BotTrial, int(user_id))
        if row is None:
            return False, "Бу фойдаланувчида TEST режими йўқ."
        row.status = "cancelled"
        row.updated_at = _now()
        session.commit()
    record_event(user_id, "trial_cancelled", f"by {admin_id}")
    return True, f"🚫 TEST доступ тўхтатилди: {_display_name(user_id)}"


def convert_to_paid(admin_id: int, user_id: int) -> tuple[bool, str]:
    if not is_admin(admin_id):
        return False, "Бу амал фақат Admin учун."
    with Session(engine) as session:
        row = session.get(BotTrial, int(user_id))
        if row is None:
            return False, "Бу фойдаланувчи TEST рўйхатида йўқ."
        row.status = "paid"
        row.updated_at = _now()
        session.commit()
    record_event(user_id, "trial_converted", f"paid by {admin_id}")
    return True, f"💳 Пуллик мижозга ўтказилди: {_display_name(user_id)}"


def trial_status(user_id: int) -> str:
    row = _trial(user_id)
    if row is None:
        return "TEST режими йўқ."
    if row.status == "paid":
        return "💳 Пуллик доступ фаол."
    if row.status == "cancelled":
        return "🚫 TEST доступ Admin томонидан тўхтатилган."
    if row.status == "expired" or row.expires_at <= _now():
        return "⏳ TEST муддати тугаган."
    return (
        "🧪 TEST режим фаол.\n"
        f"Қолган вақт: {_remaining_text(row.expires_at)}\n"
        f"Тугаш санаси: {(row.expires_at + timedelta(hours=5)).strftime('%d.%m.%Y %H:%M')}"
    )


def _list_trials_text() -> str:
    with Session(engine) as session:
        rows = list(session.scalars(select(BotTrial).order_by(BotTrial.updated_at.desc())).all())
    if not rows:
        return "🧪 Ҳозирча TEST мижозлар йўқ."
    lines = [
        "🧪 TEST / PILOT МИЖОЗЛАР",
        f"Актив лимит: {_active_trial_count()}/{_pilot_limit()}",
        "",
    ]
    for row in rows[:30]:
        if row.status == "trial" and row.expires_at > _now():
            state = f"🟢 {_remaining_text(row.expires_at)}"
        elif row.status == "paid":
            state = "💳 пуллик"
        elif row.status == "cancelled":
            state = "🚫 тўхтатилган"
        else:
            state = "⏳ тугаган"
        lines.append(f"• {_display_name(row.telegram_user_id)} | {row.telegram_user_id} | {state}")
    return "\n".join(lines)[:3900]


def _expire_if_needed(user_id: int):
    row = _trial(user_id)
    if row is None:
        return None
    if row.status == "trial" and row.expires_at <= _now():
        with Session(engine) as session:
            current = session.get(BotTrial, int(user_id))
            if current and current.status == "trial":
                current.status = "expired"
                current.updated_at = _now()
                session.commit()
        record_event(user_id, "trial_expired", "Trial period ended")
        row.status = "expired"
    return row


def install_trial_control(bot_module) -> None:
    init_trial_db()

    original_access_check = bot_module.access_check
    original_text_handler = bot_module.text_handler
    original_start = bot_module.start

    async def trial_access_check(update, context):
        await original_access_check(update, context)
        user = update.effective_user
        if not user or is_admin(user.id):
            return

        row = _expire_if_needed(user.id)
        if row is None or row.status == "paid":
            return
        if row.status == "trial" and row.expires_at > _now():
            return

        message = update.effective_message
        if update.callback_query:
            await update.callback_query.answer(
                "⏳ TEST муддати тугаган. Admin билан боғланинг.",
                show_alert=True,
            )
        elif message:
            await message.reply_text(
                "⏳ TEST ДАВРИ ТУГАДИ\n\n"
                "Ботдан фойдаланишни давом эттириш учун Admin орқали пуллик тарифни фаоллаштиринг."
            )
        raise ApplicationHandlerStop

    async def trial_start(update, context):
        await original_start(update, context)
        user = update.effective_user
        if user and not is_admin(user.id):
            row = _trial(user.id)
            if row and row.status == "trial" and row.expires_at > _now():
                await update.effective_message.reply_text(trial_status(user.id))

    async def trial_text_handler(update, context):
        text = (update.effective_message.text or "").strip()
        lower = text.casefold()
        user = update.effective_user
        if not user:
            return

        if lower in ("тест статус", "test status", "🧪 тест статуси"):
            await update.effective_message.reply_text(trial_status(user.id))
            return

        if is_admin(user.id):
            if lower == "тестлар":
                await update.effective_message.reply_text(_list_trials_text())
                return

            if lower.startswith(("тест ўчир:", "тест учир:", "test off:")):
                try:
                    target_id = int(text.split(":", 1)[1].strip())
                except Exception:
                    await update.effective_message.reply_text("❌ Формат: тест ўчир: 123456789")
                    return
                _, message = cancel_trial(user.id, target_id)
                await update.effective_message.reply_text(message)
                return

            if lower.startswith(("пуллик:", "paid:")):
                try:
                    target_id = int(text.split(":", 1)[1].strip())
                except Exception:
                    await update.effective_message.reply_text("❌ Формат: пуллик: 123456789")
                    return
                _, message = convert_to_paid(user.id, target_id)
                await update.effective_message.reply_text(message)
                return

            if lower.startswith(("тест:", "test:")):
                payload = text.split(":", 1)[1].strip()
                parts = [p.strip() for p in payload.split("|")]
                try:
                    target_id = int(parts[0])
                    days = int(parts[1]) if len(parts) > 1 and parts[1] else DEFAULT_TRIAL_DAYS
                except Exception:
                    await update.effective_message.reply_text(
                        "❌ Формат: тест: 123456789 | 14\n"
                        "Кун ёзилмаса автоматик 14 кун берилади."
                    )
                    return
                note = parts[2] if len(parts) > 2 else None
                _, message = start_trial(user.id, target_id, days, note)
                await update.effective_message.reply_text(message)
                return

        await original_text_handler(update, context)

        if is_admin(user.id) and text in ("👑 Admin панел", "🔐 Доступ"):
            await update.effective_message.reply_text(
                "🧪 TEST / PILOT бошқаруви\n\n"
                "14 кунлик тест бериш:\n"
                "тест: 123456789\n\n"
                "Муддатни ўзингиз белгилаш:\n"
                "тест: 123456789 | 7\n\n"
                "TEST'ни тўхтатиш:\n"
                "тест ўчир: 123456789\n\n"
                "Пуллик мижозга ўтказиш:\n"
                "пуллик: 123456789\n\n"
                "Рўйхат:\n"
                "тестлар"
            )

    bot_module.access_check = trial_access_check
    bot_module.start = trial_start
    bot_module.text_handler = trial_text_handler
