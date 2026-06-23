import re
from datetime import datetime
import telebot
from google.genai import types
import config
from bot_instance import bot
from key_manager import key_manager
from drive_service import (
    read_file_from_drive,
    append_line_to_drive,
    write_file_to_drive,
)

# === REGEX CONSTANTS ===
TAG_LINE_RE = re.compile(r'^\[(TASK|FINANCE|HEALTH|MEMORY|SCHEDULE|QUESTION)\]\s*(.+)$', re.MULTILINE)
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


def apply_gemini_tags(tags):
    for tag_type, payload in tags:
        if not payload:
            continue
        try:
            if tag_type == "TASK":
                append_line_to_drive("Tasks.md", f"* [ ] {payload}")
            elif tag_type == "FINANCE":
                append_line_to_drive("Finance.md", f"* {payload}")
            elif tag_type == "HEALTH":
                append_line_to_drive("Health.md", f"* {payload}")
            elif tag_type == "MEMORY":
                append_line_to_drive("Memory.md", f"* {payload}")
            elif tag_type == "QUESTION":
                append_line_to_drive("Questions.md", f"* {payload}")
            elif tag_type == "SCHEDULE" and "|" in payload:
                dt_str, task_text = payload.split("|", 1)
                dt_str, task_text = dt_str.strip(), task_text.strip()
                run_date = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
                run_date = config.msk_tz.localize(run_date)
                
                # Import scheduler and job dynamically to prevent circular dependencies
                from scheduler_jobs import scheduler, send_dynamic_reminder
                scheduler.add_job(
                    send_dynamic_reminder,
                    'date',
                    run_date=run_date,
                    args=[config.MY_TELEGRAM_ID, task_text],
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
        bot.reply_to(message, f"💸 **Расход записан!**\n\n> {amount} ₽ · {category}\n> {desc}")
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
        bot.reply_to(message, f"🛌 **Сон записан!**\n\n> {today_str} · {hours} ч.")
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

        prompt = f"""Текущее время в Москве: {now_msk}
Сегодняшняя дата: {today_str}

Долгосрочная память (Memory.md):
---
{current_memory}
---

Пользователь прислал голосовое сообщение. Текст голосового сообщения находится в прикрепленном аудиофайле.
Внимательно прослушай аудиофайл и распознай, что говорит S1get.

Ответь чётко и по делу. В [ОТВЕТ] — только живой ответ пользователю, без дублирования памяти.

Если из сообщения нужно извлечь данные, добавь в конце ответа ОДНУ строку на каждый тип (только если применимо):
[TASK] ГГГГ-ММ-ДД ЧЧ:ММ | Описание задачи или рутины
[FINANCE] ГГГГ-ММ-ДД: сумма | категория | описание
[HEALTH] ГГГГ-ММ-ДД: часы
[MEMORY] факт для долгосрочной памяти
[SCHEDULE] ГГГГ-ММ-ДД ЧЧ:ММ | Текст напоминания
[QUESTION] Name: суть вопроса

Примеры распознавания:
- "поспал 8 часов" → [HEALTH] {today_str}: 8
- "потратил 450р на обед" → [FINANCE] {today_str}: 450 | еда | обед
- "напомни в 21:00 позвонить маме" → [SCHEDULE] {today_str} 21:00 | Позвонить маме
- "завтра в 9 утра тренировка" → [TASK] <дата> 09:00 | Тренировка

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

        prompt = f"""Текущее время в Москве: {now_msk}
Сегодняшняя дата: {today_str}

Долгосрочная память (Memory.md):
---
{current_memory}
---

S1get пишет: "{user_message_text}"

Ответь чётко и по делу. В [ОТВЕТ] — только живой ответ пользователю, без дублирования памяти.

Если из сообщения нужно извлечь данные, добавь в конце ответа ОДНУ строку на каждый тип (только если применимо):
[TASK] ГГГГ-ММ-ДД ЧЧ:ММ | Описание задачи или рутины
[FINANCE] ГГГГ-ММ-ДД: сумма | категория | описание
[HEALTH] ГГГГ-ММ-ДД: часы
[MEMORY] факт для долгосрочной памяти
[SCHEDULE] ГГГГ-ММ-ДД ЧЧ:ММ | Текст напоминания
[QUESTION] Name: суть вопроса

ПРАВИЛО ДЛЯ ПЕРЕСЛАННЫХ СООБЩЕНИЙ [QUESTION]:
Если пересланное сообщение содержит вопрос или требует ответа, обязательно добавь тег:
[QUESTION] Name: суть вопроса
Где Name — это имя оригинального отправителя (из "Pavel forwarded a message from Name:"), а "суть вопроса" — краткое описание вопроса.

Примеры распознавания:
- "поспал 8 часов" → [HEALTH] {today_str}: 8
- "потратил 450р на обед" → [FINANCE] {today_str}: 450 | еда | обед
- "напомни в 21:00 позвонить маме" → [SCHEDULE] {today_str} 21:00 | Позвонить маме
- "завтра в 9 утра тренировка" → [TASK] <дата> 09:00 | Тренировка
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
