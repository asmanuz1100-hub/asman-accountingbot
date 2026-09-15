from io import BytesIO

from telegram import InputFile

import financial_report_pdf as financial_pdf

# Compatibility wrapper for the KPI helper argument order used by the report builder.
_original_kpi_cell = financial_pdf._kpi_cell


def _kpi_cell_compat(title, value, bg, style):
    return _original_kpi_cell(title, value, style, bg)


financial_pdf._kpi_cell = _kpi_cell_compat


def install_financial_pdf_ui(bot_module, business_module):
    original_text_handler = bot_module.text_handler

    async def text_handler(update, context):
        text = (update.message.text or "").strip()

        if text == "📈 Молиявий таҳлил":
            status = await update.message.reply_text("⏳ Молиявий таҳлил ва PDF инфографика тайёрланяпти...")
            try:
                business_module.reclassify_existing_expenses()
                summary = business_module.financial_analysis()
                snapshot = financial_pdf.collect_financial_snapshot()
                pdf_bytes = financial_pdf.build_financial_report_pdf(snapshot)

                # Telegram text messages have a practical length limit. Keep the
                # chat summary compact and put the full visual analysis in PDF.
                if len(summary) > 3800:
                    summary = summary[:3750] + "\n\n... Давоми PDF ҳисоботда."
                await status.edit_text(summary)

                stream = BytesIO(pdf_bytes)
                stream.seek(0)
                await update.message.reply_document(
                    document=InputFile(stream, filename="moliyaviy_tahlil_infografika.pdf"),
                    caption=(
                        "📊 Молиявий таҳлил — инфографика PDF\n"
                        "🟢 аванслар | 🔴 қарздорлик | 📈 диаграммалар | 📑 ҳужжат назорати"
                    ),
                )
            except Exception as exc:
                await status.edit_text(f"❌ Молиявий PDF ҳисобот тайёрлашда хато: {exc}")
            return

        await original_text_handler(update, context)

    bot_module.text_handler = text_handler
