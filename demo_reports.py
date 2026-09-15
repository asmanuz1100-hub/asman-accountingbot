"""Generate clearly labelled samples in an isolated temporary database."""
import os
import tempfile
from pathlib import Path


def main():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ['DATABASE_URL']='sqlite:///'+tmp+'/sample.db'
        import start
        import database as db
        from ledger import Ledger
        from report_design import build_reconciliation_pdf,build_financial_report_pdf
        output=Path(os.getenv('DEMO_OUTPUT','samples'));output.mkdir(parents=True,exist_ok=True)
        def save(data,name):db.save_document(data,None,name,'application/json')
        company='ASMAN SILICAT / СИНОВ МАЪЛУМОТЛАРИ'
        partners=[('FARGONA PREMIUM QURILISH MCHJ','300100200'),('VODIY BUILDING MATERIALS MCHJ','300100201'),('AKRIL KIMYO SERVIS MCHJ','300100202')]
        invoices=[];payments=[]
        for i in range(6):
            invoices.append({'document_number':str(37+i),'document_date':f'2026-0{7+i//2}-{3+i:02d}','counterparty':partners[0][0],'counterparty_tin':partners[0][1],'direction':'outgoing','status_group':'signed','document_type_name':'Ҳисоб-фактура','total':11200000+i*1120000,'vat':1200000+i*120000,'amount_without_vat':10000000+i*1000000,'contract':'37/2026','currency':'UZS'})
            payments.append({'date':f'2026-0{7+i//2}-{10+i:02d}','document_number':str(401+i),'counterparty':'«FARGONA PREMIUM QURILISH» ООО','counterparty_account':'20208000123456789001','incoming':11200000+i*1120000,'outgoing':0,'purpose':f'Счет-фактура №{37+i} бўйича қурилиш материаллари учун тўлов, шартнома 37/2026'})
        invoices.append({'document_number':'81','document_date':'2026-09-10','counterparty':partners[1][0],'counterparty_tin':partners[1][1],'direction':'outgoing','status_group':'signed','document_type_name':'invoice','total':23520000,'vat':2520000,'amount_without_vat':21000000,'currency':'UZS'})
        invoices.append({'document_number':'109','document_date':'2026-09-11','counterparty':partners[2][0],'counterparty_tin':partners[2][1],'direction':'incoming','status_group':'signed','document_type_name':'invoice','total':44800000,'vat':4800000,'amount_without_vat':40000000,'currency':'UZS'})
        payments.append({'date':'2026-09-12','document_number':'501','counterparty':partners[2][0],'counterparty_tin':partners[2][1],'incoming':0,'outgoing':35000000,'purpose':'Акрил хом ашёси учун тўлов'})
        payments.append({'date':'2026-09-13','document_number':'502','counterparty':'YANGI HAMKOR MCHJ','counterparty_tin':'300100203','incoming':5500000,'outgoing':0,'purpose':'Олдиндан тўлов'})
        save({'document_type':'invoice_registry','invoices':invoices,'currency':'UZS'},'СИНОВ_фактуралар.xlsx')
        save({'document_type':'bank_statement','account_holder':company,'tax_id':'300999000','account_number':'20208000987654321000','currency':'UZS','transactions':payments},'СИНОВ_банк.xlsx')
        book=Ledger();recon=book.reconciliation('300100200','outgoing');recon['report_id']='DEMO-01'
        (output/'ASMAN_akt_sverka_namuna.pdf').write_bytes(build_reconciliation_pdf(recon))
        (output/'ASMAN_moliyaviy_tahlil_namuna.pdf').write_bytes(build_financial_report_pdf(book.snapshot()))
        print('Sample PDFs generated:',output)

if __name__=='__main__':main()
