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
from flask import Flask, render_template_string, request


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
# === КОМАНДА ЗАПИСИ РАСХОДОВ ===
@bot.message_handler(commands=['spent'])
def track_expense(message):
    if not is_me(message): return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        args = message.text.split()
        if len(args) < 2:
            bot.reply_to(message, "Укажи сумму и категорию. Пример: `/spent 500 пицца`")
            return
        
        amount = args[1]
        desc = " ".join(args[2:]) if len(args) > 2 else "Расход"
        today_str = datetime.now(msk_tz).strftime("%Y-%m-%d")
        
        # Читаем старые финансы, дописываем новую строчку
        try:
            current_finance = read_file_from_drive("Finance.md")
        except:
            current_finance = ""
            
        new_finance = current_finance.strip() + f"\n* {today_str}: {amount} | {desc}"
        write_file_to_drive("Finance.md", new_finance.strip())
        
        bot.reply_to(message, f"💸 **Расход записан!**\n\n> Сумма: {amount} ₽\n> Описание: {desc}\nВнесено в твой Obsidian `Finance.md`!")
    except Exception as e:
        bot.reply_to(message, f"Ошибка записи расхода: {e}")

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

        today_str = datetime.now(msk_tz).strftime("%Y-%m-%d")

        prompt = f"""Текущее время в Москве: {now_msk}
Сегодняшняя дата: {today_str}

Вот твоя текущая долгосрочная память:
---
{current_memory}
---

S1get пишет тебе: "{message.text}"

Твоя задача:
1. Ответь на его сообщение в своем фирменном стиле (четко, по делу, структурировано). В блоке [ОТВЕТ] НЕ перечисляй и НЕ дублируй весь список памяти — только живой ответ пользователю.
2. Если в сообщении есть важные факты, обнови долгосрочную память (сохранив старые данные) в блоке [ПАМЯТЬ].
3. ЕСЛИ пользователь просит напомнить о чём-то в конкретное время ("напомни сегодня в 21:00...", "напомни через 20 минут..."), вычисли точную дату и время в формате ГГГГ-ММ-ДД ЧЧ:ММ по Москве. Блок [SCHEDULE]: ГГГГ-ММ-ДД ЧЧ:ММ | Текст напоминания.
4. ЕСЛИ пользователь в свободной форме упомянул сон ("поспал 8 часов", "спал 7.5 часов", "сон 6 ч"), извлеки часы. Блок [HEALTH_RECORD]: ГГГГ-ММ-ДД: <часы>
5. ЕСЛИ пользователь упомянул расход ("потратил 450р на обед", "купил проездной за 300 рублей"), извлеки сумму и описание. Блок [FINANCE_RECORD]: ГГГГ-ММ-ДД: <сумма> | <описание>

Выведи ответ СТРОГО в формате:
[ОТВЕТ]
Текст ответа пользователю (кратко подтверди запись сна/расхода или напоминание, если применимо).
[SEPARATOR]
[ПАМЯТЬ]
Весь обновлённый список памяти (старые + новые факты). Если изменений нет — скопируй старую память. Не дублируй записи!
[SEPARATOR]
[SCHEDULE]
Текст напоминания (если нужно, иначе пусто).
[SEPARATOR]
[HEALTH_RECORD]
Данные сна (если были, иначе пусто).
[SEPARATOR]
[FINANCE_RECORD]
Данные расхода (если были, иначе пусто).
"""
        
        response = model.generate_content(prompt)
        raw_text = response.text

        def extract_block(text, block_name):
            marker = f"[{block_name}]"
            if marker not in text:
                return ""
            part = text.split(marker, 1)[1]
            if "[SEPARATOR]" in part:
                return part.split("[SEPARATOR]", 1)[0].strip()
            return part.strip()

        schedule_part = extract_block(raw_text, "SCHEDULE")
        health_record = extract_block(raw_text, "HEALTH_RECORD")
        finance_record = extract_block(raw_text, "FINANCE_RECORD")

        if "[SEPARATOR]" in raw_text:
            parts = raw_text.split("[SEPARATOR]")
            reply_part = parts[0].replace("[ОТВЕТ]", "").strip()
            memory_part = parts[1].replace("[ПАМЯТЬ]", "").strip() if len(parts) > 1 else current_memory
        else:
            reply_part = raw_text.replace("[ОТВЕТ]", "").strip()
            memory_part = current_memory

        # 1. Записываем память на Диск
        try:
            write_file_to_drive("Memory.md", memory_part)
        except Exception as drive_err:
            reply_part += f"\n\n⚠️ (Не удалось сохранить в Memory: {drive_err})"

        # 2. Если ИИ вытащил сон — пишем в Health.md
        if health_record and ":" in health_record:
            try:
                dt_s, hours = health_record.split(":", 1)
                dt_s, hours = dt_s.strip(), hours.strip()
                try:
                    current_health = read_file_from_drive("Health.md")
                except Exception:
                    current_health = ""
                new_health = current_health.strip() + f"\n* {dt_s}: {hours}"
                write_file_to_drive("Health.md", new_health.strip())
            except Exception as e:
                print(f"Ошибка записи сна из разговора: {e}")

        # 3. Если ИИ вытащил расход — пишем в Finance.md
        if finance_record and ":" in finance_record:
            try:
                dt_s, val_p = finance_record.split(":", 1)
                dt_s, val_p = dt_s.strip(), val_p.strip()
                try:
                    current_finance = read_file_from_drive("Finance.md")
                except Exception:
                    current_finance = ""
                new_finance = current_finance.strip() + f"\n* {dt_s}: {val_p}"
                write_file_to_drive("Finance.md", new_finance.strip())
            except Exception as e:
                print(f"Ошибка записи финансов из разговора: {e}")

        # 4. Планируем напоминание
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
            except Exception as sched_err:
                reply_part += f"\n\n⚠️ (Не удалось завести будильник: {sched_err})"

        bot.reply_to(message, reply_part)

    except Exception as e:
        bot.reply_to(message, f"Ошибка: {e}")

# === WEB SERVER ===
app = Flask(__name__)
# === ПРОАКТИВНЫЙ ПИНГ (Проверка сна в 10:00 утра) ===
def check_daily_sleep():
    try:
        today_str = datetime.now(msk_tz).strftime("%Y-%m-%d")
        health_content = read_file_from_drive("Health.md")
        
        # Если в файле здоровья нет сегодняшней даты, бот сам начинает диалог
        if today_str not in health_content:
            bot.send_message(
                MY_TELEGRAM_ID, 
                "Павел, доброе утро! 🛌 Я заметил, что сегодня ты еще не записал свой сон. Расскажи, сколько часов удалось поспать и как самочувствие?"
            )
    except Exception as e:
        print(f"Ошибка проактивного пинга: {e}")

# Добавляем задачу в наш планировщик (каждый день в 10:00 по Москве)
scheduler.add_job(check_daily_sleep, 'cron', hour=10, minute=0)
@app.route('/')
def home():
    # 1. Читаем активные задачи из Memory.md
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

    # 2. Читаем и суммируем расходы за текущий месяц из Finance.md
    try:
        finance_content = read_file_from_drive("Finance.md")
        finance_lines = [l.strip() for l in finance_content.split("\n") if l.strip()]
        current_month = datetime.now(msk_tz).strftime("%Y-%m")
        total_spent = 0
        for line in finance_lines:
            if ":" not in line:
                continue
            date_part = line.split(":", 1)[0].replace("*", "").strip()
            if not date_part.startswith(current_month):
                continue
            val_part = line.split(":", 1)[1].strip()
            num_part = val_part.split("|")[0].strip().replace("₽", "").replace("р", "").replace("руб", "").strip()
            try:
                total_spent += int(float(num_part))
            except ValueError:
                pass
    except Exception:
        total_spent = 0

    # 3. Читаем данные сна для графика (последние 7 записей)
    sleep_data = []
    sleep_labels = []
    last_sleep = "0"
    try:
        health_content = read_file_from_drive("Health.md")
        health_lines = [l.strip() for l in health_content.split("\n") if l.strip()]
        if health_lines:
            last_line = health_lines[-1]
            last_sleep = last_line.split(":")[-1].strip()
            
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
        sleep_data = [0]
        sleep_labels = ["Нет данных"]

    if not sleep_data:
        sleep_data = [0]
        sleep_labels = ["Нет данных"]

    # 4. Считаем дни до дедлайна
    try:
        target_date = datetime(2026, 7, 25, tzinfo=msk_tz)
        days_left = (target_date.date() - datetime.now(msk_tz).date()).days
    except:
        days_left = "?"

    # Красивый HTML-шаблон Дашборда (с неоновым графиком сна и бюджетом!)
    html_template = """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>Второй Мозг</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
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

            <!-- График тренда сна -->
            <div class="bg-slate-900 p-4 rounded-2xl border border-slate-800">
                <h2 class="text-xs font-bold text-slate-400 uppercase tracking-wider mb-3">📈 Тренд сна (последние 7 дней)</h2>
                <div class="h-28">
                    <canvas id="sleepChart"></canvas>
                </div>
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
                    <span class="text-xl font-extrabold text-emerald-400 mt-1">{{ total_spent }} ₽</span>
                    <span class="text-[9px] text-slate-500">Расходы за месяц</span>
                </div>
                <div class="bg-slate-900 p-4 rounded-2xl border border-slate-800 flex flex-col justify-between h-28">
                    <span class="text-[10px] font-bold text-slate-400 uppercase">Последний Сон</span>
                    <span class="text-xl font-extrabold text-indigo-400 mt-1">{{ sleep_hours }} ч.</span>
                    <span class="text-[9px] text-slate-500">Показатель из Obsidian</span>
                </div>
            </div>
            
            <p class="text-center text-[10px] text-slate-600">Синхронизировано с Obsidian & Google Drive</p>
        </div>

        <script>
            const ctx = document.getElementById('sleepChart').getContext('2d');
            const neonGradient = ctx.createLinearGradient(0, 0, 0, 112);
            neonGradient.addColorStop(0, 'rgba(129, 140, 248, 0.35)');
            neonGradient.addColorStop(1, 'rgba(129, 140, 248, 0)');

            const neonGlow = {
                id: 'neonGlow',
                beforeDatasetsDraw(chart) {
                    const { ctx } = chart;
                    ctx.save();
                    ctx.shadowColor = 'rgba(129, 140, 248, 0.9)';
                    ctx.shadowBlur = 14;
                },
                afterDatasetsDraw(chart) {
                    chart.ctx.restore();
                }
            };

            new Chart(ctx, {
                type: 'line',
                data: {
                    labels: {{ sleep_labels|tojson }},
                    datasets: [{
                        data: {{ sleep_data|tojson }},
                        borderColor: '#818cf8',
                        backgroundColor: neonGradient,
                        tension: 0.4,
                        fill: true,
                        borderWidth: 2.5,
                        pointBackgroundColor: '#c7d2fe',
                        pointBorderColor: '#818cf8',
                        pointBorderWidth: 2,
                        pointRadius: 4,
                        pointHoverRadius: 6
                    }]
                },
                plugins: [neonGlow],
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { display: false } },
                    scales: {
                        x: {
                            grid: { display: false },
                            ticks: { color: '#94a3b8', font: { size: 9 } }
                        },
                        y: {
                            min: 0,
                            grid: { color: 'rgba(129, 140, 248, 0.08)' },
                            ticks: { color: '#94a3b8', font: { size: 9 }, stepSize: 2 }
                        }
                    }
                }
            });
        </script>
    </body>
    </html>
    """
    return render_template_string(html_template, tasks=todo_list, days_left=days_left, sleep_hours=last_sleep, total_spent=total_spent, sleep_data=sleep_data, sleep_labels=sleep_labels)

# Запуск веб-сервера
threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))).start()

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

# Не забудь добавить 'request' в импорты (from flask import Flask, render_template_string, request)
