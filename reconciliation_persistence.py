import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database import Document, engine, save_document


def _norm_name(value):
    return " ".join(str(value or "").split()).strip().casefold()


def _money(value):
    try:
        return f"{float(value):,.2f}".replace(",", " ")
    except Exception:
        return "0.00"


def _matches_partner(candidate: str, requested: str) -> bool:
    a = _norm_name(candidate)
    b = _norm_name(requested)
    if not a or not b:
        return False
    if a == b:
        return True
    return min(len(a), len(b)) >= 5 and (a in b or b in a)


def _collect_reconciliation(partner_name: str) -> dict:
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
    period_from = min(dates) if dates else None
    period_to = max(dates) if dates else None

    return {
        "document_type": "reconciliation_report",
        "partner": {"name": partner_name},
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


def _save_snapshot(partner_name: str, data: dict) -> tuple[int | None, bool]:
    signature_data = {
        "partner": _norm_name(partner_name),
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
            .limit(100)
        ).all()

        for doc in recent:
            if _norm_name(doc.partner_name) != _norm_name(partner_name):
                continue
            try:
                previous = json.loads(doc.raw_json or "{}")
            except Exception:
                continue
            if previous.get("snapshot_key") == snapshot_key:
                return doc.id, False

    doc_id = save_document(
        data,
        telegram_file_id=None,
        filename=f"act-sverka-{partner_name[:80]}.json",
        mime_type="application/json",
    )
    return doc_id, True


def reconciliation_for_partner(partner_name: str) -> str:
    data = _collect_reconciliation(partner_name)
    bank_rows = data["bank_rows"]
    invoice_rows = data["invoice_rows"]

    if not bank_rows and not invoice_rows:
        return (
            f"🧮 АКТ СВЕРКА\n\nҲамкор: {partner_name}\n\n"
            "Бу ҳамкор бўйича тасдиқланган банк операцияси ёки фактура топилмади.\n"
            "Банк выпискаси ва фактура реестрини юбориб, «✅ Тасдиқлаш»ни босинг."
        )

    doc_id, created = _save_snapshot(partner_name, data)
    period = data["statement_period"]

    lines = [
        "🧮 АКТ СВЕРКА",
        "",
        f"Ҳамкор: {partner_name}",
        f"Давр: {period.get('from') or '—'} — {period.get('to') or '—'}",
        "",
        f"🧾 Имзоланган сотув фактуралари: {_money(data['sales'])} UZS",
        f"📥 Ҳамкордан тушган тўлов: {_money(data['payments_from_partner'])} UZS",
        f"💰 Дебитор қарз: {_money(data['receivable'])} UZS",
        "",
        f"📦 Кирувчи фактуралар: {_money(data['purchases'])} UZS",
        f"📤 Ҳамкорга тўланган: {_money(data['payments_to_partner'])} UZS",
        f"💸 Кредитор қарз: {_money(data['payable'])} UZS",
        "",
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
        text = (update.message.text or "").strip()
        if text != "📊 Ҳисоботлар":
            await original_text_handler(update, context)
            return

        base = bot_module.report()
        total, recent = _recent_reports(limit=5)
        lines = [
            "📊 ҲИСОБОТ",
            "",
            f"👥 Ҳамкорлар: {base['partners']}",
            f"📄 Шартномалар: {base['contracts']}",
            f"📦 Хом ашё турлари: {base['materials']}",
            f"📎 Сақланган ҳужжатлар: {base['documents']}",
            f"🧮 Сақланган акт сверка таҳлиллари: {total}",
            f"💰 Шартнома умумий қолдиғи: {_money(base['contract_balance'])}",
        ]

        if recent:
            lines += ["", "🗂 Сўнгги акт сверка таҳлиллари:"]
            for doc, data in recent:
                period = data.get("statement_period") or {}
                partner = doc.partner_name or (data.get("partner") or {}).get("name") or "—"
                lines.append(
                    f"• {partner} | {period.get('from') or '—'} — {period.get('to') or '—'} | "
                    f"дебитор {_money(data.get('receivable'))} | "
                    f"кредитор {_money(data.get('payable'))} | "
                    f"фарқ {_money(data.get('net'))}"
                )

        await update.message.reply_text("\n".join(lines), reply_markup=bot_module.MENU)

    bot_module.text_handler = text_handler
