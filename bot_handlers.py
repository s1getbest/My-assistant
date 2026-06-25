import re
from datetime import datetime
import telebot
from google.genai import types
from uuid import uuid4
import threading
import config
from bot_instance import bot
from key_manager import key_manager
from drive_service import (
    append_line_to_drive,
    delete_line_from_task_file,
    edit_line_in_task_file,
    get_task_line_by_token,
    list_markdown_files,
    mark_task_done_by_token,
    read_json_from_drive,
    read_file_from_drive,
    write_json_to_drive,
    write_file_to_drive,
)

# === REGEX CONSTANTS ===
TAG_LINE_RE = re.compile(
    r'^\[(TASK_ADD|TASK_DEL|TASK_EDIT|FINANCE|HEALTH|MEMORY|SCHEDULE|QUESTION|MOOD|INBOX|NOTE|CARD)\]\s*(.+)$',
    re.MULTILINE
)
TASK_TIME_RE = re.compile(r'(\d{2}:\d{2})\s*\|\s*(.+)$')
TELEGRAM_FORMAT_RULE = (
    "IMPORTANT FORMATTING RULE: Do NOT use double asterisks `**` for bolding under any "
    "circumstances. Telegram does not support it. Use standard single asterisks `*` or avoid bolding entirely."
)


def is_me(message):
    return message.from_user.id == config.MY_TELEGRAM_ID


def parse_gemini_tags(raw_text):
    tags = []
    for match in TAG_LINE_RE.finditer(raw_text):
        tags.append((match.group(1), match.group(2).strip()))
    return tags


def apply_format_rule(prompt):
    return f"{prompt}\n\n{TELEGRAM_FORMAT_RULE}"


def sanitize_telegram_text(text):
    return (text or "").replace("**", "*").strip()


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
    return sanitize_telegram_text("\n".join(reply_lines).strip() or raw_text.strip())


def get_extraction_rules(today_str):
    return f"""Если из сообщения нужно извлечь данные, добавь в конце ответа ОДНУ строку на каждый тип (только если применимо):
[TASK_ADD] ГГГГ-ММ-ДД ЧЧ:ММ | Описание задачи или рутины
[TASK_DEL] text_to_find
[TASK_EDIT] text_to_find || ГГГГ-ММ-ДД ЧЧ:ММ | Новое описание задачи
[FINANCE] ГГГГ-ММ-ДД: сумма | категория | описание
[HEALTH] ГГГГ-ММ-ДД: часы
[MEMORY] факт для долгосрочной памяти
[SCHEDULE] ГГГГ-ММ-ДД ЧЧ:ММ | Текст напоминания
[INBOX] сырой текст мысли или заметки
[NOTE] Category | Text с [[wikilinks]] и #tags
[CARD] Question | Answer
[QUESTION] Name: суть вопроса

Если пользователь просит удалить задачу, используй [TASK_DEL] и передай уникальный фрагмент текста для поиска.
Если пользователь просит изменить задачу, используй [TASK_EDIT] в формате `старый_текст || новая_строка`.
Если пользователь просит напомнить заранее, например "за 1 час" или "за 1 день" до события, вычисли точную дату и время напоминания и выдай [SCHEDULE] с уже рассчитанным временем.
Если пользователь просто выгружает мысли, идеи, наблюдения или факты без явного действия, используй [INBOX].
Если это атомарная заметка для Второго Мозга, используй [NOTE] и автоматически оборачивай ключевые сущности, концепты и имена в [[wikilinks]], а также добавляй релевантные #tags.
Если можно сформулировать учебную карточку вопрос-ответ, используй [CARD].

Примеры распознавания:
- "поспал 8 часов" → [HEALTH] {today_str}: 8
- "потратил 450р на обед" → [FINANCE] {today_str}: 450 | еда | обед
- "напомни в 21:00 позвонить маме" → [SCHEDULE] {today_str} 21:00 | Позвонить маме
- "завтра в 9 утра тренировка" → [TASK_ADD] <дата> 09:00 | Тренировка
- "удали задачу созвон с Димой" → [TASK_DEL] созвон с Димой
- "перенеси тренировку на завтра в 8" → [TASK_EDIT] тренировка || <новая дата> 08:00 | Тренировка
- "идея: сделать метод для сравнения привычек" → [INBOX] идея: сделать метод для сравнения привычек
- "концепт atomic habits помогает строить систему" → [NOTE] Productivity | [[Atomic Habits]] помогает строить систему #productivity #habits
- "что такое Zettelkasten? | система связанных атомарных заметок" → [CARD] Что такое Zettelkasten? | Система связанных атомарных заметок
"""


def build_task_line(payload):
    return f"* [ ] {payload.strip()}"


def sanitize_note_category(category):
    category = re.sub(r'[\\/:*?"<>|]+', '_', (category or "").strip())
    return category or "Notes"


def extract_task_text_from_line(task_line):
    stripped = (task_line or "").strip()
    stripped = re.sub(r'^[\*\-\s]*\[[ xX]\]\s*', '', stripped)
    if "|" in stripped:
        return stripped.split("|", 1)[1].strip().replace("⏰ REMINDER:", "", 1).strip()
    return stripped.replace("⏰ REMINDER:", "", 1).strip()


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
            elif tag_type == "INBOX":
                append_line_to_drive("Inbox.md", f"* {payload}")
            elif tag_type == "NOTE" and "|" in payload:
                category, note_text = payload.split("|", 1)
                note_filename = f"{sanitize_note_category(category)}.md"
                append_line_to_drive(note_filename, f"* {note_text.strip()}")
            elif tag_type == "CARD" and "|" in payload:
                question, answer = payload.split("|", 1)
                flashcards = read_json_from_drive("Flashcards.json")
                if not isinstance(flashcards, list):
                    flashcards = []
                flashcards.append({
                    "id": str(uuid4()),
                    "q": question.strip(),
                    "a": answer.strip(),
                    "next_review": datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M:%S"),
                })
                write_json_to_drive("Flashcards.json", flashcards)
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
                task_line = f"* [ ] {dt_str} | ⏰ REMINDER: {task_text}"

                from scheduler_jobs import schedule_reminder_job
                schedule_reminder_job(config.MY_TELEGRAM_ID, task_text, run_date, task_line=task_line)
                append_line_to_drive("Tasks.md", task_line)
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


# === MULTI-AGENT PIPELINE ===

def agent_router(user_message):
    """
    Agent Router: Uses MODEL_LITE to classify user intent.
    Returns EXACTLY ONE word: TASK, FINANCE, HEALTH, QUESTION, or NOTE.
    """
    try:
        prompt = apply_format_rule(f"""Analyze the user's message. Output EXACTLY ONE word: TASK, FINANCE, HEALTH, QUESTION, or NOTE.

User message: "{user_message}"
""")
        response = key_manager.generate_content(
            model=config.MODEL_LITE,
            contents=prompt
        )
        classification = response.text.strip().upper()
        valid_classes = ["TASK", "FINANCE", "HEALTH", "QUESTION", "NOTE"]
        if classification not in valid_classes:
            classification = "QUESTION"
        print(f"[Agent Router] Classified as: {classification}")
        return classification
    except Exception as e:
        print(f"[Agent Router] Error: {e}")
        return "QUESTION"


def agent_archivist(user_message):
    """
    Agent Archivist: Uses MODEL_COMPLEX to format user's thought into Zettelkasten note.
    Output: [NOTE] Category | Formatted Text with [[wikilinks]] and #tags.
    """
    try:
        prompt = apply_format_rule(f"""Format the user's thought into a Zettelkasten note. Add Obsidian [[wikilinks]] for key entities, and generate appropriate #tags. Output: [NOTE] Category | Formatted Text.

User thought: "{user_message}"
""")
        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=prompt
        )
        return response.text.strip()
    except Exception as e:
        print(f"[Agent Archivist] Error: {e}")
        return None


def agent_tutor_background(note_text):
    """
    Agent Tutor (Background): Uses MODEL_COMPLEX to generate Anki flashcard from note.
    Runs in background thread to avoid blocking Telegram reply.
    Output: [CARD] Question | Answer
    """
    def generate_flashcard():
        try:
            prompt = apply_format_rule(f"""Create a Q&A flashcard based on this note. Output: [CARD] Question | Answer.

Note: "{note_text}"
""")
            response = key_manager.generate_content(
                model=config.MODEL_COMPLEX,
                contents=prompt
            )
            card_text = response.text.strip()
            
            # Parse and save flashcard
            if "[CARD]" in card_text and "|" in card_text:
                card_body = card_text.split("[CARD]", 1)[1].strip()
                if "|" in card_body:
                    question, answer = card_body.split("|", 1)
                    flashcards = read_json_from_drive("Flashcards.json")
                    if not isinstance(flashcards, list):
                        flashcards = []
                    flashcards.append({
                        "id": str(uuid4()),
                        "q": question.strip(),
                        "a": answer.strip(),
                        "next_review": datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M:%S"),
                    })
                    write_json_to_drive("Flashcards.json", flashcards)
                    print(f"[Agent Tutor] Flashcard generated and saved")
        except Exception as e:
            print(f"[Agent Tutor] Error: {e}")
    
    thread = threading.Thread(target=generate_flashcard)
    thread.daemon = True
    thread.start()


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
            prompt = apply_format_rule(f"""Текущее время в Москве: {now_msk}
Сегодняшняя дата: {today_str}

Пользователь прислал голосовую запись в свой личный дневник (Journal).
Внимательно прослушай аудиофайл и распознай глубокие размышления Павла.

Act as an empathetic listener and coach. Respond with a short, supportive reply. At the very end of your response, add a new tag: `[MOOD] score/10`, where score is your assessment of their emotional state (1-10).

Помимо тегов, начни свой живой поддерживающий ответ с [ОТВЕТ], чтобы отделить живой ответ от тегов.

Формат ответа:
[ОТВЕТ]
Твой ответ пользователю на русском языке
[MOOD] score/10
""")
        else:
            extraction_rules = get_extraction_rules(today_str)
            prompt = apply_format_rule(f"""Текущее время в Москве: {now_msk}
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
""")
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

        prompt = apply_format_rule(f"""Текущее время в Москве: {now_msk}
Сегодняшняя дата: {today_str}

Пользователь прислал изображение. Вот его описание/подпись (если есть): "{caption}"

Analyze this image. If it's a receipt, calculate the total and output `[FINANCE] YYYY-MM-DD: amount | category | description`. If it's handwritten notes or a whiteboard, extract actionable items as `[TASK_ADD] YYYY-MM-DD HH:MM | Task`. If it's an article/screenshot, summarize it as `[MEMORY] summary`.
{extraction_rules}

Помимо тегов, напиши пользователю краткий содержательный ответ/комментарий. Начни свой ответ с [ОТВЕТ], чтобы отделить живой ответ от тегов.

Формат ответа:
[ОТВЕТ]
Твой ответ пользователю
(далее теги, если нужны — каждый с новой строки)
""")

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

        prompt = apply_format_rule(f"""Текущее время в Москве: {now_msk}
Сегодняшняя дата: {today_str}

Пользователь отправил быструю заметку через Inline-режим: "{text}"

{extraction_rules}
Пожалуйста, будь точен в распознавании. Никакого другого текста писать НЕ нужно, только теги с новой строки (если применимо).
""")
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
        action, task_token = call.data.split(':', 1)
        
        # Remove reply markup (the inline buttons) to prevent double clicks
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass

        if action == "task_done":
            updated_line = mark_task_done_by_token(task_token)
            task_text = extract_task_text_from_line(updated_line)
            if updated_line:
                from drive_service import add_user_xp
                add_user_xp(10)
                bot.answer_callback_query(call.id, "Отмечено как выполнено! +10 XP")
                bot.send_message(call.message.chat.id, f"✅ Выполнено: **{task_text}** (+10 XP)", parse_mode="Markdown")
            else:
                bot.answer_callback_query(call.id, "Задача уже выполнена или не найдена.")
                bot.send_message(call.message.chat.id, "✅ Задача уже обработана или не найдена.", parse_mode="Markdown")
                
        elif action in ["task_snooze_1h", "task_snooze_24h"]:
            import datetime
            delay_hours = 1 if "1h" in action else 24
            old_task_line = get_task_line_by_token(task_token)
            if not old_task_line:
                bot.answer_callback_query(call.id, "Исходная задача не найдена.", show_alert=True)
                return

            task_text = extract_task_text_from_line(old_task_line)
            run_date = datetime.datetime.now(config.msk_tz) + datetime.timedelta(hours=delay_hours)
            new_task_line = f"* [ ] {run_date.strftime('%Y-%m-%d %H:%M')} | ⏰ REMINDER: {task_text}"

            from scheduler_jobs import schedule_reminder_job
            delete_line_from_task_file(old_task_line)
            append_line_to_drive("Tasks.md", new_task_line)
            schedule_reminder_job(config.MY_TELEGRAM_ID, task_text, run_date, task_line=new_task_line)
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

        prompt = apply_format_rule(f"""Текущее время в Москве: {now_msk}
Сегодняшняя дата: {today_str}

Пользователь пишет личную рефлексию/дневник (journaling):
"{journal_text}"

Act as an empathetic listener and coach. Respond with a short, supportive reply. At the very end of your response, add a new tag: `[MOOD] score/10`, where score is your assessment of their emotional state (1-10).

Помимо тегов, начни свой живой поддерживающий ответ с [ОТВЕТ], чтобы отделить живой ответ от тегов.

Формат ответа:
[ОТВЕТ]
Твой ответ пользователю коуча на русском языке
[MOOD] score/10
""")
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

        prompt = apply_format_rule(f"""Ты — ИИ-система "Второй Мозг" пользователя Павла. Твоя задача — проанализировать все файлы его личной базы знаний (Obsidian) и дать развернутый, глубокий и точный ответ на его вопрос.
Текущее время: {datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")}

Вопрос пользователя: "{query}"

Контекст его базы знаний (файлы из Google Drive):
---
{context}
---

Write a comprehensive, deep, and structured analysis or answer in Russian language. Focus on accuracy and facts. Use formatting to make it readable.
""")
        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=prompt
        )
        reply = sanitize_telegram_text(response.text)
        
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
        prompt = apply_format_rule(f"""You are the user's digital Second Brain. Answer the query: "{query}" using the provided Obsidian notes. Cite which file (.md) the information comes from.

If the answer is uncertain, say so clearly. Reply in Russian and keep the answer structured and concise.

Notes:
---
{notes_context}
---
""")
        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=prompt
        )
        bot.reply_to(message, sanitize_telegram_text(response.text))
    except Exception as e:
        bot.reply_to(message, f"Ошибка глобального поиска: {e}")


@bot.message_handler(commands=['digest'])
def handle_digest(message):
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        raw_inbox = read_file_from_drive("Raw_Inbox.md")
        if not raw_inbox.strip():
            bot.reply_to(message, "Raw_Inbox пуст.")
            return

        now_msk = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        prompt = apply_format_rule(f"""Текущее время в Москве: {now_msk}

Ниже сырые входящие сообщения из Raw_Inbox.md. Extract tasks into [TASK_ADD] and questions into [QUESTION]. Ignore casual chat.

Если времени у задачи нет, но есть день/дата, выбери разумное время. Если информации недостаточно, не создавай тег.
Выводи только теги, по одному на строку.

Raw_Inbox.md:
---
{raw_inbox}
---
""")
        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=prompt
        )
        raw_text = response.text.strip()
        tags = parse_gemini_tags(raw_text)
        apply_gemini_tags(tags)
        write_file_to_drive("Raw_Inbox.md", "")
        bot.reply_to(message, f"📥 Inbox разобран. Извлечено тегов: {len(tags)}")
    except Exception as e:
        bot.reply_to(message, f"Ошибка digest: {e}")


@bot.message_handler(commands=['process'])
def handle_process_inbox(message):
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        inbox_content = read_file_from_drive("Inbox.md")
        if not inbox_content.strip():
            bot.reply_to(message, "Inbox пуст.")
            return

        now_msk = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        prompt = apply_format_rule(f"""Текущее время в Москве: {now_msk}

Ниже содержимое Inbox.md с сырыми заметками пользователя.
Преобразуй материал только в теги:
- [NOTE] Category | Text
- [CARD] Question | Answer

Для [NOTE]:
- делай атомарные заметки;
- автоматически оборачивай ключевые сущности, концепты и имена в [[wikilinks]];
- добавляй релевантные #tags;
- выбирай краткую и понятную Category.

Для [CARD]:
- создавай только полезные карточки формата вопрос-ответ.

Игнорируй шум и повторы. Выводи только теги, по одному на строку.

Inbox.md:
---
{inbox_content}
---
""")
        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=prompt
        )
        raw_text = response.text.strip()
        tags = parse_gemini_tags(raw_text)
        apply_gemini_tags(tags)
        write_file_to_drive("Inbox.md", "")
        bot.reply_to(message, f"🗂 Inbox обработан. Извлечено тегов: {len(tags)}")
    except Exception as e:
        bot.reply_to(message, f"Ошибка process: {e}")


@bot.message_handler(func=lambda message: True)
def chat_with_gemini(message):
    """
    Handles general chat with Multi-Agent Pipeline:
    - Agent Router: Uses MODEL_LITE to classify intent
    - Agent Archivist: If NOTE, uses MODEL_COMPLEX to format Zettelkasten note
    - Agent Tutor: Background thread generates flashcard from saved NOTE
    """
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        current_memory = read_file_from_drive("Memory.md")
        if not current_memory.strip():
            current_memory = "Пока пустая долгосрочная память."

        current_tasks = read_file_from_drive("Tasks.md")
        if not current_tasks.strip():
            current_tasks = "Пока нет задач."

        now_msk = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")

        # Extract forwarding context
        sender_name = get_forward_sender_name(message)
        is_forwarded = (sender_name is not None)
        if is_forwarded:
            user_message_text = f"Pavel forwarded a message from {sender_name}:\n{message.text}"
        else:
            user_message_text = message.text

        # Multi-Agent Pipeline: Router
        classification = agent_router(user_message_text)
        print(f"[Multi-Agent Pipeline] Router classified as: {classification}")

        # If NOTE, use Archivist agent
        if classification == "NOTE":
            note_output = agent_archivist(user_message_text)
            if note_output and "[NOTE]" in note_output:
                # Parse and save the note
                tags = parse_gemini_tags(note_output)
                apply_gemini_tags(tags)
                
                # Extract note text for background Tutor
                if "|" in note_output:
                    note_body = note_output.split("|", 1)[1].strip()
                    # Trigger background Agent Tutor
                    agent_tutor_background(note_body)
                
                reply_part = f"📝 Заметка сохранена: {note_output.replace('[NOTE]', '').strip()}"
                bot.reply_to(message, reply_part)
                return

        # For other classifications, use standard flow
        text_len = len(message.text) if message.text else 0
        if is_forwarded or text_len >= 40:
            selected_model = config.MODEL_COMPLEX
        else:
            selected_model = config.MODEL_LITE

        print(f"[Model Router] Routing input (length={text_len}, forwarded={is_forwarded}) to model: {selected_model}")
        extraction_rules = get_extraction_rules(today_str)

        prompt = apply_format_rule(f"""Текущее время в Москве: {now_msk}
Сегодняшняя дата: {today_str}

Долгосрочная память (Memory.md):
---
{current_memory}
---

Задачи пользователя (Tasks.md):
---
{current_tasks}
---

S1get пишет: "{user_message_text}"

You have access to the user's tasks (Tasks.md). If the user asks about their schedule, plans, or what they have to do today/tomorrow/this week, analyze the Tasks.md list and give them a precise answer.

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
""")
        response = key_manager.generate_content(
            model=selected_model,
            contents=prompt
        )
        raw_text = response.text

        tags = parse_gemini_tags(raw_text)
        reply_part = extract_reply(raw_text)
        apply_gemini_tags(tags)

        # Check if a NOTE was generated in the standard flow
        for tag_type, payload in tags:
            if tag_type == "NOTE" and "|" in payload:
                note_body = payload.split("|", 1)[1].strip()
                agent_tutor_background(note_body)

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

        prompt = apply_format_rule(f"""Текущее время в Москве: {now_msk}
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
""")
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
