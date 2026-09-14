import csv
import io
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from python_calamine import CalamineWorkbook


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def _decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8-sig", "cp1251", "windows-1251", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


class _TableHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.current_row: list[str] | None = None
        self.current_cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs):
        tag = tag.lower()
        if tag == "tr":
            self.current_row = []
        elif tag in ("td", "th"):
            self.current_cell = []
        elif tag == "br" and self.current_cell is not None:
            self.current_cell.append(" ")

    def handle_data(self, data: str):
        if self.current_cell is not None:
            self.current_cell.append(data)

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag in ("td", "th") and self.current_cell is not None:
            value = " ".join("".join(self.current_cell).split())
            if self.current_row is not None:
                self.current_row.append(value)
            self.current_cell = None
        elif tag == "tr" and self.current_row is not None:
            if any(v.strip() for v in self.current_row):
                self.rows.append(self.current_row)
            self.current_row = None


def _html_to_rows(data: bytes) -> list[list[str]]:
    parser = _TableHTMLParser()
    parser.feed(_decode_text(data))
    if not parser.rows:
        raise ValueError("HTML банк файли ичида жадвал топилмади")
    return parser.rows


def _xml_to_rows(data: bytes) -> list[list[str]]:
    root = ET.fromstring(_decode_text(data))
    rows: list[list[str]] = []
    for elem in root.iter():
        if elem.tag.split("}")[-1].lower() != "row":
            continue
        row: list[str] = []
        for cell in list(elem):
            if cell.tag.split("}")[-1].lower() != "cell":
                continue
            values = []
            for node in cell.iter():
                if node.tag.split("}")[-1].lower() == "data" and node.text:
                    values.append(node.text)
            row.append(" ".join(values).strip())
        if any(row):
            rows.append(row)
    if not rows:
        raise ValueError("XML банк файли ичида жадвал топилмади")
    return rows


def _delimited_to_rows(data: bytes) -> list[list[str]]:
    text = _decode_text(data)
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("Файл ичида маълумот топилмади")
    sample = "\n".join(lines[:30])
    delimiter = "\t" if sample.count("\t") >= max(sample.count(";"), sample.count(",")) else ";"
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters="\t;,|").delimiter
    except csv.Error:
        pass
    rows = []
    for row in csv.reader(io.StringIO(text), delimiter=delimiter):
        values = [str(v).strip() for v in row]
        if any(values):
            rows.append(values)
    return rows


def _calamine_to_rows(data: bytes, filename: str) -> list[list[str]]:
    suffix = Path(filename or "statement.xlsx").suffix.lower()
    if suffix not in {".xls", ".xlsx", ".xlsm", ".xlsb", ".ods"}:
        suffix = ".xlsx"
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            temp_path = tmp.name
        workbook = CalamineWorkbook.from_path(temp_path)
        rows: list[list[str]] = []
        for sheet_name in workbook.sheet_names[:20]:
            rows.append([f"ВАРАҚ: {sheet_name}"])
            for row in workbook.get_sheet_by_name(sheet_name).to_python():
                values = [_cell_text(v) for v in row[:80]]
                if any(values):
                    rows.append(values)
                if len(rows) >= 10000:
                    break
            if len(rows) >= 10000:
                break
        if not rows:
            raise ValueError("Excel файл ичида маълумот топилмади")
        return rows
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _spreadsheet_to_rows(data: bytes, filename: str) -> list[list[str]]:
    head = data[:1024].lstrip().lower()
    if head.startswith(b"<html") or head.startswith(b"<!doctype html") or b"<table" in head:
        return _html_to_rows(data)
    if head.startswith(b"<?xml") or head.startswith(b"<workbook"):
        try:
            return _xml_to_rows(data)
        except Exception:
            return _delimited_to_rows(data)
    try:
        return _calamine_to_rows(data, filename)
    except Exception as calamine_error:
        text_head = _decode_text(data[:4096]).lstrip().lower()
        if text_head.startswith("<html") or "<table" in text_head:
            return _html_to_rows(data)
        if text_head.startswith("<?xml"):
            try:
                return _xml_to_rows(data)
            except Exception:
                pass
        try:
            return _delimited_to_rows(data)
        except Exception:
            raise ValueError(
                "Банк файлини ўқиб бўлмади. XLS кенгайтмаси бор, лекин формат стандарт Excel эмас. "
                f"Техник хато: {calamine_error}"
            ) from calamine_error


def _norm(value: Any) -> str:
    text = _cell_text(value).lower().replace("ё", "е")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip().replace("\xa0", " ")
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = re.sub(r"[^0-9,\.\-]", "", text)
    if not cleaned or cleaned in {"-", ".", ","}:
        return None
    if cleaned.count(".") > 1 and "," not in cleaned:
        cleaned = cleaned.replace(".", "")
    elif cleaned.count(",") > 1 and "." not in cleaned:
        cleaned = cleaned.replace(",", "")
    elif "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if negative:
        number = -abs(number)
    return number


def _parse_date(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    patterns = [
        (r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\b", "dmy"),
        (r"\b(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})\b", "ymd"),
    ]
    for pattern, kind in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            if kind == "dmy":
                d, m, y = map(int, match.groups())
            else:
                y, m, d = map(int, match.groups())
            return date(y, m, d).isoformat()
        except ValueError:
            continue
    return None


def _header_score(row: list[str]) -> int:
    text = " | ".join(_norm(v) for v in row)
    score = 0
    if "дата" in text or "date" in text or "сана" in text:
        score += 2
    if any(k in text for k in ("дебет", "расход", "списан", "чиқим", "chiqim")):
        score += 3
    if any(k in text for k in ("кредит", "приход", "поступ", "кирим", "kirim")):
        score += 3
    if any(k in text for k in ("контрагент", "получатель", "плательщик", "ҳамкор", "hamkor")):
        score += 2
    if any(k in text for k in ("назначение", "мақсад", "maqsad", "purpose")):
        score += 2
    if "сумм" in text or "amount" in text:
        score += 1
    if "документ" in text or "номер" in text:
        score += 1
    return score


def _find_header(rows: list[list[str]]) -> tuple[int, list[str], int]:
    best_index = 0
    best_row: list[str] = rows[0] if rows else []
    best_score = -1
    for i, row in enumerate(rows[:120]):
        score = _header_score(row)
        if score > best_score:
            best_index, best_row, best_score = i, row, score
    return best_index, best_row, best_score


def _find_col(headers: list[str], include: tuple[str, ...], exclude: tuple[str, ...] = ()) -> int | None:
    for i, header in enumerate(headers):
        h = _norm(header)
        if any(term in h for term in include) and not any(term in h for term in exclude):
            return i
    return None


def _value_at(row: list[str], index: int | None) -> str:
    if index is None or index < 0 or index >= len(row):
        return ""
    return row[index]


def _labeled_amount(rows: list[list[str]], labels: tuple[str, ...]) -> float | None:
    for row in rows[:200]:
        text = " ".join(_norm(v) for v in row)
        if not any(label in text for label in labels):
            continue
        candidates = []
        for cell in row:
            raw = re.sub(r"\D", "", str(cell))
            if len(raw) >= 18:
                continue
            number = _parse_number(cell)
            if number is not None and abs(number) < 10**16:
                candidates.append(number)
        if candidates:
            return candidates[-1]
    return None


def _metadata(rows: list[list[str]]) -> dict:
    preview = rows[:120]
    bank_name = None
    account_holder = None
    account_number = None
    currency = None

    for row in preview:
        joined = " | ".join(str(v) for v in row if str(v).strip())
        lower = _norm(joined)
        if bank_name is None and "банк" in lower:
            parts = [str(v).strip() for v in row if str(v).strip()]
            bank_name = " | ".join(parts)[:250]
        if account_holder is None and any(k in lower for k in ("клиент", "владелец", "наименование клиента", "ҳисоб эгаси")):
            parts = [str(v).strip() for v in row if str(v).strip()]
            if len(parts) > 1:
                account_holder = parts[-1][:250]
        if account_number is None and any(k in lower for k in ("счет", "счёт", "account", "ҳисоб")):
            m = re.search(r"(?<!\d)(\d{20})(?!\d)", joined.replace(" ", ""))
            if m:
                account_number = m.group(1)
        if currency is None:
            upper = joined.upper()
            for code in ("UZS", "USD", "RUB", "KZT", "EUR"):
                if re.search(rf"\b{code}\b", upper):
                    currency = code
                    break
            if currency is None and any(k in lower for k in ("сум", "so'm", "сўм")):
                currency = "UZS"

    if account_number is None:
        for row in preview:
            joined = "".join(str(v) for v in row).replace(" ", "")
            m = re.search(r"(?<!\d)(\d{20})(?!\d)", joined)
            if m:
                account_number = m.group(1)
                break

    return {
        "bank_name": bank_name,
        "account_holder": account_holder,
        "account_number": account_number,
        "currency": currency,
    }


def _transaction_rows(rows: list[list[str]]) -> tuple[list[dict], int]:
    if not rows:
        return [], 0
    header_index, header_row, score = _find_header(rows)
    headers = [_norm(v) for v in header_row]

    date_col = _find_col(headers, ("дата", "date", "сана"))
    outgoing_col = _find_col(headers, ("дебет", "расход", "списан", "чиқим", "chiqim"), ("счет", "счёт"))
    incoming_col = _find_col(headers, ("кредит", "приход", "поступ", "кирим", "kirim"), ("счет", "счёт"))
    counterparty_col = _find_col(headers, ("контрагент", "получатель", "плательщик", "ҳамкор", "hamkor", "корреспондент"), ("счет", "счёт"))
    purpose_col = _find_col(headers, ("назначение", "мақсад", "maqsad", "purpose", "основание"))
    amount_col = _find_col(headers, ("сумма", "amount"), ("дебет", "кредит", "итого"))
    direction_col = _find_col(headers, ("тип", "операц", "направлен", "дебет/кредит"))

    transactions: list[dict] = []
    for row in rows[header_index + 1:]:
        joined = " | ".join(str(v) for v in row if str(v).strip())
        lower = _norm(joined)
        if not lower:
            continue
        is_summary = any(k in lower for k in ("итого", "оборот", "остаток", "сальдо", "жами"))

        tx_date = _parse_date(_value_at(row, date_col)) if date_col is not None else None
        if tx_date is None:
            for cell in row[:5]:
                tx_date = _parse_date(cell)
                if tx_date:
                    break

        outgoing = _parse_number(_value_at(row, outgoing_col)) if outgoing_col is not None else None
        incoming = _parse_number(_value_at(row, incoming_col)) if incoming_col is not None else None

        if outgoing_col is None and incoming_col is None and amount_col is not None:
            amount = _parse_number(_value_at(row, amount_col)) or 0.0
            direction = _norm(_value_at(row, direction_col)) if direction_col is not None else ""
            if any(k in direction for k in ("кредит", "приход", "поступ", "кирим", "credit", "in")):
                incoming, outgoing = amount, 0.0
            elif any(k in direction for k in ("дебет", "расход", "списан", "чиқим", "debit", "out")):
                outgoing, incoming = amount, 0.0

        outgoing = float(outgoing or 0.0)
        incoming = float(incoming or 0.0)
        if is_summary and not tx_date:
            continue
        if not tx_date and incoming == 0 and outgoing == 0:
            continue
        if incoming == 0 and outgoing == 0:
            continue

        counterparty = _value_at(row, counterparty_col).strip() if counterparty_col is not None else ""
        purpose = _value_at(row, purpose_col).strip() if purpose_col is not None else ""

        if not counterparty:
            for cell in row:
                text = str(cell).strip()
                n = _norm(text)
                if len(text) >= 4 and not _parse_date(text) and _parse_number(text) is None:
                    if not any(k in n for k in ("дебет", "кредит", "итого", "оборот", "остаток")):
                        counterparty = text[:250]
                        break

        transactions.append({
            "date": tx_date,
            "counterparty": counterparty or None,
            "purpose": purpose or None,
            "incoming": round(incoming, 2),
            "outgoing": round(outgoing, 2),
        })

    return transactions, score


def _analyze_rows(rows: list[list[str]], filename: str) -> dict:
    meta = _metadata(rows)
    transactions, header_score = _transaction_rows(rows)

    total_incoming = round(sum(t["incoming"] for t in transactions), 2)
    total_outgoing = round(sum(t["outgoing"] for t in transactions), 2)

    opening = _labeled_amount(rows, ("входящий остаток", "начальный остаток", "остаток на начало", "бошлангич колдик", "бошланғич қолдиқ"))
    closing = _labeled_amount(rows, ("исходящий остаток", "конечный остаток", "остаток на конец", "якуний колдик", "якуний қолдиқ"))

    warnings: list[str] = []
    if not transactions:
        warnings.append("Операциялар жадвали автоматик аниқланмади; банк формати учун алоҳида мослаштириш керак бўлиши мумкин.")

    if opening is not None and closing is not None and transactions:
        expected = round(opening + total_incoming - total_outgoing, 2)
        reverse_expected = round(opening + total_outgoing - total_incoming, 2)
        tolerance = max(1.0, abs(closing) * 0.0001)
        if abs(expected - closing) > tolerance and abs(reverse_expected - closing) <= tolerance:
            for tx in transactions:
                tx["incoming"], tx["outgoing"] = tx["outgoing"], tx["incoming"]
            total_incoming, total_outgoing = total_outgoing, total_incoming
            warnings.append("Банк форматида дебет/кредит йўналиши тескари берилгани аниқланиб, кирим-чиқим автоматик тўғриланди.")
        elif abs(expected - closing) > tolerance:
            warnings.append(
                f"Қолдиқ назоратида фарқ бор: ҳисобланган {expected:,.2f}, файлдаги якуний қолдиқ {closing:,.2f}."
            )

    dated = [t["date"] for t in transactions if t.get("date")]
    period_from = min(dated) if dated else None
    period_to = max(dated) if dated else None

    counterparty_totals: dict[str, dict[str, float]] = defaultdict(lambda: {"incoming": 0.0, "outgoing": 0.0})
    for tx in transactions:
        name = (tx.get("counterparty") or "").strip()
        if not name:
            continue
        counterparty_totals[name]["incoming"] += tx["incoming"]
        counterparty_totals[name]["outgoing"] += tx["outgoing"]

    top_counterparties = []
    for name, sums in sorted(
        counterparty_totals.items(),
        key=lambda item: item[1]["incoming"] + item[1]["outgoing"],
        reverse=True,
    )[:10]:
        top_counterparties.append({
            "name": name[:250],
            "incoming": round(sums["incoming"], 2),
            "outgoing": round(sums["outgoing"], 2),
        })

    confidence = 0.45
    if header_score >= 6:
        confidence += 0.2
    if transactions:
        confidence += 0.2
    if meta.get("account_number"):
        confidence += 0.05
    if opening is not None or closing is not None:
        confidence += 0.05
    confidence = min(confidence, 0.98)

    summary = (
        f"{len(transactions)} та операция ўқилди. "
        f"Жами кирим {total_incoming:,.2f}, жами чиқим {total_outgoing:,.2f}."
    )
    if opening is not None and closing is not None:
        summary += f" Бошланғич қолдиқ {opening:,.2f}, якуний қолдиқ {closing:,.2f}."

    return {
        "document_type": "bank_statement",
        "confidence": round(confidence, 2),
        "language": "mixed",
        "bank_name": meta.get("bank_name"),
        "account_holder": meta.get("account_holder"),
        "account_number": meta.get("account_number"),
        "statement_period": {"from": period_from, "to": period_to},
        "currency": meta.get("currency") or "UZS",
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
    }


def analyze_spreadsheet_bytes(data: bytes, filename: str = "statement.xlsx") -> dict:
    """Bank statement analysis without an OpenAI API call.

    This avoids TPM/rate-limit failures for large XLS/XLSX/HTML bank exports.
    """
    rows = _spreadsheet_to_rows(data, filename)
    return _analyze_rows(rows, filename)
