import re
import json
from datetime import datetime, timedelta
import telebot
from google.genai import types
import config
import vault_files
import vault_index
import srs
import note_templates
from bot_instance import bot
from key_manager import key_manager
from logging_config import get_logger
from drive_service import (
    delete_line_from_task_file,
    get_task_line_by_token,
    get_folder_id,
    list_markdown_files,
    mark_task_done_by_token,
    read_json_from_drive,
    read_file_from_drive,
    update_file_on_drive,
    update_json_file_on_drive,
    append_line_to_drive,
)
from ai_pipeline import (
    apply_format_rule,
    sanitize_telegram_text,
    is_ai_response_empty,
    parse_gemini_tags,
    apply_gemini_tags,
    extract_reply,
    get_extraction_rules,
    extract_task_text_from_line,
    append_journal_entry,
    append_health_metric,
)

logger = get_logger(__name__)

AI_UNAVAILABLE_MESSAGE = "Sorry, the AI service is temporarily unavailable or overloaded right now. Please try again in a minute."


class StatusMessage:
    """
    A single Telegram message, edited in place to show pipeline progress
    (ARCHITECTURE.md step 5) instead of leaving the user watching a bare
    "typing..." indicator for however long the AI call takes. Threaded as
    a reply to the original message, same as a normal bot.reply_to would be.

    Any failure here (network hiccup editing/sending) is logged and
    swallowed - status UX is a nice-to-have and must never be what breaks
    a reply the user is actually waiting for.
    """

    def __init__(self, message, initial_text="🧠 Thinking..."):
        self._chat_id = message.chat.id
        self._message_id = None
        self._last_text = None
        try:
            sent = bot.reply_to(message, initial_text)
            self._message_id = sent.message_id
            self._last_text = initial_text
        except Exception as e:
            logger.warning(f"[StatusMessage] Failed to send initial status message: {e}")

    def update(self, text):
        if not self._message_id or text == self._last_text:
            return
        try:
            bot.edit_message_text(chat_id=self._chat_id, message_id=self._message_id, text=text)
            self._last_text = text
        except Exception as e:
            logger.warning(f"[StatusMessage] Failed to update status message: {e}")

    def finish(self, final_text):
        final_text = final_text or "Done."
        if not self._message_id:
            try:
                bot.send_message(self._chat_id, final_text)
            except Exception as e:
                logger.warning(f"[StatusMessage] Failed to send final message: {e}")
            return
        try:
            bot.edit_message_text(chat_id=self._chat_id, message_id=self._message_id, text=final_text)
        except Exception as e:
            logger.warning(f"[StatusMessage] Failed to finalize status message, sending a new one: {e}")
            try:
                bot.send_message(self._chat_id, final_text)
            except Exception:
                pass


def _fallback_note(response):
    """
    Small suffix appended to a reply when key_manager had to rotate to a
    different API key or fall back to MODEL_LITE to get this response
    (ARCHITECTURE.md step 5's "process transparency" - the user asked to
    see when the bot switched to a backup instead of it happening silently).
    """
    if getattr(response, "used_fallback", False):
        return "\n\n⚡ (this reply was generated via a backup key/model — the primary service was temporarily unavailable)"
    return ""


def is_me(message):
    return message.from_user.id == config.MY_TELEGRAM_ID


def get_forward_sender_name(message):
    """
    Safely extracts the original sender's name from a forwarded message.
    """
    if not message.forward_origin:
        return None
    try:
        origin = message.forward_origin
        o_type = getattr(origin, 'type', None)
        if o_type == 'user':
            u = getattr(origin, 'sender_user', None)
            if u:
                parts = []
                if getattr(u, 'first_name', None):
                    parts.append(u.first_name)
                if getattr(u, 'last_name', None):
                    parts.append(u.last_name)
                name = " ".join(parts).strip()
                if not name and getattr(u, 'username', None):
                    name = u.username
                return name or "User"
        elif o_type == 'hidden_user':
            return getattr(origin, 'sender_user_name', "Hidden User")
        elif o_type == 'chat':
            c = getattr(origin, 'sender_chat', None)
            if c:
                return getattr(c, 'title', "Chat")
        elif o_type == 'channel':
            c = getattr(origin, 'chat', None)
            if c:
                return getattr(c, 'title', "Channel")

        # Fallback to older telegram message fields
        if getattr(message, 'forward_from', None):
            u = message.forward_from
            parts = [getattr(u, 'first_name', ""), getattr(u, 'last_name', "")]
            name = " ".join([p for p in parts if p]).strip()
            return name or getattr(u, 'username', None) or "User"
        elif getattr(message, 'forward_from_chat', None):
            return getattr(message.forward_from_chat, 'title', "Chat")
        elif getattr(message, 'forward_sender_name', None):
            return message.forward_sender_name
    except Exception as e:
        logger.error(f"[Forwards] Error getting forward sender name: {e}")
    return "Unknown Sender"


# === MULTI-AGENT PIPELINE ===

def agent_router(user_message):
    """
    Agent Router: Uses MODEL_LITE to classify user intent.
    Returns EXACTLY ONE word: TASK, FINANCE, HEALTH, QUESTION, or NOTE.
    If the message contains a URL (especially YouTube, educational articles, PDFs), classify as NOTE.
    """
    try:
        # Check for URLs first
        url_pattern = re.compile(r'https?://\S+|www\.\S+')
        has_url = bool(url_pattern.search(user_message))

        if has_url:
            logger.info("[Agent Router] URL detected, classifying as NOTE")
            return "NOTE"

        prompt = apply_format_rule(f"""Analyze the user's message. Output EXACTLY ONE word: TASK, FINANCE, HEALTH, QUESTION, or NOTE.

User message: "{user_message}"
""")
        response = key_manager.generate_content(
            model=config.MODEL_LITE,
            contents=prompt
        )
        classification = (response.text or "").strip().upper()
        valid_classes = ["TASK", "FINANCE", "HEALTH", "QUESTION", "NOTE"]
        if classification not in valid_classes:
            classification = "QUESTION"
        logger.info(f"[Agent Router] Classified as: {classification}")
        return classification
    except Exception as e:
        logger.error(f"[Agent Router] Error: {e}")
        return "QUESTION"


def agent_archivist(user_message):
    """
    Agent Archivist: Uses MODEL_COMPLEX to format user's thought into Zettelkasten note.
    For URLs/educational content, performs deep analysis with wikilinks and tags.
    Output: [NOTE] Category | # Title\n\n**Summary:** ...\n\n**Key Concepts:** ...
    """
    try:
        prompt = apply_format_rule(f"""You are an expert academic researcher and Zettelkasten archivist. Analyze the provided link/text (e.g., YouTube lecture or article). Create a comprehensive summary. You MUST format key entities, theories, and concepts using Obsidian wikilinks [[Concept]] and add #tags. Output strictly: [NOTE] Category | # Title

**Summary:** ...

**Key Concepts:** ...

User thought: "{user_message}"
""")
        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=prompt
        )
        return (response.text or "").strip()
    except Exception as e:
        logger.error(f"[Agent Archivist] Error: {e}")
        return None


# Agent Tutor (background flashcard generation) moved to ai_pipeline.py and
# is now triggered automatically from apply_gemini_tags() for NOTE/MEDIA/
# PROJECT tags, so it fires consistently from every handler that applies
# tags (chat, voice, photo, /journal, /digest, /process) instead of only
# from chat_with_gemini as before.


# === BOT HANDLERS ===

HELP_TEXT = """🧠 Your personal second brain. Just write plain text, voice, or a photo — I'll figure out what to do with it (task, expense, note, movie, person, project...).

Commands:
/sleep <hours> — log sleep, e.g. /sleep 7.5
/steps <number> — log today's steps, e.g. /steps 9500
/pulse <bpm> — log heart rate, e.g. /pulse 62
/stress <1-10> — log stress level, e.g. /stress 4
/distance <km> — log distance, e.g. /distance 5.4
/calories <number> — log calories, e.g. /calories 2150
/health_report — AI analysis of your whole health history: trends, deviations, recommendations
/journal <text> — personal diary/reflection (can also reply to a message)
/quiz — review flashcards (Anki-style, also available in the mini-app)
/who <name> — pull up a person/media/project card right in the chat
/brain <question> — ask your Second Brain (Tasks/Finance/Health/Memory/Goals + people/media/projects)
/search <query> — full-text search across all your Obsidian notes
/digest — parse Raw_Inbox.md (external notes) into tasks/questions
/process — parse Inbox.md into notes/flashcards

📍 Share your location (paperclip → Location) — I'll log it to Location.md.

Open the menu button next to the input field — that's the dashboard with stats, tasks, finances, and flashcard review."""


@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if not is_me(message):
        return
    bot.reply_to(message, HELP_TEXT)


@bot.message_handler(commands=['sleep'])
def track_sleep(message):
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        args = message.text.split()
        if len(args) < 2:
            bot.reply_to(message, "Tell me the hours slept. Example: `/sleep 7.5`", parse_mode="Markdown")
            return
        hours = args[1]
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")
        append_line_to_drive(vault_files.HEALTH, f"* {today_str}: {hours}")
        bot.reply_to(message, f"🛌 **Sleep logged!**\n\n> {today_str} · {hours} h")
    except Exception as e:
        bot.reply_to(message, f"Error logging sleep: {e}")


@bot.message_handler(commands=['steps'])
def track_steps(message):
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        args = message.text.split()
        if len(args) < 2:
            bot.reply_to(message, "Tell me the step count. Example: `/steps 9500`", parse_mode="Markdown")
            return
        steps = args[1]
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")
        append_health_metric("Steps", f"{today_str}: {steps}")
        bot.reply_to(message, f"🚶 **Steps logged!**\n\n> {today_str} · {steps} steps")
    except Exception as e:
        bot.reply_to(message, f"Error logging steps: {e}")


@bot.message_handler(commands=['pulse'])
def track_pulse(message):
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        args = message.text.split()
        if len(args) < 2:
            bot.reply_to(message, "Tell me your heart rate (bpm). Example: `/pulse 62`", parse_mode="Markdown")
            return
        bpm = args[1]
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")
        append_health_metric("HR", f"{today_str}: {bpm}")
        bot.reply_to(message, f"❤️ **Heart rate logged!**\n\n> {today_str} · {bpm} bpm")
    except Exception as e:
        bot.reply_to(message, f"Error logging heart rate: {e}")


@bot.message_handler(commands=['stress'])
def track_stress(message):
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        args = message.text.split()
        if len(args) < 2:
            bot.reply_to(message, "Tell me the stress level 1-10. Example: `/stress 4`", parse_mode="Markdown")
            return
        level = args[1]
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")
        append_health_metric("Stress", f"{today_str}: {level}")
        bot.reply_to(message, f"🧘 **Stress logged!**\n\n> {today_str} · {level}/10")
    except Exception as e:
        bot.reply_to(message, f"Error logging stress: {e}")


@bot.message_handler(commands=['distance'])
def track_distance(message):
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        args = message.text.split()
        if len(args) < 2:
            bot.reply_to(message, "Tell me the distance in km. Example: `/distance 5.4`", parse_mode="Markdown")
            return
        km = args[1]
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")
        append_health_metric("Distance", f"{today_str}: {km}")
        bot.reply_to(message, f"🏃 **Distance logged!**\n\n> {today_str} · {km} km")
    except Exception as e:
        bot.reply_to(message, f"Error logging distance: {e}")


@bot.message_handler(commands=['calories'])
def track_calories(message):
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        args = message.text.split()
        if len(args) < 2:
            bot.reply_to(message, "Tell me the calories. Example: `/calories 2150`", parse_mode="Markdown")
            return
        kcal = args[1]
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")
        append_health_metric("Calories", f"{today_str}: {kcal}")
        bot.reply_to(message, f"🔥 **Calories logged!**\n\n> {today_str} · {kcal} kcal")
    except Exception as e:
        bot.reply_to(message, f"Error logging calories: {e}")


@bot.message_handler(commands=['health_report'])
def handle_health_report(message):
    """
    AI-driven longitudinal analysis of accumulated health data - not
    today's numbers, but trends and deviations across everything logged
    so far: Health.md's simple daily metrics (via /sleep, /steps, /pulse,
    /stress, /distance, /calories, or a plain message) plus
    HealthDetailed.json's richer per-day records (sleep phases, HR
    range/HRV, stress distribution, ...), if the user has populated that
    file directly (no bulk-import command for it - a single-user vault,
    edited by hand when needed, is simpler than maintaining an import
    pipeline for occasional bulk backfills). Uses MODEL_COMPLEX since this
    needs actual reasoning over a real dataset, not a one-line lookup.

    No truncation of the input yet - fine while HealthDetailed.json is
    small (weeks/months of history), but if it grows into years of daily
    records this will eventually need summarizing before it blows past
    the model's context, same concern already noted for /brain and
    /search over the vault.
    """
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        health_content = read_file_from_drive(vault_files.HEALTH)
        detailed_data = read_json_from_drive(vault_files.HEALTH_DETAILED)
        if not health_content.strip() and not detailed_data:
            bot.reply_to(
                message,
                "No data to analyze yet. Start logging with `/sleep`, `/steps`, "
                "`/pulse`, `/stress`, etc. (or fill in `HealthDetailed.json` by hand in Drive).",
                parse_mode="Markdown",
            )
            return

        status = StatusMessage(message, "📊 Analyzing health history...")
        detailed_json_text = (
            json.dumps(detailed_data, ensure_ascii=False, indent=2)
            if isinstance(detailed_data, dict) and detailed_data
            else "No detailed data."
        )

        prompt = apply_format_rule(f"""You are a health data analyst. Analyze the entire history below and give a substantive report in English.

Look for:
- Trends over time (sleep, heart rate, stress, steps, calories) — improving/worsening/stable.
- Deviations and anomalies — days or periods that stand out sharply from the overall pattern.
- Possible correlations between metrics (e.g. between sleep and stress, activity and mood).
- Concrete, actionable recommendations, not generic phrases.

Simple daily log (Health.md):
---
{health_content or "Empty."}
---

Detailed per-day data (sleep phases, heart rate range, stress distribution, etc.):
---
{detailed_json_text}
---

Structure your answer into sections: Sleep, Heart Rate, Stress, Activity, Overall Conclusions. Cite specific dates where relevant, not just generalities.
""")
        response = key_manager.generate_content(model=config.MODEL_COMPLEX, contents=prompt)
        raw_text = response.text or ""
        if is_ai_response_empty(raw_text):
            status.finish(AI_UNAVAILABLE_MESSAGE)
            return
        status.finish(sanitize_telegram_text(raw_text) + _fallback_note(response))
    except Exception as e:
        bot.reply_to(message, f"Error analyzing: {e}")


def _send_next_due_flashcard(chat_id):
    """
    Core of /quiz, factored out so it can be called with a plain chat_id -
    needed because handle_srs_review used to call quiz_flashcards(call.message)
    to auto-advance to the next card, but call.message is the *bot's own*
    message (the one with the inline keyboard), whose from_user is the bot
    itself - so is_me(call.message) was always False and the "send next
    card" step silently did nothing. The caller here is already
    responsible for having verified the request came from MY_TELEGRAM_ID.
    """
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
        bot.send_message(chat_id, "🎉 No flashcards due for review!")
        return

    card = due_cards[0][1]
    keyboard = telebot.types.InlineKeyboardMarkup()
    keyboard.add(telebot.types.InlineKeyboardButton("Show answer", callback_data=f"show_answer:{card['id']}"))
    bot.send_message(chat_id, f"🎓 **Question:**\n\n{card['q']}", reply_markup=keyboard, parse_mode="Markdown")


@bot.message_handler(commands=['quiz'])
def quiz_flashcards(message):
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        _send_next_due_flashcard(message.chat.id)
    except Exception as e:
        bot.reply_to(message, f"Error loading flashcard: {e}")


@bot.message_handler(content_types=['voice'])
def handle_voice(message):
    """
    Handles voice messages using MODEL_COMPLEX (native audio support).
    """
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    status = StatusMessage(message, "🎙️ Listening to voice message...")
    try:
        voice_info = bot.get_file(message.voice.file_id)
        downloaded_file = bot.download_file(voice_info.file_path)

        current_memory = read_file_from_drive(vault_files.MEMORY)
        if not current_memory.strip():
            current_memory = "Long-term memory is still empty."

        now_msk = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")

        # Check if it's a journal entry
        is_journal = False
        if message.reply_to_message and message.reply_to_message.text and '/journal' in message.reply_to_message.text:
            is_journal = True
        elif message.caption and message.caption.startswith('/journal'):
            is_journal = True

        if is_journal:
            prompt = apply_format_rule(f"""Current time in Moscow: {now_msk}
Today's date: {today_str}

The user sent a voice recording for their personal journal.
Listen carefully to the audio and recognize S1get's deep reflections.

Act as an empathetic listener and coach. Respond with a short, supportive reply. At the very end of your response, add a tag: `[JOURNAL] transcript_or_faithful_summary`, containing a faithful transcript (or, if speech was unclear in places, a close paraphrase) of what the user actually said - this is the diary entry itself and gets saved verbatim, so do not shorten it into a generic summary.

Besides the tag, start your warm, supportive reply with [REPLY] to separate the live reply from the tag.

Reply format:
[REPLY]
Your reply to the user in English
[JOURNAL] Transcript or faithful paraphrase of what the user said
""")
        else:
            extraction_rules = get_extraction_rules(today_str)
            prompt = apply_format_rule(f"""Current time in Moscow: {now_msk}
Today's date: {today_str}

Long-term memory (Memory.md):
---
{current_memory}
---

The user sent a voice message. The voice message's content is in the attached audio file.
Listen carefully to the audio and recognize what S1get is saying.

Reply clearly and to the point. In [REPLY] — only the live reply to the user, without repeating memory content.

{extraction_rules}

Reply format:
[REPLY]
Your reply to the user
(then tags, if needed — each on a new line)
""")
        # Voice messages always use MODEL_COMPLEX
        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=[
                types.Part.from_bytes(
                    data=downloaded_file,
                    mime_type="audio/ogg"
                ),
                prompt
            ]
        )
        raw_text = response.text or ""
        if is_ai_response_empty(raw_text):
            status.finish(AI_UNAVAILABLE_MESSAGE)
            return

        tags = parse_gemini_tags(raw_text)
        reply_part = extract_reply(raw_text)
        apply_gemini_tags(tags)

        status.finish(reply_part + _fallback_note(response))
    except Exception as e:
        status.finish(f"Error processing voice message: {e}")


@bot.message_handler(content_types=['location'])
def handle_location(message):
    """
    Logs a location shared via Telegram's own "share location" feature
    (paperclip -> Location) to Location.md - the lowest-friction possible
    starting point on geolocation (ARCHITECTURE.md 9 "Отложено": method
    was undecided). No extra app, no Google OAuth scope, no phone
    automation to set up - works identically on Android and iOS since it's
    just a native Telegram message type.

    Only the initial share is logged, not every live-location update.
    Telegram resends a "live" share (message.location.live_period set) as
    edited_message events every ~15-60s while it's active - logging each
    one would flood Location.md for little value. A future iteration could
    throttle those into periodic checkpoints (e.g. one entry per 30 min)
    via @bot.edited_message_handler(content_types=['location']) if
    continuous tracking (not just point-in-time check-ins) turns out to be
    what's actually wanted.
    """
    if not is_me(message):
        return
    try:
        loc = message.location
        now_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        maps_link = f"https://maps.google.com/?q={loc.latitude},{loc.longitude}"
        is_live = bool(getattr(loc, "live_period", None))
        label = "Live location (start)" if is_live else "Location"
        append_line_to_drive(vault_files.LOCATION, f"* {now_str}: {label} — {maps_link}")
        bot.reply_to(message, f"📍 {label} logged.")
    except Exception as e:
        bot.reply_to(message, f"Error logging location: {e}")


@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    """
    Handles photo messages using MODEL_COMPLEX (vision support).
    """
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    status = StatusMessage(message, "🖼️ Looking at the image...")
    try:
        # Get highest resolution photo
        photo = message.photo[-1]
        file_info = bot.get_file(photo.file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        now_msk = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")

        caption = message.caption or ""
        extraction_rules = get_extraction_rules(today_str)

        prompt = apply_format_rule(f"""Current time in Moscow: {now_msk}
Today's date: {today_str}

The user sent an image. Here is its caption (if any): "{caption}"

Analyze this image. If it's a receipt, calculate the total and output `[FINANCE] YYYY-MM-DD: amount | category | description`. If it's handwritten notes or a whiteboard, extract actionable items as `[TASK_ADD] YYYY-MM-DD HH:MM | Task`. If it's an article/screenshot, summarize it as `[MEMORY] summary`.
{extraction_rules}

Besides the tags, write the user a brief, substantive reply/comment. Start your reply with [REPLY] to separate the live reply from the tags.

Reply format:
[REPLY]
Your reply to the user
(then tags, if needed — each on a new line)
""")

        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=[
                types.Part.from_bytes(
                    data=downloaded_file,
                    mime_type="image/jpeg"
                ),
                prompt
            ]
        )
        raw_text = response.text or ""
        if is_ai_response_empty(raw_text):
            status.finish(AI_UNAVAILABLE_MESSAGE)
            return

        tags = parse_gemini_tags(raw_text)
        reply_part = extract_reply(raw_text)
        apply_gemini_tags(tags)

        status.finish(reply_part + _fallback_note(response))
    except Exception as e:
        status.finish(f"Error processing image: {e}")


@bot.inline_handler(func=lambda query: len(query.query) > 0)
def handle_inline_query(inline_query):
    """
    Inline mode handler for quick capture. Uses MODEL_LITE.
    """
    if inline_query.from_user.id != config.MY_TELEGRAM_ID:
        return
    try:
        text = inline_query.query.strip()
        now_msk = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")
        extraction_rules = get_extraction_rules(today_str)

        prompt = apply_format_rule(f"""Current time in Moscow: {now_msk}
Today's date: {today_str}

The user sent a quick note via inline mode: "{text}"

{extraction_rules}
Please be precise in recognition. Do NOT write any other text, only tags on new lines (if applicable).
""")
        response = key_manager.generate_content(
            model=config.MODEL_LITE,
            contents=prompt
        )
        raw_text = response.text or ""

        if is_ai_response_empty(raw_text):
            r = telebot.types.InlineQueryResultArticle(
                id='1',
                title='⚠️ AI service unavailable',
                input_message_content=telebot.types.InputTextMessageContent(
                    message_text=f"⚠️ Couldn't process (AI service unavailable): {text}"
                ),
                description="Nothing was saved — please try again later."
            )
            bot.answer_inline_query(inline_query.id, [r], cache_time=1)
            return

        tags = parse_gemini_tags(raw_text)
        apply_gemini_tags(tags)

        # Build inline result
        r = telebot.types.InlineQueryResultArticle(
            id='1',
            title='✅ Task/Data captured!',
            input_message_content=telebot.types.InputTextMessageContent(
                message_text=f"✅ Successfully saved to Time OS: {text}"
            ),
            description=f"Recognize and save: {text}"
        )
        bot.answer_inline_query(inline_query.id, [r], cache_time=1)
    except Exception as e:
        logger.error(f"[Inline Query] Error handling query: {e}")


@bot.callback_query_handler(func=lambda call: call.data.startswith('task_done:') or call.data.startswith('task_snooze_1h:') or call.data.startswith('task_snooze_24h:'))
def handle_task_callback(call):
    """
    Callback query handler for interactive notifications.
    """
    if call.from_user.id != config.MY_TELEGRAM_ID:
        bot.answer_callback_query(call.id, "Error: Access denied.", show_alert=True)
        return
    try:
        action, task_token = call.data.split(':', 1)

        # Remove reply markup (the inline buttons) to prevent double clicks
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass

        if action == "task_done":
            updated_line = mark_task_done_by_token(task_token)
            task_text = extract_task_text_from_line(updated_line)
            if updated_line:
                bot.answer_callback_query(call.id, "Marked as done!")
                bot.send_message(call.message.chat.id, f"✅ Done: **{task_text}**", parse_mode="Markdown")
            else:
                bot.answer_callback_query(call.id, "Task already done or not found.")
                bot.send_message(call.message.chat.id, "✅ Task already handled or not found.", parse_mode="Markdown")

        elif action in ["task_snooze_1h", "task_snooze_24h"]:
            delay_hours = 1 if "1h" in action else 24
            old_task_line = get_task_line_by_token(task_token)
            if not old_task_line:
                bot.answer_callback_query(call.id, "Original task not found.", show_alert=True)
                return

            task_text = extract_task_text_from_line(old_task_line)
            run_date = datetime.now(config.msk_tz) + timedelta(hours=delay_hours)
            new_task_line = f"* [ ] {run_date.strftime('%Y-%m-%d %H:%M')} | ⏰ REMINDER: {task_text}"

            from scheduler_jobs import schedule_reminder_job
            delete_line_from_task_file(old_task_line)
            append_line_to_drive(vault_files.TASKS, new_task_line)
            schedule_reminder_job(config.MY_TELEGRAM_ID, task_text, run_date, task_line=new_task_line)
            bot.answer_callback_query(call.id, f"Snoozed for {delay_hours}h.")
            bot.send_message(call.message.chat.id, f"⏰ Reminder **{task_text}** successfully snoozed for {delay_hours}h.", parse_mode="Markdown")

    except Exception as e:
        logger.error(f"[Callback Error] Error handling task callback: {e}")
        bot.answer_callback_query(call.id, "An error occurred while processing.")


@bot.callback_query_handler(func=lambda call: call.data.startswith('show_answer:'))
def handle_show_answer(call):
    if call.from_user.id != config.MY_TELEGRAM_ID:
        bot.answer_callback_query(call.id, "Error: Access denied.", show_alert=True)
        return
    try:
        card_id = call.data.split(':', 1)[1]
        flashcards = read_json_from_drive(vault_files.FLASHCARDS)
        if not isinstance(flashcards, list):
            flashcards = []

        card = None
        for c in flashcards:
            if c.get("id") == card_id:
                card = c
                break

        if not card:
            bot.answer_callback_query(call.id, "Card not found.", show_alert=True)
            return

        keyboard = telebot.types.InlineKeyboardMarkup()
        keyboard.row(
            telebot.types.InlineKeyboardButton(srs.RATING_LABELS["again"], callback_data=f"srs:{card_id}:again"),
            telebot.types.InlineKeyboardButton(srs.RATING_LABELS["hard"], callback_data=f"srs:{card_id}:hard"),
        )
        keyboard.row(
            telebot.types.InlineKeyboardButton(srs.RATING_LABELS["good"], callback_data=f"srs:{card_id}:good"),
            telebot.types.InlineKeyboardButton(srs.RATING_LABELS["easy"], callback_data=f"srs:{card_id}:easy"),
        )

        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"🎓 **Question:**\n\n{card['q']}\n\n💡 **Answer:**\n\n{card['a']}",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.error(f"[Quiz Callback] Error showing answer: {e}")
        bot.answer_callback_query(call.id, f"Error: {e}", show_alert=True)


@bot.callback_query_handler(func=lambda call: call.data.startswith('srs:'))
def handle_srs_review(call):
    """
    Rates a flashcard using the SM-2 algorithm (srs.py) - Again/Hard/Good/
    Easy, same as Anki's review screen, instead of the old fixed-delay
    buttons (1m/6h/24h/168h/720h chosen manually every time, which wasn't
    real spaced repetition).
    """
    if call.from_user.id != config.MY_TELEGRAM_ID:
        bot.answer_callback_query(call.id, "Error: Access denied.", show_alert=True)
        return
    try:
        _, card_id, rating = call.data.split(':', 2)

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
            bot.answer_callback_query(call.id, "Card not found.", show_alert=True)
            return

        interval_days = updated_card["value"].get("interval_days", 0) if updated_card["value"] else 0
        next_label = "less than an hour" if rating == "again" else f"in {interval_days}d"

        # Offer a fresh AI explanation when the card wasn't remembered -
        # a small step towards an "AI tutor" rather than just re-showing
        # the same Q&A again later.
        explain_keyboard = None
        if rating == "again":
            explain_keyboard = telebot.types.InlineKeyboardMarkup()
            explain_keyboard.add(telebot.types.InlineKeyboardButton("🎓 Explain differently", callback_data=f"explain:{card_id}"))

        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"{srs.RATING_LABELS.get(rating, '✅')} · next review {next_label}",
            reply_markup=explain_keyboard
        )
        bot.answer_callback_query(call.id)

        # Automatically send next due card. Uses call.message.chat.id
        # directly rather than quiz_flashcards(call.message) - see
        # _send_next_due_flashcard's docstring for why that never worked.
        _send_next_due_flashcard(call.message.chat.id)
    except Exception as e:
        logger.error(f"[Quiz Callback] Error handling SRS: {e}")
        bot.answer_callback_query(call.id, f"Error: {e}", show_alert=True)


@bot.callback_query_handler(func=lambda call: call.data.startswith('explain:'))
def handle_explain_card(call):
    """
    Small step towards an "AI tutor" rather than a static Q&A: when a card
    wasn't remembered (rated "again"), offers a fresh explanation of the
    same concept - a different angle, an analogy or a mnemonic - instead
    of just showing the identical answer again next time.
    """
    if call.from_user.id != config.MY_TELEGRAM_ID:
        bot.answer_callback_query(call.id, "Error: Access denied.", show_alert=True)
        return
    try:
        card_id = call.data.split(':', 1)[1]
        flashcards = read_json_from_drive(vault_files.FLASHCARDS)
        if not isinstance(flashcards, list):
            flashcards = []
        card = next((c for c in flashcards if c.get("id") == card_id), None)
        if not card:
            bot.answer_callback_query(call.id, "Card not found.", show_alert=True)
            return

        bot.answer_callback_query(call.id, "Explaining...")
        bot.send_chat_action(call.message.chat.id, 'typing')

        prompt = apply_format_rule(f"""You are a patient tutor. The user couldn't recall the answer to a study flashcard. Explain the concept AGAIN, a different way (don't repeat the old answer verbatim): use an analogy, a mnemonic, or a simpler phrasing so it sticks better. Be brief (3-5 sentences).

Question: {card.get('q', '')}
Correct answer: {card.get('a', '')}
""")
        response = key_manager.generate_content(model=config.MODEL_COMPLEX, contents=prompt)
        explanation = (response.text or "").strip()
        if not explanation:
            explanation = "Couldn't generate an explanation, please try again later."

        bot.send_message(call.message.chat.id, f"🎓 {explanation}")
    except Exception as e:
        logger.error(f"[Explain Card] Error: {e}")
        bot.answer_callback_query(call.id, f"Error: {e}", show_alert=True)


@bot.message_handler(commands=['journal'])
def handle_journal_command(message):
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        # Extract journaling text
        args = message.text.split(maxsplit=1)
        journal_text = args[1].strip() if len(args) > 1 else ""

        # If no text in the command, check if they replied to a message
        if not journal_text and message.reply_to_message:
            journal_text = message.reply_to_message.text or message.reply_to_message.caption or ""

        if not journal_text:
            bot.reply_to(message, "Please write your thoughts after the `/journal` command, or reply to a message with this command. Example:\n`/journal Today was a great, productive day.`")
            return

        # Save the entry itself right away, independent of the AI call below -
        # the reflection the user actually wrote is the point of a journal,
        # and it shouldn't be lost just because the AI call times out or
        # the API key pool is temporarily exhausted.
        append_journal_entry(journal_text)

        status = StatusMessage(message, "📔 Reading your entry...")
        now_msk = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")

        prompt = apply_format_rule(f"""Current time in Moscow: {now_msk}
Today's date: {today_str}

The user is writing a personal reflection/journal entry:
"{journal_text}"

Act as an empathetic listener and coach. Respond with a short, supportive reply.

Start your warm, supportive reply with [REPLY].

Reply format:
[REPLY]
Your coach's reply to the user, in English
""")
        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=prompt
        )
        raw_text = response.text or ""
        if is_ai_response_empty(raw_text):
            status.finish(AI_UNAVAILABLE_MESSAGE)
            return

        tags = parse_gemini_tags(raw_text)
        reply_part = extract_reply(raw_text)
        apply_gemini_tags(tags)

        status.finish(reply_part + _fallback_note(response))
    except Exception as e:
        bot.reply_to(message, f"Error saving journal entry: {e}")


@bot.message_handler(commands=['brain'])
def handle_brain_search(message):
    """
    RAG-lite global search over Second Brain files using MODEL_COMPLEX.
    """
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        # Extract query text
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            bot.reply_to(message, "Ask your Second Brain a question. Example: `/brain How are my health goals coming along?`", parse_mode="Markdown")
            return
        query = args[1].strip()
        status = StatusMessage(message, "🧠 Reading your Second Brain...")

        # Read context files
        tasks = read_file_from_drive(vault_files.TASKS)
        finance = read_file_from_drive(vault_files.FINANCE)
        health = read_file_from_drive(vault_files.HEALTH)
        memory = read_file_from_drive(vault_files.MEMORY)
        goals = read_file_from_drive(vault_files.GOALS)
        journal = read_file_from_drive(vault_files.JOURNAL)

        # Compact Index.json summary so /brain also knows about
        # Media/People/Project entities (ARCHITECTURE.md step 3) - their
        # full note bodies aren't included here (would blow up context
        # fast with many entities), just names/titles/tags, so the model
        # can answer "who/what do I have" questions and point to /search
        # for full details on a specific one.
        index_data = vault_index.read_index()
        people_list = ", ".join(p.get("name", "?") for p in index_data.get("people", [])) or "none"
        projects_list = ", ".join(p.get("name", "?") for p in index_data.get("projects", [])) or "none"
        media_list = ", ".join(m.get("title", "?") for m in index_data.get("media", [])) or "none"
        tags_list = ", ".join(index_data.get("tags", [])) or "none"

        # Combine into context, safely truncating each to prevent context limit issues (e.g. max 4000 chars each)
        def truncate_context(text, max_chars=4000):
            if len(text) > max_chars:
                return text[-max_chars:]  # take recent part
            return text

        context = f"""[FILE Goals.md]
{truncate_context(goals)}

[FILE Tasks.md]
{truncate_context(tasks)}

[FILE Finance.md]
{truncate_context(finance)}

[FILE Health.md]
{truncate_context(health)}

[FILE Memory.md]
{truncate_context(memory)}

[FILE Journal.md - personal diary/reflection]
{truncate_context(journal)}

[SECOND BRAIN INDEX - names/titles only, not the full note contents]
People: {people_list}
Projects: {projects_list}
Media (movies/anime/books/games): {media_list}
Tags: {tags_list}
"""

        prompt = apply_format_rule(f"""You are S1get's "Second Brain" AI system. Your job is to analyze all the files in their personal knowledge base (Obsidian) and give a thorough, deep, and accurate answer to their question.
Current time: {datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")}

User's question: "{query}"

The [SECOND BRAIN INDEX] section contains only a list of names/titles (people, projects, media, tags), WITHOUT the full text of their notes - if the question needs details on a specific person/project/title rather than just a list, be upfront that they should ask `/search <name>` for details.

Context from their knowledge base (files from Google Drive):
---
{context}
---

Write a comprehensive, deep, and structured analysis or answer in English. Focus on accuracy and facts. Use formatting to make it readable.
""")
        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=prompt
        )
        raw_text = response.text or ""
        if is_ai_response_empty(raw_text):
            status.finish(AI_UNAVAILABLE_MESSAGE)
            return

        status.finish(sanitize_telegram_text(raw_text) + _fallback_note(response))
    except Exception as e:
        bot.reply_to(message, f"Error searching your Second Brain: {e}")


@bot.message_handler(commands=['search'])
def handle_global_search(message):
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            bot.reply_to(message, "Use `/search query`.", parse_mode="Markdown")
            return
        query = args[1].strip()

        files = list_markdown_files(limit=10)
        if not files:
            bot.reply_to(message, "Couldn't find any Markdown files in Google Drive.")
            return

        status = StatusMessage(message, "🔍 Searching your notes...")
        collected_chunks = []
        total_chars = 0
        for file_meta in files:
            filename = file_meta.get("name", "")
            if not filename.endswith(".md"):
                continue
            content = read_file_from_drive(filename)
            if not content:
                continue
            remaining = 50000 - total_chars
            if remaining <= 0:
                break
            snippet = content[:remaining]
            collected_chunks.append(f"[FILE: {filename}]\n{snippet}")
            total_chars += len(snippet)

        if not collected_chunks:
            status.finish("Files were found, but their content is empty.")
            return

        notes_context = "\n\n".join(collected_chunks)
        prompt = apply_format_rule(f"""You are the user's digital Second Brain. Answer the query: "{query}" using the provided Obsidian notes. Cite which file (.md) the information comes from.

If the answer is uncertain, say so clearly. Reply in English and keep the answer structured and concise.

Notes:
---
{notes_context}
---
""")
        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=prompt
        )
        raw_text = response.text or ""
        if is_ai_response_empty(raw_text):
            status.finish(AI_UNAVAILABLE_MESSAGE)
            return
        status.finish(sanitize_telegram_text(raw_text) + _fallback_note(response))
    except Exception as e:
        bot.reply_to(message, f"Error during global search: {e}")


_WHO_CATEGORY_META = {
    "people": {"folder": vault_files.FOLDER_PEOPLE, "icon": "👤", "name_field": "name"},
    "media": {"folder": vault_files.FOLDER_MEDIA, "icon": "🎬", "name_field": "title"},
    "projects": {"folder": vault_files.FOLDER_PROJECTS, "icon": "📁", "name_field": "name"},
}


@bot.message_handler(commands=['who'])
def handle_who(message):
    """
    Looks up a person/media/project by name (via vault_index.find_entity,
    which now also fuzzy-matches close spellings) and sends the note's
    content directly in the chat - no need to open Obsidian just to recall
    a detail. No AI call involved, just a direct Index.json + note lookup.
    """
    if not is_me(message):
        return
    try:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            bot.reply_to(message, "Give me a name/title. Example: `/who Ivan`", parse_mode="Markdown")
            return
        query = args[1].strip()

        entry = None
        category = None
        for candidate_category in _WHO_CATEGORY_META:
            entry = vault_index.find_entity(candidate_category, query)
            if entry:
                category = candidate_category
                break

        if not entry:
            bot.reply_to(
                message,
                f"Couldn't find \"{query}\" among people/media/projects. Try /search to search all your notes."
            )
            return

        meta = _WHO_CATEGORY_META[category]
        filename = entry["file"].split("/")[-1]
        content = read_file_from_drive(filename, folder_id=get_folder_id(meta["folder"]))
        fields, body = note_templates.parse_note(content)

        display_name = entry.get(meta["name_field"], query)
        info_bits = [f"{key}: {fields[key]}" for key in ("category", "status", "rating", "relationship") if fields.get(key)]

        lines = [f"{meta['icon']} {display_name}"]
        if info_bits:
            lines.append(" | ".join(info_bits))
        lines.append("")
        lines.append(body or "(note is still empty)")

        bot.reply_to(message, "\n".join(lines))
    except Exception as e:
        bot.reply_to(message, f"Error looking up card: {e}")


@bot.message_handler(commands=['digest'])
def handle_digest(message):
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        raw_inbox = read_file_from_drive(vault_files.RAW_INBOX)
        if not raw_inbox.strip():
            bot.reply_to(message, "Raw_Inbox is empty.")
            return

        now_msk = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        prompt = apply_format_rule(f"""Current time in Moscow: {now_msk}

Below are raw incoming messages from Raw_Inbox.md. Extract tasks into [TASK_ADD] and questions into [QUESTION]. Ignore casual chat.

If a task has no time but has a day/date, pick a sensible time. If there isn't enough information, don't create a tag.
Output only tags, one per line.

Raw_Inbox.md:
---
{raw_inbox}
---
""")
        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=prompt
        )
        raw_text = (response.text or "").strip()
        tags = parse_gemini_tags(raw_text)
        apply_gemini_tags(tags)

        def clear_if_unchanged(current_content):
            # Only clear the file if nothing was appended to it (e.g. via the
            # external webhook) while the AI call above was in flight -
            # otherwise we'd silently discard that new content.
            if current_content != raw_inbox:
                logger.warning("[Digest] Raw_Inbox.md changed during processing; leaving new content in place.")
                return None
            return ""

        update_file_on_drive(vault_files.RAW_INBOX, clear_if_unchanged)
        bot.reply_to(message, f"📥 Inbox processed. Tags extracted: {len(tags)}")
    except Exception as e:
        bot.reply_to(message, f"Digest error: {e}")


@bot.message_handler(commands=['process'])
def handle_process_inbox(message):
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        inbox_content = read_file_from_drive(vault_files.INBOX)
        if not inbox_content.strip():
            bot.reply_to(message, "Inbox is empty.")
            return

        now_msk = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        prompt = apply_format_rule(f"""Current time in Moscow: {now_msk}

Below is the content of Inbox.md with the user's raw notes.
Convert the material into tags only:
- [NOTE] Category | Text
- [CARD] Question | Answer

For [NOTE]:
- make atomic notes;
- automatically wrap key entities, concepts, and names in [[wikilinks]];
- add relevant #tags;
- pick a short, clear Category.

For [CARD]:
- only create genuinely useful question-answer cards.

Ignore noise and repeats. Output only tags, one per line.

Inbox.md:
---
{inbox_content}
---
""")
        response = key_manager.generate_content(
            model=config.MODEL_COMPLEX,
            contents=prompt
        )
        raw_text = (response.text or "").strip()
        tags = parse_gemini_tags(raw_text)
        apply_gemini_tags(tags)

        def clear_if_unchanged(current_content):
            if current_content != inbox_content:
                logger.warning("[Process Inbox] Inbox.md changed during processing; leaving new content in place.")
                return None
            return ""

        update_file_on_drive(vault_files.INBOX, clear_if_unchanged)
        bot.reply_to(message, f"🗂 Inbox processed. Tags extracted: {len(tags)}")
    except Exception as e:
        bot.reply_to(message, f"Process error: {e}")


@bot.message_handler(func=lambda message: True)
def chat_with_gemini(message):
    """
    Handles general chat with Multi-Agent Pipeline:
    - Agent Router: Uses MODEL_LITE to classify intent
    - Agent Archivist: If NOTE, uses MODEL_COMPLEX to format Zettelkasten note
    - Agent Tutor: Background thread generates flashcard from saved NOTE
    """
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    status = StatusMessage(message)
    try:
        current_memory = read_file_from_drive(vault_files.MEMORY)
        if not current_memory.strip():
            current_memory = "Long-term memory is still empty."

        current_tasks = read_file_from_drive(vault_files.TASKS)
        if not current_tasks.strip():
            current_tasks = "No tasks yet."

        now_msk = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")

        # Extract forwarding context
        sender_name = get_forward_sender_name(message)
        is_forwarded = (sender_name is not None)
        if is_forwarded:
            user_message_text = f"Pavel forwarded a message from {sender_name}:\n{message.text}"
        else:
            user_message_text = message.text

        # Multi-Agent Pipeline: Router
        status.update("🔎 Analyzing message...")
        classification = agent_router(user_message_text)
        logger.info(f"[Multi-Agent Pipeline] Router classified as: {classification}")

        # If NOTE, use Archivist agent
        if classification == "NOTE":
            status.update("📝 Writing a note to your knowledge base...")
            note_output = agent_archivist(user_message_text)
            if note_output and "[NOTE]" in note_output:
                # Parse and save the note. apply_gemini_tags() itself
                # triggers the background Agent Tutor (flashcard
                # generation) for NOTE tags - see ai_pipeline.py.
                tags = parse_gemini_tags(note_output)
                apply_gemini_tags(tags)

                reply_part = f"📝 Note saved: {note_output.replace('[NOTE]', '').strip()}"
                if not reply_part or not reply_part.strip():
                    reply_part = "Successfully saved new knowledge to your brain! 🧠"
                status.finish(reply_part)
                return

        # For other classifications, use standard flow
        text_len = len(message.text) if message.text else 0
        if is_forwarded or text_len >= 40:
            selected_model = config.MODEL_COMPLEX
        else:
            selected_model = config.MODEL_LITE

        logger.info(f"[Model Router] Routing input (length={text_len}, forwarded={is_forwarded}) to model: {selected_model}")
        status.update("💬 Preparing a reply...")
        extraction_rules = get_extraction_rules(today_str)

        prompt = apply_format_rule(f"""Current time in Moscow: {now_msk}
Today's date: {today_str}

Long-term memory (Memory.md):
---
{current_memory}
---

User's tasks (Tasks.md):
---
{current_tasks}
---

S1get says: "{user_message_text}"

You have access to the user's tasks (Tasks.md). If the user asks about their schedule, plans, or what they have to do today/tomorrow/this week, analyze the Tasks.md list and give them a precise answer.

Reply clearly and to the point. In [REPLY] — only the live reply to the user, without repeating memory content.

{extraction_rules}

RULE FOR FORWARDED MESSAGES [QUESTION]:
If a forwarded message contains a question or needs a reply, be sure to add the tag:
[QUESTION] Name: gist of the question
Where Name is the original sender's name (from "Pavel forwarded a message from Name:"), and "gist of the question" is a brief description of the question.

Recognition examples:
- "Pavel forwarded a message from Ivan:\nWill you come to the meeting?" → [QUESTION] Ivan: Will you come to the meeting?

Reply format:
[REPLY]
Your reply to the user
(then tags, if needed — each on a new line)
""")
        response = key_manager.generate_content(
            model=selected_model,
            contents=prompt
        )
        raw_text = response.text or ""
        if is_ai_response_empty(raw_text):
            status.finish(AI_UNAVAILABLE_MESSAGE)
            return

        tags = parse_gemini_tags(raw_text)
        reply_part = extract_reply(raw_text)
        apply_gemini_tags(tags)

        # Prevent empty reply to avoid Telegram 400 errors
        if not reply_part or not reply_part.strip():
            reply_part = "Successfully saved new knowledge to your brain! 🧠"
        status.finish(reply_part + _fallback_note(response))
    except Exception as e:
        status.finish(f"Error: {e}")


def process_external_text(text):
    """
    Exposes AI tag processing for external requests (Siri/shortcuts).
    """
    try:
        current_memory = read_file_from_drive(vault_files.MEMORY)
        if not current_memory.strip():
            current_memory = "Long-term memory is still empty."

        now_msk = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")
        extraction_rules = get_extraction_rules(today_str)

        prompt = apply_format_rule(f"""Current time in Moscow: {now_msk}
Today's date: {today_str}

Long-term memory (Memory.md):
---
{current_memory}
---

S1get says (via Siri/Shortcut): "{text}"

Reply clearly and to the point. In [REPLY] — only the live reply to the user, without repeating memory content.

{extraction_rules}

Reply format:
[REPLY]
Your reply to the user
(then tags, if needed — each on a new line)
""")
        response = key_manager.generate_content(
            model=config.MODEL_LITE,
            contents=prompt
        )
        raw_text = response.text or ""
        if is_ai_response_empty(raw_text):
            return {"success": False, "error": "AI service temporarily unavailable"}

        tags = parse_gemini_tags(raw_text)
        reply_part = extract_reply(raw_text)
        apply_gemini_tags(tags)
        return {
            "success": True,
            "reply": reply_part,
            "tags_found": [t[0] for t in tags]
        }
    except Exception as e:
        logger.error(f"[External Process] Error: {e}")
        return {
            "success": False,
            "error": str(e)
        }
