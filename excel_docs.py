import csv
import io
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from python_calamine import CalamineWorkbook

MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
OPENAI_API_URL = "https://api.openai.com/v1/responses"

BANK_STATEMENT_PROMPT = """
Сен банк выпискасини таҳлил қилувчи профессионал бухгалтер AI ёрдамчисан.
Қуйида банкдан экспорт қилинган Excel/HTML/XML жадвалидан ўқилган матн берилади.

Вазифа:
- банк ва ҳисоб рақамини аниқлаш;
- выписка даврини аниқлаш;
- кирим ва чиқим операцияларини ажратиш;
- бошланғич ва якуний қолдиқни аниқлаш;
- жами кирим ва жами чиқимни текшириш;
- операциялар сонини ҳисоблаш;
- асосий контрагентларни аниқлаш;
- тўлов мақсадларини таҳлил қилиш;
- дубликат, манфий қолдиқ, тушунарсиз ёки шубҳали операциялар бўлса огоҳлантириш.

Фақат JSON қайтар. Markdown ёки ``` ишлатма.

Қуйидаги структурада қайтар:
{
  "document_type": "bank_statement",
  "confidence": 0.0,
  "language": "uz|ru|en|mixed",
  "bank_name": null,
  "account_holder": null,
  "account_number": null,
  "statement_period": {"from": null, "to": null},
  "currency": "UZS|USD|RUB|KZT|null",
  "opening_balance": null,
  "total_incoming": null,
  "total_outgoing": null,
  "closing_balance": null,
  "operations_count": 0,
  "top_counterparties": [
    {"name": "", "incoming": 0, "outgoing": 0}
  ],
  "transactions_preview": [
    {
      "date": null,
      "counterparty": null,
      "purpose": null,
      "incoming": 0,
      "outgoing": 0
    }
  ],
  "warnings": [],
  "summary": "",
  "recommended_action": ""
}

Қоидалар:
- Рақамларни ўйлаб топма.
- Агар жадвалда бир нечта варақ бўлса, ҳаммасини ҳисобга ол.
- Дебет/кредит устунларининг маъноси банк форматига қараб фарқ қилиши мумкин; сарлавҳалар ва қолдиқ мантиғи билан текшир.
- Суммаларни рақам шаклида бер.
- transactions_preview ичида энг муҳим ёки биринчи 10 та операцияни бер.
- top_counterparties ичида энг катта 10 та контрагентни бер.
- summary ва warnings ўзбек кирилл тилида қисқа ва аниқ бўлсин.
"""


def _api_key() -> str:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY киритилмаган")
    return key


def _extract_output_text(response: dict) -> str:
    pieces = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                pieces.append(content.get("text", ""))
    if not pieces:
        raise RuntimeError("OpenAI жавобида матн топилмади")
    return "\n".join(pieces)


def _parse_json(text: str) -> dict:
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start:end + 1])
        raise ValueError("AI жавобини JSON сифатида ўқиб бўлмади")


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")[:1000]


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


def _html_to_text(data: bytes) -> str:
    text = _decode_text(data)
    parser = _TableHTMLParser()
    parser.feed(text)
    if parser.rows:
        return "\n".join("\t".join(row) for row in parser.rows)

    plain = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    plain = re.sub(r"<style\b[^>]*>.*?</style>", " ", plain, flags=re.I | re.S)
    plain = re.sub(r"<[^>]+>", " ", plain)
    plain = re.sub(r"\s+", " ", plain).strip()
    if not plain:
        raise ValueError("HTML файл ичида ўқиладиган маълумот топилмади")
    return plain


def _xml_to_text(data: bytes) -> str:
    text = _decode_text(data)
    root = ET.fromstring(text)
    rows_out: list[str] = []

    for elem in root.iter():
        if elem.tag.split("}")[-1].lower() != "row":
            continue
        cells: list[str] = []
        for cell in list(elem):
            if cell.tag.split("}")[-1].lower() != "cell":
                continue
            values = []
            for node in cell.iter():
                if node.tag.split("}")[-1].lower() == "data" and node.text:
                    values.append(node.text)
            cells.append(" ".join(values).strip())
        if any(cells):
            rows_out.append("\t".join(cells))

    if rows_out:
        return "\n".join(rows_out)

    generic = []
    for elem in root.iter():
        if elem.text and elem.text.strip():
            generic.append(elem.text.strip())
    if generic:
        return "\n".join(generic)
    raise ValueError("XML файл ичида ўқиладиган маълумот топилмади")


def _delimited_text_to_text(data: bytes) -> str:
    text = _decode_text(data)
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("Файл ичида ўқиладиган маълумот топилмади")

    sample = "\n".join(lines[:30])
    delimiter = "\t" if sample.count("\t") >= max(sample.count(";"), sample.count(",")) else ";"
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters="\t;,|")
        delimiter = dialect.delimiter
    except csv.Error:
        pass

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    out = []
    for row in reader:
        vals = [str(v).strip() for v in row]
        if any(vals):
            out.append("\t".join(vals))
    return "\n".join(out)


def _calamine_to_text(data: bytes, filename: str) -> str:
    suffix = Path(filename or "statement.xlsx").suffix.lower()
    if suffix not in {".xls", ".xlsx", ".xlsm", ".xlsb", ".ods"}:
        suffix = ".xlsx"

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            temp_path = tmp.name

        workbook = CalamineWorkbook.from_path(temp_path)
        blocks = []
        total_rows = 0
        max_total_rows = 2000
        max_cols = 60
        chars = 0
        max_chars = 180_000

        for sheet_name in workbook.sheet_names[:20]:
            header = f"\n=== ВАРАҚ: {sheet_name} ==="
            blocks.append(header)
            chars += len(header)

            rows = workbook.get_sheet_by_name(sheet_name).to_python()
            for row in rows:
                if total_rows >= max_total_rows:
                    blocks.append("[Қолган қаторлар лимит сабаб қисқартирилди]")
                    break
                values = [_cell_text(v) for v in row[:max_cols]]
                if not any(values):
                    continue
                line = "\t".join(values)
                if chars + len(line) > max_chars:
                    blocks.append("[Файл ҳажми катта бўлгани учун матн қисқартирилди]")
                    return "\n".join(blocks)
                blocks.append(line)
                chars += len(line) + 1
                total_rows += 1
            if total_rows >= max_total_rows:
                break

        if total_rows == 0:
            raise ValueError("Excel файл ичида ўқиладиган маълумот топилмади")
        return "\n".join(blocks)
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _spreadsheet_to_text(data: bytes, filename: str) -> str:
    head = data[:512].lstrip().lower()

    # Кўп банклар .xls деб HTML файл экспорт қилади.
    if head.startswith(b"<html") or head.startswith(b"<!doctype html") or b"<html" in head:
        return _html_to_text(data)

    # Excel 2003 XML / SpreadsheetML ёки бошқа XML экспорт.
    if head.startswith(b"<?xml") or head.startswith(b"<workbook"):
        try:
            return _xml_to_text(data)
        except Exception:
            return _delimited_text_to_text(data)

    try:
        return _calamine_to_text(data, filename)
    except Exception as calamine_error:
        # Агар файл кенгайтмаси .xls бўлса ҳам, ичи HTML/CSV бўлиши мумкин.
        text_head = _decode_text(data[:2048]).lstrip().lower()
        if text_head.startswith("<html") or "<table" in text_head:
            return _html_to_text(data)
        if text_head.startswith("<?xml"):
            try:
                return _xml_to_text(data)
            except Exception:
                pass
        try:
            return _delimited_text_to_text(data)
        except Exception:
            raise ValueError(
                "Банк файлини ўқиб бўлмади. XLS кенгайтмаси бор, лекин формат стандарт Excel эмас. "
                f"Техник хато: {calamine_error}"
            ) from calamine_error


def analyze_spreadsheet_bytes(data: bytes, filename: str = "statement.xlsx") -> dict:
    table_text = _spreadsheet_to_text(data, filename)
    payload = {
        "model": MODEL,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": BANK_STATEMENT_PROMPT},
                    {
                        "type": "input_text",
                        "text": f"Файл номи: {filename}\n\n{table_text[:180000]}",
                    },
                ],
            }
        ],
    }

    request = urllib.request.Request(
        OPENAI_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API хатоси {exc.code}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenAI API билан алоқа хатоси: {exc}") from exc

    result = _parse_json(_extract_output_text(body))
    result.setdefault("document_type", "bank_statement")
    return result
