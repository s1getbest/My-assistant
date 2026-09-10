import hashlib
import re
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
import config
import vault_files
import university_schedule
import vault_index
from bot_instance import bot
from key_manager import key_manager
from logging_config import get_logger
from drive_service import (
    get_today_tasks,
    get_task_line_token,
    read_json_from_drive,
    read_file_from_drive,
    read_or_create_goals,
    update_file_on_drive,
    has_health_entry_for_date,
)
from ai_pipeline import (
    apply_format_rule,
    sanitize_telegram_text,
    is_ai_response_empty,
    parse_gemini_tags,
    apply_gemini_tags,
)

logger = get_logger(__name__)

# Initialize BackgroundScheduler with Moscow Timezone
scheduler = BackgroundScheduler(timezone=config.msk_tz)
TASK_LINE_RE = re.compile(r'^\s*[\*\-]?\s*\[(?P<status>[ xX])\]\s*(?P<body>.+)$')
TASK_DATETIME_RE = re.compile(r'(?P<dt>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\s*\|\s*(?P<text>.+)$')


def _normalize_run_date(run_date):
    if run_date.tzinfo is None:
        return config.msk_tz.localize(run_date)
    return run_date.astimezone(config.msk_tz)


def _clean_reminder_text(task_text):
    return task_text.replace("⏰ REMINDER:", "", 1).strip()


def _build_reminder_job_id(run_date, task_text):
    raw = f"{run_date.strftime('%Y-%m-%d %H:%M')}|{task_text}"
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]
    return f"reminder_{digest}"


def schedule_reminder_job(chat_id, task_text, run_date, task_line=None):
    run_date = _normalize_run_date(run_date)
    if not task_line:
        task_line = f"* [ ] {run_date.strftime('%Y-%m-%d %H:%M')} | ⏰ REMINDER: {task_text}"
    job_id = _build_reminder_job_id(run_date, task_text)
    scheduler.add_job(
        send_dynamic_reminder,
        'date',
        id=job_id,
        replace_existing=True,
        run_date=run_date,
        args=[chat_id, task_text, task_line],
        misfire_grace_time=300,
    )
    return job_id


def restore_reminders_on_startup(bot_instance):
    try:
        now = datetime.now(config.msk_tz)
        content = read_file_from_drive(vault_files.TASKS)
        if not content.strip():
            return 0

        restored_count = 0
        for line in content.split("\n"):
            match = TASK_LINE_RE.match(line.strip())
            if not match or match.group("status").lower() != " ":
                continue

            body = match.group("body").strip()
            dt_match = TASK_DATETIME_RE.search(body)
            if not dt_match:
                continue

            task_text = dt_match.group("text").strip()
            # Only re-schedule lines that were actually created as dynamic
            # reminders (the "⏰ REMINDER:" marker - see
            # ai_pipeline.py's SCHEDULE tag and schedule_reminder_job's own
            # default task_line). This used to also require "|" not in
            # body, which is impossible to satisfy here since
            # TASK_DATETIME_RE.search(body) already matched on a body
            # containing "|" - that clause was dead code, so the guard
            # never actually filtered anything, and EVERY future-dated open
            # task (a plain [TASK_ADD] chore, an injected university class,
            # a goal-driven micro-task from the morning briefing - none of
            # which the user asked to be pinged about) got turned into a
            # pushy AI-narrated Telegram notification with Done/Snooze
            # buttons, but only on days the process happened to restart
            # before their time - a startup-timing accident, not a
            # deliberate choice.
            if "⏰ REMINDER:" not in task_text:
                continue

            try:
                run_date = config.msk_tz.localize(
                    datetime.strptime(dt_match.group("dt"), "%Y-%m-%d %H:%M")
                )
            except ValueError:
                continue

            if run_date <= now:
                continue

            schedule_reminder_job(
                config.MY_TELEGRAM_ID,
                _clean_reminder_text(task_text),
                run_date,
                task_line=line.strip(),
            )
            restored_count += 1

        logger.info(f"[Scheduler] Restored {restored_count} reminders/tasks from Tasks.md on startup.")
        return restored_count
    except Exception as e:
        logger.error(f"[Scheduler] Reminder restore error: {e}")
        return 0

def send_dynamic_reminder(chat_id, task_text, task_line=None):
    """
    Triggers dynamic reminders registered by users. Uses MODEL_COMPLEX.
    """
    try:
        prompt = apply_format_rule(
            f"Ты личный строгий ассистент Павел. Сработало напоминание: '{task_text}'. Напиши короткое, очень емкое и мотивирующее сообщение прямо сейчас."
        )
        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=prompt
        )
        raw_text = response.text or ""
        reply = sanitize_telegram_text(raw_text) if not is_ai_response_empty(raw_text) else f"Пора делать: {task_text}"
    except Exception:
        reply = f"Пора делать: {task_text}"
    try:
        import telebot
        markup = telebot.types.InlineKeyboardMarkup(row_width=1)
        task_token = get_task_line_token(task_line)
        if not task_token:
            raise ValueError("Task token could not be generated for reminder callback.")
        btn_done = telebot.types.InlineKeyboardButton("✅ Done", callback_data=f"task_done:{task_token}")
        btn_snooze_1h = telebot.types.InlineKeyboardButton("⏰ Snooze 1h", callback_data=f"task_snooze_1h:{task_token}")
        btn_snooze_24h = telebot.types.InlineKeyboardButton("📅 Tomorrow", callback_data=f"task_snooze_24h:{task_token}")
        markup.add(btn_done, btn_snooze_1h, btn_snooze_24h)

        bot.send_message(chat_id, f"⏰ **НАПОМИНАНИЕ!**\n\n{reply}", reply_markup=markup)
    except Exception as e:
        logger.error(f"[Scheduler] Dynamic reminder send error: {e}")


def check_daily_sleep():
    """
    Scheduled job at 10:00 AM checking if sleep hours were logged.
    """
    try:
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")
        if not has_health_entry_for_date("sleep", today_str):
            bot.send_message(
                config.MY_TELEGRAM_ID,
                "Павел, доброе утро! 🛌 Я заметил, что сегодня ты еще не записал свой сон. Расскажи, сколько часов удалось поспать и как самочувствие?",
            )
    except Exception as e:
        logger.error(f"[Scheduler] Sleep check error: {e}")


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
        logger.error(f"[Scheduler] Evening reminder error: {e}")


def compress_memory():
    """
    Weekly background job to compress long-term memory (Memory.md) using MODEL_COMPLEX.
    Executed every Sunday at 03:00 AM.
    """
    try:
        logger.info("[Scheduler] Starting weekly memory compression job...")
        content = read_file_from_drive(vault_files.MEMORY)
        if not content.strip():
            logger.info("[Scheduler] Memory.md is empty, skipping compression.")
            return

        prompt = apply_format_rule(f"""This is a long-term memory file. Compress it, remove duplicates, and keep only the most important facts as a concise list.

Current content:
---
{content}
---

Output only the resulting compressed list in Markdown format (using bullet points like "* fact"). Do not include any intro, outro, or additional conversational text.
""")
        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=prompt
        )
        raw_text = response.text or ""
        if is_ai_response_empty(raw_text):
            logger.warning("[Scheduler] Memory compression AI response was empty, skipping update.")
            return

        compressed_text = sanitize_telegram_text(raw_text)

        if compressed_text:
            def mutate(current_content):
                # The AI compressed a specific snapshot of Memory.md (`content`,
                # read above). If the file changed since then - e.g. the user
                # added a new [MEMORY] fact while this job was waiting on the
                # AI call - overwriting now would silently drop that fact.
                # Bail out and let next week's run compress it instead.
                if current_content != content:
                    logger.warning(
                        "[Scheduler] Memory.md changed while compression was in progress; "
                        "skipping this write to avoid discarding the newer content."
                    )
                    return None
                return compressed_text

            result = update_file_on_drive(vault_files.MEMORY, mutate)
            if result is not None:
                logger.info("[Scheduler] Memory.md successfully compressed and updated.")
        else:
            logger.warning("[Scheduler] Warning: Compressed memory content is empty, skipping update.")
    except Exception as e:
        logger.error(f"[Scheduler] Error compressing memory: {e}")


def auto_archive_stale_tasks():
    """
    Finds open tasks in Tasks.md older than 7 days, removes them, and appends to Icebox.md.
    Returns the number of archived tasks.
    """
    try:
        date_pattern = re.compile(r'\d{4}-\d{2}-\d{2}')
        now = datetime.now(config.msk_tz)
        seven_days_ago = now - timedelta(days=7)
        stale_tasks_holder = {"lines": []}

        def mutate_tasks(content):
            if not content.strip():
                return None
            lines = content.split("\n")
            remaining_lines = []
            stale_tasks = []

            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue

                is_open = "[ ]" in stripped
                if is_open:
                    m = date_pattern.search(stripped)
                    if m:
                        try:
                            task_date = datetime.strptime(m.group(0), "%Y-%m-%d")
                            task_date = config.msk_tz.localize(task_date)
                            if task_date < seven_days_ago:
                                stale_tasks.append(line)
                                continue
                        except Exception:
                            pass
                remaining_lines.append(line)

            if not stale_tasks:
                return None
            stale_tasks_holder["lines"] = stale_tasks
            return "\n".join(remaining_lines)

        # Both Tasks.md and Icebox.md are updated under their own per-file
        # lock (via update_file_on_drive) so a concurrent write to either
        # file can't be lost mid-archive.
        tasks_result = update_file_on_drive(vault_files.TASKS, mutate_tasks)
        if tasks_result is None:
            # Either there was nothing stale to remove, or the Drive write
            # itself failed after mutate_tasks() ran - in both cases nothing
            # was actually removed from Tasks.md, so nothing should be
            # copied into Icebox.md either (that would duplicate the task).
            return 0

        stale_tasks = stale_tasks_holder["lines"]

        if stale_tasks:
            def mutate_icebox(icebox_content):
                if icebox_content.strip():
                    return icebox_content.rstrip() + "\n" + "\n".join(stale_tasks)
                return "# Icebox (Someday/Maybe)\n\n" + "\n".join(stale_tasks)

            update_file_on_drive(vault_files.ICEBOX, mutate_icebox)

        return len(stale_tasks)
    except Exception as e:
        logger.error(f"[Auto-Archiver] Error: {e}")
        return 0


def morning_briefing():
    """
    Scheduled job at 06:00 AM providing Morning AI Briefing using MODEL_COMPLEX.
    Reads today's tasks, memory, and goals, and generates an inspiring briefing.
    Auto-adds goal-driven micro-tasks to Tasks.md via parse_gemini_tags.
    """
    try:
        logger.info("[Scheduler] Starting morning briefing job...")
        today_tasks = get_today_tasks()
        current_memory = read_file_from_drive(vault_files.MEMORY)
        goals_content = read_or_create_goals()
        flashcards = read_json_from_drive(vault_files.FLASHCARDS)
        now = datetime.now(config.msk_tz)
        today_str = now.strftime("%Y-%m-%d")

        # Format today's tasks
        tasks_text = ""
        if today_tasks:
            for task in today_tasks:
                status = "[x]" if task.get("done") else "[ ]"
                tasks_text += f"- {status} {task.get('time', '—')} | {task.get('text')}\n"
        else:
            tasks_text = "Нет запланированных задач на сегодня."

        review_cards = []
        if isinstance(flashcards, list):
            sortable_cards = []
            for card in flashcards:
                try:
                    review_dt = datetime.strptime(card.get("next_review", ""), "%Y-%m-%d %H:%M:%S")
                    review_dt = config.msk_tz.localize(review_dt)
                    sortable_cards.append((review_dt, card))
                except Exception:
                    continue
            sortable_cards.sort(key=lambda item: item[0])
            overdue = [card for review_dt, card in sortable_cards if review_dt <= now]
            review_cards = overdue[:2]

        review_text = "Нет карточек для повторения."
        if review_cards:
            review_text = "\n".join(
                f"🧠 Повторение: {card.get('q', '—')} -> {card.get('a', '—')}"
                for card in review_cards
            )

        prompt = apply_format_rule(f"""You are an elite productivity coach. Review the user's long-term goals in Goals.md and their schedule in Tasks.md.
  1. Write an inspiring, concise morning briefing (NO double asterisks `**`, NO TTS audio).
  2. Formulate EXACTLY ONE actionable micro-task for today that advances the user toward one of their long-term goals.
  3. At the very end of your output, emit the tag: [TASK_ADD] YYYY-MM-DD 10:00 | 🎯 GOAL: <micro-task text> (using today's date in Europe/Moscow timezone).

Today's date: {today_str}
Today's time: 10:00

Today's tasks:
{tasks_text}

Flashcards for review:
{review_text}

Long-term memory (Memory.md):
---
{current_memory or "Empty."}
---

Long-term goals (Goals.md):
---
{goals_content or "Empty."}
---

Write a concise, inspiring morning briefing in Russian. Highlight key tasks, add review block, suggest ONE small actionable micro-task for today that advances long-term goals, and wish productive day. Be brief and to the point.
""")
        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=prompt
        )
        raw_text = (response.text or "").strip()
        if is_ai_response_empty(raw_text):
            logger.warning("[Scheduler] Morning briefing AI response was empty, skipping send.")
            return

        # Parse and apply tags to auto-add goal task to Tasks.md
        tags = parse_gemini_tags(raw_text)
        if tags:
            apply_gemini_tags(tags)
            logger.info(f"[Scheduler] Morning briefing generated {len(tags)} tags")

        brief_reply_clean = sanitize_telegram_text(raw_text)

        if review_cards:
            review_block = "\n".join(
                f"🧠 Повторение: {card.get('q', '—')} -> {card.get('a', '—')}"
                for card in review_cards
            )
            brief_reply_clean = f"{brief_reply_clean}\n\n{review_block}"

        bot.send_message(
            config.MY_TELEGRAM_ID,
            f"☀️ ЕЖЕДНЕВНЫЙ УТРЕННИЙ БРИФИНГ\n\n{brief_reply_clean}"
        )

        logger.info("[Scheduler] Morning briefing successfully sent.")
    except Exception as e:
        logger.error(f"[Scheduler] Error generating morning briefing: {e}")


def weekly_audit():
    """
    Weekly cron job at 20:00 Sunday (Moscow time) analyzing the past 7 days.
    """
    try:
        logger.info("[Scheduler] Starting weekly audit job...")

        # Anti-Burnout Auto-Archiver
        archived_count = auto_archive_stale_tasks()

        now = datetime.now(config.msk_tz)
        dates = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]

        tasks_content = read_file_from_drive(vault_files.TASKS)
        finance_content = read_file_from_drive(vault_files.FINANCE)
        health_content = read_file_from_drive(vault_files.HEALTH)

        def filter_last_7_days(content, dates_list):
            filtered = []
            for line in content.split("\n"):
                if any(d in line for d in dates_list):
                    filtered.append(line)
            return "\n".join(filtered)

        tasks_7d = filter_last_7_days(tasks_content, dates)
        finance_7d = filter_last_7_days(finance_content, dates)
        health_7d = filter_last_7_days(health_content, dates)

        # Second Brain weekly activity: which people/media/project entities
        # were first created this week, using Index.json's "added" date
        # (see vault_index.py). Only entities *created* this week show up
        # here, not every update to an existing one - good enough for a
        # "what's new" weekly summary without needing per-update history.
        index_data = vault_index.read_index()

        def added_this_week(category):
            return [e for e in index_data.get(category, []) if e.get("added") in dates]

        new_people = added_this_week("people")
        new_media = added_this_week("media")
        new_projects = added_this_week("projects")
        second_brain_summary = (
            f"Новые люди: {', '.join(e.get('name', '?') for e in new_people) or 'нет'}\n"
            f"Новые медиа (фильмы/аниме/книги/игры): {', '.join(e.get('title', '?') for e in new_media) or 'нет'}\n"
            f"Новые проекты: {', '.join(e.get('name', '?') for e in new_projects) or 'нет'}"
        )

        prompt = apply_format_rule(f"""Act as a strict but supportive life coach. Analyze this 7-day data.
Summarize spending, average sleep, task completion, and social/media/project activity from the Second Brain section. Provide 1 actionable insight and ask for next week's goals.

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

### Second Brain (новое за неделю - люди/медиа/проекты):
---
{second_brain_summary}
---

Write a comprehensive, professional, yet warm and inspiring Markdown report. Deliver direct feedback as a dedicated coach. Use clear headings, list structures, and highlighted insights.
""")
        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=prompt
        )
        raw_text = response.text or ""
        if is_ai_response_empty(raw_text):
            logger.warning("[Scheduler] Weekly audit AI response was empty, skipping send.")
            return

        report = sanitize_telegram_text(raw_text)

        # Append anti-burnout stat
        report += f"\n\n🧊 Moved {archived_count} stale tasks to the Icebox."

        try:
            bot.send_message(
                config.MY_TELEGRAM_ID,
                f"📊 **ЕЖЕНЕДЕЛЬНЫЙ ИНФОРМАЦИОННЫЙ АУДИТ (RESET)**\n\n{report}",
                parse_mode="Markdown"
            )
        except Exception as parse_err:
            logger.warning(f"[Scheduler] Telegram markdown parsing failed, trying HTML/plain: {parse_err}")
            bot.send_message(
                config.MY_TELEGRAM_ID,
                f"📊 ЕЖЕНЕДЕЛЬНЫЙ ИНФОРМАЦИОННЫЙ АУДИТ (RESET)\n\n{report}"
            )
        logger.info("[Scheduler] Weekly audit successfully sent.")
    except Exception as e:
        logger.error(f"[Scheduler] Error generating weekly audit: {e}")


def inject_todays_classes():
    """
    Daily job (ARCHITECTURE.md step 6): looks up today's classes in
    Расписание.md (by day-of-week + week parity, computed deterministically
    - not AI-parsed) and adds any missing ones to Tasks.md as todo items.

    Idempotent by design: safe to run more than once for the same day
    (e.g. after a mid-day restart) - a class already present (matched by
    today's date + subject + the 🎓 marker) is not added again.
    """
    try:
        today = datetime.now(config.msk_tz).date()
        classes = university_schedule.get_classes_for_date(today)
        if not classes:
            logger.info("[Scheduler] No classes scheduled for today.")
            return

        today_str = today.strftime("%Y-%m-%d")
        added_count = {"n": 0}

        def mutate(content):
            lines = content.split("\n") if content.strip() else []
            new_lines = []
            for time_str, subject in classes:
                already_present = any(
                    today_str in existing_line and subject in existing_line and "🎓" in existing_line
                    for existing_line in lines
                )
                if already_present:
                    continue
                new_lines.append(f"* [ ] {today_str} {time_str} | 🎓 {subject}")
                added_count["n"] += 1
            if not new_lines:
                return None
            return "\n".join(lines + new_lines) if lines else "\n".join(new_lines)

        update_file_on_drive(vault_files.TASKS, mutate)
        if added_count["n"]:
            logger.info(f"[Scheduler] Added {added_count['n']} class(es) from Расписание.md to today's Tasks.md.")
    except Exception as e:
        logger.error(f"[Scheduler] Error injecting today's classes: {e}")


# Register scheduled cron jobs
scheduler.add_job(inject_todays_classes, 'cron', hour=5, minute=55)
scheduler.add_job(morning_briefing, 'cron', hour=6, minute=0)
scheduler.add_job(check_daily_sleep, 'cron', hour=10, minute=0)
scheduler.add_job(evening_planning_reminder, 'cron', hour=20, minute=0)
scheduler.add_job(compress_memory, 'cron', day_of_week='sun', hour=3, minute=0)
scheduler.add_job(weekly_audit, 'cron', day_of_week='sun', hour=20, minute=0)
