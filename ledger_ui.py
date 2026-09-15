"""Review queue with persistent, attributable corrections and explicit confirmation."""
import secrets
from io import BytesIO
from telegram import ReplyKeyboardMarkup, InputFile
from ledger import Ledger, serial, money, save_adjustment, row_version

LABEL='🔎 Топилмаган / хатоли'
FILTERS={'❗ Хатоли ёзувлар':'errors','🧾 Тўловсиз фактуралар':'invoice','🏦 Фактурасиз тўловлар':'bank','📋 Барча ёзувлар':'all'}
HOME='🏠 Асосий меню'
PAGE_SIZE=7


def keyboard(rows):
    return ReplyKeyboardMarkup(rows,resize_keyboard=True)


def clear(context):
    for k in list(context.user_data):
        if k.startswith('review_') or k in ('awaiting_reconciliation_partner','awaiting_saved_act_search','expense_category','act_partner_map','act_saved_map'):
            context.user_data.pop(k,None)


async def show_queue(update,context,category='errors',page=0):
    book=Ledger()
    if category=='all':
        items=[{'uid':r['uid'],'reason':'Ҳисобда' if r['active'] else 'Ҳисобга кирмаган','name':r['name'],'number':r['number'],'amount':r['amount'],'currency':r['currency']} for r in book.rows]
    else:
        items=[i for i in book.issues if (i['severity']!='info' and i['code'] not in ('unmatched_bank','unmatched_invoice') if category=='errors' else i['code']==('unmatched_invoice' if category=='invoice' else 'unmatched_bank'))]
    # One row can have several errors; show once, detail expands all reasons.
    unique={}
    for i in items: unique.setdefault(i['uid'],i)
    items=list(unique.values())
    max_page=max((len(items)-1)//PAGE_SIZE,0);page=max(0,min(page,max_page))
    context.user_data.update(review_category=category,review_page=page,review_mode='list')
    context.user_data.pop('review_pending',None)
    mapping={};rows=[]
    text=[LABEL,f'Жами: {len(items)} | Саҳифа {page+1}/{max_page+1}', '',
          'Қопланмаган сумма ҳар доим хато эмас: аванс ёки тўланмаган қарз ҳам бўлиши мумкин.','']
    for item in items[page*PAGE_SIZE:(page+1)*PAGE_SIZE]:
        label=f"📄 {item['uid']} {str(item.get('name') or 'Номсиз')[:20]}"
        mapping[label]=item['uid'];rows.append([label])
        text += [f"{item['uid']} | №{item.get('number') or '-'} | {str(item.get('name') or '-')[:70]}",f"{money(item.get('amount'))} {item.get('currency') or ''}",item['reason'],'']
    rows += [list(FILTERS)[:2],list(FILTERS)[2:],['⬅️ Текширув олдинги','➡️ Текширув кейинги'],['📥 Текширув PDF',HOME]]
    context.user_data['review_map']=mapping
    await update.message.reply_text('\n'.join(text)[:3900],reply_markup=keyboard(rows))


async def show_row(update,context,uid):
    book=Ledger();r=book.get_row(uid)
    if not r:
        await update.message.reply_text('Манба файлини ўқиб бўлмади. Файлни қайта юкланг.',reply_markup=keyboard([[LABEL,HOME]]));return
    context.user_data.update(review_uid=uid,review_mode='detail')
    problems=[i['reason'] for i in book.issues if i['uid']==uid]
    allocations=[a for a in book.allocations if uid in (a['bank_uid'],a['invoice_uid'])]
    text=[f"📄 {uid} | №{r['number'] or '-'}",f"Манба: {str(r['filename'] or '-')[:160]} | қатор {r['source_row']}",f"Ҳамкор: {r['name'] or '-'}",f"СТИР: {r['tin'] or '-'}",f"Ҳисоб рақами: {r['account'] or '-'}",f"Сана: {r['date'] or '-'}",f"Сумма: {money(r['amount'])} {r['currency']}",f"Йўналиш: { {'outgoing':'Сотув','incoming':'Харид'}.get(r.get('flow'),'белгиланмаган')}",f"Ҳисобда: {'ҳа' if r['active'] else 'йўқ'}",f"Ҳамкор мослиги: {r.get('match_method') or 'топилмаган'}",'',*problems]
    if allocations:
        text+=['','Тақсимланган суммалар:']+[f"{a['bank_uid']} → {a['invoice_uid']}: {money(a['amount'])} {a['currency']} ({a['method']})" for a in allocations[:12]]
    rows=[['🔗 Ҳамкорни боғлаш','✏️ Майдонни тузатиш'],['📤 Сотув йўналиши','📥 Харид йўналиши']]
    if r['kind']=='bank': rows += [['🧾 Фактурага боғлаш','🏦 Савдога тегишли эмас']]
    rows += [['↩️ Ҳисобга қайтариш' if r.get('excluded') else '🚫 Ҳисобдан чиқариш'],[LABEL,HOME]]
    await update.message.reply_text('\n'.join(text)[:3900],reply_markup=keyboard(rows))


async def preview_fix(update,context,patch):
    book=Ledger();r=book.get_row(context.user_data.get('review_uid'))
    if not r: raise ValueError('Қатор топилмади.')
    context.user_data['review_pending']={'uid':r['uid'],'patch':patch,'version':row_version(r)}
    context.user_data['review_mode']='confirm'
    lines=['Тузатишни текширинг:',r['uid']]
    labels={'name':'Ҳамкор','tin':'СТИР','account':'Ҳисоб рақами','direction':'Йўналиш','trade_flow':'Йўналиш','date':'Сана','amount':'Сумма','number':'Рақам','currency':'Валюта','status':'Ҳолат','excluded':'Ҳисобдан чиқариш','non_trade':'Савдога тегишли эмас','invoice_uid':'Фактура ID','incoming':'Кирим','outgoing':'Чиқим','contract':'Шартнома','vat':'ҚҚС','net_amount':'ҚҚСсиз сумма'}
    display={'incoming':'Харид','outgoing':'Сотув','signed':'Тасдиқланган','pending':'Кутилмоқда','deleted':'Бекор қилинган','invalid':'Ҳақиқий эмас',True:'Ҳа',False:'Йўқ'}
    for k,v in patch.items():
        before=r.get(k)
        before_text = display.get(before,before) if isinstance(before,(str,bool)) else before
        after_text = display.get(v,v) if isinstance(v,(str,bool)) else v
        lines.append(f"{labels.get(k,k)}: {before_text if before_text is not None else '-'} → {after_text}")
    lines += ['','Манба ҳужжат сақланади. Тузатиш алоҳида тарихга ёзилади.']
    await update.message.reply_text('\n'.join(lines),reply_markup=keyboard([['✅ Тузатишни сақлаш','❌ Тузатишни бекор қилиш'],[HOME]]))


def install_review_ui(bot_module):
    original=bot_module.text_handler
    menu=[[getattr(b,'text',b) for b in row] for row in bot_module.MENU.keyboard]
    menu.insert(-1,[LABEL]);bot_module.MENU=keyboard(menu)
    main_labels={b for row in menu for b in row}|{HOME,'🧮 Акт сверка'}

    async def handler(update,context):
        text=(update.message.text or '').strip()
        try:
            if text=='📊 Ҳисоботлар':
                clear(context)
                from ledger import contract_controls
                book=Ledger();lines=['📊 ҲИСОБОТЛАР',f'Манба файллар: {book.source_count}',f'Ҳисобдаги ёзувлар: {len(book.active)}',f'Текширув ёзувлари: {len(book.issues)}']
                contracts=contract_controls(book)
                for cur in sorted({r['currency'] for r in contracts}):
                    rows=[r for r in contracts if r['currency']==cur]
                    from ledger import ZERO
                    total=sum((r['remaining'] for r in rows if r['remaining'] is not None),ZERO)
                    lines.append(f'Шартнома қолдиғи: {money(total)} {cur}')
                    if any(r['ambiguous'] for r in rows):lines.append('Йўналиши ноаниқ шартномалар жамига киритилмаган.')
                await update.message.reply_text('\n'.join(lines),reply_markup=bot_module.MENU);return
            if text==LABEL:
                clear(context);await show_queue(update,context);return
            if text in FILTERS:
                await show_queue(update,context,FILTERS[text]);return
            if text in ('⬅️ Текширув олдинги','➡️ Текширув кейинги'):
                await show_queue(update,context,context.user_data.get('review_category','errors'),context.user_data.get('review_page',0)+(1 if text.startswith('➡️') else -1));return
            if text=='📥 Текширув PDF':
                from report_design import build_issues_pdf
                pdf=build_issues_pdf(Ledger().snapshot())
                await update.message.reply_document(InputFile(BytesIO(pdf),filename='tekshiruv.pdf'));return
            mapping=context.user_data.get('review_map',{})
            if text in mapping:
                await show_row(update,context,mapping[text]);return
            if text in main_labels:
                clear(context)
                await original(update,context);return
            mode=context.user_data.get('review_mode')
            if text=='❌ Тузатишни бекор қилиш':
                context.user_data.pop('review_pending',None)
                await show_row(update,context,context.user_data.get('review_uid'));return
            if text=='✅ Тузатишни сақлаш':
                pending=context.user_data.get('review_pending')
                if not pending: raise ValueError('Тасдиқланадиган тузатиш йўқ.')
                save_adjustment(pending['uid'],pending['patch'],update.effective_user.id,pending['version'])
                context.user_data.pop('review_pending',None)
                await update.message.reply_text('✅ Тузатиш сақланди. Акт сверка ва янги ҳисобот қайта ҳисобланади.')
                await show_row(update,context,pending['uid']);return
            if text=='🔗 Ҳамкорни боғлаш':
                context.user_data['review_mode']='partner'
                await update.message.reply_text('Ҳамкорнинг СТИРини, ҳисоб рақамини ёки аниқ номини ёзинг. Янги реквизит учун: ном | СТИР | ҳисоб рақами');return
            if text=='✏️ Майдонни тузатиш':
                context.user_data['review_mode']='field'
                await update.message.reply_text('Майдон | қиймат шаклида ёзинг.\nСана | 15.09.2026\nРақам | 42\nСумма | 1250000\nВалюта | UZS\nШартнома | 37\nКирим | 1250000\nЧиқим | 0\nҚҚС | 150000\nҚҚСсиз | 1250000\nҲолат | signed / pending / deleted / invalid');return
            if text in ('📤 Сотув йўналиши','📥 Харид йўналиши'):
                r=Ledger().get_row(context.user_data.get('review_uid'))
                await preview_fix(update,context,{'direction' if r['kind']=='invoice' else 'trade_flow':'outgoing' if text.startswith('📤') else 'incoming'});return
            if text=='🧾 Фактурага боғлаш':
                context.user_data['review_mode']='invoice'
                await update.message.reply_text('Фактуранинг қатор ID’сини ёзинг (масалан d12:r0). ID «Барча ёзувлар» бўлимида кўринади. Ҳамкор, валюта ва йўналиш мос бўлиши керак.');return
            if text=='🏦 Савдога тегишли эмас':
                await preview_fix(update,context,{'non_trade':True});return
            if text in ('🚫 Ҳисобдан чиқариш','↩️ Ҳисобга қайтариш'):
                await preview_fix(update,context,{'excluded':text.startswith('🚫'),'non_trade':False});return
            if mode=='partner':
                if '|' in text:
                    parts=[p.strip() for p in text.split('|')]
                    if len(parts)!=3 or not parts[0] or not (parts[1] or parts[2]): raise ValueError('Ном | СТИР | ҳисоб рақами шаклида ёзинг.')
                    patch=dict(zip(('name','tin','account'),parts))
                else:
                    p=Ledger().resolve(text)
                    if not p: raise ValueError('Ягона ҳамкор топилмади. Аниқ СТИР ёки реквизитларни ёзинг.')
                    patch={k:p[k] for k in ('name','tin','account')}
                await preview_fix(update,context,patch);return
            if mode=='field':
                parts=[p.strip() for p in text.split('|',1)]
                fields={'сана':'date','рақам':'number','сумма':'amount','валюта':'currency','шартнома':'contract','кирим':'incoming','чиқим':'outgoing','ққс':'vat','ққссиз':'net_amount','ҳолат':'status'}
                if len(parts)!=2 or parts[0].casefold() not in fields: raise ValueError('Майдон | қиймат шаклида ёзинг.')
                await preview_fix(update,context,{fields[parts[0].casefold()]:parts[1]});return
            if mode=='invoice':
                await preview_fix(update,context,{'invoice_uid':text});return
            if mode:
                await update.message.reply_text('Текширув тугмаларидан фойдаланинг.',reply_markup=keyboard([[LABEL,HOME]]));return
        except (ValueError,KeyError,TypeError) as exc:
            await update.message.reply_text(f'⚠️ {exc}');return
        await original(update,context)
    bot_module.text_handler=handler


def install_confirmation_safety(bot_module):
    original=bot_module.confirm_callback
    async def callback(update,context):
        query=update.callback_query
        action,_,nonce=(query.data or '').partition(':')
        pending=context.user_data.get('pending_doc') or {}
        if not nonce or nonce!=pending.get('nonce') or query.message.message_id!=pending.get('message_id') or query.message.chat.id!=pending.get('chat_id'):
            await query.answer('Бу тугма эскирган. Охирги ҳужжат тугмасидан фойдаланинг.',show_alert=True)
            return
        from types import SimpleNamespace
        forwarded=SimpleNamespace(callback_query=SimpleNamespace(
            data=action, answer=query.answer, edit_message_reply_markup=query.edit_message_reply_markup,
            message=query.message))
        await original(forwarded,context)
    bot_module.confirm_callback=callback
