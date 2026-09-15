from io import BytesIO
from telegram import InputFile
from ledger import Ledger, financial_text
from report_design import build_financial_report_pdf


def install_financial_pdf_ui(bot_module, business_module):
    original = bot_module.text_handler
    async def handler(update,context):
        if (update.message.text or '').strip() == '📈 Молиявий таҳлил':
            status=await update.message.reply_text('⏳ Молиявий ҳисобот тайёрланяпти...')
            try:
                snapshot=Ledger().snapshot()
                pdf=build_financial_report_pdf(snapshot)
                await status.edit_text(financial_text(snapshot)[:3900])
                await update.message.reply_document(InputFile(BytesIO(pdf),filename='ASMAN_moliyaviy_tahlil.pdf'),caption='Молиявий таҳлил: ҳар бир валюта алоҳида, қарзлар ва ҳужжат назорати.')
            except Exception:
                bot_module.log.exception('Financial report failed')
                await status.edit_text('Ҳисоботни тайёрлаб бўлмади. Қайта уриниб кўринг.')
            return
        await original(update,context)
    bot_module.text_handler=handler
