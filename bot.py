import os
import sqlite3
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TOKEN = os.getenv("BOT_TOKEN")
DB = "asman.db"


# =========================
# RENDER WEB SERVER
# =========================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ASMAN Accounting Bot is running")

    def log_message(self, format, *args):
        return


def run_web_server():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"Web server running on port {port}")
    server.serve_forever()


# =========================
# DATABASE
# =========================

def db():
    con = sqlite3.connect(DB)

    con.execute("""
        CREATE TABLE IF NOT EXISTS materials(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            qty REAL NOT NULL DEFAULT 0,
            unit TEXT NOT NULL DEFAULT 'kg'
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS partners(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS contracts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            partner TEXT NOT NULL,
            number TEXT NOT NULL,
            total REAL NOT NULL DEFAULT 0,
            used REAL NOT NULL DEFAULT 0
        )
    """)

    con.commit()
    return con


# =========================
# MENU
# =========================

MENU = ReplyKeyboardMarkup(
    [
        ["📦 Омбор", "🤝 Ҳамкорлар"],
        ["📄 Шартномалар", "📊 Ҳисобот"],
        ["ℹ️ Ёрдам"],
    ],
    resize_keyboard=True,
)


# =========================
# TELEGRAM
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏢 ASMAN Бухгалтерия\n\n"
        "Бот ишга тушди ✅\n"
        "Керакли бўлимни танланг:",
        reply_markup=MENU,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ Ёрдам\n\n"
        "📥 Хом ашё кирими:\n"
        "+ Акрил | 500 | kg\n\n"
        "🤝 Ҳамкор қўшиш:\n"
        "ҳамкор: Компания номи\n\n"
        "📄 Шартнома қўшиш:\n"
        "шартнома: Компания | 37/2026 | 10000000"
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    con = db()

    try:

        # OMBOR
        if text == "📦 Омбор":
            rows = con.execute(
                "SELECT name, qty, unit FROM materials ORDER BY name"
            ).fetchall()

            if not rows:
                message = (
                    "📦 Хом ашё омбори\n\n"
                    "Ҳозирча омбор бўш.\n\n"
                    "Кирим қилиш:\n"
                    "+ Номи | миқдор | бирлик"
                )
            else:
                message = "📦 Хом ашё қолдиғи:\n\n"

                for name, qty, unit in rows:
                    message += f"• {name}: {qty:g} {unit}\n"

            await update.message.reply_text(message)
            return

        # KIRIM
        if text.startswith("+"):
            try:
                parts = [x.strip() for x in text[1:].split("|")]

                name = parts[0]
                qty = float(parts[1].replace(",", "."))
                unit = parts[2] if len(parts) > 2 else "kg"

                con.execute(
                    """
                    INSERT INTO materials(name, qty, unit)
                    VALUES (?, ?, ?)
                    ON CONFLICT(name)
                    DO UPDATE SET
                        qty = qty + excluded.qty,
                        unit = excluded.unit
                    """,
                    (name, qty, unit),
                )

                con.commit()

                await update.message.reply_text(
                    f"✅ Кирим сақланди\n\n"
                    f"{name}: +{qty:g} {unit}"
                )

            except Exception:
                await update.message.reply_text(
                    "❌ Формат нотўғри.\n\n"
                    "Масалан:\n"
                    "+ Акрил | 500 | kg"
                )

            return

        # PARTNERS
        if text == "🤝 Ҳамкорлар":
            rows = con.execute(
                "SELECT name FROM partners ORDER BY name"
            ).fetchall()

            if rows:
                message = "🤝 Ҳамкорлар:\n\n"
                message += "\n".join(
                    f"• {row[0]}" for row in rows
                )
            else:
                message = "🤝 Ҳамкорлар ҳозирча йўқ."

            message += (
                "\n\nЯнги ҳамкор қўшиш:\n"
                "ҳамкор: Компания номи"
            )

            await update.message.reply_text(message)
            return

        # ADD PARTNER
        if text.lower().startswith("ҳамкор:"):
            name = text.split(":", 1)[1].strip()

            if not name:
                await update.message.reply_text(
                    "❌ Компания номини киритинг."
                )
                return

            con.execute(
                "INSERT OR IGNORE INTO partners(name) VALUES (?)",
                (name,),
            )

            con.commit()

            await update.message.reply_text(
                f"✅ Ҳамкор қўшилди:\n{name}"
            )
            return

        # CONTRACTS
        if text == "📄 Шартномалар":
            rows = con.execute(
                """
                SELECT partner, number, total, used
                FROM contracts
                ORDER BY id DESC
                """
            ).fetchall()

            if rows:
                message = "📄 Шартномалар:\n\n"

                for partner, number, total, used in rows:
                    balance = total - used

                    message += (
                        f"• {partner}\n"
                        f"  № {number}\n"
                        f"  Қолдиқ: {balance:,.0f}\n\n"
                    )
            else:
                message = "📄 Шартномалар ҳозирча йўқ."

            message += (
                "\nЯнги шартнома:\n"
                "шартнома: Компания | 37/2026 | 10000000"
            )

            await update.message.reply_text(message)
            return

        # ADD CONTRACT
        if text.lower().startswith("шартнома:"):
            try:
                parts = [
                    x.strip()
                    for x in text.split(":", 1)[1].split("|")
                ]

                partner = parts[0]
                number = parts[1]

                total = float(
                    parts[2]
                    .replace(" ", "")
                    .replace(",", ".")
                )

                con.execute(
                    "INSERT OR IGNORE INTO partners(name) VALUES (?)",
                    (partner,),
                )

                con.execute(
                    """
                    INSERT INTO contracts(
                        partner,
                        number,
                        total
                    )
                    VALUES (?, ?, ?)
                    """,
                    (partner, number, total),
                )

                con.commit()

                await update.message.reply_text(
                    "✅ Шартнома сақланди\n\n"
                    f"Ҳамкор: {partner}\n"
                    f"№: {number}\n"
                    f"Сумма: {total:,.0f}"
                )

            except Exception:
                await update.message.reply_text(
                    "❌ Формат нотўғри.\n\n"
                    "Масалан:\n"
                    "шартнома: Компания | 37/2026 | 10000000"
                )

            return

        # REPORT
        if text == "📊 Ҳисобот":
            materials = con.execute(
                """
                SELECT COUNT(*),
                       COALESCE(SUM(qty), 0)
                FROM materials
                """
            ).fetchone()

            partners = con.execute(
                "SELECT COUNT(*) FROM partners"
            ).fetchone()[0]

            contracts = con.execute(
                """
                SELECT COUNT(*),
                       COALESCE(SUM(total-used), 0)
                FROM contracts
                """
            ).fetchone()

            await update.message.reply_text(
                "📊 ASMAN ҲИСОБОТИ\n\n"
                f"📦 Хом ашё турлари: {materials[0]}\n"
                f"📥 Умумий миқдор: {materials[1]:g}\n"
                f"🤝 Ҳамкорлар: {partners}\n"
                f"📄 Шартномалар: {contracts[0]}\n"
                f"💰 Шартнома қолдиғи: "
                f"{contracts[1]:,.0f}"
            )

            return

        if text == "ℹ️ Ёрдам":
            await help_cmd(update, context)
            return

        await update.message.reply_text(
            "Керакли бўлимни менюдан танланг 👇",
            reply_markup=MENU,
        )

    finally:
        con.close()


# =========================
# START
# =========================

def main():
    if not TOKEN:
        raise RuntimeError(
            "BOT_TOKEN Render Environment Variables'да топилмади"
        )

    db().close()

    # Render Web Service учун порт
    Thread(
        target=run_web_server,
        daemon=True
    ).start()

    # Telegram bot
    app = Application.builder().token(TOKEN).build()

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("help", help_cmd)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    print("ASMAN Accounting Bot started")

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
