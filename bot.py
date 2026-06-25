import os
import threading
import config
from bot_instance import bot
from scheduler_jobs import restore_reminders_on_startup, scheduler
from dashboard import app

# Import handlers to ensure all bot command & message routes are registered
import bot_handlers


def restore_reminders_background():
    """Run reminder restoration in background thread to avoid blocking startup."""
    try:
        print("[Main] Background: Starting reminder restoration from Tasks.md...")
        restore_reminders_on_startup(bot)
        print("[Main] Background: Reminder restoration completed.")
    except Exception as e:
        print(f"[Main] Background: Error during reminder restoration: {e}")


if __name__ == "__main__":
    print("[Main] Starting Telegram Bot & Flask Dashboard (Modular Edition)...")
    
    # === WEBHOOK SETUP ===
    WEBHOOK_URL = f"https://my-assistant-k7rq.onrender.com/webhook/{config.TELEGRAM_TOKEN}"
    print(f"[Main] Setting Telegram webhook to: {WEBHOOK_URL}")
    try:
        bot.remove_webhook()
        bot.set_webhook(url=WEBHOOK_URL)
        print("[Main] Webhook set successfully.")
    except Exception as e:
        print(f"[Main] Warning: Failed to set webhook: {e}")
    
    # === START SCHEDULER ===
    print("[Main] Starting Background Scheduler...")
    scheduler.start()
    print("[Main] Scheduler started.")

    # === RESTORE REMINDERS IN BACKGROUND ===
    # Run in background thread to avoid blocking Flask startup
    reminder_thread = threading.Thread(target=restore_reminders_background, daemon=True)
    reminder_thread.start()
    print("[Main] Reminder restoration started in background thread.")
    
    # === RUN FLASK APP ON MAIN THREAD ===
    # This prevents the container from exiting early on Render and loops synchronously
    port_number = int(os.environ.get("PORT", 10000))
    print(f"[Main] Running Flask application on port {port_number} synchronously on main thread...")
    app.run(host="0.0.0.0", port=port_number, use_reloader=False)
