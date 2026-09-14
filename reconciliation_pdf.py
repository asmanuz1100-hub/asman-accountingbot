import os
import re
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _money(value):
    try:
        return f"{float(value):,.2f}".replace(",", " ")
    except Exception:
        return "0.00"


def _fmt_date(value):
    text = str(value or "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        y, m, d = text.split("-")
        return f"{d}.{m}.{y}"
    return text or "—"


def _font_paths():
    candidates = [
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ),
        (
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        ),
        (
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ),
    ]
    for regular, bold in candidates:
        if os.path.exists(regular) and os.path.exists(bold):
            return regular, bold
    raise RuntimeError("PDF учун кирилл шрифти топилмади")


def _register_fonts():
    regular, bold = _font_paths()
    if "ReconSans" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("ReconSans", regular))
    if "ReconSansBold" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("ReconSansBold", bold))


def _entries(data):
    rows = list(data.get("entries") or [])
    if rows:
        return rows

    result = []
    for inv in data.get("invoice_rows") or []:
        direction = inv.get("direction")
        amount = float(inv.get("amount") or 0)
        result.append(
            {
                "date": inv.get("date"),
                "document": f"Фактура №{inv.get('number') or '—'}",
                "basis": inv.get("contract") or "Имзоланган электрон фактура",
                "debit": amount if direction == "outgoing" else 0,
                "credit": amount if direction == "incoming" else 0,
            }
        )

    for tx in data.get("bank_rows") or []:
        incoming = float(tx.get("incoming") or 0)
        outgoing = float(tx.get("outgoing") or 0)
        doc_no = tx.get("document_number")
        result.append(
            {
                "date": tx.get("date"),
                "document": f"Банк тўлови{f' №{doc_no}' if doc_no else ''}",
                "basis": tx.get("purpose") or "Банк операцияси",
                "debit": outgoing,
                "credit": incoming,
            }
        )

    return sorted(result, key=lambda x: (str(x.get("date") or ""), str(x.get("document") or "")))


def _closing_text(data):
    debit = float(data.get("debit_total") or 0)
    credit = float(data.get("credit_total") or 0)
    balance = round(debit - credit, 2)
    if abs(balance) < 0.01:
        return "Якуний сальдо: 0.00 UZS"
    if balance > 0:
        return f"Якуний сальдо: ДЕБЕТ {_money(balance)} UZS"
    return f"Якуний сальдо: КРЕДИТ {_money(abs(balance))} UZS"


def build_reconciliation_pdf(data: dict) -> bytes:
    _register_fonts()

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=10 * mm,
        rightMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        title="Акт сверки взаиморасчетов",
        author="Бухгалтерия ва савдо AI",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReconTitle",
        parent=styles["Title"],
        fontName="ReconSansBold",
        fontSize=15,
        leading=18,
        alignment=TA_CENTER,
        spaceAfter=4,
    )
    center_style = ParagraphStyle(
        "ReconCenter",
        parent=styles["Normal"],
        fontName="ReconSans",
        fontSize=9.5,
        leading=12,
        alignment=TA_CENTER,
    )
    label_style = ParagraphStyle(
        "ReconLabel",
        parent=styles["Normal"],
        fontName="ReconSansBold",
        fontSize=8.5,
        leading=10,
    )
    normal_style = ParagraphStyle(
        "ReconNormal",
        parent=styles["Normal"],
        fontName="ReconSans",
        fontSize=8.2,
        leading=10,
        alignment=TA_LEFT,
    )
    right_style = ParagraphStyle("ReconRight", parent=normal_style, alignment=TA_RIGHT)
    small_style = ParagraphStyle(
        "ReconSmall",
        parent=normal_style,
        fontSize=7.4,
        leading=9,
    )

    partner = data.get("partner") or {}
    own = data.get("own_company") or {}
    period = data.get("statement_period") or {}
    flow = data.get("flow")
    flow_title = {
        "outgoing": "ЧИҚИМ / СОТУВ - ХАРИДОР БИЛАН",
        "incoming": "КИРИМ / ХАРИД - ЕТКАЗИБ БЕРУВЧИ БИЛАН",
    }.get(flow, "ЎЗАРО ҲИСОБ-КИТОБ")

    story = [
        Paragraph("АКТ СВЕРКИ ВЗАИМОРАСЧЕТОВ", title_style),
        Paragraph(flow_title, center_style),
        Spacer(1, 2 * mm),
    ]

    left_name = own.get("name") or "Бизнинг корхона"
    left_tin = own.get("tin") or "—"
    left_acc = own.get("account") or "—"
    right_name = partner.get("name") or "—"
    right_tin = partner.get("tin") or "—"
    right_acc = partner.get("account_number") or partner.get("account") or "—"

    info = Table(
        [
            [
                Paragraph("<b>Корхона:</b>", label_style),
                Paragraph(left_name, normal_style),
                Paragraph("<b>Ҳамкор:</b>", label_style),
                Paragraph(right_name, normal_style),
            ],
            [
                Paragraph("<b>ИНН:</b>", label_style),
                Paragraph(str(left_tin), normal_style),
                Paragraph("<b>ИНН:</b>", label_style),
                Paragraph(str(right_tin), normal_style),
            ],
            [
                Paragraph("<b>Ҳисоб рақами:</b>", label_style),
                Paragraph(str(left_acc), normal_style),
                Paragraph("<b>Ҳисоб рақами:</b>", label_style),
                Paragraph(str(right_acc), normal_style),
            ],
            [
                Paragraph("<b>Давр:</b>", label_style),
                Paragraph(f"{_fmt_date(period.get('from'))} - {_fmt_date(period.get('to'))}", normal_style),
                Paragraph("<b>Ҳужжат ID:</b>", label_style),
                Paragraph(str(data.get("report_id") or "—"), normal_style),
            ],
        ],
        colWidths=[28 * mm, 92 * mm, 30 * mm, 107 * mm],
    )
    info.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B9C2CC")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F3F6F9")),
                ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#F3F6F9")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story += [info, Spacer(1, 4 * mm)]

    entries = _entries(data)
    opening_debit = float(data.get("opening_debit") or 0)
    opening_credit = float(data.get("opening_credit") or 0)

    table_data = [
        [
            Paragraph("№", label_style),
            Paragraph("Сана", label_style),
            Paragraph("Ҳужжат", label_style),
            Paragraph("Асос / мазмун", label_style),
            Paragraph("Дебет", label_style),
            Paragraph("Кредит", label_style),
        ],
        [
            "",
            "",
            Paragraph("Бошланғич сальдо", label_style),
            "",
            Paragraph(_money(opening_debit), right_style),
            Paragraph(_money(opening_credit), right_style),
        ],
    ]

    for idx, entry in enumerate(entries, 1):
        table_data.append(
            [
                Paragraph(str(idx), center_style),
                Paragraph(_fmt_date(entry.get("date")), center_style),
                Paragraph(str(entry.get("document") or "—"), small_style),
                Paragraph(str(entry.get("basis") or "—"), small_style),
                Paragraph(_money(entry.get("debit")), right_style),
                Paragraph(_money(entry.get("credit")), right_style),
            ]
        )

    debit_total = float(data.get("debit_total") or 0)
    credit_total = float(data.get("credit_total") or 0)
    balance = round(debit_total - credit_total, 2)
    closing_debit = balance if balance > 0 else 0
    closing_credit = abs(balance) if balance < 0 else 0

    table_data += [
        ["", "", Paragraph("ЖАМИ:", label_style), "", Paragraph(_money(debit_total), right_style), Paragraph(_money(credit_total), right_style)],
        ["", "", Paragraph("Якуний сальдо:", label_style), "", Paragraph(_money(closing_debit), right_style), Paragraph(_money(closing_credit), right_style)],
    ]

    main_table = Table(
        table_data,
        colWidths=[10 * mm, 24 * mm, 43 * mm, 126 * mm, 31 * mm, 31 * mm],
        repeatRows=1,
        hAlign="LEFT",
    )
    main_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "ReconSans"),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#6E7781")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF5")),
                ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#F8FAFC")),
                ("BACKGROUND", (0, -2), (-1, -1), colors.HexColor("#FFF7D6")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (1, -1), "CENTER"),
                ("RIGHTPADDING", (4, 0), (5, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
            ]
        )
    )
    story += [main_table, Spacer(1, 4 * mm)]

    receivable = float(data.get("receivable") or 0)
    payable = float(data.get("payable") or 0)
    if flow == "outgoing":
        if receivable > 0:
            conclusion = f"Ҳамкорнинг қарзи: {_money(receivable)} UZS"
        elif receivable < 0:
            conclusion = f"Ҳамкор аванси: {_money(abs(receivable))} UZS"
        else:
            conclusion = "Ҳисоб-китоб бўйича қарздорлик йўқ."
    elif flow == "incoming":
        if payable > 0:
            conclusion = f"Бизнинг ҳамкор олдидаги қарзимиз: {_money(payable)} UZS"
        elif payable < 0:
            conclusion = f"Етказиб берувчи аванси: {_money(abs(payable))} UZS"
        else:
            conclusion = "Ҳисоб-китоб бўйича қарздорлик йўқ."
    else:
        conclusion = _closing_text(data)

    story += [
        Paragraph(f"<b>{conclusion}</b>", normal_style),
        Spacer(1, 2 * mm),
        Paragraph(
            "Ушбу акт бот базасида сақланган банк операциялари ва тасдиқланган фактуралар асосида "
            "автоматик шакллантирилди. Имзолашдан олдин бухгалтер томонидан текшириш тавсия этилади.",
            small_style,
        ),
    ]

    doc.build(story)
    return buffer.getvalue()
