import os

from telegram.ext import ApplicationHandlerStop

import database as business_db
from access_control import is_admin
from tenant_control import ensure_tenant_for_user, set_business_schema


def _message(update):
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    if message is not None:
        # Lightweight mocks and older wrappers may omit standard Telegram fields.
        for name in ("text", "document", "photo"):
            if not hasattr(message, name):
                try:
                    setattr(message, name, None)
                except Exception:
                    pass
        if getattr(update, "effective_message", None) is None:
            try:
                setattr(update, "effective_message", message)
            except Exception:
                pass
    return message


def _legacy_allow_ids() -> set[str]:
    return {
        value.strip()
        for value in (os.getenv("ALLOWED_USER_IDS") or "").split(",")
        if value.strip()
    }


def install_runtime_compat(bot_module) -> None:
    original_access_check = bot_module.access_check
    original_text_handler = bot_module.text_handler

    async def compatible_access_check(update, context):
        message = _message(update)
        user = getattr(update, "effective_user", None)
        legacy = _legacy_allow_ids()

        # Preserve the project's original explicit allow-list behaviour when the
        # env var is configured. Admin is never locked out by a stale list.
        if legacy and user and not is_admin(user.id):
            if str(user.id) not in legacy:
                if getattr(update, "callback_query", None):
                    await update.callback_query.answer("Киришга рухсат йўқ.", show_alert=True)
                elif message and hasattr(message, "reply_text"):
                    await message.reply_text(
                        f"🔐 Киришга рухсат берилмаган. Telegram ID: {user.id}"
                    )
                raise ApplicationHandlerStop

            # Production uses PostgreSQL tenant isolation. Local/CI SQLite keeps
            # the legacy public workspace so old tests and development tools work.
            if business_db.engine.dialect.name == "postgresql":
                tenant = ensure_tenant_for_user(user.id, getattr(user, "full_name", None))
                set_business_schema(tenant["schema_name"])
                if context is not None and hasattr(context, "user_data"):
                    context.user_data["tenant_id"] = tenant["id"]
            else:
                set_business_schema("public")
            return

        await original_access_check(update, context)

    async def compatible_text_handler(update, context):
        _message(update)
        await original_text_handler(update, context)

    bot_module.access_check = compatible_access_check
    bot_module.text_handler = compatible_text_handler
