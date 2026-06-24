import re
from datetime import datetime
import telebot
from google.genai import types
import config
from bot_instance import bot
from key_manager import key_manager
from drive_service import (
    append_line_to_drive,
    delete_line_from_task_file,
    edit_line_in_task_file,
    list_markdown_files,
    read_file_from_drive,
    write_file_to_drive,
)

# === REGEX CONSTANTS ===
TAG_LINE_RE = re.compile(
    r'^\[(TASK_ADD|TASK_DEL|TASK_EDIT|FINANCE|HEALTH|MEMORY|SCHEDULE|QUESTION|MOOD)\]\s*(.+)$',
    re.MULTILINE
)
TASK_TIME_RE = re.compile(r'(\d{2}:\d{2})\s*\|\s*(.+)$')


def is_me(message):
    return message.from_user.id == config.MY_TELEGRAM_ID


def parse_gemini_tags(raw_text):
    tags = []
    for match in TAG_LINE_RE.finditer(raw_text):
        tags.append((match.group(1), match.group(2).strip()))
    return tags


def extract_reply(raw_text):
    if "[ОТВЕТ]" in raw_text:
        body = raw_text.split("[ОТВЕТ]", 1)[1]
    else:
        body = raw_text
    reply_lines = []
    for line in body.split("\n"):
        if TAG_LINE_RE.match(line.strip()):
            continue
        reply_lines.append(line)
    return "\n".join(reply_lines).strip() or raw_text.strip()


def get_extraction_rules(today_str):
    return f"""Если из сообщения нужно извлечь данные, добавь в конце ответа ОДНУ строку на каждый тип (только если применимо):
[TASK_ADD] ГГГГ-ММ-ДД ЧЧ:ММ | Описание задачи или рутины
[TASK_DEL] text_to_find
[TASK_EDIT] text_to_find || ГГГГ-ММ-ДД ЧЧ:ММ | Новое описание задачи
[FINANCE] ГГГГ-ММ-ДД: сумма | категория | описание
[HEALTH] ГГГГ-ММ-ДД: часы
[MEMORY] факт для долгосрочной памяти
[SCHEDULE] ГГГГ-ММ-ДД ЧЧ:ММ | Текст напоминания
[QUESTION] Name: суть вопроса

Если пользователь просит удалить задачу, используй [TASK_DEL] и передай уникальный фрагмент текста для поиска.
Если пользователь просит изменить задачу, используй [TASK_EDIT] в формате `старый_текст || новая_строка`.
Если пользователь просит напомнить заранее, например "за 1 час" или "за 1 день" до события, вычисли точную дату и время напоминания и выдай [SCHEDULE] с уже рассчитанным временем.

Примеры распознавания:
- "поспал 8 часов" → [HEALTH] {today_str}: 8
- "потратил 450р на обед" → [FINANCE] {today_str}: 450 | еда | обед
- "напомни в 21:00 позвонить маме" → [SCHEDULE] {today_str} 21:00 | Позвонить маме
- "завтра в 9 утра тренировка" → [TASK_ADD] <дата> 09:00 | Тренировка
- "удали задачу созвон с Димой" → [TASK_DEL] созвон с Димой
- "перенеси тренировку на завтра в 8" → [TASK_EDIT] тренировка || <новая дата> 08:00 | Тренировка
"""


def build_task_line(payload):
    return f"* [ ] {payload.strip()}"


def apply_gemini_tags(tags):
    for tag_type, payload in tags:
        if not payload:
            continue
        try:
            if tag_type == "TASK_ADD":
                append_line_to_drive("Tasks.md", build_task_line(payload))
            elif tag_type == "TASK_DEL":
                delete_line_from_task_file(payload)
            elif tag_type == "TASK_EDIT" and "||" in payload:
                search_text, new_line_text = payload.split("||", 1)
                edit_line_in_task_file(
                    search_text.strip(),
                    build_task_line(new_line_text)
                )
            elif tag_type == "FINANCE":
                append_line_to_drive("Finance.md", f"* {payload}")
            elif tag_type == "HEALTH":
                append_line_to_drive("Health.md", f"* {payload}")
            elif tag_type == "MEMORY":
                append_line_to_drive("Memory.md", f"* {payload}")
            elif tag_type == "QUESTION":
                append_line_to_drive("Questions.md", f"* {payload}")
            elif tag_type == "MOOD":
                today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")
                append_line_to_drive("Health.md", f"* {today_str}: Mood {payload}")
                from drive_service import add_user_xp
                add_user_xp(5)
            elif tag_type == "SCHEDULE" and "|" in payload:
                dt_str, task_text = payload.split("|", 1)
                dt_str, task_text = dt_str.strip(), task_text.strip()
                run_date = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
                run_date = config.msk_tz.localize(run_date)

                from scheduler_jobs import schedule_reminder_job
                schedule_reminder_job(config.MY_TELEGRAM_ID, task_text, run_date)
                append_line_to_drive(
                    "Tasks.md",
                    f"* [ ] {dt_str} | ⏰ REMINDER: {task_text}"
                )
        except Exception as e:
            print(f"[Tag Apply] Tag apply error [{tag_type}]: {e}")


def get_forward_sender_name(message):
    """
    Safely extracts the original sender's name from a forwarded message.
    """
    if not message.forward_origin:
        return None
    try:
        origin = message.forward_origin
        o_type = getattr(origin, 'type', None)
        if o_type == 'user':
            u = getattr(origin, 'sender_user', None)
            if u:
                parts = []
                if getattr(u, 'first_name', None):
                    parts.append(u.first_name)
                if getattr(u, 'last_name', None):
                    parts.append(u.last_name)
                name = " ".join(parts).strip()
                if not name and getattr(u, 'username', None):
                    name = u.username
                return name or "User"
        elif o_type == 'hidden_user':
            return getattr(origin, 'sender_user_name', "Hidden User")
        elif o_type == 'chat':
            c = getattr(origin, 'sender_chat', None)
            if c:
                return getattr(c, 'title', "Chat")
        elif o_type == 'channel':
            c = getattr(origin, 'chat', None)
            if c:
                return getattr(c, 'title', "Channel")
        
        # Fallback to older telegram message fields
        if getattr(message, 'forward_from', None):
            u = message.forward_from
            parts = [getattr(u, 'first_name', ""), getattr(u, 'last_name', "")]
            name = " ".join([p for p in parts if p]).strip()
            return name or getattr(u, 'username', None) or "User"
        elif getattr(message, 'forward_from_chat', None):
            return getattr(message.forward_from_chat, 'title', "Chat")
        elif getattr(message, 'forward_sender_name', None):
            return message.forward_sender_name
    except Exception as e:
        print(f"[Forwards] Error getting forward sender name: {e}")
    return "Unknown Sender"


# === BOT HANDLERS ===

@bot.message_handler(commands=['start'])
def send_welcome(message):
    if not is_me(message):
        return
    bot.reply_to(message, "Привет! Твой личный мозг запущен. Я подключен к Google Диску и Obsidian!")


@bot.message_handler(commands=['spent'])
def track_expense(message):
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        args = message.text.split()
        if len(args) < 2:
            bot.reply_to(message, "Укажи сумму и описание. Пример: `/spent 500 еда обед`")
            return
        amount = args[1]
        category = args[2] if len(args) > 2 else "Разное"
        desc = " ".join(args[3:]) if len(args) > 3 else category
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")
        line = f"* {today_str}: {amount} | {category} | {desc}"
        append_line_to_drive("Finance.md", line)
        from drive_service import add_user_xp
        add_user_xp(5)
        bot.reply_to(message, f"💸 **Расход записан!** (+5 XP)\n\n> {amount} ₽ · {category}\n> {desc}")
    except Exception as e:
        bot.reply_to(message, f"Ошибка записи расхода: {e}")


@bot.message_handler(commands=['sleep'])
def track_sleep(message):
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        args = message.text.split()
        if len(args) < 2:
            bot.reply_to(message, "Укажи часы сна. Пример: `/sleep 7.5`", parse_mode="Markdown")
            return
        hours = args[1]
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")
        append_line_to_drive("Health.md", f"* {today_str}: {hours}")
        from drive_service import add_user_xp
        add_user_xp(5)
        bot.reply_to(message, f"🛌 **Сон записан!** (+5 XP)\n\n> {today_str} · {hours} ч.")
    except Exception as e:
        bot.reply_to(message, f"Ошибка записи сна: {e}")


@bot.message_handler(content_types=['voice'])
def handle_voice(message):
    """
    Handles voice messages using MODEL_COMPLEX (native audio support).
    """
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        voice_info = bot.get_file(message.voice.file_id)
        downloaded_file = bot.download_file(voice_info.file_path)

        current_memory = read_file_from_drive("Memory.md")
        if not current_memory.strip():
            current_memory = "Пока пустая долгосрочная память."

        now_msk = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")

        # Check if it's a journal entry
        is_journal = False
        if message.reply_to_message and message.reply_to_message.text and '/journal' in message.reply_to_message.text:
            is_journal = True
        elif message.caption and message.caption.startswith('/journal'):
            is_journal = True

        if is_journal:
            prompt = f"""Текущее время в Москве: {now_msk}
Сегодняшняя дата: {today_str}

Пользователь прислал голосовую запись в свой личный дневник (Journal).
Внимательно прослушай аудиофайл и распознай глубокие размышления Павла.

Act as an empathetic listener and coach. Respond with a short, supportive reply. At the very end of your response, add a new tag: `[MOOD] score/10`, where score is your assessment of their emotional state (1-10).

Помимо тегов, начни свой живой поддерживающий ответ с [ОТВЕТ], чтобы отделить живой ответ от тегов.

Формат ответа:
[ОТВЕТ]
Твой ответ пользователю на русском языке
[MOOD] score/10
"""
        else:
            extraction_rules = get_extraction_rules(today_str)
            prompt = f"""Текущее время в Москве: {now_msk}
Сегодняшняя дата: {today_str}

Долгосрочная память (Memory.md):
---
{current_memory}
---

Пользователь прислал голосовое сообщение. Текст голосового сообщения находится в прикрепленном аудиофайле.
Внимательно прослушай аудиофайл и распознай, что говорит S1get.

Ответь чётко и по делу. В [ОТВЕТ] — только живой ответ пользователю, без дублирования памяти.

{extraction_rules}

Формат ответа:
[ОТВЕТ]
Твой ответ пользователю
(далее теги, если нужны — каждый с новой строки)
"""
        # Voice messages always use MODEL_COMPLEX
        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=[
                types.Part.from_bytes(
                    data=downloaded_file,
                    mime_type="audio/ogg"
                ),
                prompt
            ]
        )
        raw_text = response.text

        tags = parse_gemini_tags(raw_text)
        reply_part = extract_reply(raw_text)
        apply_gemini_tags(tags)

        bot.reply_to(message, reply_part)
    except Exception as e:
        bot.reply_to(message, f"Ошибка обработки голосового сообщения: {e}")


@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    """
    Handles photo messages using MODEL_COMPLEX (vision support).
    """
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        # Get highest resolution photo
        photo = message.photo[-1]
        file_info = bot.get_file(photo.file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        now_msk = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")
        
        caption = message.caption or ""
        extraction_rules = get_extraction_rules(today_str)

        prompt = f"""Текущее время в Москве: {now_msk}
Сегодняшняя дата: {today_str}

Пользователь прислал изображение. Вот его описание/подпись (если есть): "{caption}"

Analyze this image. If it's a receipt, calculate the total and output `[FINANCE] YYYY-MM-DD: amount | category | description`. If it's handwritten notes or a whiteboard, extract actionable items as `[TASK_ADD] YYYY-MM-DD HH:MM | Task`. If it's an article/screenshot, summarize it as `[MEMORY] summary`.
{extraction_rules}

Помимо тегов, напиши пользователю краткий содержательный ответ/комментарий. Начни свой ответ с [ОТВЕТ], чтобы отделить живой ответ от тегов.

Формат ответа:
[ОТВЕТ]
Твой ответ пользователю
(далее теги, если нужны — каждый с новой строки)
"""

        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=[
                types.Part.from_bytes(
                    data=downloaded_file,
                    mime_type="image/jpeg"
                ),
                prompt
            ]
        )
        raw_text = response.text

        tags = parse_gemini_tags(raw_text)
        reply_part = extract_reply(raw_text)
        apply_gemini_tags(tags)

        bot.reply_to(message, reply_part)
    except Exception as e:
        bot.reply_to(message, f"Ошибка обработки изображения: {e}")


@bot.inline_handler(func=lambda query: len(query.query) > 0)
def handle_inline_query(inline_query):
    """
    Inline mode handler for quick capture. Uses MODEL_LITE.
    """
    if inline_query.from_user.id != config.MY_TELEGRAM_ID:
        return
    try:
        text = inline_query.query.strip()
        now_msk = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")
        extraction_rules = get_extraction_rules(today_str)

        prompt = f"""Текущее время в Москве: {now_msk}
Сегодняшняя дата: {today_str}

Пользователь отправил быструю заметку через Inline-режим: "{text}"

{extraction_rules}
Пожалуйста, будь точен в распознавании. Никакого другого текста писать НЕ нужно, только теги с новой строки (если применимо).
"""
        response = key_manager.generate_content(
            model=config.MODEL_LITE,
            contents=prompt
        )
        raw_text = response.text

        tags = parse_gemini_tags(raw_text)
        apply_gemini_tags(tags)

        # Build inline result
        r = telebot.types.InlineQueryResultArticle(
            id='1',
            title='✅ Task/Data captured!',
            input_message_content=telebot.types.InputTextMessageContent(
                message_text=f"✅ Успешно записано в Time OS: {text}"
            ),
            description=f"Распознать и сохранить: {text}"
        )
        bot.answer_inline_query(inline_query.id, [r], cache_time=1)
    except Exception as e:
        print(f"[Inline Query] Error handling query: {e}")


@bot.callback_query_handler(func=lambda call: call.data.startswith('task_done:') or call.data.startswith('task_snooze_1h:') or call.data.startswith('task_snooze_24h:'))
def handle_task_callback(call):
    """
    Callback query handler for interactive notifications.
    """
    if call.from_user.id != config.MY_TELEGRAM_ID:
        bot.answer_callback_query(call.id, "Ошибка: Доступ запрещен.", show_alert=True)
        return
    try:
        action, task_text = call.data.split(':', 1)
        
        # Remove reply markup (the inline buttons) to prevent double clicks
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass

        if action == "task_done":
            content = read_file_from_drive("Tasks.md")
            lines = content.split("\n")
            task_found = False
            for i, line in enumerate(lines):
                if "[ ]" in line and task_text in line:
                    lines[i] = line.replace("[ ]", "[x]", 1)
                    task_found = True
                    break
            
            if task_found:
                write_file_to_drive("Tasks.md", "\n".join(lines))
                from drive_service import add_user_xp
                add_user_xp(10)
                bot.answer_callback_query(call.id, "Отмечено как выполнено! +10 XP")
                bot.send_message(call.message.chat.id, f"✅ Выполнено: **{task_text}** (+10 XP)", parse_mode="Markdown")
            else:
                bot.answer_callback_query(call.id, "Задача уже выполнена или не найдена.")
                bot.send_message(call.message.chat.id, f"✅ Задача выполнена: **{task_text}**", parse_mode="Markdown")
                
        elif action in ["task_snooze_1h", "task_snooze_24h"]:
            import datetime
            delay_hours = 1 if "1h" in action else 24
            run_date = datetime.datetime.now(config.msk_tz) + datetime.timedelta(hours=delay_hours)

            from scheduler_jobs import schedule_reminder_job
            schedule_reminder_job(config.MY_TELEGRAM_ID, task_text, run_date)
            append_line_to_drive(
                "Tasks.md",
                f"* [ ] {run_date.strftime('%Y-%m-%d %H:%M')} | ⏰ REMINDER: {task_text}"
            )
            bot.answer_callback_query(call.id, f"Отложено на {delay_hours} ч.")
            bot.send_message(call.message.chat.id, f"⏰ Напоминание **{task_text}** успешно отложено на {delay_hours} ч.", parse_mode="Markdown")
            
    except Exception as e:
        print(f"[Callback Error] Error handling task callback: {e}")
        bot.answer_callback_query(call.id, "Произошла ошибка при обработке.")


@bot.message_handler(commands=['journal'])
def handle_journal_command(message):
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        # Extract journaling text
        args = message.text.split(maxsplit=1)
        journal_text = args[1].strip() if len(args) > 1 else ""
        
        # If no text in the command, check if they replied to a message
        if not journal_text and message.reply_to_message:
            journal_text = message.reply_to_message.text or message.reply_to_message.caption or ""
            
        if not journal_text:
            bot.reply_to(message, "Пожалуйста, напиши свои мысли после команды `/journal` или ответь этой командой на сообщение. Например:\n`/journal Сегодня был прекрасный продуктивный день.`")
            return
            
        now_msk = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")

        prompt = f"""Текущее время в Москве: {now_msk}
Сегодняшняя дата: {today_str}

Пользователь пишет личную рефлексию/дневник (journaling):
"{journal_text}"

Act as an empathetic listener and coach. Respond with a short, supportive reply. At the very end of your response, add a new tag: `[MOOD] score/10`, where score is your assessment of their emotional state (1-10).

Помимо тегов, начни свой живой поддерживающий ответ с [ОТВЕТ], чтобы отделить живой ответ от тегов.

Формат ответа:
[ОТВЕТ]
Твой ответ пользователю коуча на русском языке
[MOOD] score/10
"""
        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=prompt
        )
        raw_text = response.text

        tags = parse_gemini_tags(raw_text)
        reply_part = extract_reply(raw_text)
        apply_gemini_tags(tags)

        bot.reply_to(message, reply_part)
    except Exception as e:
        bot.reply_to(message, f"Ошибка записи дневника: {e}")


@bot.message_handler(commands=['brain'])
def handle_brain_search(message):
    """
    RAG-lite global search over Second Brain files using MODEL_COMPLEX.
    """
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        # Extract query text
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            bot.reply_to(message, "Задай вопрос своему Второму Мозгу. Пример: `/brain Как продвигаются мои цели по здоровью?`", parse_mode="Markdown")
            return
        query = args[1].strip()

        # Read context files
        tasks = read_file_from_drive("Tasks.md")
        finance = read_file_from_drive("Finance.md")
        health = read_file_from_drive("Health.md")
        memory = read_file_from_drive("Memory.md")
        goals = read_file_from_drive("Goals.md")

        # Combine into context, safely truncating each to prevent context limit issues (e.g. max 4000 chars each)
        def truncate_context(text, max_chars=4000):
            if len(text) > max_chars:
                return text[-max_chars:] # take recent part
            return text

        context = f"""[ФАЙЛ Goals.md]
{truncate_context(goals)}

[ФАЙЛ Tasks.md]
{truncate_context(tasks)}

[ФАЙЛ Finance.md]
{truncate_context(finance)}

[ФАЙЛ Health.md]
{truncate_context(health)}

[ФАЙЛ Memory.md]
{truncate_context(memory)}
"""

        prompt = f"""Ты — ИИ-система "Второй Мозг" пользователя Павла. Твоя задача — проанализировать все файлы его личной базы знаний (Obsidian) и дать развернутый, глубокий и точный ответ на его вопрос.
Текущее время: {datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")}

Вопрос пользователя: "{query}"

Контекст его базы знаний (файлы из Google Drive):
---
{context}
---

Write a comprehensive, deep, and structured analysis or answer in Russian language. Focus on accuracy and facts. Use formatting to make it readable.
"""
        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=prompt
        )
        reply = response.text.strip()
        
        bot.reply_to(message, reply)
    except Exception as e:
        bot.reply_to(message, f"Ошибка поиска по Второму Мозгу: {e}")


@bot.message_handler(commands=['search'])
def handle_global_search(message):
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            bot.reply_to(message, "Используй `/search запрос`.", parse_mode="Markdown")
            return
        query = args[1].strip()

        files = list_markdown_files(limit=10)
        if not files:
            bot.reply_to(message, "Не удалось найти Markdown-файлы в Google Drive.")
            return

        collected_chunks = []
        total_chars = 0
        for file_meta in files:
            filename = file_meta.get("name", "")
            if not filename.endswith(".md"):
                continue
            content = read_file_from_drive(filename)
            if not content:
                continue
            remaining = 50000 - total_chars
            if remaining <= 0:
                break
            snippet = content[:remaining]
            collected_chunks.append(f"[FILE: {filename}]\n{snippet}")
            total_chars += len(snippet)

        if not collected_chunks:
            bot.reply_to(message, "Файлы найдены, но их содержимое пустое.")
            return

        notes_context = "\n\n".join(collected_chunks)
        prompt = f"""You are the user's digital Second Brain. Answer the query: "{query}" using the provided Obsidian notes. Cite which file (.md) the information comes from.

If the answer is uncertain, say so clearly. Reply in Russian and keep the answer structured and concise.

Notes:
---
{notes_context}
---
"""
        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=prompt
        )
        bot.reply_to(message, response.text.strip())
    except Exception as e:
        bot.reply_to(message, f"Ошибка глобального поиска: {e}")


@bot.message_handler(func=lambda message: True)
def chat_with_gemini(message):
    """
    Handles general chat with dynamic routing:
    - MODEL_LITE for short inputs (< 40 chars)
    - MODEL_COMPLEX for complex inputs (>= 40 chars, or forwarded messages)
    """
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        current_memory = read_file_from_drive("Memory.md")
        if not current_memory.strip():
            current_memory = "Пока пустая долгосрочная память."

        now_msk = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")

        # Extract forwarding context
        sender_name = get_forward_sender_name(message)
        is_forwarded = (sender_name is not None)
        if is_forwarded:
            user_message_text = f"Pavel forwarded a message from {sender_name}:\n{message.text}"
        else:
            user_message_text = message.text

        # Dynamic model selection
        text_len = len(message.text) if message.text else 0
        if is_forwarded or text_len >= 40:
            selected_model = config.MODEL_COMPLEX
        else:
            selected_model = config.MODEL_LITE

        print(f"[Model Router] Routing input (length={text_len}, forwarded={is_forwarded}) to model: {selected_model}")
        extraction_rules = get_extraction_rules(today_str)

        prompt = f"""Текущее время в Москве: {now_msk}
Сегодняшняя дата: {today_str}

Долгосрочная память (Memory.md):
---
{current_memory}
---

S1get пишет: "{user_message_text}"

Ответь чётко и по делу. В [ОТВЕТ] — только живой ответ пользователю, без дублирования памяти.

{extraction_rules}

ПРАВИЛО ДЛЯ ПЕРЕСЛАННЫХ СООБЩЕНИЙ [QUESTION]:
Если пересланное сообщение содержит вопрос или требует ответа, обязательно добавь тег:
[QUESTION] Name: суть вопроса
Где Name — это имя оригинального отправителя (из "Pavel forwarded a message from Name:"), а "суть вопроса" — краткое описание вопроса.

Примеры распознавания:
- "Pavel forwarded a message from Ivan:\nWill you come to the meeting?" → [QUESTION] Ivan: Will you come to the meeting?

Формат ответа:
[ОТВЕТ]
Твой ответ пользователю
(далее теги, если нужны — каждый с новой строки)
"""
        response = key_manager.generate_content(
            model=selected_model,
            contents=prompt
        )
        raw_text = response.text

        tags = parse_gemini_tags(raw_text)
        reply_part = extract_reply(raw_text)
        apply_gemini_tags(tags)

        bot.reply_to(message, reply_part)
    except Exception as e:
        bot.reply_to(message, f"Ошибка: {e}")


def process_external_text(text):
    """
    Exposes AI tag processing for external requests (Siri/shortcuts).
    """
    try:
        current_memory = read_file_from_drive("Memory.md")
        if not current_memory.strip():
            current_memory = "Пока пустая долгосрочная память."

        now_msk = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")
        extraction_rules = get_extraction_rules(today_str)

        prompt = f"""Текущее время в Москве: {now_msk}
Сегодняшняя дата: {today_str}

Долгосрочная память (Memory.md):
---
{current_memory}
---

S1get пишет (через Siri/Shortcut): "{text}"

Ответь чётко и по делу. В [ОТВЕТ] — только живой ответ пользователю, без дублирования памяти.

{extraction_rules}

Формат ответа:
[ОТВЕТ]
Твой ответ пользователю
(далее теги, если нужны — каждый с новой строки)
"""
        response = key_manager.generate_content(
            model=config.MODEL_LITE,
            contents=prompt
        )
        raw_text = response.text

        tags = parse_gemini_tags(raw_text)
        reply_part = extract_reply(raw_text)
        apply_gemini_tags(tags)
        return {
            "success": True,
            "reply": reply_part,
            "tags_found": [t[0] for t in tags]
        }
    except Exception as e:
        print(f"[External Process] Error: {e}")
        return {
            "success": False,
            "error": str(e)
        }
