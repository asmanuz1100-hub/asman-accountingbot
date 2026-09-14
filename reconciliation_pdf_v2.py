from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from reconciliation_pdf import _entries, _fmt_date, _money, _register_fonts
from reconciliation_saldo import add_closing_saldo


def build_reconciliation_pdf_v2(data: dict) -> bytes:
    data = add_closing_saldo(dict(data or {}))
    _register_fonts()

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=10 * mm,
        rightMargin=10 * mm,
        topMargin=9 * mm,
        bottomMargin=9 * mm,
        title="Акт сверки взаиморасчетов",
        author="Бухгалтерия ва савдо AI",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "SaldoTitle", parent=styles["Title"], fontName="ReconSansBold",
        fontSize=15, leading=18, alignment=TA_CENTER, spaceAfter=3,
    )
    center = ParagraphStyle(
        "SaldoCenter", parent=styles["Normal"], fontName="ReconSans",
        fontSize=9, leading=11, alignment=TA_CENTER,
    )
    normal = ParagraphStyle(
        "SaldoNormal", parent=styles["Normal"], fontName="ReconSans",
        fontSize=8.2, leading=10, alignment=TA_LEFT,
    )
    bold = ParagraphStyle(
        "SaldoBold", parent=normal, fontName="ReconSansBold",
    )
    right = ParagraphStyle("SaldoRight", parent=normal, alignment=TA_RIGHT)
    small = ParagraphStyle("SaldoSmall", parent=normal, fontSize=7.3, leading=8.6)
    saldo_big = ParagraphStyle(
        "SaldoBig", parent=normal, fontName="ReconSansBold",
        fontSize=10.5, leading=13, alignment=TA_CENTER,
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
        Paragraph(flow_title, center),
        Spacer(1, 2 * mm),
    ]

    info = Table(
        [
            [Paragraph("<b>Корхона:</b>", bold), Paragraph(str(own.get("name") or "Бизнинг корхона"), normal),
             Paragraph("<b>Ҳамкор:</b>", bold), Paragraph(str(partner.get("name") or "—"), normal)],
            [Paragraph("<b>ИНН:</b>", bold), Paragraph(str(own.get("tin") or "—"), normal),
             Paragraph("<b>ИНН:</b>", bold), Paragraph(str(partner.get("tin") or "—"), normal)],
            [Paragraph("<b>Ҳисоб рақами:</b>", bold), Paragraph(str(own.get("account") or "—"), normal),
             Paragraph("<b>Ҳисоб рақами:</b>", bold), Paragraph(str(partner.get("account_number") or partner.get("account") or "—"), normal)],
            [Paragraph("<b>Давр:</b>", bold), Paragraph(f"{_fmt_date(period.get('from'))} - {_fmt_date(period.get('to'))}", normal),
             Paragraph("<b>Акт ID:</b>", bold), Paragraph(str(data.get("report_id") or "—"), normal)],
        ],
        colWidths=[28 * mm, 92 * mm, 30 * mm, 107 * mm],
    )
    info.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B9C2CC")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F3F6F9")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#F3F6F9")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story += [info, Spacer(1, 4 * mm)]

    opening_debit = float(data.get("opening_debit") or 0)
    opening_credit = float(data.get("opening_credit") or 0)
    debit_total = float(data.get("debit_total") or 0)
    credit_total = float(data.get("credit_total") or 0)
    closing_debit = float(data.get("closing_debit") or 0)
    closing_credit = float(data.get("closing_credit") or 0)
    closing_date = _fmt_date(period.get("to") or data.get("document_date"))

    rows = [
        [Paragraph("№", bold), Paragraph("Сана", bold), Paragraph("Ҳужжат", bold),
         Paragraph("Асос / мазмун", bold), Paragraph("Дебет", bold), Paragraph("Кредит", bold)],
        ["", "", Paragraph("Бошланғич сальдо", bold), "",
         Paragraph(_money(opening_debit), right), Paragraph(_money(opening_credit), right)],
    ]

    for idx, entry in enumerate(_entries(data), 1):
        rows.append([
            Paragraph(str(idx), center),
            Paragraph(_fmt_date(entry.get("date")), center),
            Paragraph(str(entry.get("document") or "—"), small),
            Paragraph(str(entry.get("basis") or "—"), small),
            Paragraph(_money(entry.get("debit")), right),
            Paragraph(_money(entry.get("credit")), right),
        ])

    rows += [
        ["", "", Paragraph("ЖАМИ АЙЛАНМА:", bold), "",
         Paragraph(_money(debit_total), right), Paragraph(_money(credit_total), right)],
        ["", "", Paragraph(f"{closing_date} ҳолатига ЯКУНИЙ САЛЬДО:", bold), "",
         Paragraph(_money(closing_debit), right), Paragraph(_money(closing_credit), right)],
    ]

    main = Table(
        rows,
        colWidths=[10 * mm, 24 * mm, 43 * mm, 126 * mm, 31 * mm, 31 * mm],
        repeatRows=1,
        hAlign="LEFT",
    )
    main.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "ReconSans"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#6E7781")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF5")),
        ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#F8FAFC")),
        ("BACKGROUND", (0, -2), (-1, -2), colors.HexColor("#FFF7D6")),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#DFF4E4")),
        ("LINEABOVE", (0, -1), (-1, -1), 1.2, colors.HexColor("#2E7D32")),
        ("LINEBELOW", (0, -1), (-1, -1), 1.2, colors.HexColor("#2E7D32")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (1, -1), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
    ]))
    story += [main, Spacer(1, 4 * mm)]

    side = "ДЕБЕТ" if closing_debit > 0 else "КРЕДИТ" if closing_credit > 0 else "0"
    amount = closing_debit if closing_debit > 0 else closing_credit
    saldo_box = Table(
        [[
            Paragraph(f"<b>{closing_date} ҲОЛАТИГА ЯКУНИЙ САЛЬДО</b>", saldo_big),
            Paragraph(f"<b>{side}</b>", saldo_big),
            Paragraph(f"<b>{_money(amount)} UZS</b>", saldo_big),
        ]],
        colWidths=[150 * mm, 35 * mm, 72 * mm],
    )
    saldo_box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#E7F6EA")),
        ("BOX", (0, 0), (-1, -1), 1.2, colors.HexColor("#2E7D32")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#8BC59A")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story += [saldo_box, Spacer(1, 3 * mm)]

    conclusion = data.get("closing_result") or ""
    if conclusion:
        story.append(Paragraph(f"<b>Натижа: {conclusion}</b>", normal))
        story.append(Spacer(1, 2 * mm))

    story.append(Paragraph(
        "Ушбу акт бот базасида сақланган банк операциялари ва тасдиқланган фактуралар асосида "
        "автоматик шакллантирилди. Имзолашдан олдин бухгалтер томонидан текшириш тавсия этилади.",
        small,
    ))

    doc.build(story)
    return buffer.getvalue()
