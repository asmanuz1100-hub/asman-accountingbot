from collections import defaultdict
from datetime import datetime
from io import BytesIO

from reportlab.graphics.charts.barcharts import HorizontalBarChart, VerticalBarChart
from reportlab.graphics.charts.legends import Legend
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import Contract, Document, engine
from reconciliation_pdf import _register_fonts


def _num(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _money(value):
    try:
        return f"{float(value or 0):,.0f}".replace(",", " ")
    except Exception:
        return "0"


def _month(value):
    text = str(value or "")
    return text[:7] if len(text) >= 7 and text[4:5] == "-" else None


def collect_financial_snapshot():
    import business_controls as bc

    docs = bc._source_documents()
    graph = bc._identity_graph(docs)
    groups = defaultdict(lambda: {
        "name": "—", "sales": 0.0, "purchases": 0.0, "received": 0.0, "paid": 0.0,
    })
    monthly = defaultdict(lambda: {"sales": 0.0, "purchases": 0.0, "bank_in": 0.0, "bank_out": 0.0})
    seen_bank, seen_inv = set(), set()
    sales = purchases = output_vat = input_vat = 0.0
    bank_in = bank_out = 0.0
    all_dates = []
    company_name = None

    def group_key(name, tin, account):
        root = graph.component(name, tin, account)
        if root:
            return root
        return bc._norm_tin(tin) or bc._norm_account(account) or bc._norm_name(name) or "unknown"

    all_registry_items = []

    for doc in docs:
        data = bc._read_json(doc)
        if data.get("document_type") == "bank_statement":
            company_name = company_name or data.get("account_holder")
            for tx in data.get("transactions") or data.get("transactions_preview") or []:
                incoming = round(_num(tx.get("incoming")), 2)
                outgoing = round(_num(tx.get("outgoing")), 2)
                sig = (
                    str(tx.get("date") or ""), bc._norm_tin(tx.get("counterparty_tin")),
                    bc._norm_account(tx.get("counterparty_account")), str(tx.get("document_number") or ""),
                    incoming, outgoing, str(tx.get("purpose") or "").strip(),
                )
                if sig in seen_bank:
                    continue
                seen_bank.add(sig)
                bank_in += incoming
                bank_out += outgoing
                date = str(tx.get("date") or "")
                if date:
                    all_dates.append(date)
                month = _month(date)
                if month:
                    monthly[month]["bank_in"] += incoming
                    monthly[month]["bank_out"] += outgoing
                key = group_key(tx.get("counterparty"), tx.get("counterparty_tin"), tx.get("counterparty_account"))
                g = groups[key]
                g["name"] = tx.get("counterparty") or g["name"]
                g["received"] += incoming
                g["paid"] += outgoing

        elif data.get("document_type") == "invoice_registry":
            items = list(data.get("invoices") or [])
            all_registry_items.extend(items)
            for inv in items:
                if str(inv.get("status_group") or "").lower() != "signed":
                    continue
                if not bc._is_invoice_type(inv.get("document_type_name")):
                    continue
                direction = str(inv.get("direction") or "")
                if direction not in ("incoming", "outgoing"):
                    continue
                amount = round(_num(inv.get("total")), 2)
                sig = (
                    direction, str(inv.get("document_date") or ""), str(inv.get("document_number") or ""),
                    bc._norm_tin(inv.get("counterparty_tin")), bc._norm_account(inv.get("counterparty_account")), amount,
                )
                if sig in seen_inv:
                    continue
                seen_inv.add(sig)
                date = str(inv.get("document_date") or "")
                if date:
                    all_dates.append(date)
                month = _month(date)
                key = group_key(inv.get("counterparty"), inv.get("counterparty_tin"), inv.get("counterparty_account"))
                g = groups[key]
                g["name"] = inv.get("counterparty") or g["name"]
                if direction == "outgoing":
                    sales += amount
                    output_vat += _num(inv.get("vat"))
                    g["sales"] += amount
                    if month:
                        monthly[month]["sales"] += amount
                else:
                    purchases += amount
                    input_vat += _num(inv.get("vat"))
                    g["purchases"] += amount
                    if month:
                        monthly[month]["purchases"] += amount

    debtors, creditors, advances = [], [], []
    unmatched_bank = unmatched_invoice = 0
    for g in groups.values():
        receivable = round(g["sales"] - g["received"], 2)
        payable = round(g["purchases"] - g["paid"], 2)
        if receivable > 0.01:
            debtors.append((g["name"], receivable))
        elif receivable < -0.01:
            advances.append((g["name"], abs(receivable), "Харидор аванси"))
        if payable > 0.01:
            creditors.append((g["name"], payable))
        elif payable < -0.01:
            advances.append((g["name"], abs(payable), "Етказиб берувчига аванс"))
        if (g["received"] > 0.01 and g["sales"] <= 0.01) or (g["paid"] > 0.01 and g["purchases"] <= 0.01):
            unmatched_bank += 1
        if (g["sales"] > 0.01 and g["received"] <= 0.01) or (g["purchases"] > 0.01 and g["paid"] <= 0.01):
            unmatched_invoice += 1

    debtors.sort(key=lambda x: x[1], reverse=True)
    creditors.sort(key=lambda x: x[1], reverse=True)
    advances.sort(key=lambda x: x[1], reverse=True)

    expense_totals = defaultdict(float)
    with Session(engine) as session:
        expense_docs = list(session.scalars(select(Document).where(Document.document_type == "expense_entry")).all())
        contracts = list(session.scalars(select(Contract).order_by(Contract.id)).all())
    seen_exp = set()
    for doc in expense_docs:
        data = bc._read_json(doc)
        sig = data.get("source_signature") or f"id:{doc.id}"
        if sig in seen_exp:
            continue
        seen_exp.add(sig)
        expense_totals[data.get("expense_category") or "other"] += _num(data.get("total"))

    contract_total = round(sum(_num(c.total_amount) for c in contracts), 2)
    contract_used = round(sum(_num(c.used_amount) for c in contracts), 2)
    contract_remaining = round(contract_total - contract_used, 2)
    over_limit_contracts = sum(1 for c in contracts if _num(c.used_amount) - _num(c.total_amount) > 0.01 and _num(c.total_amount) > 0)

    signed_invoices = [
        x for x in all_registry_items
        if str(x.get("status_group") or "").lower() == "signed" and bc._is_invoice_type(x.get("document_type_name"))
    ]
    missing_contract = sum(1 for x in signed_invoices if not str(x.get("contract") or "").strip())
    missing_poa = 0
    missing_ttn = 0
    for inv in signed_invoices:
        contract = bc._norm_name(inv.get("contract"))
        target_key = group_key(inv.get("counterparty"), inv.get("counterparty_tin"), inv.get("counterparty_account"))
        related_kinds = set()
        for item in all_registry_items:
            if contract and bc._norm_name(item.get("contract")) != contract:
                continue
            item_key = group_key(item.get("counterparty"), item.get("counterparty_tin"), item.get("counterparty_account"))
            if item_key != target_key:
                continue
            related_kinds.add(bc._doc_kind(item.get("document_type_name")))
        if "poa" not in related_kinds:
            missing_poa += 1
        if "ttn" not in related_kinds:
            missing_ttn += 1

    months = sorted(monthly.keys())[-12:]
    period_from = min(all_dates) if all_dates else None
    period_to = max(all_dates) if all_dates else None

    return {
        "company_name": company_name or "БУХГАЛТЕРИЯ ВА САВДО AI",
        "generated_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "period_from": period_from,
        "period_to": period_to,
        "sales": round(sales, 2),
        "purchases": round(purchases, 2),
        "bank_in": round(bank_in, 2),
        "bank_out": round(bank_out, 2),
        "receivable": round(sum(x[1] for x in debtors), 2),
        "payable": round(sum(x[1] for x in creditors), 2),
        "advances": round(sum(x[1] for x in advances), 2),
        "output_vat": round(output_vat, 2),
        "input_vat": round(input_vat, 2),
        "vat_difference": round(output_vat - input_vat, 2),
        "contract_total": contract_total,
        "contract_used": contract_used,
        "contract_remaining": contract_remaining,
        "over_limit_contracts": over_limit_contracts,
        "expenses": {
            "raw_material": round(expense_totals["raw_material"], 2),
            "utilities": round(expense_totals["utilities"], 2),
            "tax": round(expense_totals["tax"], 2),
            "other": round(expense_totals["other"], 2),
        },
        "monthly": [{"month": m, **{k: round(v, 2) for k, v in monthly[m].items()}} for m in months],
        "debtors": debtors[:10],
        "creditors": creditors[:10],
        "advance_rows": advances[:10],
        "unmatched_bank": unmatched_bank,
        "unmatched_invoice": unmatched_invoice,
        "missing_contract": missing_contract,
        "missing_poa": missing_poa,
        "missing_ttn": missing_ttn,
        "source_documents": len(docs),
    }


def _kpi_cell(title, value, style, bg):
    t = Table([
        [Paragraph(title, style["label"])],
        [Paragraph(value, style["value"])],
    ], colWidths=[43 * mm], rowHeights=[8 * mm, 13 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#D8DEE8")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
    ]))
    return t


def _monthly_chart(snapshot):
    rows = snapshot.get("monthly") or []
    drawing = Drawing(500, 220)
    drawing.add(String(12, 202, "Ойлар кесимида сотув ва харид (млн UZS)", fontName="ReconSansBold", fontSize=11))
    if not rows:
        drawing.add(String(12, 110, "Диаграмма учун маълумот йўқ", fontName="ReconSans", fontSize=10))
        return drawing

    chart = VerticalBarChart()
    chart.x = 45
    chart.y = 35
    chart.height = 145
    chart.width = 425
    chart.data = [
        [r["sales"] / 1_000_000 for r in rows],
        [r["purchases"] / 1_000_000 for r in rows],
    ]
    chart.categoryAxis.categoryNames = [r["month"] for r in rows]
    chart.categoryAxis.labels.fontName = "ReconSans"
    chart.categoryAxis.labels.fontSize = 7
    chart.categoryAxis.labels.angle = 25
    chart.categoryAxis.labels.dy = -12
    chart.valueAxis.labels.fontName = "ReconSans"
    chart.valueAxis.labels.fontSize = 7
    chart.valueAxis.valueMin = 0
    chart.bars[0].fillColor = colors.HexColor("#2E7D32")
    chart.bars[1].fillColor = colors.HexColor("#1976D2")
    chart.groupSpacing = 8
    chart.barSpacing = 2
    drawing.add(chart)

    legend = Legend()
    legend.x = 300
    legend.y = 196
    legend.fontName = "ReconSans"
    legend.fontSize = 8
    legend.colorNamePairs = [
        (colors.HexColor("#2E7D32"), "Сотув"),
        (colors.HexColor("#1976D2"), "Харид"),
    ]
    drawing.add(legend)
    return drawing


def _expense_pie(snapshot):
    expenses = snapshot.get("expenses") or {}
    labels = ["Хом ашё", "Коммунал", "Солиқ", "Бошқа"]
    values = [expenses.get("raw_material", 0), expenses.get("utilities", 0), expenses.get("tax", 0), expenses.get("other", 0)]
    drawing = Drawing(500, 210)
    drawing.add(String(12, 190, "Харажатлар таркиби", fontName="ReconSansBold", fontSize=11))
    if sum(values) <= 0:
        drawing.add(String(12, 100, "Харажат маълумоти йўқ", fontName="ReconSans", fontSize=10))
        return drawing

    pie = Pie()
    pie.x = 30
    pie.y = 25
    pie.width = 145
    pie.height = 145
    pie.data = values
    pie.labels = [f"{x}: {v / 1_000_000:.1f}м" for x, v in zip(labels, values)]
    pie.slices.fontName = "ReconSans"
    pie.slices.fontSize = 7
    palette = ["#455A64", "#F9A825", "#C62828", "#7E57C2"]
    for idx, color in enumerate(palette):
        pie.slices[idx].fillColor = colors.HexColor(color)
    drawing.add(pie)

    bank = VerticalBarChart()
    bank.x = 280
    bank.y = 38
    bank.height = 125
    bank.width = 160
    bank.data = [[snapshot.get("bank_in", 0) / 1_000_000, snapshot.get("bank_out", 0) / 1_000_000]]
    bank.categoryAxis.categoryNames = ["Кирим", "Чиқим"]
    bank.categoryAxis.labels.fontName = "ReconSans"
    bank.categoryAxis.labels.fontSize = 8
    bank.valueAxis.labels.fontName = "ReconSans"
    bank.valueAxis.labels.fontSize = 7
    bank.valueAxis.valueMin = 0
    bank.bars[0].fillColor = colors.HexColor("#00897B")
    drawing.add(bank)
    drawing.add(String(290, 170, "Банк ҳаракати (млн UZS)", fontName="ReconSansBold", fontSize=9))
    return drawing


def _debt_chart(snapshot):
    items = snapshot.get("debtors") or []
    drawing = Drawing(500, 220)
    drawing.add(String(12, 202, "Энг катта дебиторлар (млн UZS)", fontName="ReconSansBold", fontSize=11))
    if not items:
        drawing.add(String(12, 110, "Дебитор қарз аниқланмади", fontName="ReconSans", fontSize=10))
        return drawing

    top = items[:6][::-1]
    chart = HorizontalBarChart()
    chart.x = 155
    chart.y = 35
    chart.width = 310
    chart.height = 145
    chart.data = [[amount / 1_000_000 for _, amount in top]]
    chart.categoryAxis.categoryNames = [str(name)[:23] for name, _ in top]
    chart.categoryAxis.labels.fontName = "ReconSans"
    chart.categoryAxis.labels.fontSize = 7
    chart.valueAxis.labels.fontName = "ReconSans"
    chart.valueAxis.labels.fontSize = 7
    chart.valueAxis.valueMin = 0
    chart.bars[0].fillColor = colors.HexColor("#C62828")
    drawing.add(chart)
    return drawing


def build_financial_report_pdf(snapshot=None):
    snapshot = snapshot or collect_financial_snapshot()
    _register_fonts()

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        title="Молиявий таҳлил",
        author="Бухгалтерия ва савдо AI",
    )
    styles = getSampleStyleSheet()
    title = ParagraphStyle("FinTitle", parent=styles["Title"], fontName="ReconSansBold", fontSize=18, leading=22, textColor=colors.HexColor("#17324D"), alignment=TA_LEFT)
    subtitle = ParagraphStyle("FinSub", parent=styles["Normal"], fontName="ReconSans", fontSize=8.5, leading=11, textColor=colors.HexColor("#607D8B"))
    section = ParagraphStyle("FinSection", parent=styles["Heading2"], fontName="ReconSansBold", fontSize=12, leading=15, textColor=colors.HexColor("#17324D"), spaceBefore=5, spaceAfter=5)
    normal = ParagraphStyle("FinNormal", parent=styles["Normal"], fontName="ReconSans", fontSize=8.5, leading=11)
    right = ParagraphStyle("FinRight", parent=normal, alignment=TA_RIGHT)
    label = ParagraphStyle("KpiLabel", parent=normal, fontSize=7.2, leading=9, textColor=colors.HexColor("#52606D"), alignment=TA_CENTER)
    value = ParagraphStyle("KpiValue", parent=normal, fontName="ReconSansBold", fontSize=10.2, leading=12, textColor=colors.HexColor("#102A43"), alignment=TA_CENTER)
    kpi_styles = {"label": label, "value": value}

    period = f"{snapshot.get('period_from') or '—'} — {snapshot.get('period_to') or '—'}"
    story = [
        Paragraph("МОЛИЯВИЙ ТАҲЛИЛ", title),
        Paragraph(str(snapshot.get("company_name") or ""), subtitle),
        Paragraph(f"Давр: {period}   |   Тайёрланган: {snapshot.get('generated_at')}", subtitle),
        Spacer(1, 5 * mm),
    ]

    kpis = [
        ("Сотув", f"{_money(snapshot['sales'])} UZS", colors.HexColor("#E8F5E9")),
        ("Харид", f"{_money(snapshot['purchases'])} UZS", colors.HexColor("#E3F2FD")),
        ("Дебитор қарз", f"{_money(snapshot['receivable'])} UZS", colors.HexColor("#FFEBEE")),
        ("Кредитор қарз", f"{_money(snapshot['payable'])} UZS", colors.HexColor("#FFEBEE")),
        ("Аванслар", f"{_money(snapshot['advances'])} UZS", colors.HexColor("#E8F5E9")),
        ("Банк кирими", f"{_money(snapshot['bank_in'])} UZS", colors.HexColor("#E0F2F1")),
        ("Банк чиқими", f"{_money(snapshot['bank_out'])} UZS", colors.HexColor("#FFF8E1")),
        ("Шартнома қолдиғи", f"{_money(snapshot['contract_remaining'])} UZS", colors.HexColor("#F3E5F5")),
    ]
    kpi_table = Table([
        [_kpi_cell(*kpis[0], kpi_styles), _kpi_cell(*kpis[1], kpi_styles), _kpi_cell(*kpis[2], kpi_styles), _kpi_cell(*kpis[3], kpi_styles)],
        [_kpi_cell(*kpis[4], kpi_styles), _kpi_cell(*kpis[5], kpi_styles), _kpi_cell(*kpis[6], kpi_styles), _kpi_cell(*kpis[7], kpi_styles)],
    ], colWidths=[44.5 * mm] * 4, rowHeights=[23 * mm, 23 * mm])
    kpi_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [kpi_table, Spacer(1, 5 * mm), _monthly_chart(snapshot), Spacer(1, 2 * mm), _expense_pie(snapshot), PageBreak()]

    story += [Paragraph("ҚАРЗДОРЛИК ВА АВАНС НАЗОРАТИ", section), _debt_chart(snapshot), Spacer(1, 3 * mm)]

    debt_rows = [[Paragraph("Ҳамкор", normal), Paragraph("Дебитор", right), Paragraph("Кредитор", right), Paragraph("Аванс", right)]]
    debt_map = defaultdict(lambda: [0.0, 0.0, 0.0])
    for name, amount in snapshot.get("debtors") or []:
        debt_map[name][0] += amount
    for name, amount in snapshot.get("creditors") or []:
        debt_map[name][1] += amount
    for name, amount, _kind in snapshot.get("advance_rows") or []:
        debt_map[name][2] += amount
    for name, vals in sorted(debt_map.items(), key=lambda kv: sum(kv[1]), reverse=True)[:12]:
        debt_rows.append([
            Paragraph(str(name), normal),
            Paragraph(_money(vals[0]) if vals[0] else "—", right),
            Paragraph(_money(vals[1]) if vals[1] else "—", right),
            Paragraph(_money(vals[2]) if vals[2] else "—", right),
        ])
    if len(debt_rows) == 1:
        debt_rows.append([Paragraph("Қарздорлик ёки аванс топилмади", normal), "—", "—", "—"])
    debt_table = Table(debt_rows, colWidths=[80 * mm, 34 * mm, 34 * mm, 34 * mm], repeatRows=1)
    debt_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF5")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CBD5E1")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TEXTCOLOR", (1, 1), (2, -1), colors.HexColor("#C62828")),
        ("TEXTCOLOR", (3, 1), (3, -1), colors.HexColor("#2E7D32")),
    ]))
    story += [debt_table, Spacer(1, 5 * mm)]

    story += [Paragraph("ҲУЖЖАТЛАР ВА РИСК НАЗОРАТИ", section)]
    risk_rows = [
        ["Фактураси топилмаган банк гуруҳлари", snapshot.get("unmatched_bank", 0), "Текшириш керак"],
        ["Тўлови топилмаган фактура гуруҳлари", snapshot.get("unmatched_invoice", 0), "Текшириш керак"],
        ["Шартномасиз имзоланган фактуралар", snapshot.get("missing_contract", 0), "Ҳужжат етишмайди"],
        ["Ишончнома топилмаган фактуралар", snapshot.get("missing_poa", 0), "Ҳужжат етишмайди"],
        ["ТТЮ/ТТН топилмаган фактуралар", snapshot.get("missing_ttn", 0), "Ҳужжат етишмайди"],
        ["Лимитдан ошган шартномалар", snapshot.get("over_limit_contracts", 0), "Шартнома назорати"],
    ]
    risk_table = Table(
        [[Paragraph("Назорат", normal), Paragraph("Сони", right), Paragraph("Ҳолат", normal)]]
        + [[Paragraph(str(a), normal), Paragraph(str(b), right), Paragraph(str(c), normal)] for a, b, c in risk_rows],
        colWidths=[105 * mm, 25 * mm, 52 * mm],
    )
    risk_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#FFF3E0")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D8DEE8")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [risk_table, Spacer(1, 5 * mm)]

    vat = Table([
        [Paragraph("Чиқим ҚҚС", label), Paragraph("Кирим ҚҚС", label), Paragraph("ҚҚС фарқи", label)],
        [Paragraph(_money(snapshot.get("output_vat")), value), Paragraph(_money(snapshot.get("input_vat")), value), Paragraph(_money(snapshot.get("vat_difference")), value)],
    ], colWidths=[60 * mm] * 3)
    vat.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CBD5E1")),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [vat, Spacer(1, 5 * mm)]

    story.append(Paragraph(
        "Изоҳ: ҳисобот фақат бот базасига фойдаланувчи томонидан тасдиқланган банк выпискалари, "
        "ҳисоб-фактуралар, шартномалар ва харажатлар асосида шакллантирилади. "
        "Ишлаб чиқариш таннархи тўлиқ юритилмаган бўлса, бу ҳисобот соф фойда ҳисоботи ҳисобланмайди.",
        subtitle,
    ))

    doc.build(story)
    return buffer.getvalue()

# Public export uses the same currency-separated read model as the bot.
from report_design import build_financial_report_pdf
from ledger import Ledger
collect_financial_snapshot = lambda: Ledger().snapshot()
