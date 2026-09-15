import traceback as tb
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from access_control import engine


class AdminMonitorBase(DeclarativeBase):
    pass


class BotSystemError(AdminMonitorBase):
    __tablename__ = "bot_system_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    tenant_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    error_type: Mapped[str] = mapped_column(String(128), nullable=False)
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    traceback_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    resolved_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


def init_admin_monitor_db() -> None:
    AdminMonitorBase.metadata.create_all(engine)


def capture_error(update, error) -> int | None:
    """Persist compact diagnostics without storing message or document bodies."""
    try:
        user = getattr(update, "effective_user", None) if update is not None else None
        user_id = int(user.id) if user else None
        tenant_id = None
        try:
            from tenant_control import _tenant_for_user
            if user_id is not None:
                tenant = _tenant_for_user(user_id)
                tenant_id = int(tenant["id"]) if tenant else None
        except Exception:
            tenant_id = None

        error_type = type(error).__name__ if error is not None else "UnknownError"
        message = str(error or "Unknown error")[:1000]
        trace = "".join(tb.format_exception(type(error), error, error.__traceback__)) if error else None
        if trace:
            trace = trace[-12000:]

        with Session(engine) as session:
            row = BotSystemError(
                telegram_user_id=user_id,
                tenant_id=tenant_id,
                error_type=error_type[:128],
                message=message,
                traceback_text=trace,
                status="open",
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row.id
    except Exception:
        return None


def list_open_errors(limit: int = 30):
    with Session(engine) as session:
        return list(session.scalars(
            select(BotSystemError)
            .where(BotSystemError.status == "open")
            .order_by(BotSystemError.id.desc())
            .limit(limit)
        ).all())


def get_error(error_id: int):
    with Session(engine) as session:
        return session.get(BotSystemError, int(error_id))


def resolve_error(error_id: int, admin_id: int) -> bool:
    with Session(engine) as session:
        row = session.get(BotSystemError, int(error_id))
        if row is None:
            return False
        row.status = "resolved"
        row.resolved_by = int(admin_id)
        row.resolved_at = datetime.utcnow()
        session.commit()
        return True


init_admin_monitor_db()
