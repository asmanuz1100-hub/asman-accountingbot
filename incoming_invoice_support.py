import json
import re
from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

import invoice_registry
from database import Document, engine, save_document


CATEGORY_LABELS = {
    "raw_material": "🧱 Хом ашё",
    "utilities": "💡 Коммунал",
    "tax": "🧾 Солиқ",
    "other": "📦 Бошқа",
}


def _money(value):
    try:
        return f"{float(value or 0):,.2f}".replace(",", " ")
    except Exception:
        return "0.00"


def _norm(value):
    return " ".join(str(value or "").replace("\xa0", " ").split()).strip().casefold()


def _tin(value):
    return re.sub(r"\D", "", str(value or ""))


def _number(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def is_accounting_invoice_type(value):
    text = _norm(value).replace("ё", "е")
    return any(
        token in text
        for token in (
            "ҳисоб-фактура",
            "хисоб-фактура",
            "счет-фактура",
            "счёт-фактура",
            "invoice",
        )
    )


def _classify_purchase(inv):
    partner = _norm(inv.get("counterparty"))
    contract = _norm(inv.get("contract"))
    doc_type = _norm(inv.get("document_type_name"))
    text = f"{partner} {contract} {doc_type}"

    tax_words = (
        "солиқ", "soliq", "налог", "бюджет", "казнач", "ғазнач", "инспекция",
    )
    utility_words = (
        "hududiy elektr", "электр", "elektr", "энерг", "hududgaz", "газ",
        "suv ta'minoti", "suv taminoti", "сув таъминоти", "водоканал", "сув",
        "o`zbektelekom", "o'zbektelekom", "uzbektelekom", "телеком", "internet",
        "aloqa", "связь", "иссиқлик", "тепло", "коммун",
    )
    raw_words = (
        "kimya", "кимё", "kimyo", "chemical", "хим", "plast", "plastic",
        "пласт", "akril", "акрил", "cement", "цемент", "pigment", "пигмент",
        "qadoq", "қадоқ", "upakov", "упаков", "tara", "тара", "print",
        "product", "interchim", "mavera", "sedir", "alvon", "ferman",
    )

    if any(word in text for word in tax_words):
        return "tax"
    if any(word in text for word in utility_words):
        return "utilities"
    if any(word in text for word in raw_words):
        return "raw_material"
    return "other"


def try_analyze_invoice_registry_v2(data: bytes, filename: str = "registry.xlsx"):
    result = invoice_registry.try_analyze_invoice_registry(data, filename)
    if result is None:
        return None

    invoices = result.get("invoices") or []
    for inv in invoices:
        inv["is_accounting_invoice"] = is_accounting_invoice_type(inv.get("document_type_name"))
        if inv.get("direction") == "incoming" and inv.get("is_accounting_invoice"):
            inv["purchase_category"] = _classify_purchase(inv)

    accounting = [x for x in invoices if x.get("is_accounting_invoice")]
    signed = [x for x in accounting if x.get("status_group") == "signed"]
    signed_incoming = [x for x in signed if x.get("direction") == "incoming"]
    signed_outgoing = [x for x in signed if x.get("direction") == "outgoing"]

    result["all_document_count"] = result.get("invoice_count") or len(invoices)
    result["invoice_count"] = len(accounting)
    result["signed_count"] = len(signed)
    result["signed_incoming_invoice_count"] = len(signed_incoming)
    result["signed_outgoing_invoice_count"] = len(signed_outgoing)
    result["signed_sales_total"] = round(sum(_number(x.get("total")) for x in signed_outgoing), 2)
    result["signed_purchases_total"] = round(sum(_number(x.get("total")) for x in signed_incoming), 2)
    result["signed_purchases_without_vat"] = round(sum(_number(x.get("amount_without_vat")) for x in signed_incoming), 2)
    result["signed_input_vat"] = round(sum(_number(x.get("vat")) for x in signed_incoming), 2)
    result["total"] = round(result["signed_sales_total"] + result["signed_purchases_total"], 2)

    category_totals = defaultdict(float)
    category_counts = Counter()
    supplier_totals = defaultdict(lambda: {"name": None, "tin": None, "count": 0, "total": 0.0})
    for inv in signed_incoming:
        category = inv.get("purchase_category") or "other"
        category_counts[category] += 1
        category_totals[category] += _number(inv.get("total"))

        name = str(inv.get("counterparty") or "").strip()
        tin = _tin(inv.get("counterparty_tin")) or None
        key = f"tin:{tin}" if tin else f"name:{_norm(name)}"
        item = supplier_totals[key]
        item["name"] = name or item["name"]
        item["tin"] = tin or item["tin"]
        item["count"] += 1
        item["total"] += _number(inv.get("total"))

    result["purchase_categories"] = {
        key: {"count": int(category_counts[key]), "total": round(category_totals[key], 2)}
        for key in CATEGORY_LABELS
    }
    result["top_purchase_suppliers"] = [
        {
            "name": vals["name"],
            "tin": vals["tin"],
            "count": vals["count"],
            "total": round(vals["total"], 2),
        }
        for _, vals in sorted(supplier_totals.items(), key=lambda kv: kv[1]["total"], reverse=True)[:10]
    ]

    if signed_incoming and not signed_outgoing:
        result["summary"] = (
            f"Кирувчи реестрдан {len(accounting)} та бухгалтерия ҳисоб-фактураси аниқланди. "
            f"Имзоланган кирувчи фактуралар: {len(signed_incoming)} та, "
            f"жами {result['signed_purchases_total']:,.2f} UZS, "
            f"кирим ҚҚС {result['signed_input_vat']:,.2f} UZS."
        )
    else:
        result["summary"] = (
            f"Бухгалтерия ҳисоб-фактуралари: {len(accounting)} та. "
            f"Имзоланган чиқувчи {result['signed_sales_total']:,.2f} UZS, "
            f"имзоланган кирувчи {result['signed_purchases_total']:,.2f} UZS."
        )

    result["source_format"] = "invoice_registry_xlsx_v3"
    return result


def _known_purchase_signatures():
    result = set()
    with Session(engine) as session:
        docs = session.scalars(
            select(Document).where(Document.document_type == "purchase_invoice_entry")
        ).all()
    for doc in docs:
        try:
            data = json.loads(doc.raw_json or "{}")
        except Exception:
            continue
        sig = str(data.get("source_signature") or "").strip()
        if sig:
            result.add(sig)
    return result


def _save_purchase_invoice(inv):
    category = inv.get("purchase_category") or _classify_purchase(inv)
    data = {
        "document_type": "purchase_invoice_entry",
        "purchase_category": category,
        "document_date": inv.get("document_date"),
        "invoice_number": inv.get("document_number"),
        "contract": inv.get("contract"),
        "currency": "UZS",
        "amount_without_vat": round(_number(inv.get("amount_without_vat")), 2),
        "vat": round(_number(inv.get("vat")), 2),
        "total": round(_number(inv.get("total")), 2),
        "partner": {
            "name": inv.get("counterparty"),
            "tin": inv.get("counterparty_tin"),
        },
        "source": "invoice_registry",
        "source_signature": inv.get("source_signature"),
    }
    return save_document(
        data,
        telegram_file_id=None,
        filename=f"purchase-invoice-{data.get('invoice_number') or 'no-number'}.json",
        mime_type="application/json",
    )


def _sync_incoming_invoices(data):
    if not isinstance(data, dict) or data.get("document_type") != "invoice_registry":
        return 0, 0.0, {key: 0.0 for key in CATEGORY_LABELS}

    known = _known_purchase_signatures()
    added = 0
    total = 0.0
    amounts = {key: 0.0 for key in CATEGORY_LABELS}

    for inv in data.get("invoices") or []:
        if inv.get("status_group") != "signed" or inv.get("direction") != "incoming":
            continue
        if not (inv.get("is_accounting_invoice") or is_accounting_invoice_type(inv.get("document_type_name"))):
            continue

        amount = round(_number(inv.get("total")), 2)
        if amount <= 0:
            continue
        tin = _tin(inv.get("counterparty_tin"))
        signature = "|".join(
            (
                str(inv.get("document_date") or ""),
                tin,
                str(inv.get("document_number") or "").strip(),
                f"{amount:.2f}",
            )
        )
        if signature in known:
            continue

        inv["source_signature"] = signature
        inv["purchase_category"] = inv.get("purchase_category") or _classify_purchase(inv)
        _save_purchase_invoice(inv)
        known.add(signature)
        added += 1
        total += amount
        amounts[inv["purchase_category"]] += amount

    return added, round(total, 2), amounts


def _registry_analysis_text(data):
    period = data.get("statement_period") or {}
    categories = data.get("purchase_categories") or {}
    suppliers = data.get("top_purchase_suppliers") or []
    warnings = data.get("warnings") or []

    lines = [
        "📥 КИРУВЧИ ФАКТУРАЛАР — ТАҲЛИЛ",
        "━━━━━━━━━━━━━━━━",
        "",
        f"📅 Давр: {period.get('from') or '—'} — {period.get('to') or '—'}",
        f"📚 Реестрдаги барча ҳужжатлар: {data.get('all_document_count') or 0} та",
        f"🧾 Бухгалтерия ҳисоб-фактуралари: {data.get('invoice_count') or 0} та",
        f"✅ Имзоланган кирувчи фактуралар: {data.get('signed_incoming_invoice_count') or 0} та",
        "",
        f"💰 Жами кирувчи фактура: {_money(data.get('signed_purchases_total'))} UZS",
        f"📄 ҚҚСсиз: {_money(data.get('signed_purchases_without_vat'))} UZS",
        f"🧾 Кирим ҚҚС: {_money(data.get('signed_input_vat'))} UZS",
    ]

    if categories:
        lines += ["", "📂 Тахминий тоифалар:"]
        for key in ("raw_material", "utilities", "tax", "other"):
            item = categories.get(key) or {}
            if item.get("count"):
                lines.append(
                    f"{CATEGORY_LABELS[key]}: {item.get('count')} та | {_money(item.get('total'))} UZS"
                )

    if suppliers:
        lines += ["", "🏢 Энг йирик етказиб берувчилар:"]
        for item in suppliers[:6]:
            lines.append(
                f"• {item.get('name') or '—'} | ИНН {item.get('tin') or '—'} | {_money(item.get('total'))}"
            )

    lines += [
        "",
        "ℹ️ ТТЮ, ишончнома, эркин шаклдаги ҳужжат ва бошқа ёрдамчи ҳужжатлар "
        "қарз/акт сверка суммасига иккинчи марта қўшилмайди.",
    ]
    if warnings:
        lines += ["", "⚠️ Текшириш:"] + [f"• {w}" for w in warnings[:6]]

    lines += [
        "",
        "Маълумот базага ҳали сақланмади.",
        "Текшириб, кейин «✅ Тасдиқлаш»ни босинг.",
    ]
    return "\n".join(lines)


def patch_reconciliation_collection(reconciliation_module):
    original_collect = reconciliation_module._collect_reconciliation

    def _collect(identifier, flow=None):
        data = original_collect(identifier, flow)
        if not data:
            return data

        invoice_rows = [
            row for row in (data.get("invoice_rows") or [])
            if is_accounting_invoice_type(row.get("document_type_name"))
        ]
        bank_rows = list(data.get("bank_rows") or [])

        entries = []
        for tx in bank_rows:
            incoming = round(_number(tx.get("incoming")), 2)
            outgoing = round(_number(tx.get("outgoing")), 2)
            doc_no = tx.get("document_number")
            entries.append({
                "date": tx.get("date"),
                "kind": "bank",
                "document": f"Банк тўлови{f' №{doc_no}' if doc_no else ''}",
                "basis": tx.get("purpose") or "Банк операцияси",
                "debit": outgoing,
                "credit": incoming,
            })

        for inv in invoice_rows:
            direction = inv.get("direction")
            amount = round(_number(inv.get("amount")), 2)
            entries.append({
                "date": inv.get("date"),
                "kind": "invoice",
                "document": f"Фактура №{inv.get('number') or '—'}",
                "basis": inv.get("contract") or inv.get("document_type_name") or "Имзоланган электрон фактура",
                "debit": amount if direction == "outgoing" else 0,
                "credit": amount if direction == "incoming" else 0,
            })

        entries.sort(key=lambda x: (str(x.get("date") or ""), 0 if x.get("kind") == "bank" else 1))

        payments_from_partner = round(sum(_number(x.get("incoming")) for x in bank_rows), 2)
        payments_to_partner = round(sum(_number(x.get("outgoing")) for x in bank_rows), 2)
        sales = round(sum(_number(x.get("amount")) for x in invoice_rows if x.get("direction") == "outgoing"), 2)
        purchases = round(sum(_number(x.get("amount")) for x in invoice_rows if x.get("direction") == "incoming"), 2)
        debit_total = round(sum(_number(x.get("debit")) for x in entries), 2)
        credit_total = round(sum(_number(x.get("credit")) for x in entries), 2)

        data["invoice_rows"] = invoice_rows
        data["entries"] = entries
        data["invoice_count"] = len(invoice_rows)
        data["sales"] = sales
        data["purchases"] = purchases
        data["payments_from_partner"] = payments_from_partner
        data["payments_to_partner"] = payments_to_partner
        data["receivable"] = round(sales - payments_from_partner, 2)
        data["payable"] = round(purchases - payments_to_partner, 2)
        data["debit_total"] = debit_total
        data["credit_total"] = credit_total
        data["net"] = round(debit_total - credit_total, 2)
        data["total"] = data["net"]

        dates = [x.get("date") for x in entries if x.get("date")]
        if dates:
            data["statement_period"] = {"from": min(dates), "to": max(dates)}
            data["document_date"] = max(dates)
        return data

    reconciliation_module._collect_reconciliation = _collect


def install_incoming_invoice_support(bot_module):
    original_analysis_text = bot_module.analysis_text
    original_confirm_callback = bot_module.confirm_callback

    def analysis_text(data):
        if isinstance(data, dict) and data.get("document_type") == "invoice_registry":
            return _registry_analysis_text(data)
        return original_analysis_text(data)

    async def confirm_callback(update, context):
        pending = context.user_data.get("pending_doc") or {}
        pending_data = pending.get("data") if isinstance(pending, dict) else None
        is_confirm = bool(update.callback_query and update.callback_query.data == "doc_confirm")

        await original_confirm_callback(update, context)

        if is_confirm and isinstance(pending_data, dict) and pending_data.get("document_type") == "invoice_registry":
            try:
                added, total, amounts = _sync_incoming_invoices(pending_data)
                if added:
                    details = []
                    for key in ("raw_material", "utilities", "tax", "other"):
                        if amounts[key] > 0:
                            details.append(f"{CATEGORY_LABELS[key]}: {_money(amounts[key])} UZS")
                    await update.callback_query.message.reply_text(
                        "📥 Кирувчи фактуралар базага киритилди.\n"
                        f"Янги фактуралар: {added} та\n"
                        f"Жами: {_money(total)} UZS\n" + "\n".join(details),
                        reply_markup=bot_module.MENU,
                    )
                else:
                    await update.callback_query.message.reply_text(
                        "📥 Кирувчи фактуралар текширилди: янги фактура йўқ, дубликатлар қайта киритилмади.",
                        reply_markup=bot_module.MENU,
                    )
            except Exception:
                # Registry itself was already stored safely by the original callback.
                pass

    bot_module.analysis_text = analysis_text
    bot_module.confirm_callback = confirm_callback
