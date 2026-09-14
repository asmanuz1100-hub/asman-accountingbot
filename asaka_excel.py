import re
from collections import defaultdict
from datetime import date
from html.parser import HTMLParser


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251", "windows-1251", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "tr":
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
        elif tag == "tr" and self.row is not None:
            if any(str(v).strip() for v in self.row):
                self.rows.append(self.row)
            self.row = None


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


def _amount_from_label(rows, label):
    for row in rows[:20]:
        for cell in row:
            if label in str(cell).lower():
                m = re.search(r"([-+]?\d[\d \xa0]*(?:[.,]\d+)?)", str(cell))
                if m:
                    return _num(m.group(1))
    return None


def _counterparty(raw):
    parts = str(raw or "").strip().split("/", 2)
    if len(parts) == 3:
        return parts[2].strip()[:250] or None
    return str(raw or "").strip()[:250] or None


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

    bank_name = rows[0][0].strip() if rows and rows[0] else None
    account_number = None
    account_holder = None
    tax_id = None

    for row in rows[:20]:
        joined = " ".join(str(v) for v in row if str(v).strip())
        if account_number is None:
            m = re.search(r"(?<!\d)(\d{20})(?!\d)", joined.replace(" ", ""))
            if m:
                account_number = m.group(1)
        if tax_id is None:
            m = re.search(r"(?:ИНН|СТИР)\s*:?\s*(\d{9})", joined, flags=re.I)
            if m:
                tax_id = m.group(1)
        if account_holder is None and account_number and account_number in joined.replace(" ", ""):
            m = re.search(
                rf"{re.escape(account_number)}\s+(.+?)(?:\s+ИНН\s*:|\s+СТИР\s*:|$)",
                joined,
                flags=re.I,
            )
            if m:
                account_holder = m.group(1).strip()

    period_from = period_to = None
    for row in rows[:10]:
        joined = " ".join(str(v) for v in row)
        m = re.search(
            r"(?:с|c)\s*(\d{1,2}[./-]\d{1,2}[./-]\d{4})\s*по\s*(\d{1,2}[./-]\d{1,2}[./-]\d{4})",
            joined,
            flags=re.I,
        )
        if m:
            period_from, period_to = _date(m.group(1)), _date(m.group(2))
            break

    opening = _amount_from_label(rows, "остаток на начало периода")
    closing = _amount_from_label(rows, "остаток на конец периода")

    transactions = []
    for row in rows:
        if len(row) < 8:
            continue
        tx_date = _date(row[0])
        if not tx_date:
            continue
        outgoing = round(_num(row[5]), 2)
        incoming = round(_num(row[6]), 2)
        if outgoing == 0 and incoming == 0:
            continue
        transactions.append({
            "date": tx_date,
            "counterparty": _counterparty(row[1]),
            "purpose": str(row[7] or "").strip()[:1000] or None,
            "incoming": incoming,
            "outgoing": outgoing,
        })

    if not transactions:
        return None

    total_outgoing = round(sum(t["outgoing"] for t in transactions), 2)
    total_incoming = round(sum(t["incoming"] for t in transactions), 2)

    warnings = []
    footer_out = footer_in = None
    for row in reversed(rows[-20:]):
        if row and "итоговый оборот" in str(row[0]).lower():
            footer_out = round(_num(row[1] if len(row) > 1 else 0), 2)
            footer_in = round(_num(row[2] if len(row) > 2 else 0), 2)
            break
    if footer_out is not None and abs(footer_out - total_outgoing) > 0.01:
        warnings.append("Чиқим жами файлдаги итог билан мос эмас.")
    if footer_in is not None and abs(footer_in - total_incoming) > 0.01:
        warnings.append("Кирим жами файлдаги итог билан мос эмас.")

    if opening is not None and closing is not None:
        expected = round(opening + total_incoming - total_outgoing, 2)
        if abs(expected - closing) > 0.01:
            warnings.append(
                f"Қолдиқ назоратида фарқ бор: ҳисобланган {expected:,.2f}, файлда {closing:,.2f}."
            )

    totals = defaultdict(lambda: {"incoming": 0.0, "outgoing": 0.0})
    for tx in transactions:
        name = tx.get("counterparty")
        if name:
            totals[name]["incoming"] += tx["incoming"]
            totals[name]["outgoing"] += tx["outgoing"]

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

    if not period_from or not period_to:
        dates = [t["date"] for t in transactions]
        period_from = period_from or min(dates)
        period_to = period_to or max(dates)

    summary = (
        f"{len(transactions)} та операция ўқилди. "
        f"Жами кирим {total_incoming:,.2f}, жами чиқим {total_outgoing:,.2f}."
    )
    if opening is not None and closing is not None:
        summary += f" Бошланғич қолдиқ {opening:,.2f}, якуний қолдиқ {closing:,.2f}."

    return {
        "document_type": "bank_statement",
        "confidence": 0.99,
        "language": "mixed",
        "bank_name": bank_name,
        "account_holder": account_holder,
        "account_number": account_number,
        "tax_id": tax_id,
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
        "source_format": "asakabank_html",
    }
