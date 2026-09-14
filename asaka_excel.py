import re
from collections import defaultdict
from datetime import date
from html.parser import HTMLParser


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251", "windows-1251", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace")


class _Parser(HTMLParser):
    """Tolerant parser for malformed Asakabank HTML/XLS exports."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None

    def _flush_row(self):
        if self.row is not None and any(str(v).strip() for v in self.row):
            self.rows.append(self.row)
        self.row = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "tr":
            self._flush_row()
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("td", "th") and self.cell is not None:
            value = " ".join("".join(self.cell).split())
            if self.row is not None:
                self.row.append(value)
            self.cell = None
        elif tag == "tr":
            self._flush_row()
        elif tag in ("thead", "tbody", "table"):
            self._flush_row()


def _num(value):
    text = str(value or "").strip().replace("\xa0", " ")
    if not text:
        return 0.0
    cleaned = re.sub(r"[^0-9,.\-]", "", text)
    if not cleaned:
        return 0.0
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _date(value):
    m = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", str(value or ""))
    if not m:
        return None
    try:
        d, mo, y = map(int, m.groups())
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def _find_header(rows):
    for i, row in enumerate(rows[:100]):
        h = [" ".join(str(v).lower().replace("ё", "е").split()) for v in row]
        def col(*terms):
            return next((j for j, x in enumerate(h) if any(t in x for t in terms)), None)
        date_i = col("дата")
        party_i = col("счет/инн", "cчет/инн", "счёт/инн")
        debit_i = col("оборот дебет")
        credit_i = col("оборот кредит")
        purpose_i = col("назначение платежа")
        if None not in (date_i, party_i, debit_i, credit_i, purpose_i):
            return i, {
                "date": date_i,
                "party": party_i,
                "debit": debit_i,
                "credit": credit_i,
                "purpose": purpose_i,
                "doc": col("№ док", "номер док"),
                "op": col("оп"),
                "mfo": col("мфо"),
            }
    return None


def _label_amount(rows, label):
    for row in rows[:20]:
        for cell in row:
            text = str(cell)
            if label in text.lower():
                part = text.split(":", 1)[1] if ":" in text else text
                return _num(part)
    return None


def _meta(rows, header_index):
    bank_name = None
    account_number = None
    account_holder = None
    tax_id = None
    period_from = period_to = None

    for row in rows[:header_index]:
        joined = " ".join(str(v) for v in row if str(v).strip())
        low = joined.lower().replace("ё", "е")

        if bank_name is None and "асакабанк" in low:
            bank_name = next((str(v).strip() for v in row if str(v).strip()), joined)[:250]

        if "сведения о работе счета" in low:
            m = re.search(
                r"(?:с|c)\s*(\d{1,2}[./-]\d{1,2}[./-]\d{4})\s*по\s*(\d{1,2}[./-]\d{1,2}[./-]\d{4})",
                joined,
                flags=re.I,
            )
            if m:
                period_from, period_to = _date(m.group(1)), _date(m.group(2))

        if ("cчет:" in low or "счет:" in low or "счёт:" in low) and account_number is None:
            m = re.search(r"(?<!\d)(\d{20})(?!\d)", joined)
            if m:
                account_number = m.group(1)
                tail = joined[m.end():]
                tail = re.sub(r"\bИНН\s*:\s*\d{7,14}.*$", "", tail, flags=re.I)
                account_holder = " ".join(tail.split()).strip()[:250] or None
            mt = re.search(r"\bИНН\s*:\s*(\d{7,14})\b", joined, flags=re.I)
            if mt:
                tax_id = mt.group(1)

    return {
        "bank_name": bank_name or "ASAKABANK",
        "account_number": account_number,
        "account_holder": account_holder,
        "tax_id": tax_id,
        "period_from": period_from,
        "period_to": period_to,
        "opening": _label_amount(rows, "остаток на начало периода"),
        "closing": _label_amount(rows, "остаток на конец периода"),
    }


def _split_party(value):
    parts = str(value or "").strip().split("/", 2)
    account = parts[0].strip() if len(parts) > 0 else None
    tin = parts[1].strip() if len(parts) > 1 else None
    name = parts[2].strip() if len(parts) > 2 else str(value or "").strip()
    return account or None, tin or None, name[:250] or None


def try_analyze_asaka(data: bytes, filename: str = "statement.xls"):
    head = data[:4096].lstrip().lower()
    if b"<html" not in head and b"<table" not in head:
        return None

    parser = _Parser()
    parser.feed(_decode(data))
    rows = parser.rows
    if not rows:
        return None

    intro = " ".join(" ".join(r) for r in rows[:8]).lower()
    if "асакабанк" not in intro and "сведения о работе счета" not in intro:
        return None

    header = _find_header(rows)
    if not header:
        return None
    header_index, c = header
    meta = _meta(rows, header_index)

    transactions = []
    required_max = max(c["date"], c["party"], c["debit"], c["credit"], c["purpose"])
    for row in rows[header_index + 1:]:
        if len(row) <= required_max:
            continue
        tx_date = _date(row[c["date"]])
        if not tx_date:
            continue

        outgoing = round(_num(row[c["debit"]]), 2)
        incoming = round(_num(row[c["credit"]]), 2)
        if outgoing == 0 and incoming == 0:
            continue

        account, tin, name = _split_party(row[c["party"]])
        doc_no = row[c["doc"]].strip() if c["doc"] is not None and c["doc"] < len(row) else None
        op_code = row[c["op"]].strip() if c["op"] is not None and c["op"] < len(row) else None
        mfo = row[c["mfo"]].strip() if c["mfo"] is not None and c["mfo"] < len(row) else None

        transactions.append({
            "date": tx_date,
            "counterparty": name,
            "counterparty_account": account,
            "counterparty_tin": tin,
            "document_number": doc_no,
            "operation_code": op_code,
            "mfo": mfo,
            "purpose": str(row[c["purpose"]] or "").strip()[:1500] or None,
            "incoming": incoming,
            "outgoing": outgoing,
        })

    if not transactions:
        return None

    total_incoming = round(sum(x["incoming"] for x in transactions), 2)
    total_outgoing = round(sum(x["outgoing"] for x in transactions), 2)

    warnings = []
    opening, closing = meta["opening"], meta["closing"]
    balance_ok = False
    if opening is not None and closing is not None:
        expected = round(opening + total_incoming - total_outgoing, 2)
        balance_ok = abs(expected - closing) <= 0.05
        if not balance_ok:
            warnings.append(
                f"Қолдиқ назоратида фарқ бор: ҳисобланган {expected:,.2f}, файлда {closing:,.2f}."
            )

    totals = defaultdict(lambda: {"incoming": 0.0, "outgoing": 0.0})
    for tx in transactions:
        if tx["counterparty"]:
            totals[tx["counterparty"]]["incoming"] += tx["incoming"]
            totals[tx["counterparty"]]["outgoing"] += tx["outgoing"]

    top_counterparties = [
        {
            "name": name,
            "incoming": round(vals["incoming"], 2),
            "outgoing": round(vals["outgoing"], 2),
        }
        for name, vals in sorted(
            totals.items(),
            key=lambda item: item[1]["incoming"] + item[1]["outgoing"],
            reverse=True,
        )[:10]
    ]

    dates = [x["date"] for x in transactions]
    period_from = meta["period_from"] or min(dates)
    period_to = meta["period_to"] or max(dates)

    summary = (
        f"{len(transactions)} та операция ўқилди. "
        f"Жами кирим {total_incoming:,.2f}, жами чиқим {total_outgoing:,.2f}."
    )
    if opening is not None and closing is not None:
        summary += f" Бошланғич қолдиқ {opening:,.2f}, якуний қолдиқ {closing:,.2f}."
    if balance_ok:
        summary += " Қолдиқ назорати тўғри."

    return {
        "document_type": "bank_statement",
        "confidence": 0.99,
        "language": "mixed",
        "bank_name": meta["bank_name"],
        "account_holder": meta["account_holder"],
        "account_number": meta["account_number"],
        "tax_id": meta["tax_id"],
        "statement_period": {"from": period_from, "to": period_to},
        "currency": "UZS",
        "opening_balance": opening,
        "total_incoming": total_incoming,
        "total_outgoing": total_outgoing,
        "closing_balance": closing,
        "operations_count": len(transactions),
        "top_counterparties": top_counterparties,
        "transactions_preview": transactions[:10],
        "warnings": warnings,
        "summary": summary,
        "recommended_action": "Натижани текшириб, тўғри бўлса тасдиқланг.",
        "source_filename": filename,
        "source_format": "asakabank_html_v2",
    }
