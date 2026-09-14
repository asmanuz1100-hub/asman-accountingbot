import base64
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
OPENAI_API_URL = "https://api.openai.com/v1/responses"

PROMPT = """
Сен ASMAN SILICAT учун бухгалтерия ҳужжатларини таҳлил қилувчи AI ёрдамчисан.
Берилган ҳужжатни диққат билан ўқи. Расм ёки скан бўлса, матн ва жадвалларни ҳам ўқи.

Фақат JSON қайтар. Markdown ёки ``` ишлатма.

Қуйидаги структурада қайтар:
{
  "document_type": "contract|invoice|act|waybill|payment|warehouse_receipt|other",
  "confidence": 0.0,
  "language": "uz|ru|en|mixed",
  "partner": {
    "name": null,
    "tin": null,
    "address": null
  },
  "contract_number": null,
  "contract_date": null,
  "invoice_number": null,
  "invoice_date": null,
  "document_number": null,
  "document_date": null,
  "currency": "UZS|USD|RUB|KZT|null",
  "subtotal": null,
  "vat": null,
  "total": null,
  "items": [
    {
      "name": "",
      "quantity": null,
      "unit": null,
      "unit_price": null,
      "amount": null
    }
  ],
  "warnings": [],
  "summary": "",
  "recommended_action": ""
}

Қоидалар:
- Аниқ кўринмаган маълумотни ўйлаб топма; null қўй.
- Суммаларни рақам шаклида бер.
- НДС/ҚҚС бор бўлса алоҳида кўрсат.
- Шартнома, счёт-фактура ва товар позицияларидаги рақамларни қайта текшир.
- Агар сумма ёки реквизитларда шубҳали фарқ бўлса warnings ичида ёз.
- summary ва warnings ўзбек кирилл тилида қисқа ва аниқ бўлсин.
"""


def _api_key() -> str:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY киритилмаган")
    return key


def _parse_json(text: str) -> dict[str, Any]:
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


def _extract_output_text(response: dict) -> str:
    pieces = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                pieces.append(content.get("text", ""))
    if pieces:
        return "\n".join(pieces)
    raise RuntimeError("OpenAI жавобида матн топилмади")


def _responses_request(content: list[dict]) -> dict:
    payload = {
        "model": MODEL,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": PROMPT},
                    *content,
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
        with urllib.request.urlopen(request, timeout=120) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API хатоси {exc.code}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenAI API билан алоқа хатоси: {exc}") from exc

    return _parse_json(_extract_output_text(body))


def analyze_image_bytes(data: bytes, mime_type: str = "image/jpeg") -> dict:
    encoded = base64.b64encode(data).decode("ascii")
    data_url = f"data:{mime_type};base64,{encoded}"
    return _responses_request([
        {
            "type": "input_image",
            "image_url": data_url,
            "detail": "high",
        }
    ])


def analyze_pdf_bytes(data: bytes, filename: str = "document.pdf") -> dict:
    encoded = base64.b64encode(data).decode("ascii")
    return _responses_request([
        {
            "type": "input_file",
            "filename": filename or "document.pdf",
            "file_data": encoded,
        }
    ])
