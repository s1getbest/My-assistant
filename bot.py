import telebot
import google.generativeai as genai
import os
import json
import io
import threading
from flask import Flask, render_template_string
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
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
msk_tz = pytz.timezone("Europe/Moscow")

# Фоновый планировщик для напоминаний
scheduler = BackgroundScheduler(timezone=msk_tz)
scheduler.start()

# === ПОДКЛЮЧЕНИЕ К GOOGLE DRIVE ===
try:
    token_data = json.loads(GOOGLE_TOKEN_JSON)
    creds = Credentials.from_authorized_user_info(token_data, scopes=["https://www.googleapis.com/auth/drive"])
    drive_service = build('drive', 'v3', credentials=creds)
    print("Успешно авторизовались на Google Drive!")
except Exception as e:
    print(f"Ошибка авторизации Google Drive: {e}")

# Функции работы с Google Drive (работают стабильно)
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

# === ОТПРАВКА НАПОМИНАНИЙ ===
def send_dynamic_reminder(chat_id, task_text):
    try:
        prompt = f"Ты личный строгий ассистент Павел. Сработало напоминание: '{task_text}'. Напиши короткое, очень емкое и мотивирующее сообщение прямо сейчас."
        response = model.generate_content(prompt)
        reply = response.text
    except Exception as e:
        reply = f"Пора делать: {task_text}"
        
    bot.send_message(chat_id, f"⏰ **НАПОМИНАНИЕ!**\n\n{reply}")

# === КОМАНДЫ БОТА ===

@bot.message_handler(commands=['start'])
def send_welcome(message):
    if not is_me(message): return
    bot.reply_to(message, "Привет! Твой личный мозг запущен. Я подключен к твоей папке в Google Диске и Obsidian!")

# Команда записи сна в Obsidian
@bot.message_handler(commands=['sleep'])
def track_sleep(message):
    if not is_me(message): return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        args = message.text.split()
        if len(args) < 2:
            bot.reply_to(message, "Укажи часы сна. Пример: `/sleep 7.5`", parse_mode="Markdown")
            return
        
        hours = args[1]
        today_str = datetime.now(msk_tz).strftime("%Y-%m-%d")
        
        # Читаем старый файл здоровья, добавляем запись и пишем обратно
        try:
            current_health = read_file_from_drive("Health.md")
        except:
            current_health = ""
            
        new_health = current_health.strip() + f"\n* {today_str}: {hours}"
        write_file_to_drive("Health.md", new_health.strip())
        
        bot.reply_to(message, f"🛌 **Сон записан в твой Obsidian!**\n\n> Дата: {today_str}\n> Время: {hours} ч.\nДанные на Дашборде обновлены.")
    except Exception as e:
        bot.reply_to(message, f"Ошибка записи сна: {e}")

# Чат с ИИ и планирование напоминаний
@bot.message_handler(func=lambda message: True)
def chat_with_gemini(message):
    if not is_me(message): return 
    bot.send_chat_action(message.chat.id, 'typing') 
    
    try:
        try:
            current_memory = read_file_from_drive("Memory.md")
        except Exception as drive_err:
            print(f"Не удалось прочитать диск: {drive_err}")
            current_memory = ""
            
        if not current_memory:
            current_memory = "Пока пустая долгосрочная память. Запиши важные факты о пользователе."

        now_msk = datetime.now(msk_tz).strftime("%Y-%m-%d %H:%M")

        prompt = f"""Текущее время в Москве: {now_msk}

Вот твоя текущая долгосрочная память:
---
{current_memory}
---

S1get пишет тебе: "{message.text}"

Твоя задача:
1. Ответь на его сообщение в своем фирменном стиле (четко, по делу, структурировано).
2. Если в сообщении есть важные факты, обнови долгосрочную память (сохранив старые данные).
3. ЕСЛИ пользователь просит напомнить ему о чем-то в конкретное время (например: "напомни сегодня в 21:00...", "напомни через 20 минут...", "напомни завтра в полдень..."), ты должен вычислить точную дату и время этого события в формате ГГГГ-ММ-ДД ЧЧ:ММ по Московскому времени.

Выведи свой ответ СТРОГО в следующем формате (используй разделители [SEPARATOR] и [SCHEDULE]):
[ОТВЕТ]
Текст твоего ответа пользователю (подтверди, что ты запланировал напоминание, если он просил).
[SEPARATOR]
[ПАМЯТЬ]
Весь обновленный список памяти (включая старые и новые факты). Если изменений нет, скопируй старую память без изменений. Помни: не дублируй записи!
[SEPARATOR]
[SCHEDULE]
Если нужно создать напоминание, напиши СТРОГО в одну строку: ГГГГ-ММ-ДД ЧЧ:ММ | Текст напоминания
Если напоминаний создавать не нужно, оставь этот блок пустым.
"""

        response = model.generate_content(prompt)
        raw_text = response.text

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

        # Записываем память на Диск
        try:
            write_file_to_drive("Memory.md", memory_part)
        except Exception as drive_err:
            print(f"Не удалось записать на диск: {drive_err}")
            reply_part += f"\n\n⚠️ (Заметка не сохранилась на Диск: {drive_err})"

        # Планируем напоминание
        if schedule_part and "|" in schedule_part:
            try:
                dt_str, task_text = schedule_part.split("|", 1)
                dt_str = dt_str.strip()
                task_text = task_text.strip()
                
                run_date = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
                run_date = msk_tz.localize(run_date)
                
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

        bot.reply_to(message, reply_part)

    except Exception as e:
        bot.reply_to(message, f"Ошибка в логике ИИ: {e}")

# === WEB SERVER ===
app = Flask(__name__)

@app.route('/')
def home():
    try:
        content = read_file_from_drive("Memory.md")
        lines = content.split("\n")
        todo_list = []
        for line in lines:
            if "[ ]" in line:
                clean_task = line.replace("* [ ]", "").replace("- [ ]", "").replace("[ ]", "").strip()
                todo_list.append(clean_task)
    except Exception as e:
        todo_list = [f"Ошибка задач: {e}"]

    try:
        health_content = read_file_from_drive("Health.md")
        health_lines = [l.strip() for l in health_content.split("\n") if l.strip()]
        if health_lines:
            last_line = health_lines[-1]
            sleep_hours = last_line.split(":")[-1].strip()
        else:
            sleep_hours = "0"
    except Exception as e:
        sleep_hours = "Ошибка"

    try:
        target_date = datetime(2026, 7, 25, tzinfo=msk_tz)
        days_left = (target_date.date() - datetime.now(msk_tz).date()).days
    except:
        days_left = "?"

    html_template = """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>Второй Мозг</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-slate-950 text-slate-100 font-sans p-4 min-h-screen select-none">
        <div class="max-w-md mx-auto space-y-4">
            
            <!-- Профиль -->
            <div class="bg-slate-900 p-4 rounded-2xl border border-slate-800 flex items-center space-x-4">
                <div class="w-12 h-12 bg-indigo-600 rounded-full flex items-center justify-center text-lg font-bold text-white shadow-lg shadow-indigo-500/20">ПВ</div>
                <div>
                    <h1 class="font-bold text-base">Павел Власов</h1>
                    <p class="text-xs text-indigo-400">@S1get · Абитуриент 2026</p>
                </div>
            </div>
            
            <!-- Дедлайн -->
            <div class="bg-gradient-to-r from-indigo-950 to-slate-900 p-4 rounded-2xl border border-indigo-500/20">
                <div class="flex justify-between items-center mb-2">
                    <span class="text-xs font-semibold text-indigo-300">ПОСТУПЛЕНИЕ В ВУЗ</span>
                    <span class="text-xs bg-indigo-500/20 text-indigo-300 px-2 py-0.5 rounded-full font-bold">{{ days_left }} дней</span>
                </div>
                <div class="w-full bg-slate-800 h-2 rounded-full overflow-hidden">
                    <div class="bg-indigo-500 h-full" style="width: 75%"></div>
                </div>
                <p class="text-[10px] text-slate-400 mt-2">Критический дедлайн: 25 июля 2026 г.</p>
            </div>

            <!-- Задачи из Obsidian -->
            <div class="bg-slate-900 p-4 rounded-2xl border border-slate-800">
                <h2 class="text-xs font-bold text-slate-400 uppercase tracking-wider mb-3 flex items-center">📋 Задачи из Obsidian</h2>
                <ul class="space-y-2">
                    {% for task in tasks %}
                    <li class="flex items-center space-x-3 text-xs bg-slate-950/50 p-3 rounded-xl border border-slate-800/80">
                        <span class="w-2 h-2 bg-indigo-500 rounded-full shadow-lg shadow-indigo-500/50"></span>
                        <span class="truncate pr-2">{{ task }}</span>
                    </li>
                    {% else %}
                    <li class="text-xs text-slate-500 italic p-3 text-center">Все задачи выполнены! Отдыхай 🎉</li>
                    {% endfor %}
                </ul>
            </div>

            <!-- Разделы Аналитики -->
            <div class="grid grid-cols-2 gap-4">
                <div class="bg-slate-900 p-4 rounded-2xl border border-slate-800 flex flex-col justify-between h-28">
                    <span class="text-[10px] font-bold text-slate-400 uppercase">Бюджет</span>
                    <span class="text-xl font-extrabold text-emerald-400 mt-1">1 500 ₽</span>
                    <span class="text-[9px] text-slate-500">Вклад (Альфа-Банк)</span>
                </div>
                <div class="bg-slate-900 p-4 rounded-2xl border border-slate-800 flex flex-col justify-between h-28">
                    <span class="text-[10px] font-bold text-slate-400 uppercase">Сон (Часы)</span>
                    <span class="text-xl font-extrabold text-indigo-400 mt-1">{{ sleep_hours }} ч.</span>
                    <span class="text-[9px] text-slate-500">Показатель из Obsidian</span>
                </div>
            </div>
            
            <p class="text-center text-[10px] text-slate-600">Синхронизировано с Obsidian & Google Drive</p>
        </div>
    </body>
    </html>
    """
    return render_template_string(html_template, tasks=todo_list, days_left=days_left, sleep_hours=sleep_hours)

# Запуск веб-сервера
threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))).start()

print("Запуск...")
bot.infinity_polling()
