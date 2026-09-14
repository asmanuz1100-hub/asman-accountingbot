import re
from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Any

import excel_docs


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return " ".join(str(value).replace("\xa0", " ").split()).strip()


def _norm(value: Any) -> str:
    return _text(value).lower().replace("ё", "е").replace("ʻ", "'").replace("’", "'").strip()


def _num(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    s = _text(value).replace(" ", "")
    if not s:
        return 0.0
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    s = re.sub(r"[^0-9.\-]", "", s)
    try:
        return float(s)
    except ValueError:
        return 0.0


def _date(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    s = _text(value)
    m = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.search(r"(\d{1,2})[-./](\d{1,2})[-./](\d{4})", s)
        if not m:
            return None
        d, mo, y = map(int, m.groups())
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def _find_header(rows: list[list[str]]):
    for i, row in enumerate(rows[:50]):
        headers = [_norm(x) for x in row]
        joined = " | ".join(headers)
        score = 0
        for token in ("кир/чиқ", "контрагент номи", "контрагент стир", "хужжат рақами", "ққс билан сумма"):
            if token in joined:
                score += 1
        if score >= 4:
            return i, headers
    return None


def _find_col(headers: list[str], *terms: str):
    for idx, header in enumerate(headers):
        if any(term in header for term in terms):
            return idx
    return None


def _cell(row: list[str], idx: int | None):
    if idx is None or idx >= len(row):
        return ""
    return row[idx]


def _status_group(value: str) -> str:
    s = _norm(value)
    if any(x in s for x in ("имзоланган", "подписан", "signed")):
        return "signed"
    if any(x in s for x in ("кутил", "ожида", "pending")):
        return "pending"
    if any(x in s for x in ("ўчирилган", "учирилган", "удален", "deleted")):
        return "deleted"
    if any(x in s for x in ("хақиқий эмас", "ҳақиқий эмас", "недейств", "invalid")):
        return "invalid"
    return "other"


def _direction(value: str) -> str:
    s = _norm(value)
    if any(x in s for x in ("чиқув", "исход", "outgoing")):
        return "outgoing"
    if any(x in s for x in ("кирув", "вход", "incoming")):
        return "incoming"
    return "unknown"


def try_analyze_invoice_registry(data: bytes, filename: str = "registry.xlsx") -> dict | None:
    try:
        rows = excel_docs._spreadsheet_to_rows(data, filename)
    except Exception:
        return None

    header_info = _find_header(rows)
    if not header_info:
        return None
    header_index, headers = header_info

    c_no = _find_col(headers, "№")
    c_dir = _find_col(headers, "кир/чиқ", "кир/чик")
    c_status = _find_col(headers, "ҳолати", "холати", "статус")
    c_type = _find_col(headers, "хужжат (тури)", "ҳужжат (тури)", "хужжат тури")
    c_contract = _find_col(headers, "шартнома")
    c_partner = _find_col(headers, "контрагент номи")
    c_tin = _find_col(headers, "контрагент стир")
    c_doc_no = _find_col(headers, "хужжат рақами", "ҳужжат рақами")
    c_doc_date = _find_col(headers, "хужжат санаси", "ҳужжат санаси")
    c_net = _find_col(headers, "ққссиз сумма", "ккссиз сумма")
    c_vat = _find_col(headers, "ққс суммаси", "ккс суммаси")
    c_gross = _find_col(headers, "ққс билан сумма", "ккс билан сумма")

    if c_partner is None or c_gross is None or c_doc_no is None:
        return None

    invoices = []
    for row in rows[header_index + 1:]:
        first = _text(_cell(row, c_no))
        if not first or _norm(first) in {"жами", "итого", "total"}:
            continue
        if not re.fullmatch(r"\d+", first):
            continue

        partner = _text(_cell(row, c_partner))
        doc_no = _text(_cell(row, c_doc_no))
        if not partner and not doc_no:
            continue

        status = _text(_cell(row, c_status))
        direction_raw = _text(_cell(row, c_dir))
        invoices.append({
            "row_no": int(first),
            "direction": _direction(direction_raw),
            "direction_raw": direction_raw,
            "status": status,
            "status_group": _status_group(status),
            "document_type_name": _text(_cell(row, c_type)),
            "contract": _text(_cell(row, c_contract)) or None,
            "counterparty": partner or None,
            "counterparty_tin": _text(_cell(row, c_tin)) or None,
            "document_number": doc_no or None,
            "document_date": _date(_cell(row, c_doc_date)) or _text(_cell(row, c_doc_date)) or None,
            "amount_without_vat": round(_num(_cell(row, c_net)), 2),
            "vat": round(_num(_cell(row, c_vat)), 2),
            "total": round(_num(_cell(row, c_gross)), 2),
        })

    if not invoices:
        return None

    status_counts = Counter(x["status_group"] for x in invoices)
    direction_counts = Counter(x["direction"] for x in invoices)

    signed = [x for x in invoices if x["status_group"] == "signed"]
    pending = [x for x in invoices if x["status_group"] == "pending"]
    deleted = [x for x in invoices if x["status_group"] == "deleted"]
    invalid = [x for x in invoices if x["status_group"] == "invalid"]

    signed_outgoing = round(sum(x["total"] for x in signed if x["direction"] == "outgoing"), 2)
    signed_incoming = round(sum(x["total"] for x in signed if x["direction"] == "incoming"), 2)
    all_total = round(sum(x["total"] for x in invoices), 2)
    pending_total = round(sum(x["total"] for x in pending), 2)

    partner_totals = defaultdict(lambda: {"count": 0, "signed_count": 0, "signed_outgoing": 0.0, "signed_incoming": 0.0, "tin": None})
    for inv in invoices:
        name = (inv.get("counterparty") or "").strip()
        if not name:
            continue
        item = partner_totals[name]
        item["count"] += 1
        if inv.get("counterparty_tin") and not item["tin"]:
            item["tin"] = inv["counterparty_tin"]
        if inv["status_group"] == "signed":
            item["signed_count"] += 1
            if inv["direction"] == "outgoing":
                item["signed_outgoing"] += inv["total"]
            elif inv["direction"] == "incoming":
                item["signed_incoming"] += inv["total"]

    counterparties = []
    for name, vals in sorted(
        partner_totals.items(),
        key=lambda kv: kv[1]["signed_outgoing"] + kv[1]["signed_incoming"],
        reverse=True,
    ):
        counterparties.append({
            "name": name[:255],
            "tin": vals["tin"],
            "invoice_count": vals["count"],
            "signed_count": vals["signed_count"],
            "signed_outgoing": round(vals["signed_outgoing"], 2),
            "signed_incoming": round(vals["signed_incoming"], 2),
        })

    dated = [x["document_date"] for x in invoices if x.get("document_date") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(x["document_date"]))]
    period_from = min(dated) if dated else None
    period_to = max(dated) if dated else None

    warnings = []
    if pending:
        warnings.append(f"{len(pending)} та ҳужжат ҳамкор имзосини кутяпти ({pending_total:,.2f} UZS).")
    if deleted:
        warnings.append(f"{len(deleted)} та ўчирилган ҳужжат бор; улар акт/қарз ҳисобида қатнашмайди.")
    if invalid:
        warnings.append(f"{len(invalid)} та ҳақиқий эмас ҳужжат бор; улар акт/қарз ҳисобида қатнашмайди.")

    return {
        "document_type": "invoice_registry",
        "confidence": 0.99,
        "currency": "UZS",
        "statement_period": {"from": period_from, "to": period_to},
        "invoice_count": len(invoices),
        "signed_count": len(signed),
        "pending_count": len(pending),
        "deleted_count": len(deleted),
        "invalid_count": len(invalid),
        "outgoing_count": direction_counts.get("outgoing", 0),
        "incoming_count": direction_counts.get("incoming", 0),
        "signed_sales_total": signed_outgoing,
        "signed_purchases_total": signed_incoming,
        "pending_total": pending_total,
        "registry_total": all_total,
        "total": signed_outgoing + signed_incoming,
        "partner_count": len(counterparties),
        "counterparties": counterparties,
        "invoices": invoices,
        "warnings": warnings,
        "summary": (
            f"Реестрдан {len(invoices)} та ҳужжат ва {len(counterparties)} та контрагент ўқилди. "
            f"Имзоланган чиқувчи фактуралар {signed_outgoing:,.2f} UZS, "
            f"имзоланган кирувчи фактуралар {signed_incoming:,.2f} UZS."
        ),
        "source_filename": filename,
        "source_format": "invoice_registry_xlsx_v1",
    }
