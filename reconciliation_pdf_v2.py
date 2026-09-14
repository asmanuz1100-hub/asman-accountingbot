from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from reconciliation_pdf import _entries, _fmt_date, _money, _register_fonts
from reconciliation_saldo import add_closing_saldo


def _saldo_palette(data):
    status = data.get("closing_status")
    if status == "debt":
        return colors.HexColor("#FDE8E8"), colors.HexColor("#C62828"), colors.HexColor("#8E0000")
    if status == "advance":
        return colors.HexColor("#E7F6EA"), colors.HexColor("#2E7D32"), colors.HexColor("#1B5E20")
    return colors.HexColor("#F1F3F5"), colors.HexColor("#6C757D"), colors.HexColor("#495057")


def _yes_no(value):
    return "BOR" if value else "YO'Q"


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
    bold = ParagraphStyle("SaldoBold", parent=normal, fontName="ReconSansBold")
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
    saldo_bg, saldo_border, saldo_text_color = _saldo_palette(data)

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
        ("BACKGROUND", (0, -1), (-1, -1), saldo_bg),
        ("LINEABOVE", (0, -1), (-1, -1), 1.2, saldo_border),
        ("LINEBELOW", (0, -1), (-1, -1), 1.2, saldo_border),
        ("TEXTCOLOR", (0, -1), (-1, -1), saldo_text_color),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (1, -1), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
    ]))
    story += [main, Spacer(1, 4 * mm)]

    side = "ДЕБЕТ" if closing_debit > 0 else "КРЕДИТ" if closing_credit > 0 else "0"
    amount = closing_debit if closing_debit > 0 else closing_credit
    status_label = {
        "debt": "ҚАРЗДОРЛИК",
        "advance": "АВАНС",
        "settled": "ҚАРЗ ЙЎҚ",
    }.get(data.get("closing_status"), "САЛЬДО")
    saldo_box = Table(
        [[
            Paragraph(f"<b>{closing_date} ҲОЛАТИГА ЯКУНИЙ САЛЬДО</b>", saldo_big),
            Paragraph(f"<b>{status_label}</b>", saldo_big),
            Paragraph(f"<b>{side}: {_money(amount)} UZS</b>", saldo_big),
        ]],
        colWidths=[130 * mm, 55 * mm, 72 * mm],
    )
    saldo_box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), saldo_bg),
        ("BOX", (0, 0), (-1, -1), 1.2, saldo_border),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, saldo_border),
        ("TEXTCOLOR", (0, 0), (-1, -1), saldo_text_color),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story += [saldo_box, Spacer(1, 4 * mm)]

    contracts = data.get("contract_summary") or []
    if contracts:
        story.append(Paragraph("<b>ШАРТНОМА НАЗОРАТИ</b>", bold))
        contract_rows = [[
            Paragraph("Шартнома", bold), Paragraph("База", bold), Paragraph("Сумма", bold),
            Paragraph("Фактура қилинган", bold), Paragraph("Қолдиқ", bold), Paragraph("Ҳолат", bold),
        ]]
        for item in contracts[:20]:
            remaining = item.get("remaining")
            status = "ЛИМИТДАН ОШГАН" if item.get("over_limit") else "ТЎҒРИ" if item.get("registered") else "БАЗАДА ЙЎҚ"
            contract_rows.append([
                Paragraph(str(item.get("number") or "—"), small),
                Paragraph("BOR" if item.get("registered") else "YO'Q", center),
                Paragraph(_money(item.get("total")), right),
                Paragraph(_money(item.get("used")), right),
                Paragraph(_money(remaining) if remaining is not None else "—", right),
                Paragraph(status, center),
            ])
        ctable = Table(contract_rows, colWidths=[55 * mm, 22 * mm, 38 * mm, 43 * mm, 38 * mm, 55 * mm], repeatRows=1)
        ctable.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#8B949E")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF5")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        for r, item in enumerate(contracts[:20], 1):
            if item.get("over_limit"):
                ctable.setStyle(TableStyle([("BACKGROUND", (0, r), (-1, r), colors.HexColor("#FDE8E8"))]))
            elif not item.get("registered"):
                ctable.setStyle(TableStyle([("BACKGROUND", (0, r), (-1, r), colors.HexColor("#FFF7D6"))]))
        story += [ctable, Spacer(1, 4 * mm)]

    chain = data.get("document_chain") or []
    if chain:
        story.append(Paragraph("<b>ҲУЖЖАТЛАР ЗАНЖИРИ НАЗОРАТИ</b>", bold))
        chain_rows = [[
            Paragraph("Фактура", bold), Paragraph("Сана", bold), Paragraph("Шартнома", bold),
            Paragraph("Ишончнома", bold), Paragraph("ТТЮ/ТТН", bold), Paragraph("Банк тўлови", bold),
        ]]
        for item in chain[:30]:
            chain_rows.append([
                Paragraph(str(item.get("invoice_number") or "—"), small),
                Paragraph(_fmt_date(item.get("date")), center),
                Paragraph((str(item.get("contract") or "—") + (" | BOR" if item.get("contract_ok") else " | YO'Q")), small),
                Paragraph(_yes_no(item.get("poa_ok")), center),
                Paragraph(_yes_no(item.get("ttn_ok")), center),
                Paragraph(_yes_no(item.get("bank_payment_found")), center),
            ])
        dtable = Table(chain_rows, colWidths=[42 * mm, 28 * mm, 75 * mm, 38 * mm, 35 * mm, 39 * mm], repeatRows=1)
        dtable.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#8B949E")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF5")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        for r, item in enumerate(chain[:30], 1):
            if not item.get("contract_ok") or not item.get("poa_ok") or not item.get("ttn_ok"):
                dtable.setStyle(TableStyle([("BACKGROUND", (0, r), (-1, r), colors.HexColor("#FFF3CD"))]))
        story += [dtable, Spacer(1, 4 * mm)]

    if flow == "outgoing":
        financial_rows = [
            ["Сотув фактуралари", _money(data.get("sales")), "Ҳамкордан тўлов", _money(data.get("payments_from_partner"))],
            ["Дебитор ҳолат", _money(data.get("receivable")), "Банк операциялари", str(data.get("bank_operations_count") or 0)],
        ]
    elif flow == "incoming":
        financial_rows = [
            ["Кирим фактуралари", _money(data.get("purchases")), "Ҳамкорга тўлов", _money(data.get("payments_to_partner"))],
            ["Кредитор ҳолат", _money(data.get("payable")), "Банк операциялари", str(data.get("bank_operations_count") or 0)],
        ]
    else:
        financial_rows = [["Дебет", _money(data.get("debit_total")), "Кредит", _money(data.get("credit_total"))]]
    story.append(Paragraph("<b>ҚИСҚА МОЛИЯВИЙ ТАҲЛИЛ</b>", bold))
    ftable = Table(financial_rows, colWidths=[65 * mm, 60 * mm, 65 * mm, 60 * mm])
    ftable.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#AAB2BB")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F3F6F9")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#F3F6F9")),
        ("FONTNAME", (0, 0), (-1, -1), "ReconSans"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story += [ftable, Spacer(1, 3 * mm)]

    conclusion = data.get("closing_result") or ""
    if conclusion:
        story.append(Paragraph(f"<b>Натижа: {conclusion}</b>", normal))
        story.append(Spacer(1, 2 * mm))

    story.append(Paragraph(
        "Ушбу акт бот базасида сақланган банк операциялари, тасдиқланган фактуралар ва мавжуд шартнома/ҳужжатлар "
        "асосида автоматик шакллантирилди. Имзолашдан олдин бухгалтер томонидан текшириш тавсия этилади.",
        small,
    ))

    doc.build(story)
    return buffer.getvalue()
