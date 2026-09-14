import json
import os
import tempfile
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any

from python_calamine import CalamineWorkbook

MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
OPENAI_API_URL = "https://api.openai.com/v1/responses"

BANK_STATEMENT_PROMPT = """
Сен ASMAN SILICAT учун банк выпискасини таҳлил қилувчи профессионал бухгалтер AI ёрдамчисан.
Қуйида Excel банк выпискасидан ўқилган жадвал матни берилади.

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
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw[:-3].strip()
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
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")[:500]


def _spreadsheet_to_text(data: bytes, filename: str) -> str:
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
        max_total_rows = 1500
        max_cols = 50
        chars = 0
        max_chars = 160_000

        for sheet_name in workbook.sheet_names[:12]:
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
                        "text": f"Файл номи: {filename}\n\n{table_text}",
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
