# ASMAN Accounting AI Bot v2

Telegram учун ASMAN SILICAT бухгалтерия ва AI ҳужжат таҳлил боти.

## Ҳозир тайёр функциялар

- PDF ҳужжатни ўқиш ва AI таҳлил
- JPG / JPEG / PNG / WEBP расмни ўқиш ва AI таҳлил
- Ҳужжат турини аниқлаш
- Контрагент, СТИР, шартнома/фактура рақами, сана, валюта, ҚҚС, умумий сумма ва товар позицияларини ажратиш
- Шубҳали фарқлар бўйича огоҳлантириш
- `✅ Тасдиқлаш / ❌ Бекор қилиш`
- Ҳужжат фақат тасдиқдан кейин базага сақланади
- Ҳамкорлар
- Шартномалар ва шартнома қолдиғи
- Фақат хом ашё омбори
- Хом ашё кирими
- Қисқа ҳисобот

## Хавфсизлик

`BOT_TOKEN` ва `OPENAI_API_KEY` ни GitHub кодига ёзманг. Уларни Render -> Environment Variables орқали киритинг.

## База

Код `DATABASE_URL` берилса PostgreSQL'дан фойдаланади. `DATABASE_URL` бўлмаса локал тест учун SQLite (`asman.db`) ишлайди.

Render'да бухгалтерия маълумотларини доимий сақлаш учун PostgreSQL ёки бошқа ташқи доимий база улаш керак. Render Free Web Service локал диски доимий база сифатида ишончли эмас.

## Render

Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
python bot.py
```

Environment Variables:

- `BOT_TOKEN` — BotFather токени
- `OPENAI_API_KEY` — OpenAI API калити
- `OPENAI_MODEL` — `gpt-5.6-luna`
- `RUN_MODE` — `webhook`
- `DATABASE_URL` — PostgreSQL connection string
- `WEBHOOK_SECRET` — махфий тасодифий қиймат

Render одатда `RENDER_EXTERNAL_URL` ни ўзи беради. Агар берилмаса:

- `WEBHOOK_BASE_URL=https://YOUR-SERVICE.onrender.com`

## Локал тест

```bash
pip install -r requirements.txt
python bot.py
```

Локалда `RUN_MODE=polling` ишлатилади.

## Кейинги босқич

1. Чиқиш фактураси конструктори
2. Фактурадан шартнома суммасини автоматик айириш
3. Шартнома лимити етарли эмаслиги ҳақида огоҳлантириш
4. PDF фактура яратиш
5. Хом ашё киримини ҳужжатдан бир босишда омборга қабул қилиш
6. Excel/PDF ҳисобот
7. Фойдаланувчи роллари ва аудит журнали
