import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session
from telegram import ReplyKeyboardMarkup

from asaka_excel import _Parser, _date, _decode, _num, _split_party
from database import Document, engine, list_partners, upsert_partner


def _norm_name(value):
    return " ".join(str(value or "").split()).strip().casefold()


def _norm_tin(value):
    return re.sub(r"\D", "", str(value or ""))


def _norm_account(value):
    return re.sub(r"[^0-9A-Za-z]", "", str(value or "")).upper()


def _money(value):
    try:
        return f"{float(value):,.2f}".replace(",", " ")
    except Exception:
        return "0.00"


def _party_has_id(party):
    return bool(_norm_tin(party.get("tin")) or _norm_account(party.get("account")))


def _same_identity(a, b):
    """Legal identity: same TIN first, bank account second, name only as fallback."""
    a_tin, b_tin = _norm_tin(a.get("tin")), _norm_tin(b.get("tin"))
    if a_tin and b_tin:
        return a_tin == b_tin

    a_acc, b_acc = _norm_account(a.get("account")), _norm_account(b.get("account"))
    if a_acc and b_acc:
        return a_acc == b_acc

    if not _party_has_id(a) and not _party_has_id(b):
        return bool(_norm_name(a.get("name"))) and _norm_name(a.get("name")) == _norm_name(b.get("name"))
    return False


def _add_party(records, name, tin=None, account=None, flow=None):
    name = " ".join(str(name or "").split()).strip()
    tin = _norm_tin(tin) or None
    account = _norm_account(account) or None
    if not name and not tin and not account:
        return

    item = {
        "name": name[:255] or "Номсиз ҳамкор",
        "tin": tin,
        "account": account,
        "flows": set([flow]) if flow in ("incoming", "outgoing") else set(),
    }

    for existing in records:
        if _same_identity(existing, item):
            if not existing.get("tin") and tin:
                existing["tin"] = tin
            if not existing.get("account") and account:
                existing["account"] = account
            if len(item["name"]) > len(existing.get("name") or ""):
                existing["name"] = item["name"]
            existing["flows"].update(item["flows"])
            return
    records.append(item)


def _party_label(party):
    parts = [party.get("name") or "—"]
    if party.get("tin"):
        parts.append(f"ИНН {party['tin']}")
    if party.get("account"):
        parts.append(f"с/р {party['account']}")
    return " | ".join(parts)


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


def _partner_records_from_data(data: dict):
    records = []

    partner = data.get("partner") or {}
    if isinstance(partner, dict) and (partner.get("name") or partner.get("tin")):
        _add_party(
            records,
            partner.get("name"),
            partner.get("tin"),
            partner.get("account_number") or partner.get("bank_account") or partner.get("account"),
        )

    if data.get("document_type") == "bank_statement":
        own_name = _norm_name(data.get("account_holder"))
        for tx in data.get("transactions") or data.get("transactions_preview") or []:
            name = " ".join(str(tx.get("counterparty") or "").split()).strip()
            norm = _norm_name(name)
            if not name or norm == own_name or len(norm) < 3:
                continue
            if any(bad in norm for bad in (
                "итоговый оборот", "остаток на", "оборот дебет", "оборот кредит"
            )):
                continue

            # Money received usually belongs to a sales/customer reconciliation;
            # money paid usually belongs to a purchase/supplier reconciliation.
            incoming = float(tx.get("incoming") or 0)
            outgoing = float(tx.get("outgoing") or 0)
            flow = "outgoing" if incoming > 0 else "incoming" if outgoing > 0 else None
            _add_party(
                records,
                name,
                tx.get("counterparty_tin"),
                tx.get("counterparty_account"),
                flow,
            )

    if data.get("document_type") == "invoice_registry":
        for item in data.get("counterparties") or []:
            name = " ".join(str(item.get("name") or "").split()).strip()
            if not name:
                continue
            out_sum = float(item.get("signed_outgoing") or 0)
            in_sum = float(item.get("signed_incoming") or 0)
            if out_sum > 0:
                _add_party(records, name, item.get("tin"), item.get("account"), "outgoing")
            if in_sum > 0:
                _add_party(records, name, item.get("tin"), item.get("account"), "incoming")
            if out_sum <= 0 and in_sum <= 0:
                _add_party(records, name, item.get("tin"), item.get("account"))

    return records


def sync_partners_from_data(data: dict) -> int:
    saved = 0
    for item in _partner_records_from_data(data):
        flows = item.get("flows") or set()
        partner_type = "both" if len(flows) > 1 else next(iter(flows), "auto")
        try:
            upsert_partner(
                item.get("name") or "Номсиз ҳамкор",
                tin=item.get("tin"),
                account_number=item.get("account"),
                partner_type=partner_type,
            )
            saved += 1
        except Exception:
            continue
    return saved


def reconciliation_partner_groups():
    """Return deduplicated act-sverka partners split by outgoing/incoming invoices."""
    parties = []
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
                incoming = float(tx.get("incoming") or 0)
                outgoing = float(tx.get("outgoing") or 0)
                flow = "outgoing" if incoming > 0 else "incoming" if outgoing > 0 else None
                _add_party(
                    parties,
                    tx.get("counterparty"),
                    tx.get("counterparty_tin"),
                    tx.get("counterparty_account"),
                    flow,
                )

        elif data.get("document_type") == "invoice_registry":
            for inv in data.get("invoices") or []:
                if inv.get("status_group") != "signed":
                    continue
                direction = inv.get("direction")
                if direction not in ("incoming", "outgoing"):
                    continue
                _add_party(
                    parties,
                    inv.get("counterparty"),
                    inv.get("counterparty_tin"),
                    inv.get("counterparty_account"),
                    direction,
                )

    outgoing = sorted(
        [p for p in parties if "outgoing" in p["flows"]],
        key=lambda p: (p.get("name") or "").casefold(),
    )
    incoming = sorted(
        [p for p in parties if "incoming" in p["flows"]],
        key=lambda p: (p.get("name") or "").casefold(),
    )
    return outgoing, incoming


def resolve_reconciliation_party(identifier):
    identifier = " ".join(str(identifier or "").split()).strip()
    if not identifier:
        return None

    outgoing, incoming = reconciliation_partner_groups()
    parties = []
    for p in outgoing + incoming:
        if not any(_same_identity(p, old) for old in parties):
            parties.append(p)

    tin_input = _norm_tin(identifier)
    acc_input = _norm_account(identifier)

    # Exact identifier, or identifier embedded in a copied display line.
    for p in parties:
        tin = _norm_tin(p.get("tin"))
        account = _norm_account(p.get("account"))
        if tin and (tin_input == tin or tin in identifier):
            return p
        if account and (acc_input == account or account in _norm_account(identifier)):
            return p

    # Name is only a convenience fallback. If the name is ambiguous, force TIN/account.
    name_matches = [p for p in parties if _norm_name(p.get("name")) == _norm_name(identifier)]
    if len(name_matches) == 1:
        return name_matches[0]
    return None


def party_matches_target(name, tin, account, target):
    """Match transactions/invoices by strong identifiers; never fuzzy-match names when IDs exist."""
    cand_tin = _norm_tin(tin)
    target_tin = _norm_tin(target.get("tin"))
    if cand_tin and target_tin:
        return cand_tin == target_tin

    cand_acc = _norm_account(account)
    target_acc = _norm_account(target.get("account"))
    if cand_acc and target_acc:
        return cand_acc == target_acc

    if target_tin or target_acc or cand_tin or cand_acc:
        return False
    return _norm_name(name) == _norm_name(target.get("name"))


def reconciliation_for_partner(identifier: str) -> str:
    target = resolve_reconciliation_party(identifier)
    if not target:
        return (
            "❌ Ҳамкор аниқланмади.\n\n"
            "Акт сверкада ҳамкор номи асосий калит эмас. "
            "Рўйхатдаги ИНН ёки ҳисоб рақамини юборинг."
        )

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
                if not party_matches_target(
                    tx.get("counterparty"),
                    tx.get("counterparty_tin"),
                    tx.get("counterparty_account"),
                    target,
                ):
                    continue
                incoming = round(float(tx.get("incoming") or 0), 2)
                outgoing = round(float(tx.get("outgoing") or 0), 2)
                key = (
                    str(tx.get("date") or ""),
                    _norm_tin(tx.get("counterparty_tin")),
                    _norm_account(tx.get("counterparty_account")),
                    str(tx.get("purpose") or "").strip(),
                    incoming,
                    outgoing,
                )
                if key in seen_bank:
                    continue
                seen_bank.add(key)
                bank_rows.append({
                    "date": key[0], "incoming": incoming, "outgoing": outgoing, "purpose": key[3]
                })

        elif data.get("document_type") == "invoice_registry":
            for inv in data.get("invoices") or []:
                if inv.get("status_group") != "signed":
                    continue
                if not party_matches_target(
                    inv.get("counterparty"),
                    inv.get("counterparty_tin"),
                    inv.get("counterparty_account"),
                    target,
                ):
                    continue
                amount = round(float(inv.get("total") or 0), 2)
                key = (
                    str(inv.get("document_number") or ""),
                    str(inv.get("document_date") or ""),
                    _norm_tin(inv.get("counterparty_tin")),
                    str(inv.get("direction") or ""),
                    amount,
                )
                if key in seen_invoice:
                    continue
                seen_invoice.add(key)
                invoice_rows.append({
                    "number": key[0], "date": key[1], "direction": key[3], "amount": amount
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

    lines = [
        "🧮 АКТ СВЕРКА", "",
        f"Ҳамкор: {target.get('name') or '—'}",
        f"ИНН: {target.get('tin') or '—'}",
        f"Ҳисоб рақами: {target.get('account') or '—'}",
        f"Давр: {period_from} — {period_to}", "",
        f"🧾 Имзоланган сотув фактуралари: {_money(sales)} UZS",
        f"📥 Ҳамкордан тушган тўлов: {_money(payments_from_partner)} UZS",
        f"💰 Дебитор қарз: {_money(receivable)} UZS", "",
        f"📦 Кирувчи фактуралар: {_money(purchases)} UZS",
        f"📤 Ҳамкорга тўланган: {_money(payments_to_partner)} UZS",
        f"💸 Кредитор қарз: {_money(payable)} UZS", "",
        f"⚖️ Соф фарқ: {_money(net)} UZS",
        f"🏦 Банк операциялари: {len(bank_rows)} та",
        f"🧾 Фактуралар: {len(invoice_rows)} та",
    ]
    return "\n".join(lines)


def invoice_registry_text(data: dict) -> str:
    period = data.get("statement_period") or {}
    warnings = data.get("warnings") or []
    counterparties = data.get("counterparties") or []

    lines = [
        "🧾 ФАКТУРАЛАР РЕЕСТРИ — ТАҲЛИЛ", "",
        f"Давр: {period.get('from') or '—'} — {period.get('to') or '—'}",
        f"Жами ҳужжатлар: {data.get('invoice_count') or 0} та",
        f"👥 Контрагентлар: {data.get('partner_count') or 0} та",
        f"✅ Имзоланган: {data.get('signed_count') or 0} та",
        f"⏳ Имзо кутилмоқда: {data.get('pending_count') or 0} та",
        f"🗑 Ўчирилган: {data.get('deleted_count') or 0} та",
        f"⚠️ Ҳақиқий эмас: {data.get('invalid_count') or 0} та", "",
        f"📤 Имзоланган чиқувчи фактуралар: {_money(data.get('signed_sales_total'))} UZS",
        f"📥 Имзоланган кирувчи фактуралар: {_money(data.get('signed_purchases_total'))} UZS",
        f"⏳ Имзо кутаётган сумма: {_money(data.get('pending_total'))} UZS",
    ]

    if counterparties:
        lines += ["", "👥 Йирик контрагентлар:"]
        for item in counterparties[:8]:
            id_text = f" | ИНН {item.get('tin')}" if item.get("tin") else ""
            lines.append(
                f"• {item.get('name') or '—'}{id_text} | "
                f"сотув {_money(item.get('signed_outgoing'))} | "
                f"харид {_money(item.get('signed_incoming'))}"
            )

    if warnings:
        lines += ["", "⚠️ Эътибор:"] + [f"• {w}" for w in warnings[:8]]

    lines += [
        "", "👥 Контрагентлар автоматик «Ҳамкорлар» базасига қўшилди.",
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
            if rows:
                items = []
                for p in rows:
                    ident = []
                    if p.tin:
                        ident.append(f"ИНН {p.tin}")
                    if getattr(p, "account_number", None):
                        ident.append(f"с/р {p.account_number}")
                    items.append(f"• {p.name}" + (" | " + " | ".join(ident) if ident else ""))
                body = "\n".join(items)
            else:
                body = "Ҳозирча ҳамкорлар йўқ."
            await update.message.reply_text(
                f"👥 Ҳамкорлар:\n\n{body}\n\n"
                "✅ Бир хил ИНН ёки ҳисоб рақамли ҳамкор қайта яратилмайди.",
                reply_markup=bot_module.MENU,
            )
            return

        if text == "🧮 Акт сверка":
            outgoing, incoming = reconciliation_partner_groups()
            out_body = "\n".join(f"• {_party_label(p)}" for p in outgoing[:25]) or "—"
            in_body = "\n".join(f"• {_party_label(p)}" for p in incoming[:25]) or "—"
            context.user_data["awaiting_reconciliation_partner"] = True
            await update.message.reply_text(
                "🧮 АКТ СВЕРКА\n\n"
                "📤 ЧИҚИМ — сотув / харидорлар:\n"
                f"{out_body}\n\n"
                "📥 КИРИМ — харид / етказиб берувчилар:\n"
                f"{in_body}\n\n"
                "Акт чиқариш учун ИНН ёки ҳисоб рақамини юборинг.",
                reply_markup=bot_module.MENU,
            )
            return

        if text.lower().startswith("акт:"):
            identifier = text.split(":", 1)[1].strip()
            context.user_data.pop("awaiting_reconciliation_partner", None)
            await update.message.reply_text(reconciliation_for_partner(identifier))
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
