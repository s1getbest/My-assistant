import telebot
import google.generativeai as genai
import os
import json
import io
import threading
import re
from flask import Flask
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
import pytz
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler

# === НАСТРОЙКИ ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MY_TELEGRAM_ID = int(os.getenv("MY_TELEGRAM_ID", 0))
FOLDER_ID = os.getenv("FOLDER_ID")
GOOGLE_TOKEN_JSON = os.getenv("GOOGLE_TOKEN_JSON")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-3.5-flash')
msk_tz = pytz.timezone("Europe/Moscow")

scheduler = BackgroundScheduler(timezone=msk_tz)
scheduler.start()

# === CONNECT GOOGLE DRIVE ===
try:
    token_data = json.loads(GOOGLE_TOKEN_JSON)
    creds = Credentials.from_authorized_user_info(token_data, scopes=["https://www.googleapis.com/auth/drive"])
    drive_service = build('drive', 'v3', credentials=creds)
except Exception as e:
    print(f"Drive Error: {e}")

def get_file_id(name):
    q = f"name = '{name}' and '{FOLDER_ID}' in parents and trashed = false"
    res = drive_service.files().list(q=q, fields='files(id)').execute()
    return res.get('files', [])[0]['id'] if res.get('files') else None

def read_drive(name):
    fid = get_file_id(name)
    if not fid: return ""
    request = drive_service.files().get_media(fileId=fid)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done: _, done = downloader.next_chunk()
    return fh.getvalue().decode('utf-8')

def append_to_drive(filename, text_to_add):
    file_id = get_file_id_by_name(filename)
    current_content = read_file_from_drive(filename)
    new_content = current_content + "\n" + text_to_add
    
    media = MediaIoBaseUpload(io.BytesIO(new_content.encode('utf-8')), mimetype='text/markdown', resumable=True)
    if file_id:
        drive_service.files().update(fileId=file_id, media_body=media).execute()
    else:
        file_metadata = {'name': filename, 'parents': [FOLDER_ID]}
        drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()

# === ЛОГИКА НАПОМИНАНИЙ ===
def send_alert(chat_id, text):
    try:
        res = model.generate_content(f"Ты ассистент Павла. Сработало напоминание: '{text}'. Напиши короткий, мотивирующий пинок.")
        bot.send_message(chat_id, f"⏰ **НАПОМИНАНИЕ!**\n\n{res.text}")
    except:
        bot.send_message(chat_id, f"⏰ **ПОРА ДЕЛАТЬ:** {text}")

# Умная функция: вытаскивает задачи из Obsidian и ставит в очередь
def reschedule_tasks():
    print("Сканирую Obsidian на наличие задач...")
    content = read_drive("Memory.md")
    # Ищем строки вида: [ ] ГГГГ-ММ-ДД ЧЧ:ММ | Текст
    tasks = re.findall(r"\[ \] (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) \| (.*)", content)
    now = datetime.now(msk_tz)
    for dt_str, text in tasks:
        try:
            run_dt = msk_tz.localize(datetime.strptime(dt_str, "%Y-%m-%d %H:%M"))
            if run_dt > now:
                scheduler.add_job(send_alert, 'date', run_date=run_dt, args=[MY_TELEGRAM_ID, text])
                print(f"Восстановлено напоминание: {dt_str} - {text}")
        except: continue

# === ОБРАБОТКА СООБЩЕНИЙ ===
@bot.message_handler(func=lambda m: m.from_user.id == MY_TELEGRAM_ID)
def chat(message):
    bot.send_chat_action(message.chat.id, 'typing')
    now_msk = datetime.now(msk_tz).strftime("%Y-%m-%d %H:%M")
    memory = read_drive("Memory.md")
    # Замени промпт в функции chat_with_gemini на этот:
    prompt = f"""
        Ты - S1get-Agent, исполняющая система. 
        Твоя цель - управлять файлом Memory.md.
        
        Твои команды:
        1. Если пользователь дает задачу с временем, верни ОДНУ строку: [SCHEDULE] ГГГГ-ММ-ДД ЧЧ:ММ | Текст задачи
        2. Если пользователь хочет добавить обычную задачу, верни: [ADD] [ ] Текст задачи
        3. Если это просто вопрос, ответь кратко (до 2 предложений).
        
        НЕ ПОВТОРЯЙ содержимое файла памяти в ответе. Только результат действия.
        """
    try:
        res = model.generate_content(prompt).text
        # Разделение логики (упрощенное)
        reply = res.split("[SCHEDULE]")[0].replace("[ОТВЕТ]", "").strip()
        
        # Если ИИ выдал план напоминания
        if "[SCHEDULE]" in res:
            sched_data = res.split("[SCHEDULE]")[1].strip()
            if "|" in sched_data:
                dt_s, txt = sched_data.split("|", 1)
                run_dt = msk_tz.localize(datetime.strptime(dt_s.strip(), "%Y-%m-%d %H:%M"))
                scheduler.add_job(send_alert, 'date', run_date=run_dt, args=[MY_TELEGRAM_ID, txt.strip()])
                # Добавляем задачу в память в строгом формате
                memory += f"\n* [ ] {dt_s.strip()} | {txt.strip()}"
        
        write_drive("Memory.md", memory)
        bot.reply_to(message, reply)
    except Exception as e:
        bot.reply_to(message, f"Ошибка: {e}")

# === WEB SERVER ===
app = Flask(__name__)
@app.route('/')
def home(): return "Agent Alive"

threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))).start()

# При запуске - восстанавливаем задачи из файла
reschedule_tasks()
bot.infinity_polling()
