import os
import sqlite3
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

TOKEN = os.getenv("BOT_TOKEN")
DB = "asman.db"

def db():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS materials(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        qty REAL NOT NULL DEFAULT 0,
        unit TEXT NOT NULL DEFAULT 'kg'
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS partners(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS contracts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        partner TEXT NOT NULL,
        number TEXT NOT NULL,
        total REAL NOT NULL DEFAULT 0,
        used REAL NOT NULL DEFAULT 0
    )""")
    con.commit()
    return con

MENU = ReplyKeyboardMarkup([
    ["📦 Омбор", "🤝 Ҳамкорлар"],
    ["📄 Шартномалар", "📊 Ҳисобот"],
    ["ℹ️ Ёрдам"]
], resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ASMAN бухгалтерия боти ишга тушди ✅\n\nКеракли бўлимни танланг:",
        reply_markup=MENU
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Омборга кирим:\n+ Хом ашё номи | миқдор | бирлик\n"
        "Масалан: + Акрил | 500 | kg\n\n"
        "Ҳамкор қўшиш:\nҳамкор: Компания номи\n\n"
        "Шартнома:\nшартнома: Компания | 37/2026 | 10000000"
    )

async def text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    con = db()

    if t == "📦 Омбор":
        rows = con.execute("SELECT name,qty,unit FROM materials ORDER BY name").fetchall()
        if not rows:
            msg = "Омбор ҳозирча бўш.\n\nКирим учун:\n+ Номи | миқдор | бирлик"
        else:
            msg = "📦 Хом ашё қолдиғи:\n\n" + "\n".join(
                f"• {n}: {q:g} {u}" for n,q,u in rows)
        await update.message.reply_text(msg)
        return

    if t.startswith("+"):
        try:
            p = [x.strip() for x in t[1:].split("|")]
            name, qty = p[0], float(p[1].replace(",", "."))
            unit = p[2] if len(p) > 2 else "kg"
            con.execute("""INSERT INTO materials(name,qty,unit) VALUES(?,?,?)
                ON CONFLICT(name) DO UPDATE SET qty=qty+excluded.qty, unit=excluded.unit""",
                (name, qty, unit))
            con.commit()
            await update.message.reply_text(f"✅ Кирим: {name} +{qty:g} {unit}")
        except Exception:
            await update.message.reply_text("Формат: + Акрил | 500 | kg")
        return

    if t == "🤝 Ҳамкорлар":
        rows = con.execute("SELECT name FROM partners ORDER BY name").fetchall()
        msg = "🤝 Ҳамкорлар:\n\n" + ("\n".join("• "+r[0] for r in rows) if rows else "Ҳозирча йўқ.")
        await update.message.reply_text(msg + "\n\nҚўшиш: ҳамкор: Компания номи")
        return

    if t.lower().startswith("ҳамкор:"):
        name = t.split(":",1)[1].strip()
        if name:
            con.execute("INSERT OR IGNORE INTO partners(name) VALUES(?)", (name,))
            con.commit()
            await update.message.reply_text(f"✅ Ҳамкор қўшилди: {name}")
        return

    if t == "📄 Шартномалар":
        rows = con.execute("SELECT partner,number,total,used FROM contracts ORDER BY id DESC").fetchall()
        if rows:
            msg = "📄 Шартномалар:\n\n" + "\n".join(
                f"• {p} №{n}: қолдиқ {total-used:,.0f}" for p,n,total,used in rows)
        else:
            msg = "Шартномалар ҳозирча йўқ."
        await update.message.reply_text(msg + "\n\nҚўшиш:\nшартнома: Компания | 37/2026 | 10000000")
        return

    if t.lower().startswith("шартнома:"):
        try:
            p = [x.strip() for x in t.split(":",1)[1].split("|")]
            partner, number, total = p[0], p[1], float(p[2].replace(" ",""))
            con.execute("INSERT OR IGNORE INTO partners(name) VALUES(?)", (partner,))
            con.execute("INSERT INTO contracts(partner,number,total) VALUES(?,?,?)",
                        (partner,number,total))
            con.commit()
            await update.message.reply_text(f"✅ Шартнома қўшилди: {partner} №{number}")
        except Exception:
            await update.message.reply_text("Формат: шартнома: Компания | 37/2026 | 10000000")
        return

    if t == "📊 Ҳисобот":
        m = con.execute("SELECT COUNT(*), COALESCE(SUM(qty),0) FROM materials").fetchone()
        p = con.execute("SELECT COUNT(*) FROM partners").fetchone()[0]
        c = con.execute("SELECT COUNT(*), COALESCE(SUM(total-used),0) FROM contracts").fetchone()
        await update.message.reply_text(
            f"📊 Қисқа ҳисобот\n\n"
            f"Хом ашё турлари: {m[0]}\n"
            f"Умумий миқдор: {m[1]:g}\n"
            f"Ҳамкорлар: {p}\n"
            f"Шартномалар: {c[0]}\n"
            f"Шартнома қолдиғи: {c[1]:,.0f}"
        )
        return

    if t == "ℹ️ Ёрдам":
        await help_cmd(update, context)
        return

    await update.message.reply_text("Менюдан бўлим танланг.", reply_markup=MENU)

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable топилмади")
    db().close()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text))
    print("ASMAN bot started")
    app.run_polling()

if __name__ == "__main__":
    main()
