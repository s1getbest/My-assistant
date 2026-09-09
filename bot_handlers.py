import re
from datetime import datetime, timedelta
import telebot
from google.genai import types
from uuid import uuid4
import threading
import config
import vault_files
import university_schedule
import vault_index
from bot_instance import bot
from key_manager import key_manager
from logging_config import get_logger
from drive_service import (
    delete_line_from_task_file,
    get_task_line_by_token,
    list_markdown_files,
    mark_task_done_by_token,
    read_json_from_drive,
    read_file_from_drive,
    update_file_on_drive,
    update_json_file_on_drive,
    add_user_xp,
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
)

logger = get_logger(__name__)

AI_UNAVAILABLE_MESSAGE = "Извини, сервис ИИ сейчас временно недоступен или перегружен. Попробуй, пожалуйста, ещё раз через минуту."


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

    def __init__(self, message, initial_text="🧠 Думаю..."):
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
        final_text = final_text or "Готово."
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
    (ARCHITECTURE.md step 5's "прозрачность процесса" - the user asked to
    see when the bot switched to a backup instead of it happening silently).
    """
    if getattr(response, "used_fallback", False):
        return "\n\n⚡ (ответ подготовлен через резервный ключ/модель — основной сервис был временно недоступен)"
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


def agent_tutor_background(note_text):
    """
    Agent Tutor (Background): Uses MODEL_COMPLEX to generate contextual Active Recall flashcard from note.
    Runs in background thread to avoid blocking Telegram reply.
    Uses Bloom's Taxonomy for level-appropriate questions.
    Output: [CARD] Question | Answer with [[wikilinks]]
    """
    def generate_flashcard():
        try:
            prompt = apply_format_rule(f"""You are an expert neuro-education tutor using Bloom's Taxonomy and Spaced Repetition. Analyze the saved Zettelkasten note:
  - If it's an atomic fact (Level 1: word, definition, date), generate a direct Q&A card.
  - If it's a complex concept, historical event, or university lecture (Level 2 & 3), generate a CONTEXTUAL Active Recall card. Ask 'Why' or 'How does X relate to Y?'. Include the explanatory narrative and Obsidian wikilinks in the answer so the user recalls the whole system.
  Output strictly: [CARD] Question | Answer with [[wikilinks]].

Note: "{note_text}"
""")
            response = key_manager.generate_content(
                model=config.MODEL_COMPLEX,
                contents=prompt
            )
            card_text = (response.text or "").strip()

            # Parse and save flashcard
            if "[CARD]" in card_text and "|" in card_text:
                card_body = card_text.split("[CARD]", 1)[1].strip()
                if "|" in card_body:
                    question, answer = card_body.split("|", 1)

                    def mutate(flashcards):
                        if not isinstance(flashcards, list):
                            flashcards = []
                        flashcards.append({
                            "id": str(uuid4()),
                            "q": question.strip(),
                            "a": answer.strip(),
                            "next_review": datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M:%S"),
                        })
                        return flashcards

                    update_json_file_on_drive(vault_files.FLASHCARDS, mutate, default_factory=list)
                    logger.info("[Agent Tutor] Flashcard generated and saved")
        except Exception as e:
            logger.error(f"[Agent Tutor] Error: {e}")

    thread = threading.Thread(target=generate_flashcard)
    thread.daemon = True
    thread.start()


# === BOT HANDLERS ===

@bot.message_handler(commands=['start'])
def send_welcome(message):
    if not is_me(message):
        return
    bot.reply_to(message, "Привет! Твой личный мозг запущен. Я подключен к Google Диску и Obsidian!")


@bot.message_handler(commands=['sleep'])
def track_sleep(message):
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        args = message.text.split()
        if len(args) < 2:
            bot.reply_to(message, "Укажи часы сна. Пример: `/sleep 7.5`", parse_mode="Markdown")
            return
        hours = args[1]
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")
        append_line_to_drive(vault_files.HEALTH, f"* {today_str}: {hours}")
        add_user_xp(5)
        bot.reply_to(message, f"🛌 **Сон записан!** (+5 XP)\n\n> {today_str} · {hours} ч.")
    except Exception as e:
        bot.reply_to(message, f"Ошибка записи сна: {e}")


@bot.message_handler(commands=['quiz'])
def quiz_flashcards(message):
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
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
            bot.reply_to(message, "🎉 Нет карточек для повторения!")
            return

        card = due_cards[0][1]
        keyboard = telebot.types.InlineKeyboardMarkup()
        keyboard.add(telebot.types.InlineKeyboardButton("Показать ответ", callback_data=f"show_answer:{card['id']}"))
        bot.reply_to(message, f"🎓 **Вопрос:**\n\n{card['q']}", reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"Ошибка загрузки карточки: {e}")


@bot.message_handler(content_types=['voice'])
def handle_voice(message):
    """
    Handles voice messages using MODEL_COMPLEX (native audio support).
    """
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    status = StatusMessage(message, "🎙️ Слушаю голосовое...")
    try:
        voice_info = bot.get_file(message.voice.file_id)
        downloaded_file = bot.download_file(voice_info.file_path)

        current_memory = read_file_from_drive(vault_files.MEMORY)
        if not current_memory.strip():
            current_memory = "Пока пустая долгосрочная память."

        now_msk = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")

        # Check if it's a journal entry
        is_journal = False
        if message.reply_to_message and message.reply_to_message.text and '/journal' in message.reply_to_message.text:
            is_journal = True
        elif message.caption and message.caption.startswith('/journal'):
            is_journal = True

        if is_journal:
            prompt = apply_format_rule(f"""Текущее время в Москве: {now_msk}
Сегодняшняя дата: {today_str}

Пользователь прислал голосовую запись в свой личный дневник (Journal).
Внимательно прослушай аудиофайл и распознай глубокие размышления Павла.

Act as an empathetic listener and coach. Respond with a short, supportive reply. At the very end of your response, add a new tag: `[MOOD] score/10`, where score is your assessment of their emotional state (1-10).

Помимо тегов, начни свой живой поддерживающий ответ с [ОТВЕТ], чтобы отделить живой ответ от тегов.

Формат ответа:
[ОТВЕТ]
Твой ответ пользователю на русском языке
[MOOD] score/10
""")
        else:
            extraction_rules = get_extraction_rules(today_str)
            prompt = apply_format_rule(f"""Текущее время в Москве: {now_msk}
Сегодняшняя дата: {today_str}

Долгосрочная память (Memory.md):
---
{current_memory}
---

Пользователь прислал голосовое сообщение. Текст голосового сообщения находится в прикрепленном аудиофайле.
Внимательно прослушай аудиофайл и распознай, что говорит S1get.

Ответь чётко и по делу. В [ОТВЕТ] — только живой ответ пользователю, без дублирования памяти.

{extraction_rules}

Формат ответа:
[ОТВЕТ]
Твой ответ пользователю
(далее теги, если нужны — каждый с новой строки)
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
        status.finish(f"Ошибка обработки голосового сообщения: {e}")


@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    """
    Handles photo messages using MODEL_COMPLEX (vision support).
    """
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    status = StatusMessage(message, "🖼️ Смотрю изображение...")
    try:
        # Get highest resolution photo
        photo = message.photo[-1]
        file_info = bot.get_file(photo.file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        now_msk = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")

        caption = message.caption or ""
        extraction_rules = get_extraction_rules(today_str)

        prompt = apply_format_rule(f"""Текущее время в Москве: {now_msk}
Сегодняшняя дата: {today_str}

Пользователь прислал изображение. Вот его описание/подпись (если есть): "{caption}"

Analyze this image. If it's a receipt, calculate the total and output `[FINANCE] YYYY-MM-DD: amount | category | description`. If it's handwritten notes or a whiteboard, extract actionable items as `[TASK_ADD] YYYY-MM-DD HH:MM | Task`. If it's an article/screenshot, summarize it as `[MEMORY] summary`.
{extraction_rules}

Помимо тегов, напиши пользователю краткий содержательный ответ/комментарий. Начни свой ответ с [ОТВЕТ], чтобы отделить живой ответ от тегов.

Формат ответа:
[ОТВЕТ]
Твой ответ пользователю
(далее теги, если нужны — каждый с новой строки)
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
        status.finish(f"Ошибка обработки изображения: {e}")


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

        prompt = apply_format_rule(f"""Текущее время в Москве: {now_msk}
Сегодняшняя дата: {today_str}

Пользователь отправил быструю заметку через Inline-режим: "{text}"

{extraction_rules}
Пожалуйста, будь точен в распознавании. Никакого другого текста писать НЕ нужно, только теги с новой строки (если применимо).
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
                    message_text=f"⚠️ Не удалось распознать (сервис ИИ недоступен): {text}"
                ),
                description="Ничего не сохранено — попробуйте ещё раз позже."
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
                message_text=f"✅ Успешно записано в Time OS: {text}"
            ),
            description=f"Распознать и сохранить: {text}"
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
        bot.answer_callback_query(call.id, "Ошибка: Доступ запрещен.", show_alert=True)
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
                add_user_xp(10)
                bot.answer_callback_query(call.id, "Отмечено как выполнено! +10 XP")
                bot.send_message(call.message.chat.id, f"✅ Выполнено: **{task_text}** (+10 XP)", parse_mode="Markdown")
            else:
                bot.answer_callback_query(call.id, "Задача уже выполнена или не найдена.")
                bot.send_message(call.message.chat.id, "✅ Задача уже обработана или не найдена.", parse_mode="Markdown")

        elif action in ["task_snooze_1h", "task_snooze_24h"]:
            delay_hours = 1 if "1h" in action else 24
            old_task_line = get_task_line_by_token(task_token)
            if not old_task_line:
                bot.answer_callback_query(call.id, "Исходная задача не найдена.", show_alert=True)
                return

            task_text = extract_task_text_from_line(old_task_line)
            run_date = datetime.now(config.msk_tz) + timedelta(hours=delay_hours)
            new_task_line = f"* [ ] {run_date.strftime('%Y-%m-%d %H:%M')} | ⏰ REMINDER: {task_text}"

            from scheduler_jobs import schedule_reminder_job
            delete_line_from_task_file(old_task_line)
            append_line_to_drive(vault_files.TASKS, new_task_line)
            schedule_reminder_job(config.MY_TELEGRAM_ID, task_text, run_date, task_line=new_task_line)
            bot.answer_callback_query(call.id, f"Отложено на {delay_hours} ч.")
            bot.send_message(call.message.chat.id, f"⏰ Напоминание **{task_text}** успешно отложено на {delay_hours} ч.", parse_mode="Markdown")

    except Exception as e:
        logger.error(f"[Callback Error] Error handling task callback: {e}")
        bot.answer_callback_query(call.id, "Произошла ошибка при обработке.")


@bot.callback_query_handler(func=lambda call: call.data.startswith('show_answer:'))
def handle_show_answer(call):
    if call.from_user.id != config.MY_TELEGRAM_ID:
        bot.answer_callback_query(call.id, "Ошибка: Доступ запрещен.", show_alert=True)
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
            bot.answer_callback_query(call.id, "Карточка не найдена.", show_alert=True)
            return

        keyboard = telebot.types.InlineKeyboardMarkup()
        keyboard.row(
            telebot.types.InlineKeyboardButton("Снова 1м", callback_data=f"srs:{card_id}:0.016"),
            telebot.types.InlineKeyboardButton("Позже 6ч", callback_data=f"srs:{card_id}:6")
        )
        keyboard.row(
            telebot.types.InlineKeyboardButton("Завтра 1д", callback_data=f"srs:{card_id}:24"),
            telebot.types.InlineKeyboardButton("Неделя 7д", callback_data=f"srs:{card_id}:168")
        )
        keyboard.add(telebot.types.InlineKeyboardButton("Месяц 30д", callback_data=f"srs:{card_id}:720"))

        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"🎓 **Вопрос:**\n\n{card['q']}\n\n💡 **Ответ:**\n\n{card['a']}",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.error(f"[Quiz Callback] Error showing answer: {e}")
        bot.answer_callback_query(call.id, f"Ошибка: {e}", show_alert=True)


@bot.callback_query_handler(func=lambda call: call.data.startswith('srs:'))
def handle_srs_review(call):
    if call.from_user.id != config.MY_TELEGRAM_ID:
        bot.answer_callback_query(call.id, "Ошибка: Доступ запрещен.", show_alert=True)
        return
    try:
        _, card_id, interval_hours = call.data.split(':', 2)
        interval_hours = float(interval_hours)

        next_review = datetime.now(config.msk_tz) + timedelta(hours=interval_hours)

        def mutate(flashcards):
            if not isinstance(flashcards, list):
                return None
            for card in flashcards:
                if card.get("id") == card_id:
                    card["next_review"] = next_review.strftime("%Y-%m-%d %H:%M:%S")
                    return flashcards
            return None

        result = update_json_file_on_drive(vault_files.FLASHCARDS, mutate, default_factory=list)

        if result is None:
            bot.answer_callback_query(call.id, "Карточка не найдена.", show_alert=True)
            return

        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="✅ Запомнил!",
            reply_markup=None
        )
        bot.answer_callback_query(call.id)

        # Automatically send next due card
        quiz_flashcards(call.message)
    except Exception as e:
        logger.error(f"[Quiz Callback] Error handling SRS: {e}")
        bot.answer_callback_query(call.id, f"Ошибка: {e}", show_alert=True)


@bot.message_handler(commands=['update_schedule'])
def handle_update_schedule(message):
    """
    Overwrites Расписание.md with the text that follows the command (or
    the message it's a reply to). No AI parsing involved on purpose - see
    university_schedule.py's module docstring for the expected format and
    why a strict, code-parseable format was chosen over freeform recognition.
    """
    if not is_me(message):
        return
    try:
        args = message.text.split(maxsplit=1)
        raw_text = args[1].strip() if len(args) > 1 else ""
        if not raw_text and message.reply_to_message:
            raw_text = message.reply_to_message.text or message.reply_to_message.caption or ""

        if not raw_text:
            bot.reply_to(
                message,
                "Пришли текст расписания после команды `/update_schedule` (или ответь ею на "
                "сообщение с текстом расписания). Формат:\n\n"
                "```\n## Нечётная\nПн: 09:00 Предмет; 10:40 Предмет2\nВт: 12:20 Предмет3\n\n"
                "## Чётная\nПн: 09:00 Предмет4\n```",
                parse_mode="Markdown",
            )
            return

        university_schedule.save_schedule(raw_text)
        sections = university_schedule.split_sections(raw_text)
        if not sections:
            bot.reply_to(
                message,
                "⚠️ Расписание сохранено, но не нашёл ни одного раздела \"## Нечётная\"/\"## Чётная\" - "
                "проверь формат, иначе пары не будут автоматически попадать в Tasks.md."
            )
            return

        found = ", ".join("нечётная" if p == "odd" else "чётная" for p in sections)
        bot.reply_to(message, f"📅 Расписание обновлено. Найдены разделы: {found}.")
    except Exception as e:
        bot.reply_to(message, f"Ошибка обновления расписания: {e}")


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
            bot.reply_to(message, "Пожалуйста, напиши свои мысли после команды `/journal` или ответь этой командой на сообщение. Например:\n`/journal Сегодня был прекрасный продуктивный день.`")
            return

        status = StatusMessage(message, "📔 Читаю запись...")
        now_msk = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")

        prompt = apply_format_rule(f"""Текущее время в Москве: {now_msk}
Сегодняшняя дата: {today_str}

Пользователь пишет личную рефлексию/дневник (journaling):
"{journal_text}"

Act as an empathetic listener and coach. Respond with a short, supportive reply. At the very end of your response, add a new tag: `[MOOD] score/10`, where score is your assessment of their emotional state (1-10).

Помимо тегов, начни свой живой поддерживающий ответ с [ОТВЕТ], чтобы отделить живой ответ от тегов.

Формат ответа:
[ОТВЕТ]
Твой ответ пользователю коуча на русском языке
[MOOD] score/10
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
        bot.reply_to(message, f"Ошибка записи дневника: {e}")


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
            bot.reply_to(message, "Задай вопрос своему Второму Мозгу. Пример: `/brain Как продвигаются мои цели по здоровью?`", parse_mode="Markdown")
            return
        query = args[1].strip()
        status = StatusMessage(message, "🧠 Читаю Второй Мозг...")

        # Read context files
        tasks = read_file_from_drive(vault_files.TASKS)
        finance = read_file_from_drive(vault_files.FINANCE)
        health = read_file_from_drive(vault_files.HEALTH)
        memory = read_file_from_drive(vault_files.MEMORY)
        goals = read_file_from_drive(vault_files.GOALS)

        # Compact Index.json summary so /brain also knows about
        # Media/People/Project entities (ARCHITECTURE.md step 3) - their
        # full note bodies aren't included here (would blow up context
        # fast with many entities), just names/titles/tags, so the model
        # can answer "who/what do I have" questions and point to /search
        # for full details on a specific one.
        index_data = vault_index.read_index()
        people_list = ", ".join(p.get("name", "?") for p in index_data.get("people", [])) or "нет"
        projects_list = ", ".join(p.get("name", "?") for p in index_data.get("projects", [])) or "нет"
        media_list = ", ".join(m.get("title", "?") for m in index_data.get("media", [])) or "нет"
        tags_list = ", ".join(index_data.get("tags", [])) or "нет"

        # Combine into context, safely truncating each to prevent context limit issues (e.g. max 4000 chars each)
        def truncate_context(text, max_chars=4000):
            if len(text) > max_chars:
                return text[-max_chars:]  # take recent part
            return text

        context = f"""[ФАЙЛ Goals.md]
{truncate_context(goals)}

[ФАЙЛ Tasks.md]
{truncate_context(tasks)}

[ФАЙЛ Finance.md]
{truncate_context(finance)}

[ФАЙЛ Health.md]
{truncate_context(health)}

[ФАЙЛ Memory.md]
{truncate_context(memory)}

[ИНДЕКС ВТОРОГО МОЗГА - только имена/названия, не полное содержимое заметок]
Люди: {people_list}
Проекты: {projects_list}
Медиа (фильмы/аниме/книги/игры): {media_list}
Теги: {tags_list}
"""

        prompt = apply_format_rule(f"""Ты — ИИ-система "Второй Мозг" пользователя Павла. Твоя задача — проанализировать все файлы его личной базы знаний (Obsidian) и дать развернутый, глубокий и точный ответ на его вопрос.
Текущее время: {datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")}

Вопрос пользователя: "{query}"

Раздел [ИНДЕКС ВТОРОГО МОЗГА] содержит только список имён/названий (люди, проекты, медиа, теги), БЕЗ полного текста их заметок - если вопрос требует деталей по конкретному человеку/проекту/тайтлу, а не просто списка, честно скажи, что для подробностей нужно спросить `/search <имя>`.

Контекст его базы знаний (файлы из Google Drive):
---
{context}
---

Write a comprehensive, deep, and structured analysis or answer in Russian language. Focus on accuracy and facts. Use formatting to make it readable.
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
        bot.reply_to(message, f"Ошибка поиска по Второму Мозгу: {e}")


@bot.message_handler(commands=['search'])
def handle_global_search(message):
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            bot.reply_to(message, "Используй `/search запрос`.", parse_mode="Markdown")
            return
        query = args[1].strip()

        files = list_markdown_files(limit=10)
        if not files:
            bot.reply_to(message, "Не удалось найти Markdown-файлы в Google Drive.")
            return

        status = StatusMessage(message, "🔍 Ищу по заметкам...")
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
            status.finish("Файлы найдены, но их содержимое пустое.")
            return

        notes_context = "\n\n".join(collected_chunks)
        prompt = apply_format_rule(f"""You are the user's digital Second Brain. Answer the query: "{query}" using the provided Obsidian notes. Cite which file (.md) the information comes from.

If the answer is uncertain, say so clearly. Reply in Russian and keep the answer structured and concise.

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
        bot.reply_to(message, f"Ошибка глобального поиска: {e}")


@bot.message_handler(commands=['digest'])
def handle_digest(message):
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        raw_inbox = read_file_from_drive(vault_files.RAW_INBOX)
        if not raw_inbox.strip():
            bot.reply_to(message, "Raw_Inbox пуст.")
            return

        now_msk = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        prompt = apply_format_rule(f"""Текущее время в Москве: {now_msk}

Ниже сырые входящие сообщения из Raw_Inbox.md. Extract tasks into [TASK_ADD] and questions into [QUESTION]. Ignore casual chat.

Если времени у задачи нет, но есть день/дата, выбери разумное время. Если информации недостаточно, не создавай тег.
Выводи только теги, по одному на строку.

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
        bot.reply_to(message, f"📥 Inbox разобран. Извлечено тегов: {len(tags)}")
    except Exception as e:
        bot.reply_to(message, f"Ошибка digest: {e}")


@bot.message_handler(commands=['process'])
def handle_process_inbox(message):
    if not is_me(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        inbox_content = read_file_from_drive(vault_files.INBOX)
        if not inbox_content.strip():
            bot.reply_to(message, "Inbox пуст.")
            return

        now_msk = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        prompt = apply_format_rule(f"""Текущее время в Москве: {now_msk}

Ниже содержимое Inbox.md с сырыми заметками пользователя.
Преобразуй материал только в теги:
- [NOTE] Category | Text
- [CARD] Question | Answer

Для [NOTE]:
- делай атомарные заметки;
- автоматически оборачивай ключевые сущности, концепты и имена в [[wikilinks]];
- добавляй релевантные #tags;
- выбирай краткую и понятную Category.

Для [CARD]:
- создавай только полезные карточки формата вопрос-ответ.

Игнорируй шум и повторы. Выводи только теги, по одному на строку.

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
        bot.reply_to(message, f"🗂 Inbox обработан. Извлечено тегов: {len(tags)}")
    except Exception as e:
        bot.reply_to(message, f"Ошибка process: {e}")


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
            current_memory = "Пока пустая долгосрочная память."

        current_tasks = read_file_from_drive(vault_files.TASKS)
        if not current_tasks.strip():
            current_tasks = "Пока нет задач."

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
        status.update("🔎 Анализирую сообщение...")
        classification = agent_router(user_message_text)
        logger.info(f"[Multi-Agent Pipeline] Router classified as: {classification}")

        # If NOTE, use Archivist agent
        if classification == "NOTE":
            status.update("📝 Пишу заметку в базу знаний...")
            note_output = agent_archivist(user_message_text)
            if note_output and "[NOTE]" in note_output:
                # Parse and save the note
                tags = parse_gemini_tags(note_output)
                apply_gemini_tags(tags)

                # Extract note text for background Tutor
                if "|" in note_output:
                    note_body = note_output.split("|", 1)[1].strip()
                    # Trigger background Agent Tutor
                    agent_tutor_background(note_body)

                reply_part = f"📝 Заметка сохранена: {note_output.replace('[NOTE]', '').strip()}"
                if not reply_part or not reply_part.strip():
                    reply_part = "Успешно записал новые знания в твой мозг! 🧠"
                status.finish(reply_part)
                return

        # For other classifications, use standard flow
        text_len = len(message.text) if message.text else 0
        if is_forwarded or text_len >= 40:
            selected_model = config.MODEL_COMPLEX
        else:
            selected_model = config.MODEL_LITE

        logger.info(f"[Model Router] Routing input (length={text_len}, forwarded={is_forwarded}) to model: {selected_model}")
        status.update("💬 Готовлю ответ...")
        extraction_rules = get_extraction_rules(today_str)

        prompt = apply_format_rule(f"""Текущее время в Москве: {now_msk}
Сегодняшняя дата: {today_str}

Долгосрочная память (Memory.md):
---
{current_memory}
---

Задачи пользователя (Tasks.md):
---
{current_tasks}
---

S1get пишет: "{user_message_text}"

You have access to the user's tasks (Tasks.md). If the user asks about their schedule, plans, or what they have to do today/tomorrow/this week, analyze the Tasks.md list and give them a precise answer.

Ответь чётко и по делу. В [ОТВЕТ] — только живой ответ пользователю, без дублирования памяти.

{extraction_rules}

ПРАВИЛО ДЛЯ ПЕРЕСЛАННЫХ СООБЩЕНИЙ [QUESTION]:
Если пересланное сообщение содержит вопрос или требует ответа, обязательно добавь тег:
[QUESTION] Name: суть вопроса
Где Name — это имя оригинального отправителя (из "Pavel forwarded a message from Name:"), а "суть вопроса" — краткое описание вопроса.

Примеры распознавания:
- "Pavel forwarded a message from Ivan:\nWill you come to the meeting?" → [QUESTION] Ivan: Will you come to the meeting?

Формат ответа:
[ОТВЕТ]
Твой ответ пользователю
(далее теги, если нужны — каждый с новой строки)
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

        # Check if a NOTE was generated in the standard flow
        for tag_type, payload in tags:
            if tag_type == "NOTE" and "|" in payload:
                note_body = payload.split("|", 1)[1].strip()
                agent_tutor_background(note_body)

        # Prevent empty reply to avoid Telegram 400 errors
        if not reply_part or not reply_part.strip():
            reply_part = "Успешно записал новые знания в твой мозг! 🧠"
        status.finish(reply_part + _fallback_note(response))
    except Exception as e:
        status.finish(f"Ошибка: {e}")


def process_external_text(text):
    """
    Exposes AI tag processing for external requests (Siri/shortcuts).
    """
    try:
        current_memory = read_file_from_drive(vault_files.MEMORY)
        if not current_memory.strip():
            current_memory = "Пока пустая долгосрочная память."

        now_msk = datetime.now(config.msk_tz).strftime("%Y-%m-%d %H:%M")
        today_str = datetime.now(config.msk_tz).strftime("%Y-%m-%d")
        extraction_rules = get_extraction_rules(today_str)

        prompt = apply_format_rule(f"""Текущее время в Москве: {now_msk}
Сегодняшняя дата: {today_str}

Долгосрочная память (Memory.md):
---
{current_memory}
---

S1get пишет (через Siri/Shortcut): "{text}"

Ответь чётко и по делу. В [ОТВЕТ] — только живой ответ пользователю, без дублирования памяти.

{extraction_rules}

Формат ответа:
[ОТВЕТ]
Твой ответ пользователю
(далее теги, если нужны — каждый с новой строки)
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
