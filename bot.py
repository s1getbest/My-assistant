import os
import threading
import config
from bot_instance import bot
from scheduler_jobs import restore_reminders_on_startup, scheduler
from dashboard import app
from logging_config import get_logger

# Import handlers to ensure all bot command & message routes are registered
import bot_handlers

logger = get_logger(__name__)


def restore_reminders_background():
    """Run reminder restoration in background thread to avoid blocking startup."""
    try:
        logger.info("[Main] Background: Starting reminder restoration from Tasks.md...")
        restore_reminders_on_startup(bot)
        logger.info("[Main] Background: Reminder restoration completed.")
    except Exception as e:
        logger.error(f"[Main] Background: Error during reminder restoration: {e}")


if __name__ == "__main__":
    logger.info("[Main] Starting Telegram Bot & Flask Dashboard (Modular Edition)...")

    # Note: Obsidian folder mapping is already initialized as a side effect of
    # `from dashboard import app` above (dashboard.py does it at module load
    # time so it also works when the dashboard is served standalone) - no
    # need to call initialize_folder_mapping() again here.

    # === WEBHOOK SETUP ===
    # The path segment and secret_token are derived from TELEGRAM_TOKEN (see
    # config.py) instead of being the token itself, so the real bot token
    # never appears in the webhook URL (and therefore never ends up in
    # Render/Flask access logs). The secret_token is echoed back by Telegram
    # on every update as the X-Telegram-Bot-Api-Secret-Token header, which
    # dashboard.py's webhook route verifies.
    if not config.WEBHOOK_PATH_SECRET:
        logger.error("[Main] Cannot set webhook: TELEGRAM_TOKEN is not configured.")
    else:
        WEBHOOK_URL = f"{config.WEBHOOK_BASE_URL}/webhook/{config.WEBHOOK_PATH_SECRET}"
        logger.info(f"[Main] Setting Telegram webhook to: {WEBHOOK_URL}")
        try:
            bot.remove_webhook()
            bot.set_webhook(url=WEBHOOK_URL, secret_token=config.WEBHOOK_SECRET_TOKEN)
            logger.info("[Main] Webhook set successfully.")
        except Exception as e:
            logger.warning(f"[Main] Warning: Failed to set webhook: {e}")

    # === START SCHEDULER ===
    logger.info("[Main] Starting Background Scheduler...")
    scheduler.start()
    logger.info("[Main] Scheduler started.")

    # === RESTORE REMINDERS IN BACKGROUND ===
    # Run in background thread to avoid blocking Flask startup
    reminder_thread = threading.Thread(target=restore_reminders_background, daemon=True)
    reminder_thread.start()
    logger.info("[Main] Reminder restoration started in background thread.")

    # === RUN FLASK APP ON MAIN THREAD ===
    # This prevents the container from exiting early on Render and loops synchronously
    port_number = int(os.environ.get("PORT", 10000))
    logger.info(f"[Main] Running Flask application on port {port_number} synchronously on main thread...")
    app.run(host="0.0.0.0", port=port_number, use_reloader=False)
