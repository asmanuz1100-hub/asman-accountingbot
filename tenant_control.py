import re
from contextvars import ContextVar
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, event, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from telegram.ext import ApplicationHandlerStop

import database as business_db
from access_control import engine as control_engine, is_admin, is_allowed, record_event


class TenantBase(DeclarativeBase):
    pass


class BotTenant(TenantBase):
    __tablename__ = "bot_tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    schema_name: Mapped[str] = mapped_column(String(63), nullable=False, unique=True, index=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    provisioned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BotTenantUser(TenantBase):
    __tablename__ = "bot_tenant_users"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("bot_tenants.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="owner")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


_current_schema: ContextVar[str] = ContextVar("business_tenant_schema", default="public")
_SCHEMA_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_search_path_hook_installed = False


def _validate_schema(schema: str) -> str:
    schema = (schema or "").strip().lower()
    if not _SCHEMA_RE.fullmatch(schema):
        raise ValueError("Invalid tenant schema")
    return schema


def _schema_for_user(user_id: int) -> str:
    return _validate_schema(f"tenant_u{abs(int(user_id))}")


def set_business_schema(schema: str):
    return _current_schema.set(_validate_schema(schema))


def reset_business_schema(token) -> None:
    _current_schema.reset(token)


def current_business_schema() -> str:
    return _current_schema.get()


def _apply_search_path(dbapi_connection, connection_record, connection_proxy) -> None:
    """Strict isolation: business connections see one schema only, never public fallback."""
    if business_db.engine.dialect.name != "postgresql":
        return
    schema = _validate_schema(_current_schema.get())
    old_autocommit = getattr(dbapi_connection, "autocommit", None)
    cursor = None
    try:
        if old_autocommit is not None:
            dbapi_connection.autocommit = True
        cursor = dbapi_connection.cursor()
        cursor.execute(f'SET SESSION search_path TO "{schema}"')
    finally:
        if cursor is not None:
            cursor.close()
        if old_autocommit is not None:
            dbapi_connection.autocommit = old_autocommit


def _install_search_path_hook() -> None:
    global _search_path_hook_installed
    if _search_path_hook_installed:
        return
    if business_db.engine.dialect.name == "postgresql":
        event.listen(business_db.engine.pool, "checkout", _apply_search_path)
    _search_path_hook_installed = True


def init_tenant_registry() -> None:
    TenantBase.metadata.create_all(control_engine)
    _install_search_path_hook()


def _tenant_for_user(user_id: int):
    with Session(control_engine) as session:
        membership = session.get(BotTenantUser, int(user_id))
        if membership is None:
            return None
        tenant = session.get(BotTenant, membership.tenant_id)
        if tenant is None:
            return None
        return {
            "id": tenant.id,
            "name": tenant.name,
            "schema_name": tenant.schema_name,
            "owner_user_id": tenant.owner_user_id,
            "status": tenant.status,
            "provisioned_at": tenant.provisioned_at,
            "role": membership.role,
        }


def _mark_provisioned(tenant_id: int) -> None:
    with Session(control_engine) as session:
        row = session.get(BotTenant, int(tenant_id))
        if row is not None:
            row.provisioned_at = datetime.utcnow()
            session.commit()


def _provision_schema(tenant: dict) -> None:
    schema = _validate_schema(tenant["schema_name"])
    if schema == "public":
        _mark_provisioned(tenant["id"])
        return
    if business_db.engine.dialect.name != "postgresql":
        raise RuntimeError("Customer database isolation requires PostgreSQL")

    # DDL goes through the control connection, whose search_path is always public.
    with control_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))

    token = set_business_schema(schema)
    try:
        # All existing business models are created inside this customer's schema.
        business_db.init_db()
    finally:
        reset_business_schema(token)
    _mark_provisioned(tenant["id"])


def ensure_tenant_for_user(user_id: int, name: str | None = None) -> dict:
    """Return/provision the isolated customer workspace for a bot user."""
    user_id = int(user_id)
    existing = _tenant_for_user(user_id)
    if existing is not None:
        if existing["status"] != "active":
            raise RuntimeError("Customer database is inactive")
        if existing["provisioned_at"] is None:
            _provision_schema(existing)
            existing = _tenant_for_user(user_id) or existing
        return existing

    schema = "public" if is_admin(user_id) else _schema_for_user(user_id)
    with Session(control_engine) as session:
        tenant = session.scalar(select(BotTenant).where(BotTenant.owner_user_id == user_id))
        if tenant is None:
            tenant = BotTenant(
                name=(name or "")[:255] or None,
                schema_name=schema,
                owner_user_id=user_id,
                status="active",
                provisioned_at=datetime.utcnow() if schema == "public" else None,
            )
            session.add(tenant)
            session.flush()
        membership = session.get(BotTenantUser, user_id)
        if membership is None:
            membership = BotTenantUser(
                telegram_user_id=user_id,
                tenant_id=tenant.id,
                role="owner",
            )
            session.add(membership)
        session.commit()
        tenant_id = tenant.id

    result = _tenant_for_user(user_id)
    if result is None:
        raise RuntimeError("Customer database mapping failed")
    if result["provisioned_at"] is None:
        _provision_schema(result)
        result = _tenant_for_user(user_id) or result
    record_event(user_id, "tenant_ready", f"tenant={tenant_id}")
    return result


def assign_user_to_owner_tenant(admin_id: int, user_id: int, owner_user_id: int) -> tuple[bool, str]:
    """Allow multiple employees of one customer to share that customer's isolated DB."""
    if not is_admin(admin_id):
        return False, "Бу амал фақат Admin учун."
    if is_admin(user_id):
        return False, "Admin базасини бошқа мижоз базасига ўтказиб бўлмайди."
    owner_tenant = ensure_tenant_for_user(int(owner_user_id))
    with Session(control_engine) as session:
        membership = session.get(BotTenantUser, int(user_id))
        if membership is None:
            membership = BotTenantUser(
                telegram_user_id=int(user_id),
                tenant_id=int(owner_tenant["id"]),
                role="member",
            )
            session.add(membership)
        else:
            membership.tenant_id = int(owner_tenant["id"])
            membership.role = "member"
        session.commit()
    record_event(user_id, "tenant_assigned", f"tenant={owner_tenant['id']} by {admin_id}")
    return True, f"✅ Фойдаланувчи мижоз базасига қўшилди. База #{owner_tenant['id']}"


def _list_tenants_text() -> str:
    with Session(control_engine) as session:
        tenants = list(session.scalars(select(BotTenant).order_by(BotTenant.id)).all())
        memberships = list(session.scalars(select(BotTenantUser)).all())
    counts = {}
    for row in memberships:
        counts[row.tenant_id] = counts.get(row.tenant_id, 0) + 1
    if not tenants:
        return "🔒 Ҳозирча мижоз базалари йўқ."
    lines = ["🔒 АЛОҲИДА МИЖОЗ БАЗАЛАРИ", ""]
    for tenant in tenants[:50]:
        kind = "👑 ASMAN" if tenant.schema_name == "public" else "🏢 Мижоз"
        state = "✅" if tenant.provisioned_at else "⏳"
        label = tenant.name or f"Owner {tenant.owner_user_id}"
        lines.append(
            f"{state} {kind} | База #{tenant.id} | {label}\n"
            f"   Owner ID: {tenant.owner_user_id} | Фойдаланувчи: {counts.get(tenant.id, 0)} та"
        )
    return "\n".join(lines)[:3900]


def tenant_status_text(user_id: int) -> str:
    tenant = _tenant_for_user(int(user_id))
    if tenant is None:
        return "🔒 Сиз учун алоҳида база ҳали яратилмаган."
    return (
        "🔒 МАЪЛУМОТЛАР ИЗОЛЯЦИЯСИ ФАОЛ\n\n"
        f"База ID: #{tenant['id']}\n"
        f"Ҳолат: {'✅ тайёр' if tenant['provisioned_at'] else '⏳ тайёрланмоқда'}\n"
        "Сизнинг ҳужжат, банк, фактура, акт сверка ва ҳисобот маълумотларингиз "
        "бошқа мижозлардан алоҳида сақланади."
    )


def _extract_first_id(text_value: str) -> int | None:
    payload = text_value.split(":", 1)[1] if ":" in text_value else ""
    first = payload.split("|", 1)[0].strip()
    try:
        return int(first)
    except Exception:
        return None


def install_tenant_control(bot_module) -> None:
    init_tenant_registry()

    original_access_check = bot_module.access_check
    original_text_handler = bot_module.text_handler

    # Preserve all existing ASMAN data in public and explicitly map the Admin to it.
    try:
        from access_control import list_access_users
        for row in list_access_users():
            if row.role == "admin" and row.active == "yes":
                ensure_tenant_for_user(row.telegram_user_id, "ASMAN")
    except Exception:
        pass

    async def tenant_access_check(update, context):
        user = update.effective_user
        if user and (is_admin(user.id) or is_allowed(user.id)):
            try:
                tenant = ensure_tenant_for_user(user.id, getattr(user, "full_name", None))
                set_business_schema(tenant["schema_name"])
                context.user_data["tenant_id"] = tenant["id"]
            except Exception:
                # Never fall back to public business tables for a customer.
                if update.callback_query:
                    await update.callback_query.answer(
                        "🔒 Мижоз базасини очиб бўлмади. Admin билан боғланинг.",
                        show_alert=True,
                    )
                elif update.effective_message:
                    await update.effective_message.reply_text(
                        "🔒 ХАВФСИЗЛИК: шахсий база очилмади.\n"
                        "Маълумотлар аралашмаслиги учун операция тўхтатилди. Admin билан боғланинг."
                    )
                raise ApplicationHandlerStop
        else:
            # Unauthorized users never receive a customer workspace.
            set_business_schema("public")

        await original_access_check(update, context)

    async def tenant_text_handler(update, context):
        message = update.effective_message
        text_value = (message.text or "").strip() if message else ""
        lower = text_value.casefold()
        user = update.effective_user

        if user and lower in ("база статус", "база статуси", "database status"):
            if is_admin(user.id) or is_allowed(user.id):
                await message.reply_text(tenant_status_text(user.id))
            return

        if user and is_admin(user.id):
            if lower == "базалар":
                await message.reply_text(_list_tenants_text())
                return

            if lower.startswith(("базага қўш:", "базага куш:", "tenant add:")):
                payload = text_value.split(":", 1)[1].strip()
                parts = [p.strip() for p in payload.split("|")]
                try:
                    member_id = int(parts[0])
                    owner_id = int(parts[1])
                except Exception:
                    await message.reply_text(
                        "❌ Формат: базага қўш: ХОДИМ_ID | МИЖОЗ_OWNER_ID"
                    )
                    return
                ok, reply = assign_user_to_owner_tenant(user.id, member_id, owner_id)
                await message.reply_text(reply)
                return

        await original_text_handler(update, context)

        # General access or TEST access immediately provisions that customer's DB.
        if user and is_admin(user.id) and lower.startswith(("тест:", "test:", "доступ:")):
            target_id = _extract_first_id(text_value)
            if target_id and is_allowed(target_id):
                try:
                    tenant = ensure_tenant_for_user(target_id)
                    await message.reply_text(
                        f"🔒 Алоҳида мижоз базаси тайёр. База #{tenant['id']}\n"
                        "Бу мижоз маълумотлари бошқа мижозлар билан аралашмайди."
                    )
                except Exception:
                    await message.reply_text(
                        "⚠️ Доступ берилди, лекин алоҳида базани тайёрлашда хато бўлди. "
                        "Мижоз маълумот кирита олмайди — хавфсизлик блоки ишлайди."
                    )

        if user and is_admin(user.id) and text_value in ("👑 Admin панел", "🔐 Доступ"):
            await message.reply_text(
                "🔒 Мижоз базалари\n\n"
                "Ҳар бир мижоз автоматик алоҳида базага ажратилади.\n"
                "Рўйхат: базалар\n"
                "Жорий база: база статус\n\n"
                "Бир мижознинг қўшимча ходимини шу базага улаш:\n"
                "базага қўш: ХОДИМ_ID | МИЖОЗ_OWNER_ID"
            )

    bot_module.access_check = tenant_access_check
    bot_module.text_handler = tenant_text_handler
