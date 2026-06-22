import telebot
from google import genai
from google.genai import types
import os
import json
import io
import re
import time
import threading
import hmac
import hashlib
import urllib.parse
from flask import Flask, render_template_string, request, jsonify
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
import pytz
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler


# === КЛЮЧИ И НАСТРОЙКИ ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MY_TELEGRAM_ID = int(os.getenv("MY_TELEGRAM_ID", 0))
FOLDER_ID = os.getenv("FOLDER_ID")
GOOGLE_TOKEN_JSON = os.getenv("GOOGLE_TOKEN_JSON")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
ai_client = genai.Client(api_key=GEMINI_API_KEY)
GEMINI_MODEL = 'gemini-3.5-flash'
msk_tz = pytz.timezone("Europe/Moscow")


scheduler = BackgroundScheduler(timezone=msk_tz)
scheduler.start()

_drive_creds = None
try:
    token_data = json.loads(GOOGLE_TOKEN_JSON)
    _drive_creds = Credentials.from_authorized_user_info(token_data, scopes=["https://www.googleapis.com/auth/drive"])
    print("Успешно авторизовались на Google Drive!")
except Exception as e:
    print(f"Ошибка авторизации Google Drive: {e}")


def get_drive_service():
    if _drive_creds is None:
        raise RuntimeError("Google Drive credentials not initialized")
    return build('drive', 'v3', credentials=_drive_creds)

TAG_TYPES = ("TASK", "FINANCE", "HEALTH", "MEMORY", "SCHEDULE", "QUESTION")
TAG_LINE_RE = re.compile(r'^\[(TASK|FINANCE|HEALTH|MEMORY|SCHEDULE|QUESTION)\]\s*(.+)$', re.MULTILINE)
TASK_TIME_RE = re.compile(r'(\d{2}:\d{2})\s*\|\s*(.+)$')


# === GOOGLE DRIVE CACHING ===
_FILE_CACHE = {}
_CACHE_TIME = {}
_CACHE_LOCK = threading.Lock()

# === GOOGLE DRIVE ===

def get_file_id_by_name(filename):
    try:
        service = get_drive_service()
        query = f"name = '{filename}' and '{FOLDER_ID}' in parents and trashed = false"
        results = service.files().list(q=query, spaces='drive', fields='files(id)').execute()
        files = results.get('files', [])
        return files[0]['id'] if files else None
    except Exception as e:
        print(f"Drive list error ({filename}): {e}")
        return None

def read_file_from_drive(filename):
    # Check cache first
    with _CACHE_LOCK:
        if filename in _FILE_CACHE and (time.time() - _CACHE_TIME.get(filename, 0) < 300):
            return _FILE_CACHE[filename]

    last_err = None
    for attempt in range(3):
        try:
            file_id = get_file_id_by_name(filename)
            if not file_id:
                return ""
            service = get_drive_service()
            request = service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            content = fh.getvalue().decode('utf-8')
            # Save to cache
            with _CACHE_LOCK:
                _FILE_CACHE[filename] = content
                _CACHE_TIME[filename] = time.time()
            return content
        except Exception as e:
            last_err = e
            print(f"Drive read error ({filename}) attempt {attempt + 1}/3: {e}")
            if attempt < 2:
                time.sleep(1)
    print(f"Drive read failed ({filename}): {last_err}")
    return ""

def write_file_to_drive(filename, content):
    last_err = None
    for attempt in range(3):
        try:
            service = get_drive_service()
            file_id = get_file_id_by_name(filename)
            media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown', resumable=True)
            if file_id:
                service.files().update(fileId=file_id, media_body=media).execute()
            else:
                file_metadata = {'name': filename, 'parents': [FOLDER_ID]}
                service.files().create(body=file_metadata, media_body=media, fields='id').execute()
            # Forcefully update cache upon successful write
            with _CACHE_LOCK:
                _FILE_CACHE[filename] = content
                _CACHE_TIME[filename] = time.time()
            return
        except Exception as e:
            last_err = e
            print(f"Drive write error ({filename}) attempt {attempt + 1}/3: {e}")
            if attempt < 2:
                time.sleep(1)
    print(f"Drive write failed ({filename}): {last_err}")
    raise last_err

def append_line_to_drive(filename, line):
    try:
        current = read_file_from_drive(filename)
        new_content = f"{current.rstrip()}\n{line}".strip() if current.strip() else line
        write_file_to_drive(filename, new_content)
        # Forcefully update cache upon successful append (though write_file_to_drive already does)
        with _CACHE_LOCK:
            _FILE_CACHE[filename] = new_content
            _CACHE_TIME[filename] = time.time()
        return True
    except Exception as e:
        print(f"Drive append error ({filename}): {e}")
        return False


# === DATA PARSING ===

def parse_finance_amount(line):
    if ":" not in line:
        return 0
    val_part = line.split(":", 1)[1].strip()
    num_part = val_part.split("|")[0].strip()
    num_part = re.sub(r'[₽рруб\s]', '', num_part, flags=re.IGNORECASE)
    try:
        return int(float(num_part.replace(",", ".")))
    except ValueError:
        return 0

def get_monthly_expenses():
    try:
        finance_content = read_file_from_drive("Finance.md")
        current_month = datetime.now(msk_tz).strftime("%Y-%m")
        total = 0
        recent = []
        for line in finance_content.split("\n"):
            line = line.strip()
            if not line or ":" not in line:
                continue
            date_part = line.split(":", 1)[0].replace("*", "").strip()
            if not date_part.startswith(current_month):
                continue
            amount = parse_finance_amount(line)
            total += amount
            val_part = line.split(":", 1)[1].strip()
            parts = [p.strip() for p in val_part.split("|")]
            recent.append({
                "date": date_part,
                "amount": amount,
                "category": parts[1] if len(parts) > 2 else (parts[1] if len(parts) > 1 else "—"),
                "description": parts[-1] if parts else "—",
            })
        return total, recent[-8:][::-1]
    except Exception as e:
        print(f"Finance parse error: {e}")
        return 0, []

def get_sleep_chart_data():
    sleep_data, sleep_labels = [], []
    last_sleep = "—"
    try:
        health_content = read_file_from_drive("Health.md")
        health_lines = [l.strip() for l in health_content.split("\n") if l.strip()]
        if health_lines:
            last_sleep = health_lines[-1].split(":", 1)[-1].strip()
            for line in health_lines[-7:]:
                if ":" not in line:
                    continue
                date_part = line.split(":", 1)[0].replace("*", "").strip()
                val_part = line.split(":", 1)[1].strip()
                try:
                    formatted_date = datetime.strptime(date_part, "%Y-%m-%d").strftime("%d.%m")
                except ValueError:
                    formatted_date = date_part
                try:
                    sleep_data.append(float(val_part.replace(",", ".")))
                    sleep_labels.append(formatted_date)
                except ValueError:
                    pass
    except Exception as e:
        print(f"Health parse error: {e}")
    if not sleep_data:
        sleep_data, sleep_labels = [0], ["Нет данных"]
    return sleep_data, sleep_labels, last_sleep

def get_today_tasks():
    today = datetime.now(msk_tz).strftime("%Y-%m-%d")
    tasks = []
    unchecked_idx = 0
    try:
        content = read_file_from_drive("Tasks.md")
        for line in content.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            is_open = "[ ]" in stripped
            is_done = "[x]" in stripped.lower()
            task_idx = None
            if is_open:
                task_idx = unchecked_idx
                unchecked_idx += 1
            if today not in stripped:
                continue
            m = TASK_TIME_RE.search(stripped)
            time_str = m.group(1) if m else "—"
            text = m.group(2).strip() if m else re.sub(r'^[\*\-\s]*\[[ xX]\]\s*', '', stripped)
            text = re.sub(r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s*\|\s*', '', text).strip()
            tasks.append({
                "time": time_str,
                "text": text or stripped,
                "done": is_done,
                "idx": task_idx,
            })
        tasks.sort(key=lambda t: t["time"])
    except Exception as e:
        print(f"Tasks parse error: {e}")
    return tasks

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
                run_date = msk_tz.localize(run_date)
                scheduler.add_job(
                    send_dynamic_reminder,
                    'date',
                    run_date=run_date,
                    args=[MY_TELEGRAM_ID, task_text],
                )
        except Exception as e:
            print(f"Tag apply error [{tag_type}]: {e}")


def is_me(message):
    return message.from_user.id == MY_TELEGRAM_ID


# === НАПОМИНАНИЯ ===

def send_dynamic_reminder(chat_id, task_text):
    try:
        prompt = f"Ты личный строгий ассистент Павел. Сработало напоминание: '{task_text}'. Напиши короткое, очень емкое и мотивирующее сообщение прямо сейчас."
        response = ai_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt
        )
        reply = response.text
    except Exception:
        reply = f"Пора делать: {task_text}"
    try:
        bot.send_message(chat_id, f"⏰ **НАПОМИНАНИЕ!**\n\n{reply}")
    except Exception as e:
        print(f"Reminder send error: {e}")

def check_daily_sleep():
    try:
        today_str = datetime.now(msk_tz).strftime("%Y-%m-%d")
        health_content = read_file_from_drive("Health.md")
        if today_str not in health_content:
            bot.send_message(
                MY_TELEGRAM_ID,
                "Павел, доброе утро! 🛌 Я заметил, что сегодня ты еще не записал свой сон. Расскажи, сколько часов удалось поспать и как самочувствие?",
            )
    except Exception as e:
        print(f"Sleep check error: {e}")

def evening_planning_reminder():
    try:
        bot.send_message(
            MY_TELEGRAM_ID,
            "Павел, время вечернего планирования! 🌙 Пора разобрать дела и составить план на завтра, чтобы лечь спать с чистой головой.",
        )
    except Exception as e:
        print(f"Evening reminder error: {e}")

def compress_memory():
    """
    Weekly background job to compress long-term memory (Memory.md).
    """
    try:
        print("Starting weekly memory compression job...")
        content = read_file_from_drive("Memory.md")
        if not content.strip():
            print("Memory.md is empty, skipping compression.")
            return

        prompt = f"""This is a long-term memory file. Compress it, remove duplicates, and keep only the most important facts as a concise list.

Current content:
---
{content}
---

Output only the resulting compressed list in Markdown format (using bullet points like "* fact"). Do not include any intro, outro, or additional conversational text.
"""
        response = ai_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt
        )
        compressed_text = response.text.strip()

        if compressed_text:
            write_file_to_drive("Memory.md", compressed_text)
            print("Memory.md successfully compressed and updated.")
        else:
            print("Warning: Compressed memory content is empty, skipping update.")
    except Exception as e:
        print(f"Error compressing memory: {e}")


scheduler.add_job(check_daily_sleep, 'cron', hour=10, minute=0)
scheduler.add_job(evening_planning_reminder, 'cron', hour=20, minute=0)
scheduler.add_job(compress_memory, 'cron', day_of_week='sun', hour=3, minute=0)


# === КОМАНДЫ БОТА ===

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
        today_str = datetime.now(msk_tz).strftime("%Y-%m-%d")
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
        today_str = datetime.now(msk_tz).strftime("%Y-%m-%d")
        append_line_to_drive("Health.md", f"* {today_str}: {hours}")
        bot.reply_to(message, f"🛌 **Сон записан!**\n\n> {today_str} · {hours} ч.")
    except Exception as e:
        bot.reply_to(message, f"Ошибка записи сна: {e}")

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
        print(f"Error getting forward sender name: {e}")
    return "Unknown Sender"


@bot.message_handler(content_types=['voice'])
def handle_voice(message):
    """
    Handles incoming voice messages, downloads them, and sends to Gemini for NLU processing.
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

        now_msk = datetime.now(msk_tz).strftime("%Y-%m-%d %H:%M")
        today_str = datetime.now(msk_tz).strftime("%Y-%m-%d")

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
        response = ai_client.models.generate_content(
            model=GEMINI_MODEL,
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
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        current_memory = read_file_from_drive("Memory.md")
        if not current_memory.strip():
            current_memory = "Пока пустая долгосрочная память."

        now_msk = datetime.now(msk_tz).strftime("%Y-%m-%d %H:%M")
        today_str = datetime.now(msk_tz).strftime("%Y-%m-%d")

        # Handle forwarded messages context
        sender_name = get_forward_sender_name(message)
        if sender_name is not None:
            user_message_text = f"Pavel forwarded a message from {sender_name}:\n{message.text}"
        else:
            user_message_text = message.text

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
(далее теги, if elements needed — каждый с новой строки)
"""
        response = ai_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt
        )
        raw_text = response.text

        tags = parse_gemini_tags(raw_text)
        reply_part = extract_reply(raw_text)
        apply_gemini_tags(tags)

        bot.reply_to(message, reply_part)
    except Exception as e:
        bot.reply_to(message, f"Ошибка: {e}")


# === FLASK MINI APP ===

app = Flask(__name__)

def validate_telegram_init_data(init_data_str):
    """
    Validates Telegram WebApp initData and returns parsed user dict if valid and authorized, or None.
    Uses HMAC-SHA256 with TELEGRAM_TOKEN as key.
    """
    if not init_data_str:
        return None
    try:
        # Parse query string
        parsed = urllib.parse.parse_qsl(init_data_str, keep_blank_values=True)
        params = dict(parsed)
        
        if 'hash' not in params:
            return None
        
        received_hash = params.pop('hash')
        
        # Sort remaining keys and build data-check-string
        sorted_params = sorted(params.items())
        data_check_string = "\n".join([f"{k}={v}" for k, v in sorted_params])
        
        # Calculate secret key: HMAC_SHA256("WebAppData", TELEGRAM_TOKEN)
        secret_key = hmac.new(b"WebAppData", TELEGRAM_TOKEN.encode('utf-8'), hashlib.sha256).digest()
        
        # Calculate calculated_hash: HMAC_SHA256(secret_key, data_check_string)
        calculated_hash = hmac.new(secret_key, data_check_string.encode('utf-8'), hashlib.sha256).hexdigest()
        
        if calculated_hash == received_hash:
            user_json = params.get('user')
            if user_json:
                return json.loads(user_json)
        return None
    except Exception as e:
        print(f"Error validating Telegram initData: {e}")
        return None


@app.route('/api/done', methods=['POST'])
def mark_task_done():
    try:
        # Extract init_data from header or payload
        init_data = request.headers.get('X-TG-Init-Data')
        data = request.get_json(silent=True) or {}
        if not init_data:
            init_data = data.get('init_data')

        if not init_data:
            return jsonify({"success": False, "error": "Unauthorized: Missing initData"}), 403

        # Validate init_data and verify user identity
        user_data = validate_telegram_init_data(init_data)
        if not user_data or user_data.get('id') != MY_TELEGRAM_ID:
            return jsonify({"success": False, "error": "403 Forbidden: Unauthorized"}), 403

        task_idx = data.get('task_idx')
        if task_idx is None:
            return jsonify({"success": False, "error": "task_idx required"}), 400

        content = read_file_from_drive("Tasks.md")
        lines = content.split("\n")
        unchecked_count = 0
        for i, line in enumerate(lines):
            if "[ ]" not in line:
                continue
            if unchecked_count == int(task_idx):
                lines[i] = line.replace("[ ]", "[x]", 1)
                write_file_to_drive("Tasks.md", "\n".join(lines))
                return jsonify({"success": True})
            unchecked_count += 1
        return jsonify({"success": False, "error": "Task not found"}), 404
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/')
def home():
    init_data = request.args.get('init_data')
    if not init_data:
        # Bootstrapper to extract WebApp initData client-side and reload
        bootstrapper = """
        <!DOCTYPE html>
        <html>
        <head>
            <script src="https://telegram.org/js/telegram-web-app.js"></script>
            <script>
                window.onload = function() {
                    if (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData) {
                        var initData = window.Telegram.WebApp.initData;
                        window.location.href = "/?init_data=" + encodeURIComponent(initData);
                    } else {
                        document.body.innerHTML = '<h1 style="color: #ef4444; text-align: center; margin-top: 50px; font-family: sans-serif;">403 Forbidden: Telegram WebApp Only</h1>';
                    }
                }
            </script>
        </head>
        <body style="background-color: #020617; color: white;">
            <p style="text-align: center; margin-top: 50px; font-family: sans-serif;">Loading Time OS...</p>
        </body>
        </html>
        """
        return render_template_string(bootstrapper)

    # Validate init_data and verify user identity
    user_data = validate_telegram_init_data(init_data)
    if not user_data or user_data.get('id') != MY_TELEGRAM_ID:
        return "403 Forbidden: Unauthorized", 403

    today_label = datetime.now(msk_tz).strftime("%d.%m.%Y")
    today_tasks = get_today_tasks()
    total_spent, recent_expenses = get_monthly_expenses()
    sleep_data, sleep_labels, last_sleep = get_sleep_chart_data()

    html_template = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Time OS</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { -webkit-tap-highlight-color: transparent; }
        .tab-panel { animation: fadeIn .2s ease; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
        .timeline-line { background: linear-gradient(180deg, #6366f1 0%, #312e81 100%); }
        .nav-active { color: #818cf8 !important; }
        .nav-active .nav-dot { opacity: 1; transform: scale(1); }
        .nav-dot { opacity: 0; transform: scale(0); transition: all .2s; }
    </style>
</head>
<body class="bg-slate-950 text-slate-100 font-sans min-h-screen pb-24">

    <header class="sticky top-0 z-20 bg-slate-950/90 backdrop-blur border-b border-slate-800/80 px-4 py-3">
        <div class="max-w-md mx-auto flex items-center justify-between">
            <div>
                <h1 class="text-sm font-bold tracking-wide">Time OS</h1>
                <p class="text-[10px] text-indigo-400">{{ today_label }}</p>
            </div>
            <div class="w-9 h-9 rounded-full bg-indigo-600 flex items-center justify-center text-xs font-bold shadow-lg shadow-indigo-500/30">ПВ</div>
        </div>
    </header>

    <main class="max-w-md mx-auto px-4 pt-4 space-y-4">

        <!-- TAB 1: TASKS -->
        <div id="tab-tasks" class="tab-panel">
            <div class="bg-slate-900 rounded-2xl border border-slate-800 p-4">
                <h2 class="text-xs font-bold text-slate-400 uppercase tracking-wider mb-4">📋 Сегодня</h2>
                {% if today_tasks %}
                <div class="relative pl-6 space-y-4">
                    <div class="absolute left-[7px] top-2 bottom-2 w-0.5 timeline-line rounded-full opacity-60"></div>
                    {% for task in today_tasks %}
                    <div class="relative flex items-start gap-3">
                        <div class="absolute -left-6 top-1 w-3.5 h-3.5 rounded-full border-2 {{ 'border-emerald-500 bg-emerald-500/20' if task.done else 'border-indigo-500 bg-indigo-500/30' }} z-10"></div>
                        <div class="flex-1 min-w-0 bg-slate-950/60 rounded-xl p-3 border border-slate-800/80">
                            <div class="flex items-center gap-2 mb-1">
                                <span class="text-[10px] font-mono text-indigo-400 bg-indigo-500/10 px-1.5 py-0.5 rounded">{{ task.time }}</span>
                                {% if task.done %}
                                <span class="text-[9px] text-emerald-500 uppercase font-bold">Done</span>
                                {% endif %}
                            </div>
                            {% if not task.done and task.idx is not none %}
                            <label class="flex items-start gap-2.5 cursor-pointer group">
                                <input type="checkbox"
                                    class="task-check mt-0.5 w-4 h-4 rounded accent-indigo-500 shrink-0"
                                    data-idx="{{ task.idx }}"
                                    onchange="completeTask(this)">
                                <span class="task-label text-sm leading-snug group-hover:text-indigo-200 transition-colors">{{ task.text }}</span>
                            </label>
                            {% else %}
                            <p class="text-sm {{ 'line-through opacity-40' if task.done else '' }}">{{ task.text }}</p>
                            {% endif %}
                        </div>
                    </div>
                    {% endfor %}
                </div>
                {% else %}
                <p class="text-sm text-slate-500 text-center py-8">Нет задач на сегодня 🎉</p>
                {% endif %}
            </div>
        </div>

        <!-- TAB 2: FINANCE -->
        <div id="tab-finance" class="tab-panel hidden">
            <div class="bg-gradient-to-br from-emerald-950/40 to-slate-900 rounded-2xl border border-emerald-500/20 p-5 mb-4">
                <p class="text-[10px] font-bold text-emerald-400/80 uppercase tracking-wider">Расходы за месяц</p>
                <p class="text-4xl font-extrabold text-emerald-400 mt-1">{{ total_spent }} <span class="text-lg">₽</span></p>
            </div>
            <div class="bg-slate-900 rounded-2xl border border-slate-800 p-4">
                <h2 class="text-xs font-bold text-slate-400 uppercase tracking-wider mb-3">💸 Последние траты</h2>
                {% if recent_expenses %}
                <ul class="space-y-2">
                    {% for exp in recent_expenses %}
                    <li class="flex items-center justify-between bg-slate-950/50 rounded-xl p-3 border border-slate-800/60">
                        <div class="min-w-0">
                            <p class="text-sm truncate">{{ exp.description }}</p>
                            <p class="text-[10px] text-slate-500">{{ exp.category }} · {{ exp.date }}</p>
                        </div>
                        <span class="text-sm font-bold text-emerald-400 shrink-0 ml-2">−{{ exp.amount }} ₽</span>
                    </li>
                    {% endfor %}
                </ul>
                {% else %}
                <p class="text-sm text-slate-500 text-center py-6">Нет записей</p>
                {% endif %}
            </div>
        </div>

        <!-- TAB 3: HEALTH -->
        <div id="tab-health" class="tab-panel hidden">
            <div class="grid grid-cols-2 gap-3 mb-4">
                <div class="bg-slate-900 rounded-2xl border border-slate-800 p-4">
                    <p class="text-[10px] font-bold text-slate-400 uppercase">Последний сон</p>
                    <p class="text-2xl font-extrabold text-indigo-400 mt-1">{{ last_sleep }} <span class="text-sm">ч</span></p>
                </div>
                <div class="bg-slate-900 rounded-2xl border border-slate-800 p-4">
                    <p class="text-[10px] font-bold text-slate-400 uppercase">Среднее (7д)</p>
                    <p class="text-2xl font-extrabold text-indigo-300 mt-1" id="avgSleep">—</p>
                </div>
            </div>
            <div class="bg-slate-900 rounded-2xl border border-slate-800 p-4">
                <h2 class="text-xs font-bold text-slate-400 uppercase tracking-wider mb-3">🌙 Тренд сна</h2>
                <div class="h-44">
                    <canvas id="sleepChart"></canvas>
                </div>
            </div>
        </div>

    </main>

    <!-- Bottom Nav -->
    <nav class="fixed bottom-0 inset-x-0 z-30 bg-slate-950/95 backdrop-blur border-t border-slate-800">
        <div class="max-w-md mx-auto flex justify-around py-2 pb-[max(0.5rem,env(safe-area-inset-bottom))]">
            <button onclick="switchTab('tasks')" id="nav-tasks" class="nav-btn nav-active flex flex-col items-center gap-0.5 px-6 py-1 text-slate-500 transition-colors">
                <span class="text-xl">📋</span>
                <span class="text-[9px] font-semibold uppercase tracking-wide">Tasks</span>
                <span class="nav-dot w-1 h-1 rounded-full bg-indigo-400"></span>
            </button>
            <button onclick="switchTab('finance')" id="nav-finance" class="nav-btn flex flex-col items-center gap-0.5 px-6 py-1 text-slate-500 transition-colors">
                <span class="text-xl">💸</span>
                <span class="text-[9px] font-semibold uppercase tracking-wide">Finance</span>
                <span class="nav-dot w-1 h-1 rounded-full bg-indigo-400"></span>
            </button>
            <button onclick="switchTab('health')" id="nav-health" class="nav-btn flex flex-col items-center gap-0.5 px-6 py-1 text-slate-500 transition-colors">
                <span class="text-xl">🌙</span>
                <span class="text-[9px] font-semibold uppercase tracking-wide">Health</span>
                <span class="nav-dot w-1 h-1 rounded-full bg-indigo-400"></span>
            </button>
        </div>
    </nav>

    <script>
        if (window.Telegram?.WebApp) {
            Telegram.WebApp.ready();
            Telegram.WebApp.expand();
            document.body.style.backgroundColor = '#020617';
        }

        const TABS = ['tasks', 'finance', 'health'];
        let chartInstance = null;

        function switchTab(name) {
            TABS.forEach(t => {
                document.getElementById('tab-' + t).classList.toggle('hidden', t !== name);
                document.getElementById('nav-' + t).classList.toggle('nav-active', t === name);
            });
            if (name === 'health' && !chartInstance) initChart();
        }

        async function completeTask(checkbox) {
            const idx = parseInt(checkbox.dataset.idx, 10);
            const label = checkbox.closest('label').querySelector('.task-label');
            checkbox.disabled = true;
            label.classList.add('line-through', 'opacity-50');
            try {
                const initData = window.Telegram?.WebApp?.initData || '';
                const res = await fetch('/api/done', {
                    method: 'POST',
                    headers: { 
                        'Content-Type': 'application/json',
                        'X-TG-Init-Data': initData
                    },
                    body: JSON.stringify({ task_idx: idx, init_data: initData }),
                });
                const data = await res.json();
                if (!data.success) throw new Error(data.error || 'Failed');
            } catch (e) {
                checkbox.checked = false;
                checkbox.disabled = false;
                label.classList.remove('line-through', 'opacity-50');
            }
        }

        function initChart() {
            const sleepData = {{ sleep_data|tojson }};
            const sleepLabels = {{ sleep_labels|tojson }};
            const valid = sleepData.filter(v => v > 0);
            if (valid.length) {
                const avg = (valid.reduce((a, b) => a + b, 0) / valid.length).toFixed(1);
                document.getElementById('avgSleep').textContent = avg + ' ч';
            }

            const ctx = document.getElementById('sleepChart').getContext('2d');
            const neonGradient = ctx.createLinearGradient(0, 0, 0, 176);
            neonGradient.addColorStop(0, 'rgba(129, 140, 248, 0.35)');
            neonGradient.addColorStop(1, 'rgba(129, 140, 248, 0)');

            const neonGlow = {
                id: 'neonGlow',
                beforeDatasetsDraw(chart) {
                    chart.ctx.save();
                    chart.ctx.shadowColor = 'rgba(129, 140, 248, 0.9)';
                    chart.ctx.shadowBlur = 14;
                },
                afterDatasetsDraw(chart) { chart.ctx.restore(); },
            };

            chartInstance = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: sleepLabels,
                    datasets: [{
                        data: sleepData,
                        borderColor: '#818cf8',
                        backgroundColor: neonGradient,
                        tension: 0.4,
                        fill: true,
                        borderWidth: 2.5,
                        pointBackgroundColor: '#c7d2fe',
                        pointBorderColor: '#818cf8',
                        pointBorderWidth: 2,
                        pointRadius: 4,
                    }],
                },
                plugins: [neonGlow],
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { display: false } },
                    scales: {
                        x: { grid: { display: false }, ticks: { color: '#94a3b8', font: { size: 9 } } },
                        y: { min: 0, grid: { color: 'rgba(129, 140, 248, 0.08)' }, ticks: { color: '#94a3b8', font: { size: 9 }, stepSize: 2 } },
                    },
                },
            });
        }
    </script>
</body>
</html>
"""
    return render_template_string(
        html_template,
        today_label=today_label,
        today_tasks=today_tasks,
        total_spent=total_spent,
        recent_expenses=recent_expenses,
        sleep_data=sleep_data,
        sleep_labels=sleep_labels,
        last_sleep=last_sleep,
    )


print("Запуск...")
# === НАСТРОЙКА WEBHOOK (Вместо polling) ===
WEBHOOK_URL = f"https://my-assistant-k7rq.onrender.com/webhook/{TELEGRAM_TOKEN}"

@app.route(f'/webhook/{TELEGRAM_TOKEN}', methods=['POST'])
def webhook():
    json_str = request.stream.read().decode('utf-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return '!', 200

# Устанавливаем Webhook при запуске
bot.remove_webhook()
bot.set_webhook(url=WEBHOOK_URL)

app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), use_reloader=False)

# Не забудь добавить 'request' в импорты (from flask import Flask, render_template_string, request)
