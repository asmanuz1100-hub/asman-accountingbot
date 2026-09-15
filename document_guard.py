import copy
import hashlib
import json
import re
from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from database import Document, engine


def _norm(value):
    return " ".join(str(value or "").replace("\xa0", " ").split()).strip().casefold()


def _tin(value):
    return re.sub(r"\D", "", str(value or ""))


def _account(value):
    return re.sub(r"[^0-9A-Za-z]", "", str(value or "")).upper()


def _num(value):
    try:
        return round(float(value or 0), 2)
    except Exception:
        return 0.0


def _read_json(doc):
    try:
        return json.loads(doc.raw_json or "{}")
    except Exception:
        return {}


def file_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw or b"").hexdigest()


def _all_confirmed_documents():
    with Session(engine) as session:
        return list(session.scalars(select(Document).order_by(Document.id)).all())


def _source_documents(document_type=None):
    with Session(engine) as session:
        stmt = select(Document)
        if document_type:
            stmt = stmt.where(Document.document_type == document_type)
        else:
            stmt = stmt.where(Document.document_type.in_(["bank_statement", "invoice_registry"]))
        return list(session.scalars(stmt.order_by(Document.id)).all())


def find_exact_file_duplicate(file_hash: str):
    if not file_hash:
        return None
    for doc in reversed(_all_confirmed_documents()):
        data = _read_json(doc)
        ingest = data.get("_ingest") or {}
        saved_hash = str(ingest.get("file_sha256") or data.get("file_sha256") or "").strip()
        if saved_hash and saved_hash == file_hash:
            return {
                "id": doc.id,
                "filename": doc.filename,
                "document_type": doc.document_type,
                "document_date": doc.document_date,
            }
    return None


def _party_key(name=None, tin=None, account=None):
    tin_n = _tin(tin)
    if tin_n:
        return f"tin:{tin_n}"
    acc_n = _account(account)
    if acc_n:
        return f"acc:{acc_n}"
    return f"name:{_norm(name)}"


def _bank_signature(tx):
    payload = (
        str(tx.get("date") or ""),
        _party_key(tx.get("counterparty"), tx.get("counterparty_tin"), tx.get("counterparty_account")),
        str(tx.get("document_number") or "").strip(),
        _num(tx.get("incoming")),
        _num(tx.get("outgoing")),
        _norm(tx.get("purpose")),
    )
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _invoice_signature(inv):
    payload = (
        str(inv.get("direction") or "").strip().lower(),
        str(inv.get("status_group") or inv.get("status") or "").strip().lower(),
        _norm(inv.get("document_type_name")),
        str(inv.get("document_number") or "").strip(),
        str(inv.get("document_date") or "").strip(),
        _party_key(inv.get("counterparty"), inv.get("counterparty_tin"), inv.get("counterparty_account")),
        _num(inv.get("total")),
    )
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _generic_signature(data):
    if not isinstance(data, dict):
        return None
    partner = data.get("partner") or {}
    doc_type = str(data.get("document_type") or "").strip().lower()
    number = (
        data.get("invoice_number")
        or data.get("document_number")
        or data.get("contract_number")
        or data.get("number")
    )
    date = (
        data.get("invoice_date")
        or data.get("document_date")
        or data.get("contract_date")
        or data.get("date")
    )
    total = data.get("total")
    party = _party_key(
        partner.get("name") if isinstance(partner, dict) else None,
        partner.get("tin") if isinstance(partner, dict) else None,
        (partner.get("account_number") or partner.get("bank_account") or partner.get("account"))
        if isinstance(partner, dict) else None,
    )
    if not doc_type or (not number and not date and total is None):
        return None
    payload = (doc_type, party, str(number or "").strip(), str(date or "").strip(), _num(total), str(data.get("currency") or "").upper())
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _existing_bank_signatures():
    result = set()
    for doc in _source_documents("bank_statement"):
        data = _read_json(doc)
        for tx in data.get("transactions") or data.get("transactions_preview") or []:
            result.add(_bank_signature(tx))
    return result


def _existing_invoice_signatures():
    result = set()
    for doc in _source_documents("invoice_registry"):
        data = _read_json(doc)
        for inv in data.get("invoices") or []:
            result.add(_invoice_signature(inv))
    return result


def _existing_generic_signatures():
    result = set()
    for doc in _all_confirmed_documents():
        data = _read_json(doc)
        if data.get("document_type") in ("bank_statement", "invoice_registry"):
            continue
        sig = _generic_signature(data)
        if sig:
            result.add(sig)
    return result


def _is_invoice_type(value):
    text = _norm(value).replace("ё", "е")
    return any(token in text for token in (
        "ҳисоб-фактура", "хисоб-фактура", "счет-фактура", "счёт-фактура", "invoice"
    ))


def _rebuild_bank(data, transactions, duplicate_count):
    original_count = len(data.get("transactions") or data.get("transactions_preview") or [])
    data["transactions"] = transactions
    data["transactions_preview"] = transactions[:10]
    data["operations_count"] = len(transactions)
    data["total_incoming"] = round(sum(_num(x.get("incoming")) for x in transactions), 2)
    data["total_outgoing"] = round(sum(_num(x.get("outgoing")) for x in transactions), 2)

    grouped = defaultdict(lambda: {"incoming": 0.0, "outgoing": 0.0})
    for tx in transactions:
        name = str(tx.get("counterparty") or "—").strip() or "—"
        grouped[name]["incoming"] += _num(tx.get("incoming"))
        grouped[name]["outgoing"] += _num(tx.get("outgoing"))
    data["top_counterparties"] = [
        {"name": name, "incoming": round(vals["incoming"], 2), "outgoing": round(vals["outgoing"], 2)}
        for name, vals in sorted(
            grouped.items(),
            key=lambda kv: kv[1]["incoming"] + kv[1]["outgoing"],
            reverse=True,
        )[:10]
    ]

    dates = [str(x.get("date")) for x in transactions if x.get("date")]
    if dates:
        data["statement_period"] = {"from": min(dates), "to": max(dates)}

    warnings = list(data.get("warnings") or [])
    if duplicate_count:
        warnings.insert(0, f"{duplicate_count} та олдин қабул қилинган банк операцияси қайта қўшилмади.")
        # File-level balances describe the full source file, not only the new subset.
        data["opening_balance"] = None
        data["closing_balance"] = None
    data["warnings"] = warnings[:12]
    data["summary"] = (
        f"Янги {len(transactions)} та банк операцияси топилди. "
        f"Кирим {data['total_incoming']:,.2f}, чиқим {data['total_outgoing']:,.2f} UZS."
        + (f" {duplicate_count} та такрорий операция ўтказиб юборилди." if duplicate_count else "")
    )
    return original_count


def _rebuild_invoice_registry(data, invoices, duplicate_count):
    original_count = len(data.get("invoices") or [])
    data["invoices"] = invoices
    accounting = [x for x in invoices if x.get("is_accounting_invoice") or _is_invoice_type(x.get("document_type_name"))]
    signed = [x for x in accounting if str(x.get("status_group") or "").lower() == "signed"]
    signed_in = [x for x in signed if x.get("direction") == "incoming"]
    signed_out = [x for x in signed if x.get("direction") == "outgoing"]
    pending = [x for x in accounting if str(x.get("status_group") or "").lower() == "pending"]
    deleted = [x for x in accounting if str(x.get("status_group") or "").lower() == "deleted"]
    invalid = [x for x in accounting if str(x.get("status_group") or "").lower() == "invalid"]

    data["all_document_count"] = len(invoices)
    data["invoice_count"] = len(accounting)
    data["signed_count"] = len(signed)
    data["signed_incoming_invoice_count"] = len(signed_in)
    data["signed_outgoing_invoice_count"] = len(signed_out)
    data["pending_count"] = len(pending)
    data["deleted_count"] = len(deleted)
    data["invalid_count"] = len(invalid)
    data["outgoing_count"] = sum(1 for x in accounting if x.get("direction") == "outgoing")
    data["incoming_count"] = sum(1 for x in accounting if x.get("direction") == "incoming")
    data["signed_sales_total"] = round(sum(_num(x.get("total")) for x in signed_out), 2)
    data["signed_purchases_total"] = round(sum(_num(x.get("total")) for x in signed_in), 2)
    data["signed_purchases_without_vat"] = round(sum(_num(x.get("amount_without_vat")) for x in signed_in), 2)
    data["signed_input_vat"] = round(sum(_num(x.get("vat")) for x in signed_in), 2)
    data["pending_total"] = round(sum(_num(x.get("total")) for x in pending), 2)
    data["registry_total"] = round(sum(_num(x.get("total")) for x in invoices), 2)
    data["total"] = round(data["signed_sales_total"] + data["signed_purchases_total"], 2)

    party_totals = defaultdict(lambda: {
        "name": None, "tin": None, "account": None, "invoice_count": 0,
        "signed_count": 0, "signed_outgoing": 0.0, "signed_incoming": 0.0,
    })
    purchase_categories = defaultdict(lambda: {"count": 0, "total": 0.0})
    supplier_totals = defaultdict(lambda: {"name": None, "tin": None, "count": 0, "total": 0.0})

    for inv in invoices:
        key = _party_key(inv.get("counterparty"), inv.get("counterparty_tin"), inv.get("counterparty_account"))
        p = party_totals[key]
        p["name"] = inv.get("counterparty") or p["name"]
        p["tin"] = _tin(inv.get("counterparty_tin")) or p["tin"]
        p["account"] = _account(inv.get("counterparty_account")) or p["account"]
        p["invoice_count"] += 1
        if str(inv.get("status_group") or "").lower() == "signed" and (inv.get("is_accounting_invoice") or _is_invoice_type(inv.get("document_type_name"))):
            p["signed_count"] += 1
            if inv.get("direction") == "outgoing":
                p["signed_outgoing"] += _num(inv.get("total"))
            elif inv.get("direction") == "incoming":
                p["signed_incoming"] += _num(inv.get("total"))

        if inv in signed_in:
            cat = inv.get("purchase_category") or "other"
            purchase_categories[cat]["count"] += 1
            purchase_categories[cat]["total"] += _num(inv.get("total"))
            s = supplier_totals[key]
            s["name"] = inv.get("counterparty") or s["name"]
            s["tin"] = _tin(inv.get("counterparty_tin")) or s["tin"]
            s["count"] += 1
            s["total"] += _num(inv.get("total"))

    counterparties = []
    for vals in party_totals.values():
        counterparties.append({
            "name": vals["name"] or "Номсиз ҳамкор",
            "tin": vals["tin"],
            "account": vals["account"],
            "invoice_count": vals["invoice_count"],
            "signed_count": vals["signed_count"],
            "signed_outgoing": round(vals["signed_outgoing"], 2),
            "signed_incoming": round(vals["signed_incoming"], 2),
        })
    counterparties.sort(key=lambda x: x["signed_outgoing"] + x["signed_incoming"], reverse=True)
    data["counterparties"] = counterparties
    data["partner_count"] = len(counterparties)

    category_keys = ("raw_material", "utilities", "tax", "other")
    data["purchase_categories"] = {
        key: {
            "count": int(purchase_categories[key]["count"]),
            "total": round(purchase_categories[key]["total"], 2),
        }
        for key in category_keys
    }
    data["top_purchase_suppliers"] = [
        {
            "name": vals["name"], "tin": vals["tin"], "count": vals["count"],
            "total": round(vals["total"], 2),
        }
        for vals in sorted(supplier_totals.values(), key=lambda x: x["total"], reverse=True)[:10]
    ]

    dates = [str(x.get("document_date")) for x in invoices if x.get("document_date") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(x.get("document_date")))]
    if dates:
        data["statement_period"] = {"from": min(dates), "to": max(dates)}

    warnings = list(data.get("warnings") or [])
    if duplicate_count:
        warnings.insert(0, f"{duplicate_count} та олдин қабул қилинган реестр қатори қайта қўшилмади.")
    data["warnings"] = warnings[:12]
    data["summary"] = (
        f"Янги {len(invoices)} та реестр қатори топилди. "
        f"Имзоланган чиқувчи {data['signed_sales_total']:,.2f} UZS, "
        f"кирувчи {data['signed_purchases_total']:,.2f} UZS."
        + (f" {duplicate_count} та такрорий қатор ўтказиб юборилди." if duplicate_count else "")
    )
    return original_count


def prepare_new_document(data: dict, file_hash: str = "", filename: str = ""):
    if not isinstance(data, dict):
        return {"status": "new", "data": data, "new_count": 1, "duplicate_count": 0}

    result = copy.deepcopy(data)
    doc_type = str(result.get("document_type") or "").strip()

    if doc_type == "bank_statement":
        source = list(result.get("transactions") or result.get("transactions_preview") or [])
        existing = _existing_bank_signatures()
        current = set()
        new_rows, duplicate_count = [], 0
        for row in source:
            sig = _bank_signature(row)
            if sig in existing or sig in current:
                duplicate_count += 1
                continue
            current.add(sig)
            new_rows.append(row)
        if not new_rows and source:
            return {"status": "duplicate", "data": result, "new_count": 0, "duplicate_count": duplicate_count}
        original_count = _rebuild_bank(result, new_rows, duplicate_count)
        new_count = len(new_rows)

    elif doc_type == "invoice_registry":
        source = list(result.get("invoices") or [])
        existing = _existing_invoice_signatures()
        current = set()
        new_rows, duplicate_count = [], 0
        for row in source:
            sig = _invoice_signature(row)
            if sig in existing or sig in current:
                duplicate_count += 1
                continue
            current.add(sig)
            new_rows.append(row)
        if not new_rows and source:
            return {"status": "duplicate", "data": result, "new_count": 0, "duplicate_count": duplicate_count}
        original_count = _rebuild_invoice_registry(result, new_rows, duplicate_count)
        new_count = len(new_rows)

    else:
        sig = _generic_signature(result)
        if sig and sig in _existing_generic_signatures():
            return {"status": "duplicate", "data": result, "new_count": 0, "duplicate_count": 1}
        original_count = 1
        new_count = 1
        duplicate_count = 0

    status = "partial" if duplicate_count else "new"
    previous_ingest = result.get("_ingest") or {}
    result["_ingest"] = {
        **previous_ingest,
        "file_sha256": file_hash or previous_ingest.get("file_sha256"),
        "filename": filename or previous_ingest.get("filename"),
        "source_record_count": int(previous_ingest.get("source_record_count") or original_count),
        "new_record_count": int(new_count),
        "duplicate_record_count": int(duplicate_count),
        "status": status,
        "confirmed_only": True,
    }
    return {
        "status": status,
        "data": result,
        "new_count": new_count,
        "duplicate_count": duplicate_count,
    }


def _notice(data):
    ingest = data.get("_ingest") or {} if isinstance(data, dict) else {}
    new_count = int(ingest.get("new_record_count") or 0)
    duplicate_count = int(ingest.get("duplicate_record_count") or 0)
    if duplicate_count:
        return (
            f"🆕 Янги маълумот топилди: {new_count} та\n"
            f"♻️ Олдин қабул қилинган: {duplicate_count} та — қайта қўшилмади.\n"
            "🔐 Фақат «✅ Тасдиқлаш»дан кейин базага киритилади.\n\n"
        )
    if new_count > 1:
        return (
            f"🆕 Янги маълумот топилди: {new_count} та\n"
            "🔐 Фақат «✅ Тасдиқлаш»дан кейин базага киритилади.\n\n"
        )
    return "🆕 Янги ҳужжат топилди.\n🔐 Тасдиқдан кейин базага киритилади.\n\n"


def install_document_guard(bot_module, enhancements_module):
    # The old enhancement layer synchronized partners during preview. Keep the
    # real function, but disable preview-time writes; synchronize only after a
    # successful user confirmation.
    real_partner_sync = getattr(enhancements_module, "sync_partners_from_data", None)
    if callable(real_partner_sync):
        enhancements_module.sync_partners_from_data = lambda data: 0

    original_analysis_text = bot_module.analysis_text
    original_show_analysis = bot_module.show_analysis
    original_document_handler = bot_module.document_handler
    original_photo_handler = bot_module.photo_handler
    original_confirm_callback = bot_module.confirm_callback

    def analysis_text(data):
        text = original_analysis_text(data)
        text = text.replace(
            "👥 Контрагентлар автоматик «Ҳамкорлар» базасига қўшилди.",
            "👥 Контрагентлар фақат тасдиқдан кейин «Ҳамкорлар» базасига қўшилади.",
        )
        return _notice(data) + text

    async def show_analysis(update, context, data, telegram_file_id, filename, mime_type):
        file_hash = str(context.user_data.pop("_upload_file_sha", "") or "")
        result = prepare_new_document(data, file_hash=file_hash, filename=filename)
        if result.get("status") == "duplicate":
            context.user_data.pop("pending_doc", None)
            await update.effective_message.reply_text(
                "♻️ Бу ҳужжатда янги маълумот топилмади.\n"
                "Олдин қабул қилинган маълумотлар қайта базага киритилмайди.",
                reply_markup=bot_module.MENU,
            )
            return
        await original_show_analysis(
            update, context, result["data"], telegram_file_id, filename, mime_type
        )

    async def document_handler(update, context):
        doc = update.message.document
        try:
            tg_file = await context.bot.get_file(doc.file_id)
            raw = bytes(await tg_file.download_as_bytearray())
            digest = file_sha256(raw)
            duplicate = find_exact_file_duplicate(digest)
            if duplicate:
                context.user_data.pop("pending_doc", None)
                await update.message.reply_text(
                    "♻️ Бу файл айнан олдин юкланган.\n"
                    f"Сақланган ҳужжат ID: {duplicate['id']}\n"
                    "Қайта таҳлил қилинмади ва базага қўшилмади.",
                    reply_markup=bot_module.MENU,
                )
                return
            context.user_data["_upload_file_sha"] = digest
        except Exception:
            context.user_data.pop("_upload_file_sha", None)
        try:
            await original_document_handler(update, context)
        finally:
            context.user_data.pop("_upload_file_sha", None)

    async def photo_handler(update, context):
        try:
            photo = update.message.photo[-1]
            tg_file = await context.bot.get_file(photo.file_id)
            raw = bytes(await tg_file.download_as_bytearray())
            digest = file_sha256(raw)
            duplicate = find_exact_file_duplicate(digest)
            if duplicate:
                context.user_data.pop("pending_doc", None)
                await update.message.reply_text(
                    "♻️ Бу расм айнан олдин юкланган.\n"
                    f"Сақланган ҳужжат ID: {duplicate['id']}\n"
                    "Қайта таҳлил қилинмади.",
                    reply_markup=bot_module.MENU,
                )
                return
            context.user_data["_upload_file_sha"] = digest
        except Exception:
            context.user_data.pop("_upload_file_sha", None)
        try:
            await original_photo_handler(update, context)
        finally:
            context.user_data.pop("_upload_file_sha", None)

    async def confirm_callback(update, context):
        query = update.callback_query
        is_confirm = bool(query and query.data == "doc_confirm")
        pending = context.user_data.get("pending_doc") or {}
        pending_data = pending.get("data") if isinstance(pending, dict) else None

        if is_confirm and isinstance(pending_data, dict):
            ingest = pending_data.get("_ingest") or {}
            digest = str(ingest.get("file_sha256") or "")
            duplicate_file = find_exact_file_duplicate(digest) if digest else None
            if duplicate_file:
                await query.answer()
                context.user_data.pop("pending_doc", None)
                await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_text(
                    "♻️ Тасдиқлаш пайтида бу файл олдин базага киритилгани аниқланди.\n"
                    f"Ҳужжат ID: {duplicate_file['id']}\n"
                    "Иккинчи марта сақланмади.",
                    reply_markup=bot_module.MENU,
                )
                return

            refreshed = prepare_new_document(
                pending_data,
                file_hash=digest,
                filename=str(ingest.get("filename") or pending.get("filename") or ""),
            )
            if refreshed.get("status") == "duplicate":
                await query.answer()
                context.user_data.pop("pending_doc", None)
                await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_text(
                    "♻️ Тасдиқлаш пайтида янги маълумот қолмагани аниқланди.\n"
                    "Такрорий маълумотлар базага киритилмади.",
                    reply_markup=bot_module.MENU,
                )
                return
            pending["data"] = refreshed["data"]
            context.user_data["pending_doc"] = pending
            pending_data = refreshed["data"]

        await original_confirm_callback(update, context)

        if is_confirm and isinstance(pending_data, dict) and not context.user_data.get("pending_doc"):
            if callable(real_partner_sync):
                try:
                    count = real_partner_sync(pending_data)
                    if count:
                        await query.message.reply_text(
                            f"👥 Тасдиқдан кейин ҳамкорлар қўшилди/янгиланди: {count} та.",
                            reply_markup=bot_module.MENU,
                        )
                except Exception:
                    pass

    bot_module.analysis_text = analysis_text
    bot_module.show_analysis = show_analysis
    bot_module.document_handler = document_handler
    bot_module.photo_handler = photo_handler
    bot_module.confirm_callback = confirm_callback
