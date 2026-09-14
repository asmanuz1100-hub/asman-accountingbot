import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database import Document, engine, save_document
from enhancements import (
    _money,
    _norm_account,
    _norm_tin,
    party_matches_target,
    resolve_reconciliation_party,
)


def _collect_reconciliation(identifier: str):
    target = resolve_reconciliation_party(identifier)
    if not target:
        return None

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
                    "date": key[0],
                    "incoming": incoming,
                    "outgoing": outgoing,
                    "purpose": key[3],
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
    period_from = min(dates) if dates else None
    period_to = max(dates) if dates else None

    return {
        "document_type": "reconciliation_report",
        "partner": {
            "name": target.get("name"),
            "tin": target.get("tin"),
            "account_number": target.get("account"),
        },
        "document_date": period_to,
        "currency": "UZS",
        "total": net,
        "statement_period": {"from": period_from, "to": period_to},
        "sales": sales,
        "payments_from_partner": payments_from_partner,
        "receivable": receivable,
        "purchases": purchases,
        "payments_to_partner": payments_to_partner,
        "payable": payable,
        "net": net,
        "bank_operations_count": len(bank_rows),
        "invoice_count": len(invoice_rows),
        "bank_rows": bank_rows,
        "invoice_rows": invoice_rows,
    }


def _save_snapshot(data: dict) -> tuple[int | None, bool]:
    partner = data.get("partner") or {}
    signature_data = {
        "tin": _norm_tin(partner.get("tin")),
        "account": _norm_account(partner.get("account_number")),
        "period": data.get("statement_period"),
        "sales": data.get("sales"),
        "payments_from_partner": data.get("payments_from_partner"),
        "receivable": data.get("receivable"),
        "purchases": data.get("purchases"),
        "payments_to_partner": data.get("payments_to_partner"),
        "payable": data.get("payable"),
        "net": data.get("net"),
        "bank_operations_count": data.get("bank_operations_count"),
        "invoice_count": data.get("invoice_count"),
    }
    snapshot_key = json.dumps(signature_data, ensure_ascii=False, sort_keys=True)
    data["snapshot_key"] = snapshot_key

    with Session(engine) as session:
        recent = session.scalars(
            select(Document)
            .where(Document.document_type == "reconciliation_report")
            .order_by(Document.id.desc())
            .limit(200)
        ).all()

        for doc in recent:
            try:
                previous = json.loads(doc.raw_json or "{}")
            except Exception:
                continue
            if previous.get("snapshot_key") == snapshot_key:
                return doc.id, False

    name = partner.get("name") or "partner"
    doc_id = save_document(
        data,
        telegram_file_id=None,
        filename=f"act-sverka-{str(name)[:80]}.json",
        mime_type="application/json",
    )
    return doc_id, True


def reconciliation_for_partner(identifier: str) -> str:
    data = _collect_reconciliation(identifier)
    if not data:
        return (
            "❌ Ҳамкор аниқланмади.\n\n"
            "Ҳамкорни номи билан эмас, рўйхатдаги ИНН ёки ҳисоб рақами билан танланг."
        )

    bank_rows = data["bank_rows"]
    invoice_rows = data["invoice_rows"]
    partner = data.get("partner") or {}

    if not bank_rows and not invoice_rows:
        return (
            f"🧮 АКТ СВЕРКА\n\nҲамкор: {partner.get('name') or '—'}\n"
            f"ИНН: {partner.get('tin') or '—'}\n"
            f"Ҳисоб рақами: {partner.get('account_number') or '—'}\n\n"
            "Бу ҳамкор бўйича тасдиқланган банк операцияси ёки фактура топилмади."
        )

    doc_id, created = _save_snapshot(data)
    period = data["statement_period"]

    lines = [
        "🧮 АКТ СВЕРКА", "",
        f"Ҳамкор: {partner.get('name') or '—'}",
        f"ИНН: {partner.get('tin') or '—'}",
        f"Ҳисоб рақами: {partner.get('account_number') or '—'}",
        f"Давр: {period.get('from') or '—'} — {period.get('to') or '—'}", "",
        f"🧾 Имзоланган сотув фактуралари: {_money(data['sales'])} UZS",
        f"📥 Ҳамкордан тушган тўлов: {_money(data['payments_from_partner'])} UZS",
        f"💰 Дебитор қарз: {_money(data['receivable'])} UZS", "",
        f"📦 Кирувчи фактуралар: {_money(data['purchases'])} UZS",
        f"📤 Ҳамкорга тўланган: {_money(data['payments_to_partner'])} UZS",
        f"💸 Кредитор қарз: {_money(data['payable'])} UZS", "",
        f"⚖️ Соф фарқ: {_money(data['net'])} UZS",
        f"🏦 Банк операциялари: {data['bank_operations_count']} та",
        f"🧾 Фактуралар: {data['invoice_count']} та",
    ]

    if invoice_rows:
        lines += ["", "Сўнгги фактуралар:"]
        for inv in sorted(invoice_rows, key=lambda x: x.get("date") or "")[-7:]:
            direction = "сотув" if inv["direction"] == "outgoing" else "харид"
            lines.append(
                f"• {inv.get('date') or '—'} | №{inv.get('number') or '—'} | "
                f"{direction} | {_money(inv['amount'])}"
            )

    lines += [
        "",
        (
            f"💾 Таҳлил «Ҳисоботлар» бўлимига сақланди. ID: {doc_id}"
            if created
            else f"ℹ️ Бу таҳлил аввал сақланган. ID: {doc_id}"
        ),
    ]
    return "\n".join(lines)


def _recent_reports(limit: int = 5):
    with Session(engine) as session:
        total = session.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.document_type == "reconciliation_report")
        ) or 0

        docs = session.scalars(
            select(Document)
            .where(Document.document_type == "reconciliation_report")
            .order_by(Document.id.desc())
            .limit(limit)
        ).all()

    recent = []
    for doc in docs:
        try:
            data = json.loads(doc.raw_json or "{}")
        except Exception:
            data = {}
        recent.append((doc, data))
    return int(total), recent


def install_reconciliation_persistence(bot_module, enhancements_module):
    enhancements_module.reconciliation_for_partner = reconciliation_for_partner
    original_text_handler = bot_module.text_handler

    async def text_handler(update, context):
        text_value = (update.message.text or "").strip()
        if text_value != "📊 Ҳисоботлар":
            await original_text_handler(update, context)
            return

        base = bot_module.report()
        total, recent = _recent_reports(limit=5)
        lines = [
            "📊 ҲИСОБОТ", "",
            f"👥 Ҳамкорлар: {base['partners']}",
            f"📄 Шартномалар: {base['contracts']}",
            f"📦 Хом ашё турлари: {base['materials']}",
            f"📎 Сақланган ҳужжатлар: {base['documents']}",
            f"🧮 Сақланган акт сверка таҳлиллари: {total}",
            f"💰 Шартнома умумий қолдиғи: {_money(base['contract_balance'])}",
        ]

        if recent:
            lines += ["", "🗂 Сўнгги акт сверка таҳлиллари:"]
            for doc, row in recent:
                period = row.get("statement_period") or {}
                partner = row.get("partner") or {}
                name = partner.get("name") or doc.partner_name or "—"
                tin = partner.get("tin") or "—"
                lines.append(
                    f"• {name} | ИНН {tin} | {period.get('from') or '—'} — {period.get('to') or '—'} | "
                    f"дебитор {_money(row.get('receivable'))} | "
                    f"кредитор {_money(row.get('payable'))} | "
                    f"фарқ {_money(row.get('net'))}"
                )

        await update.message.reply_text("\n".join(lines), reply_markup=bot_module.MENU)

    bot_module.text_handler = text_handler
