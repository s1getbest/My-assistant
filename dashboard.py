import hmac
import hashlib
import urllib.parse
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template
import config

from bot_instance import bot
import telebot
from drive_service import (
    read_file_from_drive,
    write_file_to_drive,
    get_today_tasks,
    get_monthly_expenses,
    get_sleep_chart_data,
    get_expenses_by_category,
    get_habit_completion_array,
    get_user_profile,
    add_user_xp,
)

# Initialize Flask Mini App
app = Flask(__name__)


def validate_telegram_data(init_data):
    if not init_data:
        return False
    try:
        vals = {
            k: urllib.parse.unquote(v)
            for k, v in [s.split('=', 1) for s in init_data.split('&')]
        }
        if 'hash' not in vals:
            return False
        data_check_string = '\n'.join(
            f"{k}={v}" for k, v in sorted(vals.items()) if k != 'hash'
        )
        secret_key = hmac.new(
            "WebAppData".encode(),
            config.TELEGRAM_TOKEN.encode(),
            hashlib.sha256
        ).digest()
        h = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256)
        return h.hexdigest() == vals['hash']
    except Exception:
        return False


@app.route('/api/done', methods=['POST'])
def mark_task_done():
    try:
        data = request.get_json(silent=True) or {}
        init_data = request.headers.get('Authorization')
        if not validate_telegram_data(init_data):
            return jsonify({"success": False, "error": "Unauthorized"}), 403

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
                # RPG Gamification: Add +10 XP for task completion
                add_user_xp(10)
                return jsonify({"success": True})
            unchecked_count += 1
        return jsonify({"success": False, "error": "Task not found"}), 404
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/manifest.json')
def get_pwa_manifest():
    manifest = {
        "short_name": "Time OS",
        "name": "Time OS 2.0 Dashboard",
        "icons": [
            {
                "src": "https://cdn-icons-png.flaticon.com/512/1162/1162456.png",
                "type": "image/png",
                "sizes": "512x512"
            }
        ],
        "start_url": "/?pwa=true",
        "background_color": "#020617",
        "theme_color": "#6366f1",
        "display": "standalone",
        "orientation": "portrait"
    }
    return jsonify(manifest)


@app.route('/api/now', methods=['GET'])
def get_focus_task():
    try:
        init_data = request.headers.get('Authorization')
        if not validate_telegram_data(init_data):
            return jsonify({"success": False, "error": "Unauthorized"}), 403

        from drive_service import get_today_tasks
        today_tasks = get_today_tasks()
        open_tasks = [t for t in today_tasks if not t.get("done")]
        
        if not open_tasks:
            return jsonify({"success": True, "task": "Нет открытых задач на сегодня! Отдыхайте 🎉"})
            
        tasks_text = "\n".join([f"- {t.get('time', '—')} | {t.get('text')}" for t in open_tasks])
        
        prompt = f"""The user has 30 minutes of free time right now. Pick exactly ONE task from this list that they should do immediately. Return ONLY the task text (do not include time, bullet points, intro, or any conversational text).

Tasks list:
{tasks_text}
"""
        from key_manager import key_manager
        response = key_manager.generate_content(
            model=config.MODEL_LITE,
            contents=prompt
        )
        task_recommendation = response.text.strip()
        return jsonify({"success": True, "task": task_recommendation})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/')
def home():
    today_label = datetime.now(config.msk_tz).strftime("%d.%m.%Y")

    # Safe Google Drive Reads & Fallbacks
    today_tasks = []
    total_spent = 0
    recent_expenses = []
    sleep_data = [0]
    sleep_labels = ["No data"]
    last_sleep = "—"
    expense_categories = {}
    habit_data = []
    profile = {"xp": 0, "level": 1}
    insight_msg = "Have a great day, Pavel! ✨"
    welcome_msg = "Привет, Павел! Рад тебя видеть в Time OS 2.0."

    # Fetch with individual try-except blocks
    try:
        today_tasks = get_today_tasks()
    except Exception as e:
        print(f"[Dashboard] Error getting today tasks: {e}")
        today_tasks = []

    try:
        total_spent, recent_expenses = get_monthly_expenses()
    except Exception as e:
        print(f"[Dashboard] Error getting monthly expenses: {e}")
        total_spent, recent_expenses = 0, []

    try:
        sleep_data, sleep_labels, last_sleep = get_sleep_chart_data()
    except Exception as e:
        print(f"[Dashboard] Error getting sleep data: {e}")
        sleep_data, sleep_labels, last_sleep = [0], ["No data"], "—"

    try:
        expense_categories = get_expenses_by_category()
    except Exception as e:
        print(f"[Dashboard] Error getting expense categories: {e}")
        expense_categories = {}

    try:
        habit_data = get_habit_completion_array()
        if not habit_data:
            raise ValueError("Empty habit completion array")
    except Exception as e:
        print(f"[Dashboard] Error getting habit completion array: {e}")
        habit_data = []
        today = datetime.now(config.msk_tz)
        for i in range(13, -1, -1):
            day = today - timedelta(days=i)
            habit_data.append({
                "date": day.strftime("%Y-%m-%d"),
                "label": day.strftime("%d.%m"),
                "total": 0,
                "done": 0,
                "completed": False
            })

    try:
        profile = get_user_profile()
        if not profile or not isinstance(profile, dict):
            profile = {"xp": 0, "level": 1}
    except Exception as e:
        print(f"[Dashboard] Error getting user profile: {e}")
        profile = {"xp": 0, "level": 1}

    try:
        content = read_file_from_drive("Insight.md")
        if content and content.strip():
            insight_msg = content.strip()
    except Exception as e:
        print(f"[Dashboard] Error reading insight: {e}")

    try:
        from key_manager import key_manager
        current_memory = read_file_from_drive("Memory.md")
        if current_memory:
            prompt = f"Напиши одно очень короткое (до 15 слов) приветствие для Павел в Time OS 2.0 на русском языке. Можешь упомянуть важный факт из его памяти: {current_memory[:500]}"
            response = key_manager.generate_content(
                model=config.MODEL_LITE,
                contents=prompt
            )
            welcome_msg = response.text.strip()
    except Exception as e:
        print(f"[Dashboard] Welcome message generation error: {e}")

    return render_template(
        "dashboard.html",
        today_label=today_label,
        today_tasks=today_tasks,
        total_spent=total_spent,
        recent_expenses=recent_expenses,
        sleep_data=sleep_data,
        sleep_labels=sleep_labels,
        last_sleep=last_sleep,
        expense_categories=expense_categories,
        habit_data=habit_data,
        welcome_msg=welcome_msg,
        profile=profile,
        insight_msg=insight_msg,
    )


@app.route('/api/webhook/external', methods=['POST'])
def external_webhook():
    import os
    expected_key = os.getenv("EXTERNAL_API_KEY", "default_secret_key_123")
    received_key = request.headers.get('X-External-API-Key')
    if not received_key:
        received_key = request.args.get('api_key')
        
    if not received_key or received_key != expected_key:
        return jsonify({"success": False, "error": "Unauthorized: Invalid API Key"}), 401
        
    data = request.get_json(silent=True) or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"success": False, "error": "Missing 'text' parameter in payload"}), 400
        
    # Import process_external_text dynamically to avoid circular dependencies
    from bot_handlers import process_external_text
    res = process_external_text(text)
    if res.get("success"):
        return jsonify({"status": "success", "reply": res.get("reply"), "tags": res.get("tags_found")})
    else:
        return jsonify({"success": False, "error": res.get("error")}), 500


@app.route(f'/webhook/{config.TELEGRAM_TOKEN}', methods=['POST'])
def webhook():
    json_str = request.stream.read().decode('utf-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return '!', 200
