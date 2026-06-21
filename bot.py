import telebot
import google.generativeai as genai
import os
from flask import Flask
import threading

# === КЛЮЧИ ТЕПЕРЬ БЕРУТСЯ ИЗ ОБЛАКА (Секретные переменные) ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MY_TELEGRAM_ID = int(os.getenv("MY_TELEGRAM_ID", 0))

# Подключаем бота и ИИ
bot = telebot.TeleBot(TELEGRAM_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)

model = genai.GenerativeModel(
    'gemini-1.5-flash', 
    system_instruction="""Ты личный ассистент и наставник по тайм-менеджменту для студента вуза. 
    Твоя цель: структурировать хаос. Общайся четко, по делу, иногда можешь быть строгим. 
    Используй списки и выделяй главное. Всегда старайся разбивать большие задачи на мелкие шаги."""
)

def is_me(message):
    return message.from_user.id == MY_TELEGRAM_ID

@bot.message_handler(commands=['start'])
def send_welcome(message):
    if not is_me(message):
        bot.reply_to(message, "Доступ запрещен.")
        return
    bot.reply_to(message, "Привет! Я переехал в облако и работаю 24/7 без сбоев. Жду задач!")

@bot.message_handler(func=lambda message: True)
def chat_with_gemini(message):
    if not is_me(message):
        return 
        
    bot.send_chat_action(message.chat.id, 'typing') 
    try:
        response = model.generate_content(message.text)
        bot.reply_to(message, response.text)
    except Exception as e:
        bot.reply_to(message, f"Бро, ошибка: {e}")

# === ХИТРОСТЬ ДЛЯ АВТОПРОБУЖДЕНИЯ (ВЕБ-СЕРВЕР) ===
app = Flask(__name__)

@app.route('/')
def home():
    return "Мозг жив и работает!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# Запускаем веб-сервер в фоновом режиме
threading.Thread(target=run_web).start()

# Запускаем бота
print("Подключение к облаку...")
bot.infinity_polling()
