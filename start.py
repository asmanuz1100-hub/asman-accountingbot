import os

# Old Render Blueprint deployments may retain an invalid Telegram webhook secret.
# Remove it before starting the bot so python-telegram-bot does not send it.
os.environ.pop("WEBHOOK_SECRET", None)

# Asakabank exports an HTML table with an .xls extension and without a normal
# Excel header row. Patch the spreadsheet analyzer before importing bot.py so
# these statements are parsed with the dedicated Asakabank layout handler.
import excel_docs
from asaka_excel import try_analyze_asaka

_original_analyze_spreadsheet = excel_docs.analyze_spreadsheet_bytes


def _analyze_spreadsheet(data: bytes, filename: str = "statement.xlsx") -> dict:
    asaka_result = try_analyze_asaka(data, filename)
    if asaka_result is not None:
        return asaka_result
    return _original_analyze_spreadsheet(data, filename)


excel_docs.analyze_spreadsheet_bytes = _analyze_spreadsheet

from bot import main

if __name__ == "__main__":
    main()
