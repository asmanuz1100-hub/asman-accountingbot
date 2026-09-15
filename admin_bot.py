import os
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from access_control import (
    BotAccessUser,
    engine as control_engine,
    grant_user,
    is_admin,
    list_access_users,
    list_pending_users,
    revoke_user,
)
from admin_monitor import get_error, list_open_errors, resolve_error
from tenant_control import BotTenant, BotTenantUser, _provision_schema
from trial_control import BotTrial, cancel_trial, convert_to_paid, start_trial


TOKEN = (os.getenv("ADMIN_BOT_TOKEN") or "").strip()


def _fmt(value: datetime | None) -> str:
    if not value:
        return "—"
    return (value + timedelta(hours=5)).strftime("%d.%m.%Y %H:%M")


def _name_for(user_id: int) -> str:
    row = next((r for r in list_access_users() if int(r.telegram_user_id) == int(user_id)), None)
    if row is None:
        return str(user_id)
    if row.full_name:
        return row.full_name
    if row.username:
        return f"@{row.username}"
    return str(user_id)


def _home_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🏢 Мижозлар", callback_data="adm:customers"),
            InlineKeyboardButton("🟡 Сўровлар", callback_data="adm:pending"),
        ],
        [
            InlineKeyboardButton("🧪 TEST / тариф", callback_data="adm:trials"),
            InlineKeyboardButton("🚨 Хатолар", callback_data="adm:errors"),
        ],
        [InlineKeyboardButton("🔄 Янгилаш", callback_data="adm:home")],
    ])


def _dashboard_text() -> str:
    now = datetime.utcnow()
    with Session(control_engine) as session:
        customers = session.scalar(
            select(func.count()).select_from(BotTenant).where(BotTenant.schema_name != "public")
        ) or 0
        active_users = session.scalar(
            select(func.count()).select_from(BotAccessUser).where(BotAccessUser.active == "yes")
        ) or 0
        trials = session.scalar(
            select(func.count()).select_from(BotTrial).where(
                BotTrial.status == "trial", BotTrial.expires_at > now
            )
        ) or 0
    pending = len(list_pending_users())
    errors = len(list_open_errors(100))
    return (
        "🛡 ASMAN ADMIN BOT\n\n"
        f"🏢 Мижоз базалари: {int(customers)}\n"
        f"👥 Фаол фойдаланувчилар: {int(active_users)}\n"
        f"🟡 Доступ сўровлари: {pending}\n"
        f"🧪 Актив TEST: {int(trials)}\n"
        f"🚨 Очиқ хатолар: {errors}\n\n"
        "Бу бот фақат мижозлар ва система бошқаруви учун."
    )


async def _edit(query, text: str, markup=None):
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except Exception:
        await query.message.reply_text(text, reply_markup=markup)


async def _require_admin(update: Update) -> bool:
    user = update.effective_user
    if user and is_admin(user.id):
        return True
    if update.callback_query:
        await update.callback_query.answer("⛔ Бу бот фақат Admin учун.", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text("⛔ Бу бот фақат асосий Admin учун.")
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    await update.effective_message.reply_text(_dashboard_text(), reply_markup=_home_markup())


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user:
        await update.effective_message.reply_text(f"Telegram ID: {user.id}")


def _customers_view():
    with Session(control_engine) as session:
        tenants = list(session.scalars(select(BotTenant).order_by(BotTenant.id.desc())).all())
        memberships = list(session.scalars(select(BotTenantUser)).all())
    counts = {}
    for m in memberships:
        counts[m.tenant_id] = counts.get(m.tenant_id, 0) + 1

    lines = ["🏢 МИЖОЗ БАЗАЛАРИ", ""]
    buttons = []
    for t in tenants[:40]:
        label = "ASMAN" if t.schema_name == "public" else (t.name or _name_for(t.owner_user_id))
        state = "✅" if t.status == "active" and t.provisioned_at else "⚠️"
        lines.append(f"{state} База #{t.id} — {label} — {counts.get(t.id, 0)} user")
        buttons.append([
            InlineKeyboardButton(f"🏢 #{t.id} {label[:24]}", callback_data=f"adm:cust:{t.id}")
        ])
    if not tenants:
        lines.append("Ҳозирча база йўқ.")
    buttons.append([InlineKeyboardButton("⬅️ Бош меню", callback_data="adm:home")])
    return "\n".join(lines)[:3900], InlineKeyboardMarkup(buttons)


def _customer_detail(tenant_id: int):
    with Session(control_engine) as session:
        t = session.get(BotTenant, int(tenant_id))
        if not t:
            return "База топилмади.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️", callback_data="adm:customers")]])
        members = list(session.scalars(select(BotTenantUser).where(BotTenantUser.tenant_id == t.id)).all())
        trial = session.get(BotTrial, int(t.owner_user_id))
        access = session.get(BotAccessUser, int(t.owner_user_id))

    label = "ASMAN" if t.schema_name == "public" else (t.name or _name_for(t.owner_user_id))
    access_state = access.active if access else "—"
    if trial is None:
        plan = "оддий доступ"
    elif trial.status == "trial" and trial.expires_at > datetime.utcnow():
        plan = f"TEST — {_fmt(trial.expires_at)} гача"
    else:
        plan = trial.status

    text = (
        f"🏢 {label}\n\n"
        f"База: #{t.id}\n"
        f"Owner ID: {t.owner_user_id}\n"
        f"Фойдаланувчилар: {len(members)} та\n"
        f"Доступ: {access_state}\n"
        f"Тариф: {plan}\n"
        f"База ҳолати: {'✅ тайёр' if t.provisioned_at else '⚠️ тайёр эмас'}\n"
        f"Яратилган: {_fmt(t.created_at)}"
    )

    if t.schema_name == "public":
        buttons = [[InlineKeyboardButton("⬅️ Мижозлар", callback_data="adm:customers")]]
    else:
        buttons = [
            [
                InlineKeyboardButton("🧪 TEST 14 кун", callback_data=f"adm:test:{t.owner_user_id}"),
                InlineKeyboardButton("💳 Пуллик", callback_data=f"adm:paid:{t.owner_user_id}"),
            ],
            [
                InlineKeyboardButton("🛠 Базани тиклаш", callback_data=f"adm:repair:{t.id}"),
                InlineKeyboardButton("🚫 Блоклаш", callback_data=f"adm:block:{t.owner_user_id}"),
            ],
            [InlineKeyboardButton("⬅️ Мижозлар", callback_data="adm:customers")],
        ]
    return text, InlineKeyboardMarkup(buttons)


def _pending_view():
    rows = list_pending_users()
    lines = ["🟡 ДОСТУП СЎРОВЛАРИ", ""]
    buttons = []
    for row in rows[:30]:
        name = row.full_name or (f"@{row.username}" if row.username else str(row.telegram_user_id))
        lines.append(f"• {name} | ID {row.telegram_user_id}")
        buttons.append([
            InlineKeyboardButton(
                f"🧪 14 кун TEST — {name[:18]}", callback_data=f"adm:test:{row.telegram_user_id}"
            )
        ])
    if not rows:
        lines.append("Янги сўровлар йўқ.")
    buttons.append([InlineKeyboardButton("⬅️ Бош меню", callback_data="adm:home")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _trials_view():
    with Session(control_engine) as session:
        rows = list(session.scalars(select(BotTrial).order_by(BotTrial.updated_at.desc()).limit(40)).all())
    lines = ["🧪 TEST / ТАРИФЛАР", ""]
    buttons = []
    now = datetime.utcnow()
    for row in rows:
        if row.status == "trial" and row.expires_at > now:
            state = f"🟢 {_fmt(row.expires_at)} гача"
        elif row.status == "paid":
            state = "💳 пуллик"
        elif row.status == "cancelled":
            state = "🚫 бекор"
        else:
            state = "⏳ тугаган"
        lines.append(f"• {_name_for(row.telegram_user_id)} — {state}")
        buttons.append([
            InlineKeyboardButton(
                f"👤 {_name_for(row.telegram_user_id)[:24]}",
                callback_data=f"adm:owner:{row.telegram_user_id}",
            )
        ])
    if not rows:
        lines.append("Ҳозирча TEST/тариф тарихи йўқ.")
    buttons.append([InlineKeyboardButton("⬅️ Бош меню", callback_data="adm:home")])
    return "\n".join(lines)[:3900], InlineKeyboardMarkup(buttons)


def _errors_view():
    rows = list_open_errors(30)
    lines = ["🚨 ОЧИҚ ХАТОЛАР", ""]
    buttons = []
    for row in rows:
        who = _name_for(row.telegram_user_id) if row.telegram_user_id else "system"
        lines.append(f"#{row.id} {row.error_type} | {who} | {_fmt(row.created_at)}")
        buttons.append([
            InlineKeyboardButton(f"🚨 #{row.id} {row.error_type[:20]}", callback_data=f"adm:error:{row.id}")
        ])
    if not rows:
        lines.append("✅ Очиқ хатолар йўқ.")
    buttons.append([InlineKeyboardButton("⬅️ Бош меню", callback_data="adm:home")])
    return "\n".join(lines)[:3900], InlineKeyboardMarkup(buttons)


def _error_detail(error_id: int):
    row = get_error(error_id)
    if not row:
        return "Хато топилмади.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️", callback_data="adm:errors")]])
    trace_tail = (row.traceback_text or "")[-1800:]
    text = (
        f"🚨 ХАТО #{row.id}\n\n"
        f"Тури: {row.error_type}\n"
        f"Вақт: {_fmt(row.created_at)}\n"
        f"User: {row.telegram_user_id or '—'}\n"
        f"База: #{row.tenant_id}" if row.tenant_id else f"🚨 ХАТО #{row.id}\n\nТури: {row.error_type}\nВақт: {_fmt(row.created_at)}\nUser: {row.telegram_user_id or '—'}\nБаза: —"
    )
    text += f"\n\nХабар:\n{row.message[:900]}"
    if trace_tail:
        text += f"\n\nДиагностика:\n{trace_tail}"
    buttons = []
    if row.tenant_id:
        buttons.append([InlineKeyboardButton("🛠 Базани тиклаш", callback_data=f"adm:repair:{row.tenant_id}")])
    buttons.append([InlineKeyboardButton("✅ Ҳал қилинди", callback_data=f"adm:resolve:{row.id}")])
    buttons.append([InlineKeyboardButton("⬅️ Хатолар", callback_data="adm:errors")])
    return text[:3900], InlineKeyboardMarkup(buttons)


def _repair_tenant(tenant_id: int) -> tuple[bool, str]:
    try:
        with Session(control_engine) as session:
            t = session.get(BotTenant, int(tenant_id))
            if t is None:
                return False, "База топилмади."
            if t.schema_name == "public":
                return False, "ASMAN public базасига автоматик repair қўлланмайди."
            data = {
                "id": t.id,
                "name": t.name,
                "schema_name": t.schema_name,
                "owner_user_id": t.owner_user_id,
                "status": t.status,
                "provisioned_at": None,
                "role": "owner",
            }
            t.provisioned_at = None
            t.status = "active"
            session.commit()
        _provision_schema(data)
        return True, f"✅ База #{tenant_id} қайта текширилди ва керакли жадваллар тикланди."
    except Exception as exc:
        return False, f"❌ Repair бажарилмади: {type(exc).__name__}: {str(exc)[:500]}"


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    admin_id = update.effective_user.id

    if data == "adm:home":
        await _edit(q, _dashboard_text(), _home_markup())
        return
    if data == "adm:customers":
        text, kb = _customers_view()
        await _edit(q, text, kb)
        return
    if data == "adm:pending":
        text, kb = _pending_view()
        await _edit(q, text, kb)
        return
    if data == "adm:trials":
        text, kb = _trials_view()
        await _edit(q, text, kb)
        return
    if data == "adm:errors":
        text, kb = _errors_view()
        await _edit(q, text, kb)
        return

    if data.startswith("adm:cust:"):
        text, kb = _customer_detail(int(data.rsplit(":", 1)[1]))
        await _edit(q, text, kb)
        return
    if data.startswith("adm:owner:"):
        owner_id = int(data.rsplit(":", 1)[1])
        with Session(control_engine) as session:
            t = session.scalar(select(BotTenant).where(BotTenant.owner_user_id == owner_id))
        if t:
            text, kb = _customer_detail(t.id)
        else:
            text, kb = "Мижоз базаси ҳали яратилмаган.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️", callback_data="adm:trials")]])
        await _edit(q, text, kb)
        return

    if data.startswith("adm:test:"):
        uid = int(data.rsplit(":", 1)[1])
        ok, msg = start_trial(admin_id, uid, 14)
        if ok:
            try:
                from tenant_control import ensure_tenant_for_user
                ensure_tenant_for_user(uid, _name_for(uid))
            except Exception as exc:
                msg += f"\n⚠️ База тайёрлашда хато: {type(exc).__name__}"
        await q.answer(msg[:180], show_alert=True)
        text, kb = _customer_detail_for_owner_or_pending(uid)
        await _edit(q, text, kb)
        return

    if data.startswith("adm:paid:"):
        uid = int(data.rsplit(":", 1)[1])
        ok, msg = convert_to_paid(admin_id, uid)
        await q.answer(msg[:180], show_alert=True)
        text, kb = _customer_detail_for_owner_or_pending(uid)
        await _edit(q, text, kb)
        return

    if data.startswith("adm:block:"):
        uid = int(data.rsplit(":", 1)[1])
        ok, msg = revoke_user(admin_id, uid)
        await q.answer(msg[:180], show_alert=True)
        text, kb = _customer_detail_for_owner_or_pending(uid)
        await _edit(q, text, kb)
        return

    if data.startswith("adm:repair:"):
        tenant_id = int(data.rsplit(":", 1)[1])
        ok, msg = _repair_tenant(tenant_id)
        await q.answer(msg[:180], show_alert=True)
        text, kb = _customer_detail(tenant_id)
        await _edit(q, text, kb)
        return

    if data.startswith("adm:error:"):
        text, kb = _error_detail(int(data.rsplit(":", 1)[1]))
        await _edit(q, text, kb)
        return

    if data.startswith("adm:resolve:"):
        error_id = int(data.rsplit(":", 1)[1])
        resolve_error(error_id, admin_id)
        await q.answer("✅ Хато ёпилди.", show_alert=True)
        text, kb = _errors_view()
        await _edit(q, text, kb)
        return


def _customer_detail_for_owner_or_pending(owner_id: int):
    with Session(control_engine) as session:
        t = session.scalar(select(BotTenant).where(BotTenant.owner_user_id == int(owner_id)))
    if t:
        return _customer_detail(t.id)
    return _pending_view()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    # Admin Bot errors are intentionally not recursively written into the same
    # incident stream; Render logs remain the fallback for Admin Bot itself.
    return


def main():
    if not TOKEN:
        raise RuntimeError("ADMIN_BOT_TOKEN киритилмаган")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CallbackQueryHandler(callback, pattern=r"^adm:"))
    app.add_error_handler(error_handler)

    mode = (os.getenv("ADMIN_RUN_MODE") or "webhook").lower()
    if mode == "polling":
        app.run_polling(drop_pending_updates=False)
        return

    port = int(os.getenv("PORT", "10000"))
    base_url = (os.getenv("ADMIN_WEBHOOK_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
    if not base_url:
        raise RuntimeError("Admin webhook учун RENDER_EXTERNAL_URL керак")
    path = (os.getenv("ADMIN_WEBHOOK_PATH") or "admin-telegram").strip("/")
    secret = (os.getenv("ADMIN_WEBHOOK_SECRET") or "").strip() or None
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=path,
        webhook_url=f"{base_url}/{path}",
        secret_token=secret,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
