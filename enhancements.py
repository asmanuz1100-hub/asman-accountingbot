import json

from sqlalchemy import select
from sqlalchemy.orm import Session
from telegram import ReplyKeyboardMarkup

from asaka_excel import _Parser, _date, _decode, _num, _split_party
from database import Document, engine, list_partners, upsert_partner


def _norm_name(value):
    return " ".join(str(value or "").split()).strip().casefold()


def _money(value):
    try:
        return f"{float(value):,.2f}".replace(",", " ")
    except Exception:
        return "0.00"


def enrich_bank_data(raw: bytes, filename: str, data: dict) -> dict:
    """Attach full bank transactions for partner sync and reconciliation."""
    if not isinstance(data, dict) or data.get("document_type") != "bank_statement":
        return data
    if data.get("transactions"):
        return data

    transactions = []
    try:
        head = raw[:4096].lstrip().lower()
        if b"<html" in head or b"<table" in head:
            parser = _Parser()
            parser.feed(_decode(raw))
            for row in parser.rows:
                if len(row) < 8:
                    continue
                tx_date = _date(row[0])
                if not tx_date:
                    continue

                outgoing = round(_num(row[5]), 2)
                incoming = round(_num(row[6]), 2)
                if incoming == 0 and outgoing == 0:
                    continue

                account, tin, name = _split_party(row[1])
                transactions.append({
                    "date": tx_date,
                    "counterparty": name,
                    "counterparty_account": account,
                    "counterparty_tin": tin,
                    "purpose": str(row[7] or "").strip()[:1500] or None,
                    "incoming": incoming,
                    "outgoing": outgoing,
                })
    except Exception:
        transactions = []

    if not transactions:
        transactions = list(data.get("transactions_preview") or [])

    data["transactions"] = transactions
    return data


def _partner_names_from_data(data: dict) -> list[tuple[str, str | None]]:
    found: dict[str, tuple[str, str | None]] = {}

    partner = data.get("partner") or {}
    if isinstance(partner, dict) and partner.get("name"):
        name = " ".join(str(partner["name"]).split()).strip()
        if name:
            tin = partner.get("tin")
            found[_norm_name(name)] = (name[:255], str(tin).strip() if tin else None)

    if data.get("document_type") == "bank_statement":
        own_name = _norm_name(data.get("account_holder"))
        for tx in data.get("transactions") or data.get("transactions_preview") or []:
            name = " ".join(str(tx.get("counterparty") or "").split()).strip()
            norm = _norm_name(name)
            if not name or norm == own_name or len(norm) < 3:
                continue
            if any(bad in norm for bad in (
                "итоговый оборот",
                "остаток на",
                "оборот дебет",
                "оборот кредит",
            )):
                continue
            tin = tx.get("counterparty_tin")
            found[norm] = (name[:255], str(tin).strip() if tin else None)

        for item in data.get("top_counterparties") or []:
            name = " ".join(str(item.get("name") or "").split()).strip()
            norm = _norm_name(name)
            if name and norm != own_name and norm not in found:
                found[norm] = (name[:255], None)

    if data.get("document_type") == "invoice_registry":
        for item in data.get("counterparties") or []:
            name = " ".join(str(item.get("name") or "").split()).strip()
            if not name:
                continue
            tin = item.get("tin")
            found[_norm_name(name)] = (name[:255], str(tin).strip() if tin else None)

    return sorted(found.values(), key=lambda x: x[0].casefold())


def sync_partners_from_data(data: dict) -> int:
    partners = _partner_names_from_data(data)
    saved = 0
    for name, tin in partners:
        try:
            upsert_partner(name, tin=tin, partner_type="auto")
            saved += 1
        except Exception:
            continue
    return saved


def _matches_partner(candidate: str, requested: str) -> bool:
    a = _norm_name(candidate)
    b = _norm_name(requested)
    if not a or not b:
        return False
    if a == b:
        return True
    return min(len(a), len(b)) >= 5 and (a in b or b in a)


def reconciliation_for_partner(partner_name: str) -> str:
    bank_rows = []
    invoice_rows = []
    seen_bank = set()
    seen_invoice = set()

    with Session(engine) as session:
        docs = session.scalars(
            select(Document)
            .where(Document.document_type.in_(["bank_statement", "invoice_registry"]))
            .order_by(Document.id)
        ).all()

    for doc in docs:
        try:
            data = json.loads(doc.raw_json or "{}")
        except Exception:
            continue

        if data.get("document_type") == "bank_statement":
            for tx in data.get("transactions") or data.get("transactions_preview") or []:
                cp = str(tx.get("counterparty") or "").strip()
                if not _matches_partner(cp, partner_name):
                    continue

                incoming = round(float(tx.get("incoming") or 0), 2)
                outgoing = round(float(tx.get("outgoing") or 0), 2)
                key = (
                    str(tx.get("date") or ""),
                    _norm_name(cp),
                    str(tx.get("purpose") or "").strip(),
                    incoming,
                    outgoing,
                )
                if key in seen_bank:
                    continue
                seen_bank.add(key)
                bank_rows.append({
                    "date": key[0],
                    "incoming": incoming,
                    "outgoing": outgoing,
                    "purpose": key[2],
                })

        elif data.get("document_type") == "invoice_registry":
            for inv in data.get("invoices") or []:
                cp = str(inv.get("counterparty") or "").strip()
                if not _matches_partner(cp, partner_name):
                    continue
                if inv.get("status_group") != "signed":
                    continue

                amount = round(float(inv.get("total") or 0), 2)
                key = (
                    str(inv.get("document_number") or ""),
                    str(inv.get("document_date") or ""),
                    _norm_name(cp),
                    str(inv.get("direction") or ""),
                    amount,
                )
                if key in seen_invoice:
                    continue
                seen_invoice.add(key)
                invoice_rows.append({
                    "number": key[0],
                    "date": key[1],
                    "direction": key[3],
                    "amount": amount,
                })

    payments_from_partner = round(sum(x["incoming"] for x in bank_rows), 2)
    payments_to_partner = round(sum(x["outgoing"] for x in bank_rows), 2)
    sales = round(sum(x["amount"] for x in invoice_rows if x["direction"] == "outgoing"), 2)
    purchases = round(sum(x["amount"] for x in invoice_rows if x["direction"] == "incoming"), 2)

    receivable = round(sales - payments_from_partner, 2)
    payable = round(purchases - payments_to_partner, 2)
    net = round(receivable - payable, 2)

    dates = [x["date"] for x in bank_rows + invoice_rows if x.get("date")]
    period_from = min(dates) if dates else "—"
    period_to = max(dates) if dates else "—"

    if not bank_rows and not invoice_rows:
        return (
            f"🧮 АКТ СВЕРКА\n\nҲамкор: {partner_name}\n\n"
            "Бу ҳамкор бўйича тасдиқланган банк операцияси ёки фактура топилмади.\n"
            "Банк выпискаси ва фактура реестрини юбориб, «✅ Тасдиқлаш»ни босинг."
        )

    lines = [
        "🧮 АКТ СВЕРКА",
        "",
        f"Ҳамкор: {partner_name}",
        f"Давр: {period_from} — {period_to}",
        "",
        f"🧾 Имзоланган сотув фактуралари: {_money(sales)} UZS",
        f"📥 Ҳамкордан тушган тўлов: {_money(payments_from_partner)} UZS",
        f"💰 Дебитор қарз: {_money(receivable)} UZS",
        "",
        f"📦 Кирувчи фактуралар: {_money(purchases)} UZS",
        f"📤 Ҳамкорга тўланган: {_money(payments_to_partner)} UZS",
        f"💸 Кредитор қарз: {_money(payable)} UZS",
        "",
        f"⚖️ Соф фарқ: {_money(net)} UZS",
        f"🏦 Банк операциялари: {len(bank_rows)} та",
        f"🧾 Фактуралар: {len(invoice_rows)} та",
    ]

    if invoice_rows:
        lines += ["", "Сўнгги фактуралар:"]
        for inv in sorted(invoice_rows, key=lambda x: x.get("date") or "")[-7:]:
            direction = "сотув" if inv["direction"] == "outgoing" else "харид"
            lines.append(
                f"• {inv.get('date') or '—'} | №{inv.get('number') or '—'} | "
                f"{direction} | {_money(inv['amount'])}"
            )

    return "\n".join(lines)


def invoice_registry_text(data: dict) -> str:
    period = data.get("statement_period") or {}
    warnings = data.get("warnings") or []
    counterparties = data.get("counterparties") or []

    lines = [
        "🧾 ФАКТУРАЛАР РЕЕСТРИ — ТАҲЛИЛ",
        "",
        f"Давр: {period.get('from') or '—'} — {period.get('to') or '—'}",
        f"Жами ҳужжатлар: {data.get('invoice_count') or 0} та",
        f"👥 Контрагентлар: {data.get('partner_count') or 0} та",
        f"✅ Имзоланган: {data.get('signed_count') or 0} та",
        f"⏳ Имзо кутилмоқда: {data.get('pending_count') or 0} та",
        f"🗑 Ўчирилган: {data.get('deleted_count') or 0} та",
        f"⚠️ Ҳақиқий эмас: {data.get('invalid_count') or 0} та",
        "",
        f"📤 Имзоланган чиқувчи фактуралар: {_money(data.get('signed_sales_total'))} UZS",
        f"📥 Имзоланган кирувчи фактуралар: {_money(data.get('signed_purchases_total'))} UZS",
        f"⏳ Имзо кутаётган сумма: {_money(data.get('pending_total'))} UZS",
    ]

    if counterparties:
        lines += ["", "👥 Йирик контрагентлар:"]
        for item in counterparties[:8]:
            lines.append(
                f"• {item.get('name') or '—'} | "
                f"сотув {_money(item.get('signed_outgoing'))} | "
                f"харид {_money(item.get('signed_incoming'))}"
            )

    if warnings:
        lines += ["", "⚠️ Эътибор:"] + [f"• {w}" for w in warnings[:8]]

    lines += [
        "",
        "👥 Контрагентлар автоматик «Ҳамкорлар» базасига қўшилди.",
        "Ҳужжатнинг ўзи базага фақат «✅ Тасдиқлаш» босилганда сақланади.",
    ]
    return "\n".join(lines)


def install_bot_enhancements(bot_module):
    bot_module.MENU = ReplyKeyboardMarkup(
        [
            ["📎 Ҳужжат юклаш", "🤖 AI таҳлил"],
            ["👥 Ҳамкорлар", "📄 Шартномалар"],
            ["📦 Хом ашё омбори", "📥 Кирим"],
            ["🧾 Чиқиш фактура", "🧮 Акт сверка"],
            ["📊 Ҳисоботлар", "ℹ️ Ёрдам"],
        ],
        resize_keyboard=True,
    )

    menu_labels = {button for row in bot_module.MENU.keyboard for button in row}
    original_text_handler = bot_module.text_handler
    original_analysis_text = bot_module.analysis_text
    original_show_analysis = bot_module.show_analysis

    def enhanced_analysis_text(data):
        if isinstance(data, dict) and data.get("document_type") == "invoice_registry":
            return invoice_registry_text(data)
        return original_analysis_text(data)

    async def enhanced_show_analysis(update, context, data, telegram_file_id, filename, mime_type):
        count = sync_partners_from_data(data) if isinstance(data, dict) else 0
        await original_show_analysis(update, context, data, telegram_file_id, filename, mime_type)
        if count:
            await update.effective_message.reply_text(
                f"👥 Ҳамкорлар автоматик қўшилди/янгиланди: {count} та.",
                reply_markup=bot_module.MENU,
            )

    async def enhanced_text_handler(update, context):
        text = (update.message.text or "").strip()

        if text == "👥 Ҳамкорлар":
            rows = list_partners(limit=100)
            body = "\n".join(
                f"• {p.name}" + (f" | СТИР {p.tin}" if p.tin else "") for p in rows
            ) if rows else "Ҳозирча ҳамкорлар йўқ."
            await update.message.reply_text(
                f"👥 Ҳамкорлар:\n\n{body}\n\n"
                "✅ Ҳамкорлар банк выпискаси, фактура реестри ва ҳужжатлардан автоматик қўшилади.",
                reply_markup=bot_module.MENU,
            )
            return

        if text == "🧮 Акт сверка":
            rows = list_partners(limit=50)
            body = "\n".join(f"• {p.name}" for p in rows) if rows else "Ҳозирча ҳамкорлар йўқ."
            context.user_data["awaiting_reconciliation_partner"] = True
            await update.message.reply_text(
                "🧮 АКТ СВЕРКА\n\n"
                f"Ҳамкорлар:\n{body}\n\n"
                "Акт чиқариш учун ҳамкор номини ёзинг.",
                reply_markup=bot_module.MENU,
            )
            return

        if text.lower().startswith("акт:"):
            name = text.split(":", 1)[1].strip()
            context.user_data.pop("awaiting_reconciliation_partner", None)
            await update.message.reply_text(reconciliation_for_partner(name))
            return

        if context.user_data.get("awaiting_reconciliation_partner") and text not in menu_labels:
            context.user_data.pop("awaiting_reconciliation_partner", None)
            await update.message.reply_text(reconciliation_for_partner(text))
            return

        if context.user_data.get("awaiting_reconciliation_partner") and text in menu_labels:
            context.user_data.pop("awaiting_reconciliation_partner", None)

        await original_text_handler(update, context)

    bot_module.analysis_text = enhanced_analysis_text
    bot_module.show_analysis = enhanced_show_analysis
    bot_module.text_handler = enhanced_text_handler
