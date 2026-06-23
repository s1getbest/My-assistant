import hmac
import hashlib
import json
import urllib.parse
from datetime import datetime
from flask import Flask, render_template, request, jsonify
import config
from bot_instance import bot
import telebot
from drive_service import (
    read_file_from_drive,
    write_file_to_drive,
    get_today_tasks,
    get_monthly_expenses,
    get_sleep_chart_data,
)

# Initialize Flask Mini App
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
        secret_key = hmac.new(
            b"WebAppData", 
            config.TELEGRAM_TOKEN.encode('utf-8'), 
            hashlib.sha256
        ).digest()
        
        # Calculate calculated_hash: HMAC_SHA256(secret_key, data_check_string)
        calculated_hash = hmac.new(
            secret_key, 
            data_check_string.encode('utf-8'), 
            hashlib.sha256
        ).hexdigest()
        
        if calculated_hash == received_hash:
            user_json = params.get('user')
            if user_json:
                return json.loads(user_json)
        return None
    except Exception as e:
        print(f"[Security] Error validating Telegram initData: {e}")
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
        if not user_data or user_data.get('id') != config.MY_TELEGRAM_ID:
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
    if not user_data or user_data.get('id') != config.MY_TELEGRAM_ID:
        return "403 Forbidden: Unauthorized", 403

    today_label = datetime.now(config.msk_tz).strftime("%d.%m.%Y")
    today_tasks = get_today_tasks()
    total_spent, recent_expenses = get_monthly_expenses()
    sleep_data, sleep_labels, last_sleep = get_sleep_chart_data()

    return render_template(
        "dashboard.html",
        today_label=today_label,
        today_tasks=today_tasks,
        total_spent=total_spent,
        recent_expenses=recent_expenses,
        sleep_data=sleep_data,
        sleep_labels=sleep_labels,
        last_sleep=last_sleep,
    )


@app.route(f'/webhook/{config.TELEGRAM_TOKEN}', methods=['POST'])
def webhook():
    json_str = request.stream.read().decode('utf-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return '!', 200
