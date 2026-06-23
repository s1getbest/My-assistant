from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
import config
from bot_instance import bot
from key_manager import key_manager
from drive_service import read_file_from_drive, write_file_to_drive, get_today_tasks

# Initialize BackgroundScheduler with Moscow Timezone
scheduler = BackgroundScheduler(timezone=config.msk_tz)

def send_dynamic_reminder(chat_id, task_text):
    """
    Triggers dynamic reminders registered by users. Uses MODEL_COMPLEX.
    """
    try:
        prompt = f"Ты личный строгий ассистент Павел. Сработало напоминание: '{task_text}'. Напиши короткое, очень емкое и мотивирующее сообщение прямо сейчас."
        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=prompt
        )
        reply = response.text
    except Exception:
        reply = f"Пора делать: {task_text}"
    try:
        bot.send_message(chat_id, f"⏰ **НАПОМИНАНИЕ!**\n\n{reply}")
    except Exception as e:
        print(f"[Scheduler] Dynamic reminder send error: {e}")


def check_daily_sleep():
    """
    Scheduled job at 10:00 AM checking if sleep hours were logged.
    """
    try:
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")
        health_content = read_file_from_drive("Health.md")
        if today_str not in health_content:
            bot.send_message(
                config.MY_TELEGRAM_ID,
                "Павел, доброе утро! 🛌 Я заметил, что сегодня ты еще не записал свой сон. Расскажи, сколько часов удалось поспать и как самочувствие?",
            )
    except Exception as e:
        print(f"[Scheduler] Sleep check error: {e}")


def evening_planning_reminder():
    """
    Scheduled job at 20:00 PM reminding the user to plan their next day.
    """
    try:
        bot.send_message(
            config.MY_TELEGRAM_ID,
            "Павел, время вечернего планирования! 🌙 Пора разобрать дела и составить план на завтра, чтобы лечь спать с чистой головой.",
        )
    except Exception as e:
        print(f"[Scheduler] Evening reminder error: {e}")


def compress_memory():
    """
    Weekly background job to compress long-term memory (Memory.md) using MODEL_COMPLEX.
    Executed every Sunday at 03:00 AM.
    """
    try:
        print("[Scheduler] Starting weekly memory compression job...")
        content = read_file_from_drive("Memory.md")
        if not content.strip():
            print("[Scheduler] Memory.md is empty, skipping compression.")
            return

        prompt = f"""This is a long-term memory file. Compress it, remove duplicates, and keep only the most important facts as a concise list.

Current content:
---
{content}
---

Output only the resulting compressed list in Markdown format (using bullet points like "* fact"). Do not include any intro, outro, or additional conversational text.
"""
        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=prompt
        )
        compressed_text = response.text.strip()

        if compressed_text:
            write_file_to_drive("Memory.md", compressed_text)
            print("[Scheduler] Memory.md successfully compressed and updated.")
        else:
            print("[Scheduler] Warning: Compressed memory content is empty, skipping update.")
    except Exception as e:
        print(f"[Scheduler] Error compressing memory: {e}")


def morning_briefing():
    """
    Scheduled job at 06:00 AM providing Morning AI Briefing using MODEL_COMPLEX.
    Reads today's tasks and memory, and generates an inspiring briefing.
    """
    try:
        print("[Scheduler] Starting morning briefing job...")
        today_tasks = get_today_tasks()
        current_memory = read_file_from_drive("Memory.md")
        
        # Format today's tasks
        tasks_text = ""
        if today_tasks:
            for task in today_tasks:
                status = "[x]" if task.get("done") else "[ ]"
                tasks_text += f"- {status} {task.get('time', '—')} | {task.get('text')}\n"
        else:
            tasks_text = "Нет запланированных задач на сегодня."

        prompt = f"""Ты личный строгий и заботливый ассистент Павел. Твоя задача — составить мотивирующий и структурированный утренний брифинг для Павла.
Сегодняшняя дата: {datetime.now(config.msk_tz).strftime('%Y-%m-%d')}

Список сегодняшних задач:
{tasks_text}

Долгосрочная память (Memory.md):
---
{current_memory or "Пока пустая."}
---

Напиши короткий, мотивирующий брифинг. Отметь ключевые дела, дай одну мудрую или практичную мысль дня на основе его долгосрочной памяти или дел, и пожелай продуктивного дня. Будь краток и пиши по делу.
"""
        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=prompt
        )
        brief_reply = response.text.strip()
        
        bot.send_message(
            config.MY_TELEGRAM_ID,
            f"☀️ **ЕЖЕДНЕВНЫЙ УТРЕННИЙ БРИФИНГ**\n\n{brief_reply}"
        )
        print("[Scheduler] Morning briefing successfully sent.")
    except Exception as e:
        print(f"[Scheduler] Error generating morning briefing: {e}")


# Register scheduled cron jobs
scheduler.add_job(morning_briefing, 'cron', hour=6, minute=0)
scheduler.add_job(check_daily_sleep, 'cron', hour=10, minute=0)
scheduler.add_job(evening_planning_reminder, 'cron', hour=20, minute=0)
scheduler.add_job(compress_memory, 'cron', day_of_week='sun', hour=3, minute=0)
