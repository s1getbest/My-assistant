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

# === ПОДКЛЮЧЕНИЕ ЧЕРЕЗ ЛИЧНЫЙ OAUTH ===
try:
    token_data = json.loads(GOOGLE_TOKEN_JSON)
    creds = Credentials.from_authorized_user_info(token_data, scopes=["https://www.googleapis.com/auth/drive"])
    drive_service = build('drive', 'v3', credentials=creds)
    print("Успешно авторизовались от твоего имени на Google Drive!")
except Exception as e:
    print(f"Ошибка OAuth авторизации: {e}")

# Функции работы с Google Drive (работают от твоего имени)
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

@bot.message_handler(commands=['start'])
def send_welcome(message):
    if not is_me(message):
        bot.reply_to(message, "Доступ запрещен.")
        return
    bot.reply_to(message, "Привет! Твой личный мозг запущен. Я подключен к твоей папке в Google Диске и Obsidian!")

@bot.message_handler(func=lambda message: True)
def chat_with_gemini(message):
    if not is_me(message):
        return 
        
    bot.send_chat_action(message.chat.id, 'typing') 
    
    try:
        # 1. Читаем текущую память
        try:
            current_memory = read_file_from_drive("Memory.md")
        except Exception as drive_err:
            print(f"Не удалось прочитать диск: {drive_err}")
            current_memory = ""
            
        if not current_memory:
            current_memory = "Пока пустая долгосрочная память. Запиши важные факты о пользователе."

        # 2. Промпт
        prompt = f"""Ты личный ассистент и наставник S1get по тайм-менеджменту. 
Ты ведешь его долгосрочную память, чтобы помогать ему планировать жизнь и учебу в ВУЗе.

Вот твоя текущая долгосрочная память (список важных фактов, планов, дедлайнов S1get):
---
{current_memory}
---

S1get пишет тебе: "{message.text}"

Твоя задача:
1. Ответь на его сообщение в своем фирменном стиле (четко, структурно, без воды).
2. Если в его сообщении есть факты, которые стоит запомнить на будущее (его имя, планы, дедлайны по предметам, расписание, цели на 10 лет/год/месяц), ОБНОВИ долгосрочную память. Добавь новые факты, сохранив старые. Держи память в виде аккуратного структурированного списка Markdown.

Выведи свой ответ СТРОГО в следующем формате (используй разделитель [SEPARATOR]):
[ОТВЕТ]
Текст твоего ответа пользователю.
[SEPARATOR]
[ПАМЯТЬ]
Весь обновленный список памяти (включая старые и новые факты). Если ничего нового записывать не нужно, просто выведи старую память без изменений.
"""

        # 3. Запрос в ИИ
        response = model.generate_content(prompt)
        raw_text = response.text

        # 4. Разбор ответа
        if "[SEPARATOR]" in raw_text:
            parts = raw_text.split("[SEPARATOR]")
            reply_part = parts[0].replace("[ОТВЕТ]", "").strip()
            memory_part = parts[1].replace("[ПАМЯТЬ]", "").strip()
        else:
            reply_part = raw_text
            memory_part = current_memory

        # 5. Записываем память на Google Диск
        try:
            write_file_to_drive("Memory.md", memory_part)
        except Exception as drive_err:
            print(f"Не удалось записать на диск: {drive_err}")
            reply_part += f"\n\n⚠️ (Заметка не сохранилась на Диск из-за технической ошибки: {drive_err})"

        # 6. Отвечаем
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
