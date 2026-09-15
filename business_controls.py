import json
import re
from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session
from telegram import ReplyKeyboardMarkup

import excel_docs
from database import Contract, Document, Partner, engine, list_partners


PAGE_SIZE = 12
_ACTIVE_GRAPH = None


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


def _money(value):
    try:
        return f"{float(value or 0):,.2f}".replace(",", " ")
    except Exception:
        return "0.00"


def _is_invoice_type(value):
    text = _norm_name(value).replace("ё", "е")
    return any(token in text for token in (
        "ҳисоб-фактура", "хисоб-фактура", "счет-фактура", "счёт-фактура", "invoice"
    ))


def _doc_kind(value):
    text = _norm_name(value).replace("ё", "е")
    if _is_invoice_type(text):
        return "invoice"
    if any(token in text for token in (
        "ттю", "ттн", "товарно-транспорт", "товар транспорт", "юк хати", "накладн"
    )):
        return "ttn"
    if any(token in text for token in ("ишончнома", "доверенн", "power of attorney")):
        return "poa"
    if any(token in text for token in ("шартнома", "договор", "contract")):
        return "contract"
    return "other"


def _source_documents():
    with Session(engine) as session:
        return list(session.scalars(
            select(Document)
            .where(Document.document_type.in_(["bank_statement", "invoice_registry"]))
            .order_by(Document.id)
        ).all())


def _read_json(doc):
    try:
        return json.loads(doc.raw_json or "{}")
    except Exception:
        return {}


class _IdentityGraph:
    def __init__(self):
        self.parent = {}
        self.names = defaultdict(Counter)

    def _add(self, key):
        if key and key not in self.parent:
            self.parent[key] = key

    def find(self, key):
        if not key or key not in self.parent:
            return None
        root = key
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[key] != key:
            nxt = self.parent[key]
            self.parent[key] = root
            key = nxt
        return root

    def union(self, a, b):
        if not a or not b:
            return
        self._add(a)
        self._add(b)
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def add_party(self, name=None, tin=None, account=None):
        tin_n = _norm_tin(tin)
        acc_n = _norm_account(account)
        name_n = _norm_name(name)
        tkey = f"tin:{tin_n}" if tin_n else None
        akey = f"acc:{acc_n}" if acc_n else None
        if tkey:
            self._add(tkey)
        if akey:
            self._add(akey)
        if tkey and akey:
            self.union(tkey, akey)
        if not tkey and not akey and name_n:
            nkey = f"name:{name_n}"
            self._add(nkey)
        root = self.component(name, tin, account)
        if root and name_n:
            self.names[root][" ".join(str(name or "").split()).strip()] += 1

    def component(self, name=None, tin=None, account=None):
        tin_n = _norm_tin(tin)
        acc_n = _norm_account(account)
        if tin_n and f"tin:{tin_n}" in self.parent:
            return self.find(f"tin:{tin_n}")
        if acc_n and f"acc:{acc_n}" in self.parent:
            return self.find(f"acc:{acc_n}")
        name_n = _norm_name(name)
        if not tin_n and not acc_n and name_n and f"name:{name_n}" in self.parent:
            return self.find(f"name:{name_n}")
        return None

    def all_tokens(self, root):
        if not root:
            return set()
        return {key for key in self.parent if self.find(key) == root}


def _identity_graph(docs=None):
    docs = docs if docs is not None else _source_documents()
    graph = _IdentityGraph()
    rows = []
    for doc in docs:
        data = _read_json(doc)
        if data.get("document_type") == "bank_statement":
            for tx in data.get("transactions") or data.get("transactions_preview") or []:
                rows.append((tx.get("counterparty"), tx.get("counterparty_tin"), tx.get("counterparty_account")))
        elif data.get("document_type") == "invoice_registry":
            for inv in data.get("invoices") or []:
                rows.append((inv.get("counterparty"), inv.get("counterparty_tin"), inv.get("counterparty_account")))

    for name, tin, account in rows:
        graph.add_party(name, tin, account)
    graph.names.clear()
    for name, tin, account in rows:
        root = graph.component(name, tin, account)
        clean = " ".join(str(name or "").split()).strip()
        if root and clean:
            graph.names[root][clean] += 1
    return graph


def enrich_registry_accounts(raw: bytes, filename: str, result: dict) -> dict:
    if not isinstance(result, dict) or result.get("document_type") != "invoice_registry":
        return result
    try:
        rows = excel_docs._spreadsheet_to_rows(raw, filename)
    except Exception:
        return result

    header_index = None
    headers = None
    for idx, row in enumerate(rows[:60]):
        h = [_norm_name(x).replace("ё", "е") for x in row]
        joined = " | ".join(h)
        if "контрагент" in joined and any(x in joined for x in ("стир", "инн")):
            header_index, headers = idx, h
            break
    if headers is None:
        return result

    def col(*terms):
        for i, h in enumerate(headers):
            if any(term in h for term in terms):
                return i
        return None

    c_no = col("№", "номер")
    c_acc = col(
        "контрагент хисоб", "контрагент ҳисоб", "контрагент счет", "контрагент счёт",
        "хисоб раками", "ҳисоб рақами", "расчетный счет", "расчётный счёт"
    )
    if c_acc is None:
        return result

    row_accounts = {}
    for row in rows[header_index + 1:]:
        if c_no is None or c_no >= len(row) or c_acc >= len(row):
            continue
        raw_no = str(row[c_no] or "").strip()
        if not re.fullmatch(r"\d+", raw_no):
            continue
        account = _norm_account(row[c_acc])
        if account:
            row_accounts[int(raw_no)] = account

    if not row_accounts:
        return result

    for inv in result.get("invoices") or []:
        account = row_accounts.get(inv.get("row_no"))
        if account and not inv.get("counterparty_account"):
            inv["counterparty_account"] = account

    by_tin = defaultdict(set)
    for inv in result.get("invoices") or []:
        tin = _norm_tin(inv.get("counterparty_tin"))
        account = _norm_account(inv.get("counterparty_account"))
        if tin and account:
            by_tin[tin].add(account)
    for item in result.get("counterparties") or []:
        tin = _norm_tin(item.get("tin"))
        accounts = by_tin.get(tin) or set()
        if len(accounts) == 1:
            item["account"] = next(iter(accounts))
    return result


def partner_groups():
    docs = _source_documents()
    graph = _identity_graph(docs)
    groups = {}

    def add(name, tin, account, flow):
        graph.add_party(name, tin, account)
        root = graph.component(name, tin, account)
        if not root:
            return
        item = groups.setdefault(root, {"name": None, "tin": None, "account": None, "accounts": set(), "flows": set()})
        clean_name = " ".join(str(name or "").split()).strip()
        if clean_name and (not item["name"] or len(clean_name) > len(item["name"])):
            item["name"] = clean_name
        tin_n = _norm_tin(tin)
        acc_n = _norm_account(account)
        if tin_n and not item["tin"]:
            item["tin"] = tin_n
        if acc_n:
            item["accounts"].add(acc_n)
            if not item["account"]:
                item["account"] = acc_n
        if flow in ("incoming", "outgoing"):
            item["flows"].add(flow)

    seen_bank, seen_invoice = set(), set()
    for doc in docs:
        data = _read_json(doc)
        if data.get("document_type") == "bank_statement":
            for tx in data.get("transactions") or data.get("transactions_preview") or []:
                incoming = round(_num(tx.get("incoming")), 2)
                outgoing = round(_num(tx.get("outgoing")), 2)
                if incoming <= 0 and outgoing <= 0:
                    continue
                sig = (
                    str(tx.get("date") or ""), _norm_tin(tx.get("counterparty_tin")),
                    _norm_account(tx.get("counterparty_account")), str(tx.get("document_number") or ""),
                    round(incoming, 2), round(outgoing, 2), str(tx.get("purpose") or "").strip(),
                )
                if sig in seen_bank:
                    continue
                seen_bank.add(sig)
                add(
                    tx.get("counterparty"), tx.get("counterparty_tin"), tx.get("counterparty_account"),
                    "outgoing" if incoming > 0 else "incoming",
                )
        elif data.get("document_type") == "invoice_registry":
            for inv in data.get("invoices") or []:
                if str(inv.get("status_group") or "").lower() != "signed":
                    continue
                direction = str(inv.get("direction") or "")
                if direction not in ("incoming", "outgoing") or not _is_invoice_type(inv.get("document_type_name")):
                    continue
                amount = round(_num(inv.get("total")), 2)
                if amount <= 0:
                    continue
                sig = (
                    direction, str(inv.get("document_date") or ""), str(inv.get("document_number") or ""),
                    _norm_tin(inv.get("counterparty_tin")), _norm_account(inv.get("counterparty_account")), amount,
                )
                if sig in seen_invoice:
                    continue
                seen_invoice.add(sig)
                add(inv.get("counterparty"), inv.get("counterparty_tin"), inv.get("counterparty_account"), direction)

    items = []
    for item in groups.values():
        item["accounts"] = sorted(item["accounts"])
        if not item["name"]:
            item["name"] = "Номсиз ҳамкор"
        items.append(item)
    outgoing = sorted([x for x in items if "outgoing" in x["flows"]], key=lambda x: x["name"].casefold())
    incoming = sorted([x for x in items if "incoming" in x["flows"]], key=lambda x: x["name"].casefold())
    return outgoing, incoming


def resolve_partner(identifier):
    raw = " ".join(str(identifier or "").split()).strip()
    if not raw:
        return None
    outgoing, incoming = partner_groups()
    parties = []
    seen = set()
    for p in outgoing + incoming:
        key = _norm_tin(p.get("tin")) or _norm_account(p.get("account")) or _norm_name(p.get("name"))
        if key in seen:
            continue
        seen.add(key)
        parties.append(p)

    digits = _norm_tin(raw)
    acc_text = _norm_account(raw)
    matches = []
    for p in parties:
        tin = _norm_tin(p.get("tin"))
        accounts = set(p.get("accounts") or []) | ({_norm_account(p.get("account"))} if p.get("account") else set())
        if tin and digits and (digits == tin or tin in digits):
            matches.append(p)
            continue
        if acc_text and any(acc and (acc == acc_text or acc in acc_text) for acc in accounts):
            matches.append(p)
    if len(matches) == 1:
        return matches[0]

    exact = [p for p in parties if _norm_name(p.get("name")) == _norm_name(raw)]
    if len(exact) == 1:
        return exact[0]
    partial = [p for p in parties if _norm_name(raw) and _norm_name(raw) in _norm_name(p.get("name"))]
    return partial[0] if len(partial) == 1 else None


def candidate_matches(name, tin, account, target):
    global _ACTIVE_GRAPH
    graph = _ACTIVE_GRAPH or _identity_graph()
    target_root = graph.component(target.get("name"), target.get("tin"), target.get("account"))
    cand_root = graph.component(name, tin, account)
    if target_root and cand_root:
        return target_root == cand_root

    cand_tin, target_tin = _norm_tin(tin), _norm_tin(target.get("tin"))
    if cand_tin and target_tin:
        return cand_tin == target_tin
    cand_acc = _norm_account(account)
    target_accounts = set(target.get("accounts") or []) | ({_norm_account(target.get("account"))} if target.get("account") else set())
    if cand_acc and target_accounts:
        return cand_acc in target_accounts
    if cand_tin or target_tin or cand_acc or target_accounts:
        return False
    return bool(_norm_name(name)) and _norm_name(name) == _norm_name(target.get("name"))


def _partner_from_model(model):
    return {
        "name": getattr(model, "name", None),
        "tin": getattr(model, "tin", None),
        "account": getattr(model, "account_number", None),
    }


def _contract_rows_for_target(target):
    rows = []
    with Session(engine) as session:
        for contract, partner in session.execute(
            select(Contract, Partner).join(Partner, Contract.partner_id == Partner.id).order_by(Contract.id)
        ).all():
            p = _partner_from_model(partner)
            if candidate_matches(p["name"], p["tin"], p["account"], target):
                rows.append((contract, partner))
    return rows


def _registry_items_for_target(target):
    result = []
    for doc in _source_documents():
        data = _read_json(doc)
        if data.get("document_type") != "invoice_registry":
            continue
        for item in data.get("invoices") or []:
            if candidate_matches(
                item.get("counterparty"), item.get("counterparty_tin"), item.get("counterparty_account"), target
            ):
                result.append(item)
    return result


def sync_contract_usage():
    global _ACTIVE_GRAPH
    previous_graph = _ACTIVE_GRAPH
    if _ACTIVE_GRAPH is None:
        _ACTIVE_GRAPH = _identity_graph()
    changed = 0
    try:
        with Session(engine) as session:
            contracts = list(session.execute(
                select(Contract, Partner).join(Partner, Contract.partner_id == Partner.id)
            ).all())
            docs = _source_documents()
            invoice_items = []
            for doc in docs:
                data = _read_json(doc)
                if data.get("document_type") == "invoice_registry":
                    invoice_items.extend(data.get("invoices") or [])

            for contract, partner in contracts:
                target = _partner_from_model(partner)
                number = _norm_name(contract.number)
                used = 0.0
                seen = set()
                for inv in invoice_items:
                    if str(inv.get("status_group") or "").lower() != "signed":
                        continue
                    if not _is_invoice_type(inv.get("document_type_name")):
                        continue
                    if _norm_name(inv.get("contract")) != number:
                        continue
                    if not candidate_matches(
                        inv.get("counterparty"), inv.get("counterparty_tin"), inv.get("counterparty_account"), target
                    ):
                        continue
                    sig = (
                        str(inv.get("direction") or ""), str(inv.get("document_number") or ""),
                        str(inv.get("document_date") or ""), round(_num(inv.get("total")), 2),
                    )
                    if sig in seen:
                        continue
                    seen.add(sig)
                    used += _num(inv.get("total"))
                used = round(used, 2)
                if abs(float(contract.used_amount or 0) - used) > 0.01:
                    contract.used_amount = used
                    changed += 1
            if changed:
                session.commit()
        return changed
    finally:
        _ACTIVE_GRAPH = previous_graph


def _enrich_reconciliation(data):
    if not isinstance(data, dict):
        return data
    partner = data.get("partner") or {}
    target = {
        "name": partner.get("name"),
        "tin": partner.get("tin"),
        "account": partner.get("account_number") or partner.get("account"),
    }
    resolved = resolve_partner(target.get("tin") or target.get("account") or target.get("name"))
    if resolved:
        target.update(resolved)
        partner["tin"] = partner.get("tin") or resolved.get("tin")
        partner["account_number"] = partner.get("account_number") or resolved.get("account")
        data["partner"] = partner

    sync_contract_usage()
    contract_models = _contract_rows_for_target(target)
    contract_by_no = {_norm_name(c.number): c for c, _ in contract_models}
    invoice_rows = data.get("invoice_rows") or []

    contract_usage = defaultdict(float)
    for inv in invoice_rows:
        number = _norm_name(inv.get("contract"))
        if number:
            contract_usage[number] += _num(inv.get("amount"))

    contract_summary = []
    all_contract_numbers = set(contract_by_no) | set(contract_usage)
    for key in sorted(all_contract_numbers):
        model = contract_by_no.get(key)
        display_no = model.number if model else next(
            (str(x.get("contract")) for x in invoice_rows if _norm_name(x.get("contract")) == key), key
        )
        total = float(model.total_amount or 0) if model else 0.0
        used = float(model.used_amount or 0) if model else round(contract_usage[key], 2)
        remaining = round(total - used, 2) if total > 0 else None
        contract_summary.append({
            "number": display_no,
            "registered": bool(model),
            "total": round(total, 2),
            "used": round(used, 2),
            "remaining": remaining,
            "over_limit": bool(total > 0 and used - total > 0.01),
        })

    registry_items = _registry_items_for_target(target)
    chain = []
    for inv in invoice_rows:
        contract_key = _norm_name(inv.get("contract"))
        scoped = []
        for item in registry_items:
            if contract_key and _norm_name(item.get("contract")) != contract_key:
                continue
            scoped.append(item)
        kinds = {_doc_kind(item.get("document_type_name")) for item in scoped}
        contract_model = contract_by_no.get(contract_key) if contract_key else None
        chain.append({
            "invoice_number": inv.get("number"),
            "date": inv.get("date"),
            "contract": inv.get("contract"),
            "contract_ok": bool(inv.get("contract")),
            "contract_registered": bool(contract_model),
            "poa_ok": "poa" in kinds,
            "ttn_ok": "ttn" in kinds,
            "bank_payment_found": bool(data.get("bank_rows")),
            "expected_poa": "Харидор ишончномаси" if data.get("flow") == "outgoing" else "Бизнинг ишончнома",
        })

    data["contract_summary"] = contract_summary
    data["document_chain"] = chain
    data["document_control"] = {
        "invoice_count": len(chain),
        "missing_contract": sum(1 for x in chain if not x["contract_ok"]),
        "missing_poa": sum(1 for x in chain if not x["poa_ok"]),
        "missing_ttn": sum(1 for x in chain if not x["ttn_ok"]),
        "over_limit_contracts": sum(1 for x in contract_summary if x["over_limit"]),
    }
    return data


def _summary_with_controls(original_summary, data, doc_id, created):
    text = original_summary(data, doc_id, created)
    controls = data.get("document_control") or {}
    contracts = data.get("contract_summary") or []
    chain = data.get("document_chain") or []

    extra = ["", "📑 ҲУЖЖАТЛАР НАЗОРАТИ"]
    if contracts:
        for item in contracts[:8]:
            if item.get("registered"):
                remaining = item.get("remaining")
                status = "🔴 Лимитдан ошган" if item.get("over_limit") else "✅"
                extra.append(
                    f"• Шартнома №{item.get('number')}: {status} | "
                    f"сумма {_money(item.get('total'))} | ишлатилган {_money(item.get('used'))} | "
                    f"қолдиқ {_money(remaining)}"
                )
            else:
                extra.append(f"• Шартнома №{item.get('number')}: ⚠️ базага шартнома карточкаси киритилмаган")
    else:
        extra.append("• Шартнома маълумоти топилмади.")

    if chain:
        extra += [
            f"• Фактуралар: {len(chain)} та",
            f"• Шартномасиз: {controls.get('missing_contract') or 0} та",
            f"• Ишончнома топилмаган: {controls.get('missing_poa') or 0} та",
            f"• ТТЮ/ТТН топилмаган: {controls.get('missing_ttn') or 0} та",
        ]
    return text + "\n" + "\n".join(extra)


def _heuristic_expense(purpose, counterparty=""):
    text = f"{purpose or ''} {counterparty or ''}".casefold().replace("ё", "е")
    tax_words = (
        "солиқ", "soliq", "налог", "бюджет", "казнач", "газнач", "ндс", "qqs", "ққс",
        "ижтимоий солиқ", "social tax", "пеня", "пенсион", "инпс", "my.soliq",
    )
    utility_words = (
        "электр", "энерг", "electric", "hududiy elektr", "газ", "hududgaz", "сув", "водоканал",
        "сув таъминоти", "suv taminoti", "коммун", "тепло", "иссиқлик", "мусор", "чиқинди",
        "интернет", "телеком", "uzbektelekom", "алоқа", "aloqa", "связь",
    )
    raw_words = (
        "хом ашё", "хомашё", "сырье", "материал", "песок", "қум", "кум", "акрил", "цемент",
        "пигмент", "упаков", "мешок", "ведро", "тара", "қадоқ", "кадок", "kimyo", "киме",
        "chemical", "plastic", "пласт",
    )
    if any(word in text for word in tax_words):
        return "tax"
    if any(word in text for word in utility_words):
        return "utilities"
    if any(word in text for word in raw_words):
        return "raw_material"
    return "other"


def _purchase_category_index():
    docs = _source_documents()
    graph = _identity_graph(docs)
    votes = defaultdict(Counter)
    for doc in docs:
        data = _read_json(doc)
        if data.get("document_type") != "invoice_registry":
            continue
        for inv in data.get("invoices") or []:
            if str(inv.get("status_group") or "").lower() != "signed" or str(inv.get("direction") or "") != "incoming":
                continue
            if not _is_invoice_type(inv.get("document_type_name")):
                continue
            root = graph.component(inv.get("counterparty"), inv.get("counterparty_tin"), inv.get("counterparty_account"))
            if not root:
                continue
            category = inv.get("purchase_category") or _heuristic_expense(
                f"{inv.get('contract') or ''} {inv.get('document_type_name') or ''}", inv.get("counterparty")
            )
            votes[root][category] += 1
    return graph, votes


def reclassify_existing_expenses():
    graph, votes = _purchase_category_index()
    changed = 0
    with Session(engine) as session:
        docs = list(session.scalars(
            select(Document).where(Document.document_type == "expense_entry").order_by(Document.id)
        ).all())
        for doc in docs:
            data = _read_json(doc)
            if data.get("source") != "bank_statement":
                continue
            partner = data.get("partner") or {}
            root = graph.component(partner.get("name"), partner.get("tin"), partner.get("account"))
            if root and votes.get(root):
                new_category = votes[root].most_common(1)[0][0]
            else:
                new_category = _heuristic_expense(data.get("summary"), partner.get("name"))
            if new_category != data.get("expense_category"):
                data["expense_category"] = new_category
                doc.raw_json = json.dumps(data, ensure_ascii=False, default=str)
                changed += 1
        if changed:
            session.commit()
    return changed


def financial_analysis():
    sync_contract_usage()
    docs = _source_documents()
    graph = _identity_graph(docs)
    groups = defaultdict(lambda: {
        "name": "—", "sales": 0.0, "purchases": 0.0, "received": 0.0, "paid": 0.0
    })
    seen_bank, seen_inv = set(), set()
    sales = purchases = output_vat = input_vat = 0.0
    bank_in = bank_out = 0.0

    for doc in docs:
        data = _read_json(doc)
        if data.get("document_type") == "bank_statement":
            for tx in data.get("transactions") or data.get("transactions_preview") or []:
                incoming, outgoing = round(_num(tx.get("incoming")), 2), round(_num(tx.get("outgoing")), 2)
                sig = (
                    str(tx.get("date") or ""), _norm_tin(tx.get("counterparty_tin")),
                    _norm_account(tx.get("counterparty_account")), str(tx.get("document_number") or ""),
                    incoming, outgoing, str(tx.get("purpose") or "").strip(),
                )
                if sig in seen_bank:
                    continue
                seen_bank.add(sig)
                bank_in += incoming
                bank_out += outgoing
                root = graph.component(tx.get("counterparty"), tx.get("counterparty_tin"), tx.get("counterparty_account"))
                if root:
                    g = groups[root]
                    g["name"] = tx.get("counterparty") or g["name"]
                    g["received"] += incoming
                    g["paid"] += outgoing
        elif data.get("document_type") == "invoice_registry":
            for inv in data.get("invoices") or []:
                if str(inv.get("status_group") or "").lower() != "signed" or not _is_invoice_type(inv.get("document_type_name")):
                    continue
                direction = str(inv.get("direction") or "")
                if direction not in ("incoming", "outgoing"):
                    continue
                amount = round(_num(inv.get("total")), 2)
                sig = (
                    direction, str(inv.get("document_date") or ""), str(inv.get("document_number") or ""),
                    _norm_tin(inv.get("counterparty_tin")), _norm_account(inv.get("counterparty_account")), amount,
                )
                if sig in seen_inv:
                    continue
                seen_inv.add(sig)
                root = graph.component(inv.get("counterparty"), inv.get("counterparty_tin"), inv.get("counterparty_account"))
                if direction == "outgoing":
                    sales += amount
                    output_vat += _num(inv.get("vat"))
                    if root:
                        groups[root]["sales"] += amount
                else:
                    purchases += amount
                    input_vat += _num(inv.get("vat"))
                    if root:
                        groups[root]["purchases"] += amount
                if root:
                    groups[root]["name"] = inv.get("counterparty") or groups[root]["name"]

    debtors, creditors, advances = [], [], []
    unmatched_bank = unmatched_invoice = 0
    for g in groups.values():
        receivable = round(g["sales"] - g["received"], 2) if g["sales"] > 0 else 0.0
        payable = round(g["purchases"] - g["paid"], 2) if g["purchases"] > 0 else 0.0
        if receivable > 0.01:
            debtors.append((g["name"], receivable))
        elif receivable < -0.01:
            advances.append((g["name"], abs(receivable), "харидор аванси"))
        if payable > 0.01:
            creditors.append((g["name"], payable))
        elif payable < -0.01:
            advances.append((g["name"], abs(payable), "етказиб берувчига аванс"))
        if (g["received"] > 0 and g["sales"] <= 0) or (g["paid"] > 0 and g["purchases"] <= 0):
            unmatched_bank += 1
        if (g["sales"] > 0 and g["received"] <= 0) or (g["purchases"] > 0 and g["paid"] <= 0):
            unmatched_invoice += 1

    debtors.sort(key=lambda x: x[1], reverse=True)
    creditors.sort(key=lambda x: x[1], reverse=True)
    advances.sort(key=lambda x: x[1], reverse=True)

    expense_totals = defaultdict(float)
    with Session(engine) as session:
        expense_docs = list(session.scalars(
            select(Document).where(Document.document_type == "expense_entry")
        ).all())
        contract_rows = list(session.scalars(select(Contract)).all())
    seen_exp = set()
    for doc in expense_docs:
        data = _read_json(doc)
        sig = data.get("source_signature") or f"id:{doc.id}"
        if sig in seen_exp:
            continue
        seen_exp.add(sig)
        expense_totals[data.get("expense_category") or "other"] += _num(data.get("total"))

    contract_total = sum(float(c.total_amount or 0) for c in contract_rows)
    contract_used = sum(float(c.used_amount or 0) for c in contract_rows)
    contract_remaining = contract_total - contract_used

    lines = [
        "📈 МОЛИЯВИЙ ТАҲЛИЛ",
        "━━━━━━━━━━━━━━━━",
        "",
        f"🧾 Сотув фактуралари: {_money(sales)} UZS",
        f"📦 Кирим фактуралари: {_money(purchases)} UZS",
        f"🏦 Банк кирими: {_money(bank_in)} UZS",
        f"🏦 Банк чиқими: {_money(bank_out)} UZS",
        "",
        f"🔴 Дебитор қарз: {_money(sum(x[1] for x in debtors))} UZS",
        f"🔴 Кредитор қарз: {_money(sum(x[1] for x in creditors))} UZS",
        f"🟢 Аванслар: {_money(sum(x[1] for x in advances))} UZS",
        "",
        f"🧾 Чиқим ҚҚС: {_money(output_vat)} UZS",
        f"🧾 Кирим ҚҚС: {_money(input_vat)} UZS",
        f"⚖️ ҚҚС фарқи: {_money(output_vat - input_vat)} UZS",
        "",
        f"📄 Шартномалар умумий суммаси: {_money(contract_total)} UZS",
        f"📉 Фактура билан ишлатилган: {_money(contract_used)} UZS",
        f"📌 Шартнома қолдиғи: {_money(contract_remaining)} UZS",
        "",
        "💸 ХАРАЖАТЛАР:",
        f"• 🧱 Хом ашё: {_money(expense_totals['raw_material'])}",
        f"• 💡 Коммунал: {_money(expense_totals['utilities'])}",
        f"• 🧾 Солиқ: {_money(expense_totals['tax'])}",
        f"• 📦 Бошқа: {_money(expense_totals['other'])}",
        "",
        f"⚠️ Фактураси топилмаган банк гуруҳлари: {unmatched_bank} та",
        f"⚠️ Тўлови топилмаган фактура гуруҳлари: {unmatched_invoice} та",
    ]
    if debtors:
        lines += ["", "🔴 ЭНГ КАТТА ДЕБИТОРЛАР:"]
        lines += [f"• {name}: {_money(amount)}" for name, amount in debtors[:5]]
    if creditors:
        lines += ["", "🔴 ЭНГ КАТТА КРЕДИТОРЛАР:"]
        lines += [f"• {name}: {_money(amount)}" for name, amount in creditors[:5]]
    if advances:
        lines += ["", "🟢 АВАНСЛАР:"]
        lines += [f"• {name}: {_money(amount)} ({kind})" for name, amount, kind in advances[:5]]
    lines += [
        "",
        "ℹ️ Бу таҳлил банк выпискаси, имзоланган ҳисоб-фактуралар, шартномалар ва ботдаги харажатлар асосида ҳисобланди.",
    ]
    return "\n".join(lines)


def contract_overview_text():
    sync_contract_usage()
    with Session(engine) as session:
        rows = list(session.execute(
            select(Contract, Partner).join(Partner, Contract.partner_id == Partner.id).order_by(Contract.id.desc())
        ).all())
    lines = ["📄 ШАРТНОМАЛАР ВА ҚОЛДИҚ", "━━━━━━━━━━━━━━━━", ""]
    if not rows:
        lines.append("Ҳозирча шартномалар йўқ.")
    else:
        for contract, partner in rows[:20]:
            total = float(contract.total_amount or 0)
            used = float(contract.used_amount or 0)
            remaining = round(total - used, 2)
            status = "🔴 ОШГАН" if remaining < -0.01 else "🟢 ҚОЛДИҚ" if remaining > 0.01 else "⚪ ЁПИЛГАН"
            lines += [
                f"🏢 {partner.name}",
                f"   №{contract.number} | {status}",
                f"   Сумма: {_money(total)} {contract.currency}",
                f"   Фактура: {_money(used)} | Қолдиқ: {_money(remaining)}",
                "",
            ]
        if len(rows) > 20:
            lines.append(f"... яна {len(rows) - 20} та шартнома")
    lines += ["Қўшиш: шартнома: Компания | 37/2026 | 10000000 | UZS"]
    return "\n".join(lines)


def _partners_page_text(rows, page):
    total = len(rows)
    max_page = max((total - 1) // PAGE_SIZE, 0)
    page = max(0, min(page, max_page))
    start = page * PAGE_SIZE
    subset = rows[start:start + PAGE_SIZE]
    lines = [
        "👥 ҲАМКОРЛАР",
        "━━━━━━━━━━━━━━━━",
        f"Жами: {total} та | Саҳифа {page + 1}/{max_page + 1}",
        "",
    ]
    for idx, p in enumerate(subset, start + 1):
        ident = []
        if p.tin:
            ident.append(f"ИНН {p.tin}")
        if getattr(p, "account_number", None):
            ident.append(f"с/р {p.account_number}")
        lines.append(f"{idx}) {p.name}" + (" | " + " | ".join(ident) if ident else ""))
    return "\n".join(lines), page, max_page


def install_business_core(stable_module, reconciliation_module, enhancements_module, menu_module):
    stable_module.partner_groups = partner_groups
    stable_module.resolve_partner = resolve_partner
    stable_module._candidate_matches = candidate_matches
    enhancements_module.reconciliation_partner_groups = partner_groups
    enhancements_module.resolve_reconciliation_party = resolve_partner
    enhancements_module.party_matches_target = candidate_matches

    original_collect = reconciliation_module._collect_reconciliation

    def collect_with_controls(identifier, flow=None):
        global _ACTIVE_GRAPH
        previous_graph = _ACTIVE_GRAPH
        _ACTIVE_GRAPH = _identity_graph()
        try:
            data = original_collect(identifier, flow)
            return _enrich_reconciliation(data) if data else data
        finally:
            _ACTIVE_GRAPH = previous_graph

    reconciliation_module._collect_reconciliation = collect_with_controls

    original_summary = reconciliation_module._summary_text

    def summary_with_controls(data, doc_id, created):
        return _summary_with_controls(original_summary, data, doc_id, created)

    reconciliation_module._summary_text = summary_with_controls

    original_sync = menu_module._sync_bank_expenses

    def sync_bank_expenses(data):
        original_classifier = menu_module._classify_expense
        try:
            menu_module._classify_expense = _heuristic_expense
            if isinstance(data, dict) and data.get("document_type") == "bank_statement":
                graph, votes = _purchase_category_index()
                cloned = dict(data)
                txs = []
                for tx in data.get("transactions") or data.get("transactions_preview") or []:
                    item = dict(tx)
                    root = graph.component(item.get("counterparty"), item.get("counterparty_tin"), item.get("counterparty_account"))
                    category = votes[root].most_common(1)[0][0] if root and votes.get(root) else None
                    if category == "tax":
                        item["purpose"] = f"СОЛИҚ {item.get('purpose') or ''}"
                    elif category == "utilities":
                        item["purpose"] = f"КОММУНАЛ {item.get('purpose') or ''}"
                    elif category == "raw_material":
                        item["purpose"] = f"ХОМ АШЁ {item.get('purpose') or ''}"
                    txs.append(item)
                cloned["transactions"] = txs
                return original_sync(cloned)
            return original_sync(data)
        finally:
            menu_module._classify_expense = original_classifier

    menu_module._sync_bank_expenses = sync_bank_expenses
    reclassify_existing_expenses()


def install_business_ui(bot_module, reconciliation_ui_module, enhancements_module):
    bot_module.MENU = ReplyKeyboardMarkup(
        [
            ["📎 Ҳужжат юклаш", "🤖 AI таҳлил"],
            ["👥 Ҳамкорлар", "📄 Шартномалар"],
            ["📦 Хом ашё омбори", "🧮 Акт сверка"],
            ["💸 Чиқимлар", "🧾 Чиқиш фактура"],
            ["📈 Молиявий таҳлил", "📊 Ҳисоботлар"],
            ["ℹ️ Ёрдам"],
        ],
        resize_keyboard=True,
    )

    async def show_recon_partners(update, context, flow, page=0):
        outgoing, incoming = enhancements_module.reconciliation_partner_groups()
        parties = outgoing if flow == "outgoing" else incoming
        total = len(parties)
        max_page = max((total - 1) // PAGE_SIZE, 0)
        page = max(0, min(int(page or 0), max_page))
        start = page * PAGE_SIZE
        subset = parties[start:start + PAGE_SIZE]

        context.user_data["act_flow"] = flow
        context.user_data["act_page"] = page
        context.user_data["awaiting_reconciliation_partner"] = True
        context.user_data.pop("awaiting_saved_act_search", None)

        mapping, rows = {}, []
        for idx, party in enumerate(subset, start + 1):
            label = f"{idx}. 🏢 {' '.join(str(party.get('name') or 'Ҳамкор').split())[:28]}"
            mapping[label] = party.get("tin") or party.get("account") or party.get("name")
            rows.append([label])
        nav = []
        if page > 0:
            nav.append("⬅️ Акт олдинги")
        if page < max_page:
            nav.append("➡️ Акт кейинги")
        if nav:
            rows.append(nav)
        rows.append(["⬅️ Акт сверка", "🏠 Асосий меню"])
        context.user_data["act_partner_map"] = mapping

        title = "📤 ЧИҚИМ - ХАРИДОРЛАР" if flow == "outgoing" else "📥 КИРИМ - ЕТКАЗИБ БЕРУВЧИЛАР"
        lines = [
            title, "━━━━━━━━━━━━━━━━", "",
            f"👥 Жами ҳамкорлар: {total} та | Саҳифа {page + 1}/{max_page + 1}", "",
        ]
        for idx, party in enumerate(subset, start + 1):
            lines += [
                f"{idx}) 🏢 {party.get('name') or '—'}",
                f"   🆔 ИНН: {party.get('tin') or '—'}",
                f"   🏦 Ҳисоб рақами: {party.get('account') or '—'}",
            ]
        lines += ["", "🔎 Ном, ИНН ёки ҳисоб рақами билан ҳам қидириш мумкин."]
        await update.message.reply_text("\n".join(lines), reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True))

    reconciliation_ui_module._show_partners = show_recon_partners

    original_text_handler = bot_module.text_handler

    async def text_handler(update, context):
        text = (update.message.text or "").strip()

        if text == "👥 Ҳамкорлар":
            context.user_data["partners_page"] = 0
            rows = list_partners(limit=100000)
            body, page, max_page = _partners_page_text(rows, 0)
            nav = []
            if max_page > 0:
                nav.append("➡️ Ҳамкорлар кейинги")
            kb = [nav] if nav else []
            kb.append(["🏠 Асосий меню"])
            await update.message.reply_text(body, reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
            return

        if text in ("➡️ Ҳамкорлар кейинги", "⬅️ Ҳамкорлар олдинги"):
            page = int(context.user_data.get("partners_page") or 0)
            page += 1 if text.startswith("➡️") else -1
            rows = list_partners(limit=100000)
            body, page, max_page = _partners_page_text(rows, page)
            context.user_data["partners_page"] = page
            nav = []
            if page > 0:
                nav.append("⬅️ Ҳамкорлар олдинги")
            if page < max_page:
                nav.append("➡️ Ҳамкорлар кейинги")
            kb = [nav] if nav else []
            kb.append(["🏠 Асосий меню"])
            await update.message.reply_text(body, reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
            return

        if text == "📄 Шартномалар":
            await update.message.reply_text(contract_overview_text(), reply_markup=bot_module.MENU)
            return

        if text == "📈 Молиявий таҳлил":
            reclassify_existing_expenses()
            await update.message.reply_text(financial_analysis(), reply_markup=bot_module.MENU)
            return

        if text in ("➡️ Акт кейинги", "⬅️ Акт олдинги"):
            flow = context.user_data.get("act_flow") or "outgoing"
            page = int(context.user_data.get("act_page") or 0)
            page += 1 if text.startswith("➡️") else -1
            await show_recon_partners(update, context, flow, page)
            return

        await original_text_handler(update, context)

    bot_module.text_handler = text_handler
