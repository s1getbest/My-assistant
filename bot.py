import telebot
import google.generativeai as genai
import os
import json
import io
import threading
from flask import Flask
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

# Библиотеки для планировщика
import pytz
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler

# === КЛЮЧИ И НАСТРОЙКИ ИЗ ОБЛАКА ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MY_TELEGRAM_ID = int(os.getenv("MY_TELEGRAM_ID", 0))
FOLDER_ID = os.getenv("FOLDER_ID")
GOOGLE_TOKEN_JSON = os.getenv("GOOGLE_TOKEN_JSON")

# Подключаем бота и ИИ
bot = telebot.TeleBot(TELEGRAM_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)

model = genai.GenerativeModel('gemini-3.5-flash')

# Настраиваем фоновый планировщик по Москве
scheduler = BackgroundScheduler(timezone=pytz.timezone("Europe/Moscow"))
scheduler.start()

# === ПОДКЛЮЧЕНИЕ ЧЕРЕЗ ЛИЧНЫЙ OAUTH ===
try:
    token_data = json.loads(GOOGLE_TOKEN_JSON)
    creds = Credentials.from_authorized_user_info(token_data, scopes=["https://www.googleapis.com/auth/drive"])
    drive_service = build('drive', 'v3', credentials=creds)
    print("Успешно авторизовались от твоего имени на Google Drive!")
except Exception as e:
    print(f"Ошибка OAuth авторизации: {e}")

# Функции работы с Google Drive
def get_file_id_by_name(filename):
    query = f"name = '{filename}' and '{FOLDER_ID}' in parents and trashed = false"
    results = drive_service.files().list(q=query, spaces='drive', fields='files(id)').execute()
    files = results.get('files', [])
    if files:
        return files[0]['id']
    return None

def read_file_from_drive(filename):
    file_id = get_file_id_by_name(filename)
    if not file_id:
        return ""
    request = drive_service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while done is False:
        status, done = downloader.next_chunk()
    return fh.getvalue().decode('utf-8')

def write_file_to_drive(filename, content):
    file_id = get_file_id_by_name(filename)
    media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='text/markdown', resumable=True)
    if file_id:
        drive_service.files().update(fileId=file_id, media_body=media).execute()
    else:
        file_metadata = {'name': filename, 'parents': [FOLDER_ID]}
        drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()

def is_me(message):
    return message.from_user.id == MY_TELEGRAM_ID

# === ФУНКЦИЯ ОТПРАВКИ НАПОМИНАНИЯ ===
def send_dynamic_reminder(chat_id, task_text):
    try:
        # ИИ генерирует бодрый текст пинка для конкретной задачи
        prompt = f"Ты личный строгий ассистент Павла. Сработало его запланированное напоминание: '{task_text}'. Напиши ему короткое, очень емкое и мотивирующее сообщение прямо сейчас, чтобы он встал и сделал это."
        response = model.generate_content(prompt)
        reply = response.text
    except Exception as e:
        reply = f"Пора делать: {task_text}"
        
    bot.send_message(chat_id, f"⏰ **НАПОМИНАНИЕ!**\n\n{reply}")

@bot.message_handler(commands=['start'])
def send_welcome(message):
    if not is_me(message):
        bot.reply_to(message, "Доступ запрещен.")
        return
    bot.reply_to(message, "Привет! Твой личный мозг запущен. Я готов ставить напоминания на любое время. Просто напиши мне: 'Напомни мне [когда] [что сделать]'.")

# Основной чат с умным планированием
@bot.message_handler(func=lambda message: True)
def chat_with_gemini(message):
    if not is_me(message):
        return 
        
    bot.send_chat_action(message.chat.id, 'typing') 
    
    try:
        # Читаем память
        try:
            current_memory = read_file_from_drive("Memory.md")
        except Exception as drive_err:
            print(f"Не удалось прочитать диск: {drive_err}")
            current_memory = ""
            
        if not current_memory:
            current_memory = "Пока пустая долгосрочная память. Запиши важные факты о пользователе."

        # Получаем текущее время в Москве для ИИ
        msk_tz = pytz.timezone("Europe/Moscow")
        now_msk = datetime.now(msk_tz).strftime("%Y-%m-%d %H:%M:%S")

        # Промпт с обучением планированию
        prompt = f"""Ты личный ассистент и наставник S1get по тайм-менеджменту. 
Ты ведешь его долгосрочную память, чтобы помогать ему планировать жизнь и учебу в ВУЗе.

Текущее время в Москве: {now_msk}

Вот твоя текущая долгосрочная память:
---
{current_memory}
---

S1get пишет тебе: "{message.text}"

Твоя задача:
1. Ответь на его сообщение в своем фирменном стиле (четко, по делу, структурировано).
2. Если в сообщении есть важные факты, обнови долгосрочную память (сохранив старые данные).
3. ЕСЛИ пользователь просит напомнить ему о чем-то в конкретное время (например: "напомни сегодня в 21:00...", "напомни через 20 минут...", "напомни завтра в полдень..."), ты должен вычислить точную дату и время этого события в формате ГГГГ-ММ-ДД ЧЧ:ММ:СС по Московскому времени.

Выведи свой ответ СТРОГО в следующем формате (используй разделители [SEPARATOR] и [SCHEDULE]):
[ОТВЕТ]
Текст твоего ответа пользователю (подтверди, что ты запланировал напоминание, если он просил).
[SEPARATOR]
[ПАМЯТЬ]
Весь обновленный список памяти (включая старые и новые факты). Если изменений нет, скопируй старую память без изменений.
[SEPARATOR]
[SCHEDULE]
Если нужно создать напоминание, напиши СТРОГО в одну строку: ГГГГ-ММ-ДД ЧЧ:ММ:СС | Текст напоминания
Если напоминаний создавать не нужно, оставь этот блок пустым.
"""

        response = model.generate_content(prompt)
        raw_text = response.text

        # Разбираем блоки [SCHEDULE], [ОТВЕТ] и [ПАМЯТЬ]
        schedule_part = ""
        if "[SCHEDULE]" in raw_text:
            parts = raw_text.split("[SCHEDULE]")
            schedule_part = parts[1].strip()
            raw_text = parts[0].strip()

        if "[SEPARATOR]" in raw_text:
            parts = raw_text.split("[SEPARATOR]")
            reply_part = parts[0].replace("[ОТВЕТ]", "").strip()
            memory_part = parts[1].replace("[ПАМЯТЬ]", "").strip()
        else:
            reply_part = raw_text
            memory_part = current_memory

        # 1. Сохраняем память на Диск
        try:
            write_file_to_drive("Memory.md", memory_part)
        except Exception as drive_err:
            print(f"Не удалось записать на диск: {drive_err}")
            reply_part += f"\n\n⚠️ (Заметка не сохранилась на Диск: {drive_err})"

        # 2. Планируем динамическое напоминание в фоновом режиме
        if schedule_part and "|" in schedule_part:
            try:
                dt_str, task_text = schedule_part.split("|", 1)
                dt_str = dt_str.strip()
                task_text = task_text.strip()
                
                # Парсим дату в Московском часовом поясе
                run_date = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                run_date = msk_tz.localize(run_date)
                
                # Добавляем задачу в планировщик
                scheduler.add_job(
                    send_dynamic_reminder, 
                    'date', 
                    run_date=run_date, 
                    args=[MY_TELEGRAM_ID, task_text]
                )
                print(f"Успешно запланировано напоминание на {dt_str}: {task_text}")
            except Exception as sched_err:
                print(f"Ошибка планирования: {sched_err}")
                reply_part += f"\n\n⚠️ (Не удалось завести будильник: {sched_err})"

        # 3. Отвечаем в ТГ
        bot.reply_to(message, reply_part)

    except Exception as e:
        bot.reply_to(message, f"Ошибка в логике ИИ: {e}")

# === WEB SERVER ===
app = Flask(__name__)
@app.route('/')
def home():
    return "Мозг жив и синхронизирован!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_web).start()

print("Запуск...")
bot.infinity_polling()
