import os

# Old Render Blueprint deployments may retain an invalid Telegram webhook secret.
# Remove it before starting the bot so python-telegram-bot does not send it.
os.environ.pop("WEBHOOK_SECRET", None)

from bot import main

if __name__ == "__main__":
    main()
