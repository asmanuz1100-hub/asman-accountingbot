import os
import tempfile
import unittest
import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

_TMP=tempfile.TemporaryDirectory()
os.environ['DATABASE_URL']='sqlite:///'+_TMP.name+'/test.db'
os.environ['WEBHOOK_SECRET']='test-secret'
import database as db
from sqlalchemy import text

db.init_db()
db.upsert_partner('KEEP AT STARTUP')
import start
from ledger import Ledger, amount, name_key, save_adjustment, row_version
import bot
import document_guard

STARTUP_PARTNERS=len(db.list_partners())


def save(data):return db.save_document(data,None,'test.xlsx','application/octet-stream')
def invoice(number='42',total=100,**kw):
    row=dict(document_number=number,document_date='2026-09-01',total=total,direction='outgoing',status_group='signed',document_type_name='Ҳисоб-фактура',counterparty='ASMAN PARTNER MCHJ',counterparty_tin='123456789',currency='UZS')
    row.update(kw);return row
def bank(total=100,**kw):
    row=dict(document_number='P1',date='2026-09-03',incoming=total,outgoing=0,counterparty='ASMAN PARTNER',counterparty_tin='123456789',purpose='Payment')
    row.update(kw);return row

def add_invoice(row):return save({'document_type':'invoice_registry','invoices':[row],'currency':'UZS'})
def add_bank(row,currency='UZS',account='20208000'):return save({'document_type':'bank_statement','transactions':[row],'currency':currency,'account_number':account,'account_holder':'ASMAN SILICAT'})


class LedgerTests(unittest.TestCase):
    def setUp(self):
        with db.engine.begin() as c:
            for table in ('contracts','materials','documents','partners'):c.execute(text('DELETE FROM '+table))
    def test_startup_keeps_existing_data_and_secret(self):
        self.assertEqual(STARTUP_PARTNERS,1)
        self.assertEqual(os.environ['WEBHOOK_SECRET'],'test-secret')
    def test_decimal(self):
        self.assertEqual(amount('1 234,56'),Decimal('1234.56'))
        self.assertIsNone(amount('NaN'));self.assertIsNone(amount('Infinity'))
    def test_name_and_strong_identifier_bridge(self):
        add_invoice(invoice());add_bank(bank(counterparty_tin='',counterparty='«ASMAN PARTNER» ООО'))
        book=Ledger();self.assertEqual(len(book.allocations),1)
        self.assertEqual(book.allocations[0]['amount'],Decimal('100'))
    def test_transliterated_exact_name_bridge(self):
        add_invoice(invoice(counterparty='АСМАН ПАРТНЕР МЧЖ'));add_bank(bank(counterparty_tin='',counterparty='ASMAN PARTNER LLC'))
        self.assertEqual(len(Ledger().allocations),1)
    def test_account_to_tin(self):
        add_invoice(invoice(counterparty_account='202012345'));add_bank(bank(counterparty_tin='',counterparty='Different display',counterparty_account='202012345'))
        self.assertEqual(len(Ledger().allocations),1)
    def test_conflicting_tins_not_merged(self):
        add_invoice(invoice(counterparty_account='2020'));add_bank(bank(counterparty_tin='987654321',counterparty_account='2020'))
        book=Ledger();self.assertEqual(book.allocations,[])
        self.assertTrue(any(i['code']=='identity_conflict' for i in book.issues))
    def test_ambiguous_name_not_auto_merged(self):
        add_invoice(invoice());add_invoice(invoice(counterparty_tin='987654321'))
        add_bank(bank(counterparty_tin=''))
        self.assertEqual(Ledger().allocations,[])
    def test_revised_invoice_only_latest(self):
        add_invoice(invoice());add_invoice(invoice(total=120))
        book=Ledger();self.assertEqual(sum(r['amount'] for r in book.invoices),Decimal(120))
    def test_cancelled_invoice_removed(self):
        add_invoice(invoice());add_invoice(invoice(status_group='deleted'))
        self.assertEqual(Ledger().invoices,[])
    def test_negative_signed_status_not_accepted(self):
        add_invoice(invoice(status='Не подписан',status_group='signed'))
        self.assertEqual(Ledger().invoices,[])
    def test_standalone_invoice_included(self):
        save({'document_type':'invoice','invoice_number':'42','invoice_date':'2026-09-01','direction':'outgoing','total':100,'partner':{'name':'ASMAN PARTNER','tin':'123456789'}})
        self.assertEqual(len(Ledger().invoices),1)
    def test_unknown_direction_in_queue(self):
        add_invoice(invoice(direction='unknown'))
        book=Ledger();self.assertFalse(book.invoices)
        self.assertTrue(any(i['code']=='direction' for i in book.issues))
    def test_partial_and_multiple_payments(self):
        add_invoice(invoice(total=150));add_bank(bank(60));add_bank(bank(40,document_number='P2'))
        book=Ledger();self.assertEqual(len(book.allocations),2)
        inv=book.invoices[0];self.assertEqual(book.remaining[inv['uid']],Decimal(50))
    def test_one_payment_many_invoices(self):
        add_invoice(invoice(total=60));add_invoice(invoice(number='43',total=40));add_bank(bank())
        self.assertEqual(len(Ledger().allocations),2)
    def test_reference_does_not_pay_other_invoice(self):
        add_invoice(invoice());add_bank(bank(purpose='Invoice №99'))
        self.assertEqual(Ledger().allocations,[])
    def test_mixed_currencies_and_accounts(self):
        add_bank(bank(),currency='UZS');add_bank(bank(),currency='USD');add_bank(bank(),currency='UZS',account='20209999')
        b=Ledger();self.assertEqual(len(b.bank),3)
        s=b.snapshot()['currencies'];self.assertEqual(s['UZS']['bank_in'],'200.00');self.assertEqual(s['USD']['bank_in'],'100.00')
    def test_overlapping_bank_files_count_once(self):
        add_bank(bank());add_bank(bank())
        self.assertEqual(len(Ledger().bank),1)
    def test_advance_same_in_text_and_pdf_data(self):
        add_bank(bank())
        from ledger import financial_text
        snap=Ledger().snapshot();self.assertEqual(snap['currencies']['UZS']['advances'],'100.00')
        self.assertIn('Аванслар: 100.00',financial_text(snap))
    def test_correction_audited_and_recalculates(self):
        add_invoice(invoice(direction='unknown'))
        b=Ledger();row=b.rows[0]
        save_adjustment(row['uid'],{'direction':'outgoing'},123,row_version(row))
        self.assertEqual(len(Ledger().invoices),1)
        with db.Session(db.engine) as s:
            events=s.query(db.Document).filter_by(document_type='ledger_adjustment').all()
        self.assertEqual(len(events),1);self.assertIn('"actor_id": 123',events[0].raw_json)
    def test_stale_adjustment_rejected(self):
        add_invoice(invoice());r=Ledger().rows[0];version=row_version(r)
        save_adjustment(r['uid'],{'amount':120},123,version)
        with self.assertRaises(ValueError):save_adjustment(r['uid'],{'amount':140},123,version)
    def test_full_registry_keeps_status_versions(self):
        add_invoice(invoice())
        data=document_guard.prepare_new_document({'document_type':'invoice_registry','invoices':[invoice(status_group='deleted')]})['data']
        save(data);self.assertFalse(Ledger().invoices)
    def test_units(self):
        db.add_material('Акрил',500,'kg');db.add_material('Акрил',1,'ton')
        self.assertEqual(db.list_materials()[0].qty,1500)
    def test_old_confirmation_does_not_save_latest(self):
        async def run():
            q=SimpleNamespace(data='doc_confirm:old',answer=AsyncMock(),message=SimpleNamespace(message_id=1,chat=SimpleNamespace(id=1)))
            context=SimpleNamespace(user_data={'pending_doc':{'nonce':'new','message_id':2,'chat_id':1}})
            await bot.confirm_callback(SimpleNamespace(callback_query=q),context)
            q.answer.assert_awaited_once()
            self.assertIn('pending_doc',context.user_data)
        asyncio.run(run())
    def test_registry_russian_no_row_number(self):
        import invoice_registry
        from unittest.mock import patch
        rows=[['Направление','Статус','Тип документа','Контрагент','ИНН','Номер документа','Дата документа','Сумма с НДС'],['исходящий','подписан','Счет-фактура','TEST','123456789','42','01.09.2026','100']]
        with patch('excel_docs._spreadsheet_to_rows',return_value=rows):result=invoice_registry.try_analyze_invoice_registry(b'fake','test.xlsx')
        self.assertEqual(result['invoices'][0]['total'],100)
    def test_new_bank_account_unique_name(self):
        add_invoice(invoice())
        add_bank(bank(counterparty_tin='',counterparty_account='2020999'))
        self.assertEqual(len(Ledger().allocations),1)
    def test_identical_rows_in_same_file_preserved(self):
        save({'document_type':'bank_statement','transactions':[bank(),bank()],'currency':'UZS'})
        self.assertEqual(len(Ledger().bank),2)
    def test_valid_confirmation_saves_selected_document(self):
        async def run():
            message=SimpleNamespace(message_id=11,chat=SimpleNamespace(id=22),reply_text=AsyncMock())
            context=SimpleNamespace(user_data={'pending_doc':{'nonce':'abcd','message_id':11,'chat_id':22,'data':{'document_type':'other','document_number':'A','total':100},'filename':'A.pdf'}})
            q=SimpleNamespace(data='doc_confirm:abcd',answer=AsyncMock(),message=message,edit_message_reply_markup=AsyncMock())
            await bot.confirm_callback(SimpleNamespace(callback_query=q),context)
            self.assertNotIn('pending_doc',context.user_data)
            with db.Session(db.engine) as session:
                self.assertEqual(session.query(db.Document).filter_by(document_number='A').count(),1)
        asyncio.run(run())
    def test_access_denied_and_allowed(self):
        from telegram.ext import ApplicationHandlerStop
        async def run():
            update=SimpleNamespace(effective_user=SimpleNamespace(id=999),effective_message=SimpleNamespace(reply_text=AsyncMock()),callback_query=None)
            os.environ['ALLOWED_USER_IDS']='123'
            with self.assertRaises(ApplicationHandlerStop):await bot.access_check(update,None)
            os.environ['ALLOWED_USER_IDS']='999'
            await bot.access_check(update,None)
        asyncio.run(run())
    def test_review_queue_user_journey(self):
        add_invoice(invoice(direction='unknown'))
        async def run():
            msg=SimpleNamespace(text='🔎 Топилмаган / хатоли',reply_text=AsyncMock())
            update=SimpleNamespace(message=msg,effective_user=SimpleNamespace(id=123))
            context=SimpleNamespace(user_data={})
            await bot.text_handler(update,context)
            self.assertTrue(context.user_data['review_map'])
            msg.text=next(iter(context.user_data['review_map']))
            await bot.text_handler(update,context)
            msg.text='📤 Сотув йўналиши';await bot.text_handler(update,context)
            self.assertIn('review_pending',context.user_data)
            msg.text='✅ Тузатишни сақлаш';await bot.text_handler(update,context)
            self.assertEqual(len(Ledger().invoices),1)
        asyncio.run(run())
    def test_pdf_safe_text_and_multipage(self):
        from report_design import build_reconciliation_pdf,build_financial_report_pdf
        add_invoice(invoice(counterparty='A & B <MCHJ>'))
        book=Ledger();data=book.reconciliation('123456789','outgoing')
        self.assertTrue(build_reconciliation_pdf(data).startswith(b'%PDF'))
        self.assertTrue(build_financial_report_pdf(book.snapshot()).startswith(b'%PDF'))

if __name__=='__main__':unittest.main()
