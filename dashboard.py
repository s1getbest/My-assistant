import hmac
import hashlib
import urllib.parse
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template
import config

from bot_instance import bot
import telebot
from drive_service import (
    append_line_to_drive,
    read_file_from_drive,
    read_json_from_drive,
    write_file_to_drive,
    write_json_to_drive,
    get_today_tasks,
    get_monthly_expenses,
    get_sleep_chart_data,
    get_expenses_by_category,
    get_habit_completion_array,
    get_user_profile,
    add_user_xp,
    initialize_folder_mapping,
)

# Initialize Flask Mini App
app = Flask(__name__)

# Initialize folder mapping on startup
try:
    initialize_folder_mapping()
    print("[Dashboard] Folder mapping initialized.")
except Exception as e:
    print(f"[Dashboard] Warning: Failed to initialize folder mapping: {e}")


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
        "name": "Time OS",
        "short_name": "TimeOS",
        "display": "standalone",
        "background_color": "#020617",
        "theme_color": "#4f46e5",
        "start_url": "/",
        "icons": [{
            "src": "https://cdn-icons-png.flaticon.com/512/8342/8342207.png",
            "sizes": "512x512",
            "type": "image/png"
        }]
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
    )


@app.route('/api/webhook/external', methods=['POST'])
def external_webhook():
    import os
    try:
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

        sender = (data.get("sender") or data.get("source") or "External").strip()
        timestamp = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        inbox_line = f"[{timestamp}] {sender}: {text}"
        if not append_line_to_drive("Raw_Inbox.md", inbox_line):
            return jsonify({"success": False, "error": "Failed to append to Raw_Inbox.md"}), 500

        return jsonify({"success": True, "status": "queued", "stored": inbox_line})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/flashcards', methods=['GET'])
def get_due_flashcard():
    try:
        init_data = request.headers.get('Authorization')
        if not validate_telegram_data(init_data):
            return jsonify({"success": False, "error": "Unauthorized"}), 403

        flashcards = read_json_from_drive("Flashcards.json")
        if not isinstance(flashcards, list):
            flashcards = []

        now = datetime.now(config.msk_tz)
        due_cards = []
        for card in flashcards:
            try:
                review_dt = datetime.strptime(card.get("next_review", ""), "%Y-%m-%d %H:%M:%S")
                review_dt = config.msk_tz.localize(review_dt)
                if review_dt <= now:
                    due_cards.append((review_dt, card))
            except Exception:
                continue

        due_cards.sort(key=lambda item: item[0])
        if not due_cards:
            return jsonify({"success": True, "card": None})
        return jsonify({"success": True, "card": due_cards[0][1]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/flashcards/review', methods=['POST'])
def review_flashcard():
    try:
        init_data = request.headers.get('Authorization')
        if not validate_telegram_data(init_data):
            return jsonify({"success": False, "error": "Unauthorized"}), 403

        data = request.get_json(silent=True) or {}
        card_id = data.get("id")
        interval_hours = data.get("interval_hours")
        if not card_id or interval_hours is None:
            return jsonify({"success": False, "error": "id and interval_hours required"}), 400

        interval_hours = float(interval_hours)
        flashcards = read_json_from_drive("Flashcards.json")
        if not isinstance(flashcards, list):
            flashcards = []

        updated = False
        next_review = datetime.now(config.msk_tz) + timedelta(hours=interval_hours)
        for card in flashcards:
            if card.get("id") == card_id:
                card["next_review"] = next_review.strftime("%Y-%m-%d %H:%M:%S")
                updated = True
                break

        if not updated:
            return jsonify({"success": False, "error": "Card not found"}), 404

        if not write_json_to_drive("Flashcards.json", flashcards):
            return jsonify({"success": False, "error": "Failed to save flashcards"}), 500

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route(f'/webhook/{config.TELEGRAM_TOKEN}', methods=['POST'])
def webhook():
    json_str = request.stream.read().decode('utf-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return '!', 200

