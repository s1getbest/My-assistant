from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
import config
from bot_instance import bot
from key_manager import key_manager
from drive_service import read_file_from_drive, write_file_to_drive, get_today_tasks, read_or_create_goals

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
        import telebot
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        btn_done = telebot.types.InlineKeyboardButton("✅ Done", callback_data=f"task_done:{task_text[:45]}")
        btn_snooze_1h = telebot.types.InlineKeyboardButton("⏰ Snooze 1h", callback_data=f"task_snooze_1h:{task_text[:45]}")
        btn_snooze_24h = telebot.types.InlineKeyboardButton("📅 Tomorrow", callback_data=f"task_snooze_24h:{task_text[:45]}")
        markup.add(btn_done, btn_snooze_1h, btn_snooze_24h)
        
        bot.send_message(chat_id, f"⏰ **НАПОМИНАНИЕ!**\n\n{reply}", reply_markup=markup)
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
    Reads today's tasks, memory, and goals, and generates an inspiring briefing.
    """
    try:
        print("[Scheduler] Starting morning briefing job...")
        today_tasks = get_today_tasks()
        current_memory = read_file_from_drive("Memory.md")
        goals_content = read_or_create_goals()
        
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

Долгосрочные цели (Goals.md):
---
{goals_content or "Пока пустые."}
---

Review the user's long-term goals. Suggest ONE small, actionable task for today that moves them closer to these goals, and include it in your briefing message.

Напиши короткий, мотивирующий брифинг на русском языке. Отметь ключевые дела, предложи одну маленькую конкретную сегодняшнюю задачу для достижения его долгосрочных целей, дай одну мудрую или практичную мысль дня и пожелай продуктивного дня. Будь краток и пиши по делу.
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
        
        # Text-to-Speech Morning Podcast generation
        audio_path = "morning_podcast.mp3"
        try:
            print("[Scheduler] Starting Morning Podcast synthesis...")
            import asyncio
            import edge_tts
            
            async def generate_podcast():
                clean_text = brief_reply.replace("**", "").replace("*", "").replace("##", "").replace("#", "").replace("[ ]", "").replace("[x]", "")
                communicate = edge_tts.Communicate(clean_text, "ru-RU-DmitryNeural")
                await communicate.save(audio_path)
                
            asyncio.run(generate_podcast())
            
            with open(audio_path, 'rb') as audio_file:
                bot.send_voice(
                    config.MY_TELEGRAM_ID,
                    audio_file,
                    caption="🌅 Your Morning Podcast"
                )
            print("[Scheduler] Morning Podcast successfully generated and sent.")
        except Exception as tts_err:
            print(f"[Scheduler] TTS Podcast generation/send failed: {tts_err}")
            
        print("[Scheduler] Morning briefing successfully sent.")
    except Exception as e:
        print(f"[Scheduler] Error generating morning briefing: {e}")


def weekly_audit():
    """
    Weekly cron job at 20:00 Sunday (Moscow time) analyzing the past 7 days.
    """
    try:
        print("[Scheduler] Starting weekly audit job...")
        import datetime
        now = datetime.datetime.now(config.msk_tz)
        dates = [(now - datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
        
        tasks_content = read_file_from_drive("Tasks.md")
        finance_content = read_file_from_drive("Finance.md")
        health_content = read_file_from_drive("Health.md")
        
        def filter_last_7_days(content, dates_list):
            filtered = []
            for line in content.split("\n"):
                if any(d in line for d in dates_list):
                    filtered.append(line)
            return "\n".join(filtered)
            
        tasks_7d = filter_last_7_days(tasks_content, dates)
        finance_7d = filter_last_7_days(finance_content, dates)
        health_7d = filter_last_7_days(health_content, dates)
        
        prompt = f"""Act as a strict but supportive life coach. Analyze this 7-day data.
Summarize spending, average sleep, and task completion. Provide 1 actionable insight and ask for next week's goals.

Here is the data for the past 7 days (dates: {', '.join(dates[::-1])}):

### Tasks.md (7-day data):
---
{tasks_7d or "Нет записей."}
---

### Finance.md (7-day data):
---
{finance_7d or "Нет записей."}
---

### Health.md (7-day data):
---
{health_7d or "Нет записей."}
---

Write a comprehensive, professional, yet warm and inspiring Markdown report. Deliver direct feedback as a dedicated coach. Use clear headings, list structures, and highlighted insights.
"""
        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=prompt
        )
        report = response.text.strip()
        
        try:
            bot.send_message(
                config.MY_TELEGRAM_ID,
                f"📊 **ЕЖЕНЕДЕЛЬНЫЙ ИНФОРМАЦИОННЫЙ АУДИТ (RESET)**\n\n{report}",
                parse_mode="Markdown"
            )
        except Exception as parse_err:
            print(f"[Scheduler] Telegram markdown parsing failed, trying HTML/plain: {parse_err}")
            bot.send_message(
                config.MY_TELEGRAM_ID,
                f"📊 ЕЖЕНЕДЕЛЬНЫЙ ИНФОРМАЦИОННЫЙ АУДИТ (RESET)\n\n{report}"
            )
        print("[Scheduler] Weekly audit successfully sent.")
    except Exception as e:
        print(f"[Scheduler] Error generating weekly audit: {e}")


# Register scheduled cron jobs
scheduler.add_job(morning_briefing, 'cron', hour=6, minute=0)
scheduler.add_job(check_daily_sleep, 'cron', hour=10, minute=0)
scheduler.add_job(evening_planning_reminder, 'cron', hour=20, minute=0)
scheduler.add_job(compress_memory, 'cron', day_of_week='sun', hour=3, minute=0)
scheduler.add_job(weekly_audit, 'cron', day_of_week='sun', hour=20, minute=0)
