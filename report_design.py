"""ASMAN print system: embedded Cyrillic fonts, currency-separated reports."""
from datetime import datetime
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether, Flowable
from ledger import money, amount, ZERO

NAVY=colors.HexColor('#142C49'); BLUE=colors.HexColor('#246BCE'); TEAL=colors.HexColor('#158779')
INK=colors.HexColor('#20354B'); MUTED=colors.HexColor('#66798C'); PALE=colors.HexColor('#F0F5FA')
LINE=colors.HexColor('#DCE5EE'); RED=colors.HexColor('#B54442'); GOLD=colors.HexColor('#C68A25')


def fonts():
    root=Path(__file__).parent/'fonts'
    for name,file in [('Asman','DejaVuSans.ttf'),('AsmanBold','DejaVuSans-Bold.ttf')]:
        if name not in pdfmetrics.getRegisteredFontNames():pdfmetrics.registerFont(TTFont(name,str(root/file)))
    pdfmetrics.registerFontFamily('Asman',normal='Asman',bold='AsmanBold',italic='Asman',boldItalic='AsmanBold')


def styles():
    fonts()
    return {
        'title':ParagraphStyle('title',fontName='AsmanBold',fontSize=24,leading=30,textColor=NAVY,spaceAfter=9),
        'subtitle':ParagraphStyle('subtitle',fontName='Asman',fontSize=9,leading=14,textColor=MUTED,spaceAfter=10),
        'section':ParagraphStyle('section',fontName='AsmanBold',fontSize=11,leading=16,textColor=NAVY,spaceBefore=12,spaceAfter=8,keepWithNext=True),
        'body':ParagraphStyle('body',fontName='Asman',fontSize=9,leading=14,textColor=INK),
        'small':ParagraphStyle('small',fontName='Asman',fontSize=7.5,leading=11,textColor=MUTED),
        'cell':ParagraphStyle('cell',fontName='Asman',fontSize=8,leading=11,textColor=INK,wordWrap='CJK'),
        'right':ParagraphStyle('right',fontName='Asman',fontSize=8,leading=11,textColor=INK,alignment=TA_RIGHT),
        'head':ParagraphStyle('head',fontName='AsmanBold',fontSize=8,leading=11,textColor=colors.white),
        'label':ParagraphStyle('label',fontName='Asman',fontSize=8,leading=12,textColor=MUTED),
        'value':ParagraphStyle('value',fontName='AsmanBold',fontSize=17,leading=23,textColor=NAVY),
    }


def para(value,style):
    return Paragraph(escape(str(value if value is not None and value!='' else '-')).replace('\n','<br/>'),style)


def fmt_date(value):
    try:return datetime.strptime(str(value),'%Y-%m-%d').strftime('%d.%m.%Y')
    except ValueError:return str(value or '-')


def footer(canvas,doc):
    width,height=doc.pagesize
    canvas.saveState()
    canvas.setFillColor(NAVY);canvas.rect(0,height-8*mm,width,8*mm,fill=1,stroke=0)
    canvas.setFillColor(TEAL);canvas.rect(14*mm,height-8*mm,26*mm,1.3*mm,fill=1,stroke=0)
    canvas.setFont('AsmanBold',9);canvas.setFillColor(NAVY);canvas.drawString(14*mm,height-16*mm,'ASMAN')
    canvas.setFont('Asman',7);canvas.setFillColor(MUTED);canvas.drawRightString(width-14*mm,height-16*mm,'ҲИСОБ ВА НАЗОРАТ')
    canvas.setStrokeColor(LINE);canvas.line(14*mm,14*mm,width-14*mm,14*mm)
    canvas.setFont('Asman',7);canvas.drawString(14*mm,9.5*mm,'ASMAN SILICAT  /  '+getattr(doc,'report_label','Ҳисобот'))
    canvas.drawRightString(width-14*mm,9.5*mm,f'{doc.page}')
    canvas.restoreState()


def build(story,pagesize,label):
    buffer=BytesIO()
    doc=SimpleDocTemplate(buffer,pagesize=pagesize,rightMargin=14*mm,leftMargin=14*mm,topMargin=25*mm,bottomMargin=20*mm,title=label,author='ASMAN SILICAT')
    doc.report_label=label
    doc.build(story,onFirstPage=footer,onLaterPages=footer)
    return buffer.getvalue()


def table(headers,rows,widths,s,number_columns=()):
    values=[[para(h,s['head']) for h in headers]]
    for row in rows:
        values.append([para(v,s['right'] if i in number_columns else s['cell']) for i,v in enumerate(row)])
    if len(values)==1:values.append([para('Маълумот йўқ' if i==0 else '',s['small']) for i in range(len(headers))])
    padding = 3 if sum(widths)>700 else 7
    t=Table(values,colWidths=widths,repeatRows=1,hAlign='LEFT',splitByRow=1,splitInRow=1)
    t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),NAVY),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,PALE]),
        ('LINEBELOW',(0,0),(-1,0),.8,TEAL),('LINEBELOW',(0,1),(-1,-1),.3,LINE),('VALIGN',(0,0),(-1,-1),'TOP'),
        ('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),padding),('BOTTOMPADDING',(0,0),(-1,-1),padding)]))
    return t


def cards(items,width,s):
    gap=4*mm;cw=(width-gap*(len(items)-1))/len(items)
    cells=[];widths=[]
    for i,(label,value) in enumerate(items):
        if i:cells.append('');widths.append(gap)
        cell=Table([[para(label,s['label'])],[para(value,s['value'])]],colWidths=[cw])
        cell.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),PALE),('LINEABOVE',(0,0),(-1,0),2,TEAL if i==0 else BLUE),('LEFTPADDING',(0,0),(-1,-1),11),('RIGHTPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,0),9),('BOTTOMPADDING',(0,-1),(-1,-1),10)]))
        cells.append(cell);widths.append(cw)
    outer=Table([cells],colWidths=widths)
    outer.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),0)]))
    return outer


class MonthlyChart(Flowable):
    def __init__(self,rows,width):
        super().__init__();self.rows=rows[-12:];self.width=width;self.height=58*mm
    def draw(self):
        c=self.canv
        if not self.rows:
            c.setFont('Asman',9);c.setFillColor(MUTED);c.drawString(0,70,'Диаграмма учун маълумот йўқ');return
        maximum=max(float(r[k]) for r in self.rows for k in ('sales','purchases')) or 1
        plot_h=110;left=44;bottom=30;plot_w=self.width-left-8;step=plot_w/len(self.rows);bw=min(14,step*.26)
        c.setFont('Asman',7)
        for tick in range(4):
            y=bottom+plot_h*tick/3;c.setStrokeColor(LINE);c.line(left,y,self.width,y)
            c.setFillColor(MUTED);c.drawRightString(left-6,y-2,f'{maximum*tick/3/1000000:,.1f}')
        for i,row in enumerate(self.rows):
            x=left+step*(i+.5)
            for offset,key,color in [(-bw-1,'sales',BLUE),(1,'purchases',TEAL)]:
                h=plot_h*float(row[key])/maximum
                if h>0:
                    c.setFillColor(color);c.rect(x+offset,bottom,bw,h,fill=1,stroke=0)
            c.setFillColor(MUTED);c.setFont('Asman',6.5);c.drawCentredString(x,bottom-13,row['month'][2:])
        c.setFont('Asman',7);c.setFillColor(MUTED);c.drawString(0,self.height-7,'млн')
        for x,label,color in [(left,'Сотув',BLUE),(left+80,'Харид',TEAL)]:
            c.setFillColor(color);c.rect(x,2,7,7,fill=1,stroke=0);c.setFillColor(INK);c.drawString(x+12,2,label)


def build_reconciliation_pdf(data):
    s=styles();s['section'].keepWithNext=False; s['title'].fontSize=21; s['title'].leading=25; s['subtitle'].leading=11; s['body'].fontSize=8; s['body'].leading=11; s['value'].fontSize=15; s['value'].leading=19; width=269*mm;story=[]
    sections=data.get('sections') or [data]
    party=data.get('partner') or {};own=data.get('own_company') or {}
    for idx,section in enumerate(sections):
        if idx:story.append(PageBreak())
        cur=section.get('currency') or data.get('currency') or 'UZS'
        period=section.get('statement_period') or data.get('statement_period') or {}
        flow=data.get('flow');title='Сотув бўйича' if flow=='outgoing' else 'Харид бўйича' if flow=='incoming' else 'Ўзаро ҳисоб-китоб'
        story += [para('АКТ СВЕРКА',s['title']),para(f"{title}  /  {fmt_date(period.get('from'))} - {fmt_date(period.get('to'))}  /  {cur}  /  Акт №{data.get('report_id') or '-'}",s['subtitle'])]
        metadata=[[f"Корхона: {own.get('name') or 'ASMAN SILICAT'}",f"Ҳамкор: {party.get('name') or '-'}"],[f"СТИР: {own.get('tin') or '-'}",f"СТИР: {party.get('tin') or '-'}"],[f"Ҳисоб рақами: {own.get('account') or '-'}",f"Ҳисоб рақами: {party.get('account_number') or party.get('account') or '-'}"]]
        meta=Table([[para(x,s['body']) for x in row] for row in metadata],colWidths=[width/2]*2)
        meta.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),0),('BOTTOMPADDING',(0,0),(-1,-1),5)]))
        story += [meta,Spacer(1,4*mm),cards([('ДЕБЕТ АЙЛАНМАСИ',money(section.get('debit_total'))),('КРЕДИТ АЙЛАНМАСИ',money(section.get('credit_total'))),('ЯКУНИЙ САЛЬДО',money((amount(section.get('closing_debit')) or ZERO)-(amount(section.get('closing_credit')) or ZERO)))],width,s)]
        story.append(para('01  /  ОПЕРАЦИЯЛАР РЎЙХАТИ',s['section']))
        entries=section.get('entries') or []
        running=(amount(section.get('opening_debit')) or ZERO)-(amount(section.get('opening_credit')) or ZERO)
        opening_label='Бошланғич қолдиқ' if section.get('opening_set') else 'Бошланғич қолдиқ киритилмаган; ҳисоб 0 дан'
        rows=[['-',opening_label,'',money(section.get('opening_debit',0)),money(section.get('opening_credit',0)),money(running)]]
        for e in entries:
            running+=(amount(e.get('debit')) or ZERO)-(amount(e.get('credit')) or ZERO)
            rows.append([fmt_date(e.get('date')),e.get('document'),e.get('basis') or '-',money(e.get('debit')),money(e.get('credit')),money(running)])
        rows.append(['','Жами айланма','',money(section.get('debit_total')),money(section.get('credit_total')),money(running)])
        story += [table(['Сана','Ҳужжат','Асос / тўлов мақсади',f'Дебет · {cur}',f'Кредит · {cur}','Сальдо'],rows,[23*mm,48*mm,99*mm,33*mm,33*mm,33*mm],s,(3,4,5))]
        net=(amount(section.get('closing_debit')) or ZERO)-(amount(section.get('closing_credit')) or ZERO)
        conclusion=('Ҳамкорнинг корхона олдидаги қарзи' if net>0 else 'Корхонанинг ҳамкор олдидаги мажбурияти' if net<0 else 'Ўзаро қолдиқ нол')
        story.append(Spacer(1,3*mm));story.append(para(f'{conclusion}: {money(abs(net))} {cur}. Мусбат сальдо - дебет; манфий сальдо - кредит.',s['body']))
        if not section.get('opening_set'):
            story.append(para('Бошланғич қарз киритилмаган: бу натижа фақат юкланган ҳужжатлар ҳаракатини акс эттиради.',s['small']))
        allocations=section.get('allocations') or []
        if allocations:
            story.append(para('02  /  ФАКТУРА ВА ТЎЛОВЛАР ТАҚСИМОТИ',s['section']))
            methods={'manual':'Қўлда тасдиқланган','reference':'Фактура рақами бўйича','contract_fifo':'Шартнома бўйича FIFO','fifo':'Ҳамкор бўйича FIFO (тахминий)'}
            rows=[[a.get('invoice_number') or a['invoice_uid'],a.get('bank_number') or a['bank_uid'],money(a['amount']),methods.get(a['method'],a['method'])] for a in allocations]
            story.append(table(['Фактура','Банк тўлови',f'Тақсимланган · {cur}','Боғлаш усули'],rows,[60*mm,55*mm,45*mm,109*mm],s,(2,)))
            story.append(Spacer(1,2*mm));story.append(para('FIFO: аввалги фактурадан бошлаб тақсимлаш. Аниқ рақам бўлмаса бу тақсимот тахминий; умумий қарзни ўзгартирмайди.',s['small']))
        if data.get('issues'):
            story.append(para('03  /  ТЕКШИРИШ КЕРАК',s['section']))
            warnings=[i for i in data['issues'] if i.get('currency')==cur]
            story.append(table(['Қатор','Ҳужжат','Сабаб'],[[i['uid'],i.get('number') or '-',i['reason']] for i in warnings],[35*mm,40*mm,194*mm],s))
        sign=Table([[para('Корхона вакили',s['small']),para('Ҳамкор вакили',s['small'])],[para('Ф.И.Ш. __________________   Имзо __________',s['body']),para('Ф.И.Ш. __________________   Имзо __________',s['body'])]],colWidths=[width/2]*2)
        sign.setStyle(TableStyle([('LEFTPADDING',(0,0),(-1,-1),0),('TOPPADDING',(0,0),(-1,-1),8)]))
        story += [Spacer(1,5*mm),KeepTogether([sign,Spacer(1,3*mm),para('Акт сақланган манба маълумотлари асосида тайёрланди. Текширув натижалари ва реквизитлар имзолашдан олдин солиштирилади.',s['small'])])]
    return build(story,landscape(A4),'Акт сверка')


def issue_table(issues,s,width):
    severity={'error':'Хато','warning':'Текшириш','info':'Маълумот'}
    return table(['Қатор / манба','Ҳамкор / ҳужжат','Сабаб'],[[f"{i['uid']}\n{str(i.get('filename') or '-')}\n{severity.get(i['severity'],'')}",f"{i.get('name') or '-'}\n№{i.get('number') or '-'}\n{money(i.get('amount'))} {i.get('currency') or ''}",i['reason']] for i in issues],[width*.25,width*.30,width*.45],s)


def build_financial_report_pdf(snapshot=None):
    if snapshot is None:
        from ledger import Ledger
        snapshot=Ledger().snapshot()
    s=styles();width=182*mm;story=[]
    currencies=snapshot.get('currencies',{})
    if not currencies:
        story=[para('МОЛИЯВИЙ ТАҲЛИЛ',s['title']),para('Ҳисобга олинадиган маълумот ҳали йўқ.',s['body'])]
    for idx,(cur,values) in enumerate(currencies.items()):
        if idx:story.append(PageBreak())
        story += [para('МОЛИЯВИЙ\nТАҲЛИЛ',s['title']),para(f"{snapshot['company_name']}\n{fmt_date(snapshot.get('period_from'))} - {fmt_date(snapshot.get('period_to'))}  /  {cur}",s['subtitle'])]
        story += [cards([('СОТУВ ФАКТУРАЛАРИ',money(values['sales'])),('ХАРИД ФАКТУРАЛАРИ',money(values['purchases']))],width,s),Spacer(1,3*mm),cards([('БАНК КИРИМИ',money(values['bank_in'])),('БАНК ЧИҚИМИ',money(values['bank_out']))],width,s)]
        story.append(para('01  /  ҚАРЗ ВА АВАНСЛАР',s['section']))
        story.append(table(['Кўрсаткич',f'Сумма · {cur}'],[['Дебитор қарз',money(values['receivable'])],['Кредитор қарз',money(values['payable'])],['Аванслар',money(values['advances'])]],[112*mm,70*mm],s,(1,)))
        story.append(para('Юкланган давр ҳаракатлари асосида. Бошланғич қолдиқлар қўшилмаган. Фактурасиз тушум аванс ёки бошқа операция бўлиши мумкин.',s['small']))
        story.append(para('02  /  ОЙЛАР БЎЙИЧА САВДО',s['section']))
        story.append(MonthlyChart(values.get('monthly') or [],width))
        story.append(para('Валюталар алоҳида кўрсатилади. Диаграммада охирги 12 ой; батафсил жадвалда барча ойлар.',s['small']))
        story.append(PageBreak())
        story += [para('ҲИСОБ ТАФСИЛОТЛАРИ',s['title']),para(cur+'  /  Ҳамкорлар ва ҳужжатлар',s['subtitle'])]
        for title,key,third in [('03  /  ДЕБИТОРЛАР','debtors',False),('04  /  КРЕДИТОРЛАР','creditors',False),('05  /  АВАНСЛАР','advance_rows',True)]:
            story.append(para(title,s['section']))
            rows=values.get(key,[])
            if third:
                story.append(table(['Ҳамкор','Тури',f'Сумма · {cur}'],[[r[0],r[2],money(r[1])] for r in rows],[82*mm,55*mm,45*mm],s,(2,)))
            else:
                story.append(table(['Ҳамкор',f'Сумма · {cur}'],[[r[0],money(r[1])] for r in rows],[127*mm,55*mm],s,(1,)))
        story.append(para('06  /  ОЙЛИК АЙЛАНМАЛАР',s['section']))
        story.append(table(['Ой','Сотув','Харид','Банк кирими','Банк чиқими'],[[r['month'],money(r['sales']),money(r['purchases']),money(r['bank_in']),money(r['bank_out'])] for r in values.get('monthly',[])],[26*mm,39*mm,39*mm,39*mm,39*mm],s,(1,2,3,4)))
        story.append(para('07  /  ҲУЖЖАТЛАРДАГИ ҚҚС',s['section']))
        story.append(table(['Чиқувчи ҚҚС','Кирувчи ҚҚС','Арифметик фарқ'],[[money(values['output_vat']),money(values['input_vat']),money(values['vat_difference'])]],[61*mm,61*mm,60*mm],s,(0,1,2)))
        story.append(para('Бу ҳужжат суммаларининг фарқи; солиқ декларацияси ёки тўланадиган солиқ ҳисоб-китоби эмас.',s['small']))
        if values.get('cash_out_categories'):
            story.append(para('08  /  БАНК ЧИҚИМЛАРИ ТАРКИБИ',s['section']))
            labels={'raw_material':'Хом ашё','utilities':'Коммунал','tax':'Солиқ','other':'Бошқа'}
            story.append(table(['Тоифа',f'Сумма · {cur}'],[[labels.get(k,k),money(v)] for k,v in values['cash_out_categories'].items()],[127*mm,55*mm],s,(1,)))
            story.append(para('Тоифа тўлов мақсади бўйича тахминий аниқланади. Бу банк пул ҳаракати, маҳсулот таннархи эмас.',s['small']))
        if values.get('contracts'):
            story.append(para('09  /  ШАРТНОМАЛАР НАЗОРАТИ',s['section']))
            story.append(table(['Ҳамкор / шартнома','Жами','Ишлатилган','Қолдиқ'],[[r['partner']+' / '+r['number'],money(r['total']),money(r['used']),money(r['remaining'])] for r in values['contracts']],[65*mm,39*mm,39*mm,39*mm],s,(1,2,3)))
            if any(r['ambiguous'] for r in values['contracts']):
                story.append(para('«-» белгиси: бир хил рақамда харид ва сотув фактуралари бор; йўналиш аниқланмаган.',s['small']))
    issues=[i for i in snapshot.get('issues',[]) if i['severity']!='info']
    if issues:
        story += [PageBreak(),para('ҲУЖЖАТ НАЗОРАТИ',s['title']),para(f"Текшириладиган ёзувлар: {len(issues)}. Тузатишлар ботдаги «Топилмаган / хатоли» бўлимида бажарилади.",s['subtitle']),issue_table(issues,s,width)]
    story += [Spacer(1,4*mm),para(f"Манба файллар: {snapshot.get('source_documents',0)}  |  Ҳисобдаги ёзувлар: {snapshot.get('active_records',0)}  |  Тайёрланган: {snapshot.get('generated_at','-')}",s['small'])]
    return build(story,A4,'Молиявий таҳлил')


def build_issues_pdf(snapshot):
    s=styles();issues=snapshot.get('issues',[])
    story=[para('ТЕКШИРУВ РЎЙХАТИ',s['title']),para('Фактуралар, банк операциялари ва импорт хатолари. Қатор ID’си орқали ботда тузатиш мумкин.',s['subtitle']),issue_table(issues,s,182*mm)]
    return build(story,A4,'Ҳужжат назорати')
