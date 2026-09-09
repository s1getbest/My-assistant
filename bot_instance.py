import telebot
import config
import sys
from logging_config import get_logger

logger = get_logger(__name__)

# Initialize shared Telegram Bot instance
if not config.TELEGRAM_TOKEN:
    logger.error("[ERROR] TELEGRAM_TOKEN environment variable is not set or is empty.")
    logger.error("[ERROR] Please set TELEGRAM_TOKEN in your environment variables.")
    sys.exit(1)

bot = telebot.TeleBot(config.TELEGRAM_TOKEN)
