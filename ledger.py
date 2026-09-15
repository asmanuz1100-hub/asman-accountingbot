"""Single accounting read model. Source documents are immutable; fixes are audited.

All amounts are Decimal. No currency conversion or fuzzy automatic party matching.
Invoice allocation is an explanatory FIFO estimate unless a reference/manual link exists.
"""
import copy
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.orm import Session
import database as db

ZERO = Decimal('0.00')
SOURCE_TYPES = {'bank_statement', 'invoice_registry', 'invoice', 'payment'}
CURRENCIES = {'UZS', 'USD', 'EUR', 'RUB', 'KZT', 'KGS', 'CNY', 'GBP', 'TRY'}


def amount(value):
    if value is None or str(value).strip() == '':
        return None
    raw = re.sub(r'[\s\u00a0\u202f]', '', str(value))
    if ',' in raw and '.' in raw:
        raw = raw.replace(',', '') if raw.rfind('.') > raw.rfind(',') else raw.replace('.', '').replace(',', '.')
    else:
        raw = raw.replace(',', '.')
    try:
        n = Decimal(raw)
        return n.quantize(Decimal('.01'), rounding=ROUND_HALF_UP) if n.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def money(value):
    n = amount(value)
    return f'{n:,.2f}'.replace(',', ' ') if n is not None else '-'


def identifier(value):
    raw = str(value or '').strip()
    if re.fullmatch(r'\d+\.0+|\d+(?:\.\d+)?[eE]\+?\d+', raw):
        try:
            raw = format(Decimal(raw), '.0f')
        except InvalidOperation:
            pass
    return re.sub(r'[^0-9A-Za-z]', '', raw).upper()


_TRANSLIT = dict(zip('абвгдеёзийклмнопрстуфхцъыьэ',
                      ['a','b','v','g','d','e','yo','z','i','y','k','l','m','n','o','p','r','s','t','u','f','x','ts','','i','','e']))
_TRANSLIT.update({'ж':'j','ч':'ch','ш':'sh','щ':'sh','ю':'yu','я':'ya','ў':'o','қ':'q','ғ':'g','ҳ':'h'})


def name_key(value):
    s = str(value or '').casefold().replace('ё', 'е')
    s = ''.join(_TRANSLIT.get(c, c) for c in s)
    s = re.sub(r"['`‘’ʻʼ\"«»]", '', s)
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    words = [w for w in s.split() if w not in {'ooo','mchj','llc','uk','chp','xk','ip','up','ооо'}]
    return ''.join(words)


def norm_date(value):
    s = str(value or '').strip()
    for fmt in ('%Y-%m-%d', '%d.%m.%Y', '%d/%m/%Y', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def doc_number(value):
    s = str(value or '').strip().casefold()
    s = re.sub(r'^(?:№|no\.?|номер)\s*', '', s)
    return re.sub(r'\s+', '', s)


def status(value):
    s = str(value or '').casefold().replace('ё', 'е')
    if any(w in s for w in ('не подпис', 'имзоланмаган', 'unsigned', 'not signed', 'ожида', 'кутил', 'pending')):
        return 'pending'
    if any(w in s for w in ('удален', 'ўчир', 'учир', 'deleted', 'отмен', 'бекор', 'cancel')):
        return 'deleted'
    if any(w in s for w in ('недейств', 'ҳақиқий эмас', 'хақиқий эмас', 'invalid', 'отклон')):
        return 'invalid'
    if any(w in s for w in ('подпис', 'имзоланган', 'signed', 'confirmed')):
        return 'signed'
    return 'unknown'


def direction(value):
    s = str(value or '').casefold()
    if any(w in s for w in ('outgoing', 'исход', 'чиқув', 'чикув', 'сотув', 'sale')):
        return 'outgoing'
    if any(w in s for w in ('incoming', 'вход', 'кирув', 'харид', 'purchase')):
        return 'incoming'
    return None


def is_invoice(value):
    s = str(value or '').casefold().replace('ё', 'е')
    s = re.sub(r'[\s_\-–]+', '', s)
    return any(w in s for w in ('счетфактура', 'ҳисобфактура', 'хисобфактура', 'invoice', 'hisobfaktura'))


def serial(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=lambda v: str(v) if isinstance(v, Decimal) else sorted(v) if isinstance(v, set) else str(v)))


def fingerprint(value):
    return hashlib.sha256(json.dumps(serial(value), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Ledger:
    def __init__(self):
        self.rows, self.issues, self.parties, self.allocations = [], [], {}, []
        self.company = {}
        self.openings = []
        self._load()
        self._identify()
        self._select_current()
        self._allocate()

    def issue(self, row, code, reason, severity='warning', remainder=None):
        entry = {'id': f"{row['uid']}:{code}", 'uid': row['uid'], 'code': code,
                 'kind': row.get('kind', 'source'), 'reason': reason, 'severity': severity,
                 'number': row.get('number'), 'date': row.get('date'),
                 'name': row.get('name'), 'currency': row.get('currency'),
                 'amount': remainder if remainder is not None else row.get('amount'),
                 'filename': row.get('filename'), 'source_id': row.get('source_id'),
                 'source_row': row.get('source_row'), 'included': row.get('active', False)}
        self.issues.append(entry)

    def _load(self):
        with Session(db.engine) as session:
            docs = list(session.scalars(select(db.Document).order_by(db.Document.id)).all())
            partners = list(session.scalars(select(db.Partner).order_by(db.Partner.id)).all())
        self.partner_seeds = [{'name': p.name, 'tin': identifier(p.tin), 'account': identifier(p.account_number)} for p in partners]
        changes = defaultdict(dict)
        for doc in docs:
            if doc.document_type != 'ledger_adjustment':
                continue
            try:
                event = json.loads(doc.raw_json)
                changes[event['uid']].update(event['patch'])
            except (ValueError, KeyError, TypeError):
                continue
        self.source_count = 0
        for doc in docs:
            if doc.document_type == 'ledger_opening':
                try:
                    self.openings.append(json.loads(doc.raw_json))
                except ValueError:
                    pass
            if doc.document_type not in SOURCE_TYPES:
                continue
            self.source_count += 1
            try:
                data = json.loads(doc.raw_json)
                if not isinstance(data, dict):
                    raise ValueError('not an object')
            except (ValueError, TypeError):
                self.issue({'uid':f'd{doc.id}:source','source_id':doc.id,'filename':doc.filename}, 'json', 'Ҳужжат маълумотини ўқиб бўлмади. Файлни қайта юкланг.', 'error')
                continue
            kind = doc.document_type
            if kind == 'bank_statement':
                self.company = self.company or {'name':data.get('account_holder'), 'tin':data.get('tax_id'), 'account':data.get('account_number')}
                records = data.get('transactions') if 'transactions' in data else data.get('transactions_preview', [])
                row_kind = 'bank'
                if 'transactions' not in data:
                    self.issue({'uid':f'd{doc.id}:source','source_id':doc.id,'filename':doc.filename}, 'preview', 'Базада выписканинг фақат қисқа рўйхати бор. Тўлиқ файлни қайта юкланг.', 'error')
            elif kind == 'invoice_registry':
                records, row_kind = data.get('invoices', []), 'invoice'
            else:
                records, row_kind = [data], 'invoice' if kind == 'invoice' else 'bank'
            if not isinstance(records, list):
                records = []
            if not records:
                self.issue({'uid':f'd{doc.id}:source','source_id':doc.id,'filename':doc.filename}, 'empty', 'Файлдан операциялар олинмаган. Форматни текшириб қайта юкланг.', 'error')
            for idx, original in enumerate(records):
                if not isinstance(original, dict):
                    self.issue({'uid':f'd{doc.id}:r{idx}','source_id':doc.id,'filename':doc.filename}, 'row', 'Қатор формати нотўғри.', 'error')
                    continue
                if kind == 'invoice_registry' and not (original.get('is_accounting_invoice') or is_invoice(original.get('document_type_name'))):
                    continue
                uid = f'd{doc.id}:r{idx}'
                row = self._normalize(original, data, row_kind, kind)
                row.update(uid=uid, source_id=doc.id, source_row=original.get('row_no', idx+1), filename=doc.filename,
                           digest=fingerprint(original), original=copy.deepcopy(original), active=False)
                row.update(changes.get(uid, {}))
                # Audit patches contain strings, normalize them through the same path.
                for key in ('amount', 'incoming', 'outgoing', 'vat', 'net_amount'):
                    if key in row and row[key] is not None:
                        row[key] = amount(row[key])
                if row_kind == 'bank':
                    row['amount'] = max(row.get('incoming') or ZERO, row.get('outgoing') or ZERO)
                    row['flow'] = row.get('trade_flow') or ('outgoing' if (row.get('incoming') or ZERO)>0 else 'incoming')
                else:
                    row['flow'] = row.get('direction')
                self.rows.append(row)

    @staticmethod
    def _normalize(raw, data, kind, source_kind):
        p = raw.get('partner') if isinstance(raw.get('partner'), dict) else {}
        currency = str(raw.get('currency') or data.get('currency') or 'UZS').upper().strip()
        currency = {'СУМ':'UZS','SUM':'UZS','SOʻM':'UZS','RUR':'RUB'}.get(currency,currency)
        inc, out = amount(raw.get('incoming', 0)), amount(raw.get('outgoing', 0))
        direct = direction(raw.get('direction') or raw.get('invoice_direction'))
        if source_kind == 'payment' and not inc and not out:
            # A payment's incoming/outgoing is cash direction, not invoice direction.
            cash = direction(raw.get('direction'))
            if cash == 'incoming': inc = amount(raw.get('total'))
            if cash == 'outgoing': out = amount(raw.get('total'))
        stated_status = raw.get('status') or raw.get('status_group')
        state = status(stated_status) if stated_status else ('signed' if source_kind == 'invoice' else 'unknown')
        return {'kind':kind, 'name':str(raw.get('counterparty') or p.get('name') or '').strip(),
                'tin':identifier(raw.get('counterparty_tin') or p.get('tin')),
                'account':identifier(raw.get('counterparty_account') or p.get('account_number') or p.get('account') or p.get('bank_account')),
                'own_account':identifier(data.get('account_number')), 'currency':currency,
                'date':norm_date(raw.get('document_date') or raw.get('invoice_date') or raw.get('date')),
                'number':str(raw.get('document_number') or raw.get('invoice_number') or '').strip(),
                'contract':str(raw.get('contract') or raw.get('contract_number') or '').strip(),
                'purpose':str(raw.get('purpose') or raw.get('summary') or ''),
                'amount':amount(raw.get('total')), 'vat':amount(raw.get('vat')), 'net_amount':amount(raw.get('amount_without_vat') or raw.get('subtotal')),
                'incoming':inc, 'outgoing':out, 'direction':direct, 'status':state,
                'reference':str(raw.get('invoice_reference') or raw.get('invoice_number') or ''),
                'external_id':str(raw.get('document_id') or raw.get('uuid') or ''),
                'trade_flow':raw.get('trade_flow'), 'excluded':False}

    def _identify(self):
        # No union of different TINs, even when a bad bank account appears under both.
        account_tins, names = defaultdict(set), defaultdict(set)
        source_rows = [r for r in self.rows if not r.get('excluded')]
        source_accounts = defaultdict(set)
        for row in source_rows:
            if row.get('account') and row.get('tin'):
                source_accounts[row['account']].add(row['tin'])
        # Old partner cache must not reintroduce an identifier the operator corrected.
        extra_seeds = [p for p in self.partner_seeds if not p.get('account') or not source_accounts[p['account']] or p.get('tin') in source_accounts[p['account']]]
        seeds = source_rows + extra_seeds
        for row in seeds:
            tin, acc = row.get('tin'), row.get('account')
            if tin and acc:
                account_tins[acc].add(tin)
        for row in seeds:
            tin, acc = row.get('tin'), row.get('account')
            if tin:
                root = 'tin:'+tin
            elif acc and len(account_tins[acc]) == 1:
                root = 'tin:'+next(iter(account_tins[acc]))
            elif acc and len(account_tins[acc]) == 0:
                root = 'acc:'+acc
            else:
                continue
            if name_key(row.get('name')):
                names[name_key(row['name'])].add(root)
        # A unique TIN wins over an unlinked account with the exact same legal name.
        for key, roots in names.items():
            tin_roots = {r for r in roots if r.startswith('tin:')}
            if tin_roots:
                names[key] = tin_roots
        for row in seeds:
            tin, acc, nk = row.get('tin'), row.get('account'), name_key(row.get('name'))
            root, method = None, None
            conflict = bool(acc and len(account_tins[acc])>1)
            if tin:
                root, method = 'tin:'+tin, 'СТИР'
            elif acc and len(account_tins[acc])==1:
                root, method = 'tin:'+next(iter(account_tins[acc])), 'Ҳисоб рақами'
            elif nk and len(names[nk])==1 and not conflict:
                root, method = next(iter(names[nk])), 'Ягона мос ном'
            elif acc and not conflict:
                root, method = 'acc:'+acc, 'Ҳисоб рақами'
            elif nk and not names[nk]:
                root, method = 'name:'+nk, 'Ном; реквизитни текширинг'
            if 'uid' in row:
                row['party_id'], row['match_method'] = root, method
                row['identity_blocked'] = conflict or not root
                if conflict:
                    self.issue(row,'identity_conflict','Бир ҳисоб рақами турли СТИРларга боғланган. Ҳамкорни текширинг.', 'error')
                elif not root:
                    self.issue(row,'identity','Ҳамкорни бир маънода аниқлаб бўлмади. СТИР ёки ҳамкорни танланг.', 'error')
            if root:
                party = self.parties.setdefault(root, {'id':root,'name':row.get('name') or 'Номсиз ҳамкор','tin':tin or '', 'account':acc or '', 'accounts':set(),'names':set(),'flows':set()})
                if tin: party['tin'] = tin
                if acc:
                    party['accounts'].add(acc)
                    if not party['account']:party['account']=acc
                if nk: party['names'].add(nk)
                if row.get('flow'): party['flows'].add(row['flow'])
        self.account_tins = account_tins

    def _select_current(self):
        latest, seen_bank = {}, {}
        occurrences = defaultdict(int)
        for row in self.rows:
            if row['kind'] != 'invoice':
                continue
            key = self.invoice_key(row)
            if key in latest:
                old = latest[key]
                self.issue(old, 'superseded', f"Янги версия билан алмашган: {row['uid']}. Икки марта ҳисобланмайди.", 'info')
                old['superseded'] = True
            latest[key] = row
        for row in self.rows:
            if row.get('superseded') or row.get('excluded'):
                continue
            if row['kind']=='invoice' and row['status'] in ('deleted','invalid','pending','unknown'):
                label = {'deleted':'Бекор қилинган','invalid':'Ҳақиқий эмас','pending':'Имзоланмаган','unknown':'Ҳолати номаълум'}[row['status']]
                self.issue(row,'status',label+' фактура ҳисобга киритилмаган.', 'info' if row['status']=='deleted' else 'warning')
                continue
            errors = []
            if row['currency'] not in CURRENCIES:
                errors.append(('currency','Валюта номаълум. Валютани белгиланг.'))
            if not row.get('date'):
                errors.append(('date','Ҳужжат санаси йўқ ёки нотўғри.'))
            if row.get('amount') is None or row['amount'] <= ZERO:
                errors.append(('amount','Сумма нол, манфий ёки нотўғри. Тузатувчи ҳужжатни алоҳида текширинг.'))
            if row['kind']=='invoice':
                if row['direction'] not in ('incoming','outgoing'):
                    errors.append(('direction','Фактуранинг сотув/харид йўналиши белгиланмаган.'))
                if not row['number']:
                    errors.append(('number','Фактура рақами йўқ.'))
                if row['vat'] is not None and row['net_amount'] is not None and row['amount'] is not None and abs(row['net_amount']+row['vat']-row['amount'])>Decimal('.02'):
                    errors.append(('arithmetic','ҚҚСсиз сумма + ҚҚС умумий суммага тенг эмас.'))
            else:
                if row['incoming'] is None or row['outgoing'] is None or row['incoming']<ZERO or row['outgoing']<ZERO or (row['incoming']>ZERO and row['outgoing']>ZERO):
                    errors.append(('amount','Банк қаторида кирим/чиқим нотўғри. Иккала томон бир вақтда мусбат бўлмасин.'))
            for code, reason in errors:
                self.issue(row, code, reason, 'error')
            if errors:
                continue
            if row['kind']=='bank':
                base_key = self.bank_key(row)
                occurrences[(row['source_id'], base_key)] += 1
                key = (base_key, occurrences[(row['source_id'], base_key)])
                if key in seen_bank:
                    self.issue(row,'duplicate',f"Такрорий операция: {seen_bank[key]}. Бир марта ҳисобланади.", 'info')
                    continue
                seen_bank[key] = row['uid']
            row['active'] = True
        self.active = [r for r in self.rows if r['active']]
        self.invoices = [r for r in self.active if r['kind']=='invoice']
        self.bank = [r for r in self.active if r['kind']=='bank']

    @staticmethod
    def invoice_key(row):
        party = row.get('party_id') or ('tin:'+row['tin'] if row.get('tin') else 'name:'+name_key(row.get('name')))
        if row.get('external_id'):
            return ('id',row['external_id'],party,row['currency'])
        if not row.get('number') or not row.get('date'):
            return ('unidentified',row['uid'])
        return (party,row.get('direction'),row['currency'],doc_number(row['number']),row['date'])

    @staticmethod
    def bank_key(row):
        party = row.get('party_id') or row.get('tin') or row.get('account') or name_key(row.get('name'))
        return (row['own_account'],row['currency'],row['date'],party,doc_number(row['number']),row['incoming'],row['outgoing'],name_key(row['purpose']))

    def _allocate(self):
        self.remaining = {r['uid']:r['amount'] for r in self.active}
        buckets = defaultdict(list)
        for inv in self.invoices:
            if not inv.get('identity_blocked'):
                buckets[(inv['party_id'], inv['currency'], inv['direction'])].append(inv)
        for rows in buckets.values():
            rows.sort(key=lambda r:(r['date'],r['source_id'],r['uid']))
        for bank in sorted(self.bank, key=lambda r:(r['date'],r['source_id'],r['uid'])):
            if bank.get('identity_blocked') or bank.get('non_trade'):
                continue
            choices = list(buckets[(bank['party_id'],bank['currency'],bank['flow'])])
            method = 'fifo'
            # Explicit user links and invoice references never fall through to an unrelated invoice.
            if bank.get('invoice_uid'):
                choices = [i for i in choices if i['uid']==bank['invoice_uid']]
                method = 'manual'
            else:
                reference = bank.get('reference')
                if not reference:
                    m = re.search(r'(?:фактур\w*|invoice|faktura)\s*(?:№|no\.?|номер)?\s*([\w/-]+)', bank['purpose'],re.I)
                    reference = m.group(1) if m else None
                if reference:
                    choices = [i for i in choices if doc_number(i['number'])==doc_number(reference)]
                    method = 'reference'
                elif bank['contract']:
                    choices = [i for i in choices if doc_number(i['contract'])==doc_number(bank['contract'])]
                    method = 'contract_fifo'
            for inv in choices:
                value = min(self.remaining[bank['uid']], self.remaining[inv['uid']])
                if value<=ZERO:
                    continue
                self.remaining[bank['uid']] -= value
                self.remaining[inv['uid']] -= value
                self.allocations.append({'bank_uid':bank['uid'],'invoice_uid':inv['uid'],'amount':value,'currency':inv['currency'], 'method':method,'bank_number':bank['number'],'invoice_number':inv['number']})
                if self.remaining[bank['uid']]<=ZERO:
                    break
            if self.remaining[bank['uid']]>ZERO:
                self.issue(bank,'unmatched_bank','Фактурага тақсимланмаган тўлов. Аванс, бошқа операция ёки етишмаётган фактура бўлиши мумкин.', remainder=self.remaining[bank['uid']])
        for inv in self.invoices:
            if self.remaining[inv['uid']]>ZERO and not inv.get('identity_blocked'):
                self.issue(inv,'unmatched_invoice','Тўлов билан қопланмаган фактура ёки қисман тўлов. Банк выпискаси ва реквизитларни текширинг.', remainder=self.remaining[inv['uid']])
        for issue in self.issues:
            row = self.get_row(issue['uid'])
            if row:
                issue['included'] = row['active']

    def get_row(self, uid):
        return next((r for r in self.rows if r['uid']==uid), None)

    def resolve(self, query):
        q = str(query or '').strip()
        if q in self.parties:
            return self.parties[q]
        ident, nk = identifier(q), name_key(q)
        exact = [p for p in self.parties.values() if ident and (ident==p['tin'] or ident in p['accounts'])]
        if len(exact)==1: return exact[0]
        names = [p for p in self.parties.values() if nk and nk in p['names']]
        if len(names)==1: return names[0]
        partial = [p for p in self.parties.values() if nk and any(nk in n for n in p['names'])]
        return partial[0] if len(partial)==1 else None

    def party_groups(self):
        used = {r['party_id'] for r in self.active if r.get('party_id')}
        parties = [p for p in self.parties.values() if p['id'] in used]
        return tuple(sorted([p for p in parties if flow in p['flows']],key=lambda p:p['name'].casefold()) for flow in ('outgoing','incoming'))

    def reconciliation(self, query, flow=None):
        party = self.resolve(query)
        if not party: return None
        rows = [r for r in self.active if r.get('party_id')==party['id'] and not r.get('identity_blocked') and not r.get('non_trade') and (not flow or r['flow']==flow)]
        sections = []
        for cur in sorted({r['currency'] for r in rows}):
            selected = sorted([r for r in rows if r['currency']==cur],key=lambda r:(r['date'],r['uid']))
            entries, invoices, bank = [], [], []
            sales=purchases=received=paid=ZERO
            for r in selected:
                if r['kind']=='invoice':
                    debit = r['amount'] if r['direction']=='outgoing' else ZERO
                    credit = r['amount'] if r['direction']=='incoming' else ZERO
                    sales+=debit; purchases+=credit
                    invoices.append({'uid':r['uid'],'number':r['number'],'date':r['date'],'direction':r['direction'],'amount':r['amount'],'contract':r['contract'],'unpaid':self.remaining[r['uid']]})
                    label = 'Фактура №'+r['number']
                else:
                    debit,credit=r['outgoing'],r['incoming']
                    paid+=debit; received+=credit
                    bank.append({'uid':r['uid'],'date':r['date'],'document_number':r['number'],'incoming':credit,'outgoing':debit,'purpose':r['purpose'],'unallocated':self.remaining[r['uid']]})
                    label='Тўлов №'+(r['number'] or '-')
                entries.append({'uid':r['uid'],'date':r['date'],'kind':r['kind'],'document':label,'basis':r['contract'] or r['purpose'],'debit':debit,'credit':credit,'match_method':r.get('match_method')})
            debit=sum((e['debit'] for e in entries),ZERO); credit=sum((e['credit'] for e in entries),ZERO)
            opening = ZERO
            opening_set = False
            for value in self.openings:
                if value.get('party_id')==party['id'] and value.get('currency')==cur and value.get('flow')==flow:
                    opening=amount(value.get('net')) or ZERO; opening_set=True
            net=opening+debit-credit
            sections.append({'currency':cur,'entries':entries,'invoice_rows':invoices,'bank_rows':bank,'sales':sales,'purchases':purchases,
                             'payments_from_partner':received,'payments_to_partner':paid,'receivable':sales-received,'payable':purchases-paid,
                             'debit_total':debit,'credit_total':credit,'net':net,'total':net,
                             'opening_debit':max(opening,ZERO),'opening_credit':max(-opening,ZERO),'opening_set':opening_set,
                             'closing_debit':max(net,ZERO),'closing_credit':max(-net,ZERO),
                             'invoice_count':len(invoices),'bank_operations_count':len(bank),
                             'allocations':[a for a in self.allocations if any(i['uid']==a['invoice_uid'] for i in invoices)],
                             'statement_period':{'from':selected[0]['date'],'to':selected[-1]['date']}})
        relevant_issues=[i for i in self.issues if (self.get_row(i['uid']) or {}).get('party_id')==party['id'] and i['severity']!='info']
        result={'document_type':'reconciliation_report','schema_version':3,'partner':{**party,'account_number':party['account']},'own_company':self.company,
                'flow':flow,'sections':sections,'issues':relevant_issues,'generated_at':datetime.now().strftime('%d.%m.%Y %H:%M'),
                'statement_period':{'from':min((r['date'] for r in rows), default=None),'to':max((r['date'] for r in rows),default=None)}}
        # Legacy UI fields are only a single-currency view; all new consumers iterate sections.
        if len(sections)==1: result.update(sections[0])
        else: result.update(currency='MULTI',entries=[e for s in sections for e in s['entries']])
        return serial(result)

    def snapshot(self):
        by_currency={}
        for cur in sorted({r['currency'] for r in self.active}):
            rows=[r for r in self.active if r['currency']==cur]
            summary={k:ZERO for k in ('sales','purchases','bank_in','bank_out','output_vat','input_vat','receivable','payable','advances')}
            groups=defaultdict(lambda:{k:ZERO for k in ('sales','purchases','received','paid')})
            monthly=defaultdict(lambda:{k:ZERO for k in ('sales','purchases','bank_in','bank_out')})
            for r in rows:
                m=monthly[r['date'][:7]]
                g=groups[r.get('party_id')] if r.get('party_id') and not r.get('identity_blocked') and not r.get('non_trade') else None
                if r['kind']=='invoice':
                    key='sales' if r['direction']=='outgoing' else 'purchases'
                    summary[key]+=r['amount']; m[key]+=r['amount']
                    summary['output_vat' if key=='sales' else 'input_vat']+=r['vat'] or ZERO
                    if g is not None: g[key]+=r['amount']
                else:
                    summary['bank_in']+=r['incoming'];summary['bank_out']+=r['outgoing']
                    m['bank_in']+=r['incoming'];m['bank_out']+=r['outgoing']
                    if g is not None:
                        g['received']+=r['incoming'];g['paid']+=r['outgoing']
            debtors,creditors,advances=[],[],[]
            for pid,g in groups.items():
                name=self.parties[pid]['name']
                recv=g['sales']-g['received']; pay=g['purchases']-g['paid']
                if recv>ZERO: debtors.append((name,recv))
                elif recv<ZERO: advances.append((name,-recv,'Харидор аванси'))
                if pay>ZERO: creditors.append((name,pay))
                elif pay<ZERO: advances.append((name,-pay,'Етказиб берувчига аванс'))
            summary.update(receivable=sum((x[1] for x in debtors),ZERO),payable=sum((x[1] for x in creditors),ZERO),advances=sum((x[1] for x in advances),ZERO),
                           debtors=sorted(debtors,key=lambda x:-x[1]),creditors=sorted(creditors,key=lambda x:-x[1]),advance_rows=sorted(advances,key=lambda x:-x[1]),
                           currency=cur, monthly=[{'month':m,**v} for m,v in sorted(monthly.items())],vat_difference=summary['output_vat']-summary['input_vat'],
                           invoice_count=sum(r['kind']=='invoice' for r in rows),bank_count=sum(r['kind']=='bank' for r in rows))
            by_currency[cur]=summary
        contracts=contract_controls(self)
        for cur in {r['currency'] for r in contracts}:
            if cur not in by_currency:
                by_currency[cur]={k:ZERO for k in ('sales','purchases','bank_in','bank_out','output_vat','input_vat','receivable','payable','advances','vat_difference')}
                by_currency[cur].update(currency=cur,monthly=[],debtors=[],creditors=[],advance_rows=[])
        from menu_customization import _classify_expense
        for cur, summary in by_currency.items():
            summary['contracts']=[c for c in contracts if c['currency']==cur]
            categories=defaultdict(lambda:ZERO)
            for row in self.bank:
                if row['currency']==cur and row['outgoing']>ZERO:
                    categories[_classify_expense(row['purpose'],row['name'])]+=row['outgoing']
            summary['cash_out_categories']=dict(categories)
        dates=[r['date'] for r in self.active]
        return serial({'schema_version':3,'company_name':self.company.get('name') or 'ASMAN SILICAT','generated_at':datetime.now().strftime('%d.%m.%Y %H:%M'),
                       'period_from':min(dates,default=None),'period_to':max(dates,default=None),'currencies':by_currency,
                       'issues':self.issues,'source_documents':self.source_count,'active_records':len(self.active),
                       'error_count':sum(i['severity']=='error' for i in self.issues),'unmatched_bank':sum(i['code']=='unmatched_bank' for i in self.issues),
                       'unmatched_invoice':sum(i['code']=='unmatched_invoice' for i in self.issues)})


def financial_text(snapshot=None):
    snap=snapshot or Ledger().snapshot()
    lines=['📈 МОЛИЯВИЙ ТАҲЛИЛ', f"Давр: {snap['period_from'] or '-'} - {snap['period_to'] or '-'}"]
    for cur,s in snap['currencies'].items():
        lines += ['',cur, f"Сотув: {money(s['sales'])}",f"Харид: {money(s['purchases'])}",f"Банк кирими: {money(s['bank_in'])}",f"Банк чиқими: {money(s['bank_out'])}",f"Дебитор: {money(s['receivable'])}",f"Кредитор: {money(s['payable'])}",f"Аванслар: {money(s['advances'])}"]
    lines += ['',f"Текшириладиган хатолар: {snap['error_count']}",f"Тақсимланмаган тўловлар: {snap['unmatched_bank']}",f"Қопланмаган фактуралар: {snap['unmatched_invoice']}",
              'Қопланмаган сумма аванс ёки ҳақиқий қарз ҳам бўлиши мумкин.', 'Қарз кўрсаткичлари юкланган давр ҳаракати бўйича; бошланғич қолдиқлар бу таҳлилга қўшилмаган.', 'Батафсил маълумот PDF ва «Топилмаган / хатоли» бўлимида.']
    return '\n'.join(lines)


def save_adjustment(uid, patch, actor_id, expected_digest):
    allowed={'name','tin','account','direction','trade_flow','currency','date','number','contract','amount','vat','net_amount','incoming','outgoing','status','excluded','non_trade','invoice_uid','reference'}
    if not patch or set(patch)-allowed:
        raise ValueError('Тузатиш майдони нотўғри.')
    ledger=Ledger(); row=ledger.get_row(uid)
    if not row or fingerprint(serial({k:v for k,v in row.items() if k not in ('original','active','superseded')}))!=expected_digest:
        raise ValueError('Маълумот ўзгарган. Қаторни қайта очинг.')
    clean=dict(patch)
    if row['kind']=='bank' and 'amount' in clean:
        raise ValueError('Банк суммаси учун Кирим ёки Чиқим майдонини тузатинг.')
    for key in ('amount','vat','net_amount','incoming','outgoing'):
        if key in clean:
            n=amount(clean[key])
            if n is None or n<ZERO: raise ValueError('Сумма мусбат рақам бўлиши керак.')
            clean[key]=str(n)
    if 'date' in clean:
        clean['date']=norm_date(clean['date'])
        if not clean['date']: raise ValueError('Сана формати: 15.09.2026')
    if 'currency' in clean:
        clean['currency']=str(clean['currency']).upper()
        if clean['currency'] not in CURRENCIES: raise ValueError('Валюта нотўғри.')
    for key in ('tin','account'):
        if key in clean: clean[key]=identifier(clean[key])
    for key in ('direction','trade_flow'):
        if key in clean and clean[key] not in ('incoming','outgoing'): raise ValueError('Йўналиш нотўғри.')
    if 'status' in clean and clean['status'] not in ('signed','deleted','pending','invalid'): raise ValueError('Ҳолат нотўғри.')
    if clean.get('invoice_uid'):
        inv=ledger.get_row(clean['invoice_uid'])
        if row['kind']!='bank' or not inv or inv['kind']!='invoice' or not inv['active'] or inv['party_id']!=row['party_id'] or inv['currency']!=row['currency'] or inv['direction']!=row['flow']:
            raise ValueError('Фактура ҳамкор, валюта ва йўналиш бўйича тўловга мос эмас.')
    event={'document_type':'ledger_adjustment','uid':uid,'patch':clean,'actor_id':actor_id,'previous':serial({k:row.get(k) for k in clean}),'source_digest':row['digest'],'created_at':datetime.now().isoformat()}
    return db.save_document(event,None,'ledger-adjustment.json','application/json')


def row_version(row):
    return fingerprint(serial({k:v for k,v in row.items() if k not in ('original','active','superseded')}))


def prepare_reconciliation(identifier, flow=None):
    data=Ledger().reconciliation(identifier,flow)
    if not data or not data.get('sections'):
        return {'ok':False,'text':'Ҳисобга олинадиган ёзув топилмади. «Топилмаган / хатоли» бўлимини текширинг.'}
    signature=fingerprint({k:v for k,v in data.items() if k not in ('generated_at','report_id')})
    with Session(db.engine) as session:
        docs=session.scalars(select(db.Document).where(db.Document.document_type=='reconciliation_report').order_by(db.Document.id.desc())).all()
    doc_id=None
    for d in docs:
        try:
            if json.loads(d.raw_json).get('snapshot_key')==signature:
                doc_id=d.id;break
        except (TypeError,ValueError): pass
    if doc_id is None:
        data['snapshot_key']=signature
        doc_id=db.save_document(data,None,'akt-sverka.json','application/json')
    data['report_id']=doc_id
    lines=[f"🧮 АКТ СВЕРКА #{doc_id}",data['partner']['name']]
    for s in data['sections']:
        lines += ['',s['currency'],f"Дебет: {money(s['debit_total'])}",f"Кредит: {money(s['credit_total'])}",f"Якуний дебет: {money(s['closing_debit'])}",f"Якуний кредит: {money(s['closing_credit'])}"]
    lines += ['', 'Бошланғич қолдиқ киритилмаган бўлса, натижа фақат юкланган давр ҳаракатини кўрсатади.', 'Фактураларга тақсимлаш усули PDF’да кўрсатилган.']
    return {'ok':True,'doc_id':doc_id,'data':data,'text':'\n'.join(lines)}


def install_ledger(enhancements_module, business_module, financial_module):
    enhancements_module.reconciliation_partner_groups=lambda:Ledger().party_groups()
    enhancements_module.resolve_reconciliation_party=lambda q:Ledger().resolve(q)
    enhancements_module.prepare_reconciliation=prepare_reconciliation
    business_module.financial_analysis=financial_text
    business_module.contract_overview_text=contract_text
    financial_module.collect_financial_snapshot=lambda:Ledger().snapshot()


def contract_controls(book=None):
    book=book or Ledger()
    with Session(db.engine) as session:
        models=session.execute(select(db.Contract,db.Partner).join(db.Partner,db.Partner.id==db.Contract.partner_id)).all()
    result=[]
    for contract,partner in models:
        target=book.resolve(partner.tin or partner.account_number or partner.name)
        currency=contract.currency or 'UZS'
        invoices=[r for r in book.invoices if target and r.get('party_id')==target['id'] and r['currency']==currency and doc_number(r['contract'])==doc_number(contract.number)]
        directions={r['direction'] for r in invoices}
        ambiguous=len(directions)>1
        used=None if ambiguous else sum((r['amount'] for r in invoices),ZERO)
        total=amount(contract.total_amount) or ZERO
        result.append({'number':contract.number,'partner':partner.name,'currency':currency,'total':total,'used':used,'remaining':total-used if used is not None else None,'ambiguous':ambiguous})
    return result


def contract_text():
    lines=['📄 ШАРТНОМАЛАР']
    for row in contract_controls():
        lines += ['',f"{row['partner']} | №{row['number']}",f"Жами: {money(row['total'])} {row['currency']}",f"Ишлатилган: {money(row['used'])}",f"Қолдиқ: {money(row['remaining'])}"]
        if row['ambiguous']:lines.append('Сотув ва харидда бир хил шартнома рақами. Йўналишни аниқлаш керак.')
    return '\n'.join(lines)
