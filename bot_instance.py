import telebot
import config
import sys

# Initialize shared Telegram Bot instance
if not config.TELEGRAM_TOKEN:
    print("[ERROR] TELEGRAM_TOKEN environment variable is not set or is empty.")
    print("[ERROR] Please set TELEGRAM_TOKEN in your environment variables.")
    sys.exit(1)

bot = telebot.TeleBot(config.TELEGRAM_TOKEN)
