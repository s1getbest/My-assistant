import hmac
import hashlib
import urllib.parse
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify, render_template, make_response
import config
import vault_files
import university_schedule
import vault_index
import srs
from logging_config import get_logger

from bot_instance import bot
import telebot
from drive_service import (
    append_line_to_drive,
    read_file_from_drive,
    read_json_from_drive,
    update_file_on_drive,
    update_json_file_on_drive,
    get_today_tasks,
    get_sleep_chart_data,
    get_habit_completion_array,
    get_user_profile,
    get_monthly_expenses,
    get_expenses_by_category,
    add_user_xp,
    initialize_folder_mapping,
)

logger = get_logger(__name__)

# Initialize Flask Mini App
app = Flask(__name__)

# Initialize folder mapping on startup
try:
    initialize_folder_mapping()
    logger.info("[Dashboard] Folder mapping initialized.")
except Exception as e:
    logger.error(f"[Dashboard] Warning: Failed to initialize folder mapping: {e}")

DASHBOARD_COOKIE_NAME = "dashboard_key"
DASHBOARD_COOKIE_MAX_AGE = 180 * 24 * 60 * 60  # ~180 days


def validate_telegram_data(init_data):
    if not init_data:
        return False
    if not config.TELEGRAM_TOKEN:
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
        # Constant-time comparison: a plain `==` on hex digests is vulnerable
        # to a (largely theoretical, but free to avoid) timing attack.
        return hmac.compare_digest(h.hexdigest(), vals['hash'])
    except Exception:
        return False


def _dashboard_key_is_valid(candidate):
    if not candidate or not config.DASHBOARD_ACCESS_KEY:
        return False
    return hmac.compare_digest(candidate, config.DASHBOARD_ACCESS_KEY)


def require_dashboard_key(view_func):
    """
    Guards the personal dashboard ('/'), which otherwise renders tasks,
    sleep/health data, XP and a snippet of Memory.md for anyone who knows the
    Render URL, with no authentication at all.

    Access is granted by a `?key=<DASHBOARD_ACCESS_KEY>` query parameter
    (meant to be embedded once in the bot's menu-button / Mini App URL); on
    success a long-lived cookie is set so the page keeps working afterwards
    without the key in the URL (e.g. reopening it from the home screen).
    If DASHBOARD_ACCESS_KEY is not configured on the server, access is
    denied entirely (fail closed) rather than left open.
    """
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not config.DASHBOARD_ACCESS_KEY:
            return jsonify({
                "success": False,
                "error": "Dashboard is not configured. Set DASHBOARD_ACCESS_KEY on the server."
            }), 503

        query_key = request.args.get("key")
        cookie_key = request.cookies.get(DASHBOARD_COOKIE_NAME)

        if _dashboard_key_is_valid(query_key):
            response = make_response(view_func(*args, **kwargs))
            response.set_cookie(
                DASHBOARD_COOKIE_NAME,
                query_key,
                max_age=DASHBOARD_COOKIE_MAX_AGE,
                httponly=True,
                secure=True,
                samesite="Lax",
            )
            return response

        if _dashboard_key_is_valid(cookie_key):
            return view_func(*args, **kwargs)

        return jsonify({"success": False, "error": "Unauthorized"}), 403

    return wrapper


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

        try:
            target_idx = int(task_idx)
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "task_idx must be an integer"}), 400

        def mutate(content):
            lines = content.split("\n")
            unchecked_count = 0
            for i, line in enumerate(lines):
                if "[ ]" not in line:
                    continue
                if unchecked_count == target_idx:
                    lines[i] = line.replace("[ ]", "[x]", 1)
                    return "\n".join(lines)
                unchecked_count += 1
            return None

        # update_file_on_drive serializes this read-modify-write against any
        # other concurrent writer of Tasks.md (e.g. a Telegram message being
        # processed at the same time), preventing a lost update. Its return
        # value is None both when the task wasn't found and when the Drive
        # write itself failed after mutate() ran, so either way we must not
        # award XP for a change that wasn't actually persisted.
        result = update_file_on_drive(vault_files.TASKS, mutate)

        if result is None:
            return jsonify({"success": False, "error": "Task not found"}), 404

        # RPG Gamification: Add +10 XP for task completion
        add_user_xp(10)
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"[Dashboard] mark_task_done error: {e}")
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
        task_recommendation = (response.text or "").strip()
        if not task_recommendation:
            return jsonify({"success": False, "error": "AI service temporarily unavailable"}), 503
        return jsonify({"success": True, "task": task_recommendation})
    except Exception as e:
        logger.error(f"[Dashboard] get_focus_task error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/')
@require_dashboard_key
def home():
    today_label = datetime.now(config.msk_tz).strftime("%d.%m.%Y")

    # Safe Google Drive Reads & Fallbacks
    today_tasks = []
    sleep_data = [0]
    sleep_labels = ["No data"]
    last_sleep = "—"
    habit_data = []
    profile = {"xp": 0, "level": 1}
    welcome_msg = "Привет, Павел! Рад тебя видеть в Time OS 2.0."
    flashcard_stats = {"total": 0, "due": 0}
    today_classes = []
    brain_stats = {"people": 0, "projects": 0, "media": 0, "tags": 0}
    finance_total = 0
    finance_recent = []
    finance_by_category = {}

    # Fetch with individual try-except blocks
    try:
        today_tasks = get_today_tasks()
    except Exception as e:
        logger.error(f"[Dashboard] Error getting today tasks: {e}")
        today_tasks = []

    try:
        sleep_data, sleep_labels, last_sleep = get_sleep_chart_data()
    except Exception as e:
        logger.error(f"[Dashboard] Error getting sleep data: {e}")
        sleep_data, sleep_labels, last_sleep = [0], ["No data"], "—"

    try:
        habit_data = get_habit_completion_array()
        if not habit_data:
            raise ValueError("Empty habit completion array")
    except Exception as e:
        logger.error(f"[Dashboard] Error getting habit completion array: {e}")
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
        logger.error(f"[Dashboard] Error getting user profile: {e}")
        profile = {"xp": 0, "level": 1}

    try:
        flashcards = read_json_from_drive(vault_files.FLASHCARDS)
        if isinstance(flashcards, list):
            flashcard_stats["total"] = len(flashcards)
            now = datetime.now(config.msk_tz)
            for card in flashcards:
                try:
                    review_dt = datetime.strptime(card.get("next_review", ""), "%Y-%m-%d %H:%M:%S")
                    review_dt = config.msk_tz.localize(review_dt)
                    if review_dt <= now:
                        flashcard_stats["due"] += 1
                except Exception:
                    continue
    except Exception as e:
        logger.error(f"[Dashboard] Error getting flashcard stats: {e}")

    try:
        raw_classes = university_schedule.get_classes_for_date(datetime.now(config.msk_tz).date())
        today_classes = [{"time": t, "subject": s} for t, s in raw_classes]
    except Exception as e:
        logger.error(f"[Dashboard] Error getting today's classes: {e}")
        today_classes = []

    try:
        finance_total, finance_recent = get_monthly_expenses()
        finance_by_category = get_expenses_by_category()
    except Exception as e:
        logger.error(f"[Dashboard] Error getting finance data: {e}")
        finance_total, finance_recent, finance_by_category = 0, [], {}

    try:
        # Index.json only stores name/title + file path per entity (not
        # status/rating - those live in the note's own frontmatter), so
        # these are plain counts, not a "currently watching" breakdown -
        # that would need reading every media note's frontmatter on every
        # dashboard load, which doesn't scale with vault size for a stat
        # tile.
        index_data = vault_index.read_index()
        brain_stats = {
            "people": len(index_data.get("people", [])),
            "projects": len(index_data.get("projects", [])),
            "media": len(index_data.get("media", [])),
            "tags": len(index_data.get("tags", [])),
        }
    except Exception as e:
        logger.error(f"[Dashboard] Error getting brain index stats: {e}")
        brain_stats = {"people": 0, "projects": 0, "media": 0, "tags": 0}

    try:
        from key_manager import key_manager
        current_memory = read_file_from_drive(vault_files.MEMORY)
        if current_memory:
            prompt = f"Напиши одно очень короткое (до 15 слов) приветствие для Павел в Time OS 2.0 на русском языке. Можешь упомянуть важный факт из его памяти: {current_memory[:500]}"
            response = key_manager.generate_content(
                model=config.MODEL_LITE,
                contents=prompt
            )
            generated = (response.text or "").strip()
            if generated:
                welcome_msg = generated
    except Exception as e:
        logger.error(f"[Dashboard] Welcome message generation error: {e}")

    return render_template(
        "dashboard.html",
        today_label=today_label,
        today_tasks=today_tasks,
        sleep_data=sleep_data,
        sleep_labels=sleep_labels,
        last_sleep=last_sleep,
        habit_data=habit_data,
        welcome_msg=welcome_msg,
        profile=profile,
        flashcard_stats=flashcard_stats,
        today_classes=today_classes,
        brain_stats=brain_stats,
        finance_total=finance_total,
        finance_recent=finance_recent,
        finance_by_category=finance_by_category,
    )


@app.route('/api/webhook/external', methods=['POST'])
def external_webhook():
    try:
        if not config.EXTERNAL_API_KEY:
            return jsonify({"success": False, "error": "External webhook is not configured"}), 503

        received_key = request.headers.get('X-External-API-Key') or request.args.get('api_key')
        if not received_key or not hmac.compare_digest(received_key, config.EXTERNAL_API_KEY):
            return jsonify({"success": False, "error": "Unauthorized: Invalid API Key"}), 401

        data = request.get_json(silent=True) or {}
        text = (data.get("text") or "").strip()
        if not text:
            return jsonify({"success": False, "error": "Missing 'text' parameter in payload"}), 400

        sender = (data.get("sender") or data.get("source") or "External").strip()
        timestamp = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        inbox_line = f"[{timestamp}] {sender}: {text}"
        if not append_line_to_drive(vault_files.RAW_INBOX, inbox_line):
            return jsonify({"success": False, "error": f"Failed to append to {vault_files.RAW_INBOX}"}), 500

        return jsonify({"success": True, "status": "queued", "stored": inbox_line})
    except Exception as e:
        logger.error(f"[Dashboard] external_webhook error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/flashcards', methods=['GET'])
def get_due_flashcard():
    try:
        init_data = request.headers.get('Authorization')
        if not validate_telegram_data(init_data):
            return jsonify({"success": False, "error": "Unauthorized"}), 403

        flashcards = read_json_from_drive(vault_files.FLASHCARDS)
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
        logger.error(f"[Dashboard] get_due_flashcard error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/flashcards/review', methods=['POST'])
def review_flashcard():
    """
    Rates a flashcard using the SM-2 algorithm (srs.py) - expects
    {"id": ..., "rating": "again"|"hard"|"good"|"easy"}, same 4-button
    scheme as Telegram's /quiz, replacing the old fixed-delay
    {"interval_hours": ...} scheme (which required the client to already
    know what delay to ask for, instead of the server computing a real
    per-card spaced-repetition schedule).
    """
    try:
        init_data = request.headers.get('Authorization')
        if not validate_telegram_data(init_data):
            return jsonify({"success": False, "error": "Unauthorized"}), 403

        data = request.get_json(silent=True) or {}
        card_id = data.get("id")
        rating = data.get("rating")
        if not card_id or rating not in srs.RATINGS:
            return jsonify({"success": False, "error": f"id and rating (one of {srs.RATINGS}) required"}), 400

        updated_card = {"value": None}

        def mutate(flashcards):
            if not isinstance(flashcards, list):
                return None
            for card in flashcards:
                if card.get("id") == card_id:
                    srs.schedule_next_review(card, rating)
                    updated_card["value"] = card
                    return flashcards
            return None

        result = update_json_file_on_drive(vault_files.FLASHCARDS, mutate, default_factory=list)

        if result is None:
            return jsonify({"success": False, "error": "Card not found"}), 404

        return jsonify({"success": True, "card": updated_card["value"]})
    except Exception as e:
        logger.error(f"[Dashboard] review_flashcard error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/webhook/<secret>', methods=['POST'])
def webhook(secret):
    # The path segment is a hash derived from TELEGRAM_TOKEN (see config.py),
    # not the token itself. Telegram also echoes back the secret_token we
    # configured in bot.py's set_webhook() call as this header on every
    # request - checking both means neither value alone (e.g. leaked via a
    # proxy/log line) is enough to inject fake updates.
    if not config.WEBHOOK_PATH_SECRET or not hmac.compare_digest(secret, config.WEBHOOK_PATH_SECRET):
        return '', 404

    header_token = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
    if not config.WEBHOOK_SECRET_TOKEN or not hmac.compare_digest(header_token, config.WEBHOOK_SECRET_TOKEN):
        logger.warning("[Dashboard] Webhook request with missing/invalid secret token header rejected.")
        return '', 403

    try:
        json_str = request.stream.read().decode('utf-8')
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
    except Exception as e:
        logger.error(f"[Dashboard] Error processing webhook update: {e}")
    # Always acknowledge with 200 so Telegram doesn't treat a single bad
    # update as a delivery failure and retry it indefinitely.
    return '!', 200
