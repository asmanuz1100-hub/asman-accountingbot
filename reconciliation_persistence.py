import json
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database import Document, engine, save_document
from enhancements import (
    _money,
    _norm_account,
    _norm_tin,
    party_matches_target,
    reconciliation_partner_groups,
    resolve_reconciliation_party,
)


def _norm_name(value):
    return " ".join(str(value or "").split()).strip().casefold()


def _safe_float(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _all_parties():
    outgoing, incoming = reconciliation_partner_groups()
    result = []
    seen = set()
    for party in outgoing + incoming:
        key = (
            _norm_tin(party.get("tin")) or "",
            _norm_account(party.get("account")) or "",
            _norm_name(party.get("name")),
        )
        strong = ("tin", key[0]) if key[0] else ("acc", key[1]) if key[1] else ("name", key[2])
        if strong in seen:
            continue
        seen.add(strong)
        result.append(party)
    return result


def _resolve_partner_flexible(identifier):
    found = resolve_reconciliation_party(identifier)
    if found:
        return found

    raw = " ".join(str(identifier or "").split()).strip()
    if not raw:
        return None

    digits = _norm_tin(raw)
    account = _norm_account(raw)
    name = _norm_name(raw)
    parties = _all_parties()

    exact = []
    for party in parties:
        tin = _norm_tin(party.get("tin"))
        acc = _norm_account(party.get("account"))
        p_name = _norm_name(party.get("name"))
        if tin and digits and (digits == tin or tin in re.sub(r"\D", "", raw)):
            exact.append(party)
            continue
        if acc and account and (account == acc or acc in account):
            exact.append(party)
            continue
        if p_name and name == p_name:
            exact.append(party)

    if len(exact) == 1:
        return exact[0]

    partial = [p for p in parties if name and name in _norm_name(p.get("name"))]
    if len(partial) == 1:
        return partial[0]
    return None


def _flow_ok(flow):
    return flow if flow in ("outgoing", "incoming") else None


def _collect_reconciliation(identifier: str, flow: str | None = None):
    flow = _flow_ok(flow)
    target = _resolve_partner_flexible(identifier)
    if not target:
        return None

    bank_rows = []
    invoice_rows = []
    entries = []
    seen_bank = set()
    seen_invoice = set()
    own_company = {}

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
            if not own_company:
                own_company = {
                    "name": data.get("account_holder"),
                    "tin": data.get("tax_id"),
                    "account": data.get("account_number"),
                }

            for tx in data.get("transactions") or data.get("transactions_preview") or []:
                if not party_matches_target(
                    tx.get("counterparty"),
                    tx.get("counterparty_tin"),
                    tx.get("counterparty_account"),
                    target,
                ):
                    continue

                incoming = round(_safe_float(tx.get("incoming")), 2)
                outgoing = round(_safe_float(tx.get("outgoing")), 2)

                if flow == "outgoing" and incoming <= 0:
                    continue
                if flow == "incoming" and outgoing <= 0:
                    continue

                key = (
                    str(tx.get("date") or ""),
                    _norm_tin(tx.get("counterparty_tin")),
                    _norm_account(tx.get("counterparty_account")),
                    str(tx.get("document_number") or ""),
                    str(tx.get("purpose") or "").strip(),
                    incoming,
                    outgoing,
                )
                if key in seen_bank:
                    continue
                seen_bank.add(key)

                row = {
                    "date": key[0],
                    "incoming": incoming,
                    "outgoing": outgoing,
                    "purpose": key[4],
                    "document_number": key[3] or None,
                }
                bank_rows.append(row)

                entries.append(
                    {
                        "date": key[0],
                        "kind": "bank",
                        "document": f"Банк тўлови{f' №{key[3]}' if key[3] else ''}",
                        "basis": key[4] or "Банк операцияси",
                        "debit": outgoing,
                        "credit": incoming,
                    }
                )

        elif data.get("document_type") == "invoice_registry":
            for inv in data.get("invoices") or []:
                if inv.get("status_group") != "signed":
                    continue
                direction = str(inv.get("direction") or "")
                if direction not in ("incoming", "outgoing"):
                    continue
                if flow and direction != flow:
                    continue
                if not party_matches_target(
                    inv.get("counterparty"),
                    inv.get("counterparty_tin"),
                    inv.get("counterparty_account"),
                    target,
                ):
                    continue

                amount = round(_safe_float(inv.get("total")), 2)
                key = (
                    str(inv.get("document_number") or ""),
                    str(inv.get("document_date") or ""),
                    _norm_tin(inv.get("counterparty_tin")),
                    direction,
                    amount,
                )
                if key in seen_invoice:
                    continue
                seen_invoice.add(key)

                row = {
                    "number": key[0],
                    "date": key[1],
                    "direction": key[3],
                    "amount": amount,
                    "contract": inv.get("contract"),
                    "document_type_name": inv.get("document_type_name"),
                }
                invoice_rows.append(row)

                entries.append(
                    {
                        "date": key[1],
                        "kind": "invoice",
                        "document": f"Фактура №{key[0] or '—'}",
                        "basis": inv.get("contract") or inv.get("document_type_name") or "Имзоланган электрон фактура",
                        "debit": amount if direction == "outgoing" else 0,
                        "credit": amount if direction == "incoming" else 0,
                    }
                )

    entries = sorted(entries, key=lambda x: (str(x.get("date") or ""), 0 if x.get("kind") == "bank" else 1))

    payments_from_partner = round(sum(x["incoming"] for x in bank_rows), 2)
    payments_to_partner = round(sum(x["outgoing"] for x in bank_rows), 2)
    sales = round(sum(x["amount"] for x in invoice_rows if x["direction"] == "outgoing"), 2)
    purchases = round(sum(x["amount"] for x in invoice_rows if x["direction"] == "incoming"), 2)

    receivable = round(sales - payments_from_partner, 2)
    payable = round(purchases - payments_to_partner, 2)

    debit_total = round(sum(_safe_float(x.get("debit")) for x in entries), 2)
    credit_total = round(sum(_safe_float(x.get("credit")) for x in entries), 2)
    net = round(debit_total - credit_total, 2)

    dates = [x["date"] for x in entries if x.get("date")]
    period_from = min(dates) if dates else None
    period_to = max(dates) if dates else None

    return {
        "document_type": "reconciliation_report",
        "flow": flow,
        "flow_label": "Чиқим / сотув" if flow == "outgoing" else "Кирим / харид" if flow == "incoming" else "Умумий",
        "partner": {
            "name": target.get("name"),
            "tin": target.get("tin"),
            "account_number": target.get("account"),
        },
        "own_company": own_company,
        "document_date": period_to,
        "currency": "UZS",
        "total": net,
        "statement_period": {"from": period_from, "to": period_to},
        "opening_debit": 0.0,
        "opening_credit": 0.0,
        "debit_total": debit_total,
        "credit_total": credit_total,
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
        "entries": entries,
    }


def _save_snapshot(data: dict) -> tuple[int | None, bool]:
    partner = data.get("partner") or {}
    signature_data = {
        "flow": data.get("flow"),
        "tin": _norm_tin(partner.get("tin")),
        "account": _norm_account(partner.get("account_number")),
        "name": _norm_name(partner.get("name")) if not partner.get("tin") and not partner.get("account_number") else None,
        "period": data.get("statement_period"),
        "debit_total": data.get("debit_total"),
        "credit_total": data.get("credit_total"),
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
            .limit(500)
        ).all()

        for doc in recent:
            try:
                previous = json.loads(doc.raw_json or "{}")
            except Exception:
                continue
            if previous.get("snapshot_key") == snapshot_key:
                previous["report_id"] = doc.id
                return doc.id, False

    name = partner.get("name") or "partner"
    doc_id = save_document(
        data,
        telegram_file_id=None,
        filename=f"act-sverka-{str(name)[:80]}.json",
        mime_type="application/json",
    )
    data["report_id"] = doc_id

    with Session(engine) as session:
        doc = session.get(Document, doc_id)
        if doc:
            doc.raw_json = json.dumps(data, ensure_ascii=False, default=str)
            session.commit()

    return doc_id, True


def _summary_text(data, doc_id, created):
    partner = data.get("partner") or {}
    period = data.get("statement_period") or {}
    flow = data.get("flow")
    lines = [
        "🧮 АКТ СВЕРКА",
        "━━━━━━━━━━━━━━━━",
        "",
        f"🏢 Ҳамкор: {partner.get('name') or '—'}",
        f"🆔 ИНН: {partner.get('tin') or '—'}",
        f"🏦 Ҳисоб рақами: {partner.get('account_number') or '—'}",
        f"📅 Давр: {period.get('from') or '—'} — {period.get('to') or '—'}",
        "",
    ]

    if flow == "outgoing":
        lines += [
            "📤 ЧИҚИМ / СОТУВ",
            f"🧾 Сотув фактуралари: {_money(data.get('sales'))} UZS",
            f"💳 Ҳамкордан тушган тўлов: {_money(data.get('payments_from_partner'))} UZS",
            f"💰 Дебитор қарз: {_money(data.get('receivable'))} UZS",
        ]
    elif flow == "incoming":
        lines += [
            "📥 КИРИМ / ХАРИД",
            f"📦 Кирувчи фактуралар: {_money(data.get('purchases'))} UZS",
            f"💳 Ҳамкорга тўланган: {_money(data.get('payments_to_partner'))} UZS",
            f"💸 Кредитор қарз: {_money(data.get('payable'))} UZS",
        ]
    else:
        lines += [
            f"📊 Дебет: {_money(data.get('debit_total'))} UZS",
            f"📊 Кредит: {_money(data.get('credit_total'))} UZS",
            f"⚖️ Фарқ: {_money(data.get('net'))} UZS",
        ]

    lines += [
        "",
        f"🏦 Банк операциялари: {data.get('bank_operations_count') or 0} та",
        f"🧾 Фактуралар: {data.get('invoice_count') or 0} та",
        "",
        (f"💾 Базага сақланди. Акт ID: {doc_id}" if created else f"🗂 Бу ҳолат аввал сақланган. Акт ID: {doc_id}"),
        "📄 Батафсил операциялар PDF файлда чиқади.",
    ]
    return "\n".join(lines)


def prepare_reconciliation(identifier: str, flow: str | None = None) -> dict:
    data = _collect_reconciliation(identifier, flow)
    if not data:
        return {
            "ok": False,
            "text": (
                "❌ Ҳамкор аниқланмади.\n\n"
                "Ҳамкор номи, ИНН ёки ҳисоб рақамини киритинг. "
                "Агар бир хил номли бир нечта ҳамкор бўлса, ИНН ёки ҳисоб рақамини киритинг."
            ),
        }

    if not data.get("entries"):
        partner = data.get("partner") or {}
        return {
            "ok": False,
            "text": (
                f"⚠️ {partner.get('name') or 'Ҳамкор'} бўйича танланган йўналишда "
                "тасдиқланган банк операцияси ёки фактура топилмади."
            ),
        }

    doc_id, created = _save_snapshot(data)
    data["report_id"] = doc_id
    return {
        "ok": True,
        "text": _summary_text(data, doc_id, created),
        "data": data,
        "doc_id": doc_id,
        "created": created,
    }


def reconciliation_for_partner(identifier: str, flow: str | None = None) -> str:
    return prepare_reconciliation(identifier, flow).get("text") or ""


def get_saved_reconciliation(doc_id: int):
    with Session(engine) as session:
        doc = session.get(Document, int(doc_id))
        if not doc or doc.document_type != "reconciliation_report":
            return None
        try:
            data = json.loads(doc.raw_json or "{}")
        except Exception:
            return None
        data["report_id"] = doc.id
        return data


def list_saved_reconciliations(query: str | None = None, limit: int = 30):
    with Session(engine) as session:
        docs = session.scalars(
            select(Document)
            .where(Document.document_type == "reconciliation_report")
            .order_by(Document.id.desc())
            .limit(1000 if query else limit)
        ).all()

    q_raw = " ".join(str(query or "").split()).strip()
    q_name = _norm_name(q_raw)
    q_digits = _norm_tin(q_raw)
    q_account = _norm_account(q_raw)
    rows = []

    for doc in docs:
        try:
            data = json.loads(doc.raw_json or "{}")
        except Exception:
            continue
        partner = data.get("partner") or {}
        if q_raw:
            hay_name = _norm_name(partner.get("name"))
            hay_tin = _norm_tin(partner.get("tin"))
            hay_acc = _norm_account(partner.get("account_number") or partner.get("account"))
            id_match = q_digits and hay_tin and (q_digits == hay_tin or q_digits in hay_tin)
            acc_match = q_account and hay_acc and (q_account == hay_acc or q_account in hay_acc)
            name_match = q_name and q_name in hay_name
            if not (id_match or acc_match or name_match or q_raw == str(doc.id)):
                continue

        data["report_id"] = doc.id
        rows.append({"id": doc.id, "data": data, "created_at": doc.created_at})
        if len(rows) >= limit:
            break

    return rows


def _recent_reports(limit: int = 5):
    rows = list_saved_reconciliations(limit=limit)
    with Session(engine) as session:
        total = session.scalar(
            select(func.count()).select_from(Document).where(Document.document_type == "reconciliation_report")
        ) or 0
    return int(total), rows


def install_reconciliation_persistence(bot_module, enhancements_module):
    enhancements_module.reconciliation_for_partner = reconciliation_for_partner
    enhancements_module.prepare_reconciliation = prepare_reconciliation
    enhancements_module.get_saved_reconciliation = get_saved_reconciliation
    enhancements_module.list_saved_reconciliations = list_saved_reconciliations

    original_text_handler = bot_module.text_handler

    async def text_handler(update, context):
        text_value = (update.message.text or "").strip()
        if text_value != "📊 Ҳисоботлар":
            await original_text_handler(update, context)
            return

        base = bot_module.report()
        total, recent = _recent_reports(limit=5)
        lines = [
            "📊 ҲИСОБОТ",
            "━━━━━━━━━━━━━━━━",
            "",
            f"👥 Ҳамкорлар: {base['partners']}",
            f"📄 Шартномалар: {base['contracts']}",
            f"📦 Хом ашё турлари: {base['materials']}",
            f"📎 Сақланган ҳужжатлар: {base['documents']}",
            f"🧮 Сақланган акт сверка: {total}",
            f"💰 Шартнома умумий қолдиғи: {_money(base['contract_balance'])}",
        ]

        if recent:
            lines += ["", "🗂 Сўнгги актлар:"]
            for item in recent:
                row = item["data"]
                period = row.get("statement_period") or {}
                partner = row.get("partner") or {}
                direction = "📤" if row.get("flow") == "outgoing" else "📥" if row.get("flow") == "incoming" else "🔄"
                lines.append(
                    f"{direction} #{item['id']} {partner.get('name') or '—'} | "
                    f"ИНН {partner.get('tin') or '—'} | "
                    f"{period.get('from') or '—'} - {period.get('to') or '—'}"
                )

        lines += ["", "🗂 Барча сақланган актларни кўриш учун «Акт сверка» -> «Сақланган актлар»ни очинг."]
        await update.message.reply_text("\n".join(lines), reply_markup=bot_module.MENU)

    bot_module.text_handler = text_handler
