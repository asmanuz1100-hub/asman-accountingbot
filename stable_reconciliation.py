import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from database import Document, engine


def _norm_name(value):
    return " ".join(str(value or "").replace("\xa0", " ").split()).strip().casefold()


def _norm_tin(value):
    return re.sub(r"\D", "", str(value or ""))


def _norm_account(value):
    return re.sub(r"[^0-9A-Za-z]", "", str(value or "")).upper()


def _num(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _same_party(a, b):
    a = a or {}
    b = b or {}
    a_tin, b_tin = _norm_tin(a.get("tin")), _norm_tin(b.get("tin"))
    a_acc, b_acc = _norm_account(a.get("account")), _norm_account(b.get("account"))
    if a_tin and b_tin:
        return a_tin == b_tin
    if a_acc and b_acc:
        return a_acc == b_acc
    if not (a_tin or b_tin or a_acc or b_acc):
        return bool(_norm_name(a.get("name"))) and _norm_name(a.get("name")) == _norm_name(b.get("name"))
    return False


def _candidate_matches(name, tin, account, target):
    target = target or {}
    cand_tin = _norm_tin(tin)
    cand_acc = _norm_account(account)
    target_tin = _norm_tin(target.get("tin"))
    target_acc = _norm_account(target.get("account"))
    if cand_tin and target_tin:
        return cand_tin == target_tin
    if cand_acc and target_acc:
        return cand_acc == target_acc
    if cand_tin or cand_acc or target_tin or target_acc:
        return False
    return _norm_name(name) == _norm_name(target.get("name"))


def _is_real_invoice(inv):
    if str(inv.get("status_group") or "").strip().lower() != "signed":
        return False
    if str(inv.get("direction") or "") not in ("incoming", "outgoing"):
        return False
    if _num(inv.get("total")) <= 0:
        return False
    doc_type = _norm_name(inv.get("document_type_name")).replace("ё", "е")
    invoice_tokens = (
        "ҳисоб-фактура",
        "хисоб-фактура",
        "счет-фактура",
        "счёт-фактура",
        "invoice",
    )
    return any(token in doc_type for token in invoice_tokens)


def _add_party(parties, name, tin=None, account=None, flow=None):
    item = {
        "name": " ".join(str(name or "").split()).strip()[:255] or "Номсиз ҳамкор",
        "tin": _norm_tin(tin) or None,
        "account": _norm_account(account) or None,
        "flows": {flow} if flow in ("incoming", "outgoing") else set(),
    }
    for existing in parties:
        if _same_party(existing, item):
            if not existing.get("tin") and item.get("tin"):
                existing["tin"] = item["tin"]
            if not existing.get("account") and item.get("account"):
                existing["account"] = item["account"]
            if len(item.get("name") or "") > len(existing.get("name") or ""):
                existing["name"] = item["name"]
            existing["flows"].update(item["flows"])
            return
    parties.append(item)


def _source_documents():
    with Session(engine) as session:
        return session.scalars(
            select(Document)
            .where(Document.document_type.in_(["bank_statement", "invoice_registry"]))
            .order_by(Document.id)
        ).all()


def partner_groups():
    parties = []
    seen_bank = set()
    seen_invoice = set()
    for doc in _source_documents():
        try:
            data = json.loads(doc.raw_json or "{}")
        except Exception:
            continue
        if data.get("document_type") == "bank_statement":
            for tx in data.get("transactions") or data.get("transactions_preview") or []:
                incoming = round(_num(tx.get("incoming")), 2)
                outgoing = round(_num(tx.get("outgoing")), 2)
                if incoming <= 0 and outgoing <= 0:
                    continue
                sig = (
                    str(tx.get("date") or ""),
                    _norm_tin(tx.get("counterparty_tin")),
                    _norm_account(tx.get("counterparty_account")),
                    str(tx.get("document_number") or ""),
                    str(tx.get("purpose") or "").strip(),
                    incoming,
                    outgoing,
                )
                if sig in seen_bank:
                    continue
                seen_bank.add(sig)
                _add_party(
                    parties,
                    tx.get("counterparty"),
                    tx.get("counterparty_tin"),
                    tx.get("counterparty_account"),
                    "outgoing" if incoming > 0 else "incoming",
                )
        elif data.get("document_type") == "invoice_registry":
            for inv in data.get("invoices") or []:
                if not _is_real_invoice(inv):
                    continue
                sig = (
                    str(inv.get("direction") or ""),
                    str(inv.get("document_date") or ""),
                    str(inv.get("document_number") or ""),
                    _norm_tin(inv.get("counterparty_tin")),
                    _norm_account(inv.get("counterparty_account")),
                    round(_num(inv.get("total")), 2),
                )
                if sig in seen_invoice:
                    continue
                seen_invoice.add(sig)
                _add_party(
                    parties,
                    inv.get("counterparty"),
                    inv.get("counterparty_tin"),
                    inv.get("counterparty_account"),
                    inv.get("direction"),
                )
    outgoing = sorted([p for p in parties if "outgoing" in p["flows"]], key=lambda p: (p.get("name") or "").casefold())
    incoming = sorted([p for p in parties if "incoming" in p["flows"]], key=lambda p: (p.get("name") or "").casefold())
    return outgoing, incoming


def resolve_partner(identifier):
    raw = " ".join(str(identifier or "").split()).strip()
    if not raw:
        return None
    outgoing, incoming = partner_groups()
    parties = []
    for party in outgoing + incoming:
        if not any(_same_party(party, old) for old in parties):
            parties.append(party)
    digits = re.sub(r"\D", "", raw)
    account_text = _norm_account(raw)
    tin_matches = [p for p in parties if _norm_tin(p.get("tin")) and _norm_tin(p.get("tin")) in digits]
    if len(tin_matches) == 1:
        return tin_matches[0]
    acc_matches = [p for p in parties if _norm_account(p.get("account")) and _norm_account(p.get("account")) in account_text]
    if len(acc_matches) == 1:
        return acc_matches[0]
    exact_name = [p for p in parties if _norm_name(p.get("name")) == _norm_name(raw)]
    if len(exact_name) == 1:
        return exact_name[0]
    partial = [p for p in parties if _norm_name(raw) and _norm_name(raw) in _norm_name(p.get("name"))]
    if len(partial) == 1:
        return partial[0]
    return None


def collect_reconciliation(identifier, flow=None):
    flow = flow if flow in ("incoming", "outgoing") else None
    target = resolve_partner(identifier)
    if not target:
        return None
    bank_rows, invoice_rows, entries = [], [], []
    seen_bank, seen_invoice = set(), set()
    own_company = {}
    for doc in _source_documents():
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
                if not _candidate_matches(tx.get("counterparty"), tx.get("counterparty_tin"), tx.get("counterparty_account"), target):
                    continue
                incoming = round(_num(tx.get("incoming")), 2)
                outgoing = round(_num(tx.get("outgoing")), 2)
                if flow == "outgoing" and incoming <= 0:
                    continue
                if flow == "incoming" and outgoing <= 0:
                    continue
                if incoming <= 0 and outgoing <= 0:
                    continue
                sig = (
                    str(tx.get("date") or ""),
                    _norm_tin(tx.get("counterparty_tin")),
                    _norm_account(tx.get("counterparty_account")),
                    str(tx.get("document_number") or ""),
                    str(tx.get("purpose") or "").strip(),
                    incoming,
                    outgoing,
                )
                if sig in seen_bank:
                    continue
                seen_bank.add(sig)
                row = {
                    "date": sig[0],
                    "incoming": incoming,
                    "outgoing": outgoing,
                    "purpose": sig[4],
                    "document_number": sig[3] or None,
                }
                bank_rows.append(row)
                bank_label = "Банк тўлови"
                if row.get("document_number"):
                    bank_label += f" №{row['document_number']}"
                entries.append({
                    "date": row["date"],
                    "kind": "bank",
                    "document": bank_label,
                    "basis": row.get("purpose") or "Банк операцияси",
                    "debit": outgoing,
                    "credit": incoming,
                })
        elif data.get("document_type") == "invoice_registry":
            for inv in data.get("invoices") or []:
                if not _is_real_invoice(inv):
                    continue
                direction = str(inv.get("direction") or "")
                if flow and direction != flow:
                    continue
                if not _candidate_matches(inv.get("counterparty"), inv.get("counterparty_tin"), inv.get("counterparty_account"), target):
                    continue
                amount = round(_num(inv.get("total")), 2)
                sig = (
                    direction,
                    str(inv.get("document_date") or ""),
                    str(inv.get("document_number") or ""),
                    _norm_tin(inv.get("counterparty_tin")),
                    _norm_account(inv.get("counterparty_account")),
                    amount,
                )
                if sig in seen_invoice:
                    continue
                seen_invoice.add(sig)
                row = {
                    "number": sig[2],
                    "date": sig[1],
                    "direction": direction,
                    "amount": amount,
                    "contract": inv.get("contract"),
                    "document_type_name": inv.get("document_type_name"),
                }
                invoice_rows.append(row)
                entries.append({
                    "date": row["date"],
                    "kind": "invoice",
                    "document": f"Фактура №{row.get('number') or '—'}",
                    "basis": row.get("contract") or row.get("document_type_name") or "Имзоланган ҳисоб-фактура",
                    "debit": amount if direction == "outgoing" else 0.0,
                    "credit": amount if direction == "incoming" else 0.0,
                })
    entries.sort(key=lambda x: (str(x.get("date") or ""), 0 if x.get("kind") == "invoice" else 1, str(x.get("document") or "")))
    payments_from_partner = round(sum(x["incoming"] for x in bank_rows), 2)
    payments_to_partner = round(sum(x["outgoing"] for x in bank_rows), 2)
    sales = round(sum(x["amount"] for x in invoice_rows if x["direction"] == "outgoing"), 2)
    purchases = round(sum(x["amount"] for x in invoice_rows if x["direction"] == "incoming"), 2)
    debit_total = round(sum(_num(x.get("debit")) for x in entries), 2)
    credit_total = round(sum(_num(x.get("credit")) for x in entries), 2)
    net = round(debit_total - credit_total, 2)
    dates = [str(x.get("date")) for x in entries if x.get("date")]
    period_from = min(dates) if dates else None
    period_to = max(dates) if dates else None
    return {
        "document_type": "reconciliation_report",
        "flow": flow,
        "flow_label": "Чиқим / сотув" if flow == "outgoing" else "Кирим / харид" if flow == "incoming" else "Умумий",
        "partner": {"name": target.get("name"), "tin": target.get("tin"), "account_number": target.get("account")},
        "own_company": own_company,
        "document_date": period_to,
        "currency": "UZS",
        "statement_period": {"from": period_from, "to": period_to},
        "opening_debit": 0.0,
        "opening_credit": 0.0,
        "debit_total": debit_total,
        "credit_total": credit_total,
        "sales": sales,
        "payments_from_partner": payments_from_partner,
        "receivable": round(sales - payments_from_partner, 2),
        "purchases": purchases,
        "payments_to_partner": payments_to_partner,
        "payable": round(purchases - payments_to_partner, 2),
        "net": net,
        "total": net,
        "bank_operations_count": len(bank_rows),
        "invoice_count": len(invoice_rows),
        "bank_rows": bank_rows,
        "invoice_rows": invoice_rows,
        "entries": entries,
    }


def install_stable_reconciliation(reconciliation_module, enhancements_module):
    enhancements_module.reconciliation_partner_groups = partner_groups
    enhancements_module.resolve_reconciliation_party = resolve_partner
    enhancements_module.party_matches_target = _candidate_matches
    reconciliation_module._collect_reconciliation = collect_reconciliation
