import os
import config
from bot_instance import bot
from scheduler_jobs import scheduler
from dashboard import app

# Import handlers to ensure all bot command & message routes are registered
import bot_handlers

if __name__ == "__main__":
    print("[Main] Starting Telegram Bot & Flask Dashboard (Modular Edition)...")
    
    # === WEBHOOK SETUP ===
    WEBHOOK_URL = f"https://my-assistant-k7rq.onrender.com/webhook/{config.TELEGRAM_TOKEN}"
    print(f"[Main] Setting Telegram webhook to: {WEBHOOK_URL}")
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    
    # === START SCHEDULER ===
    print("[Main] Starting Background Scheduler...")
    scheduler.start()
    
    # === RUN FLASK APP ON MAIN THREAD ===
    # This prevents the container from exiting early on Render and loops synchronously
    port_number = int(os.environ.get("PORT", 10000))
    print(f"[Main] Running Flask application on port {port_number} synchronously on main thread...")
    app.run(host="0.0.0.0", port=port_number, use_reloader=False)
