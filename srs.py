"""
Spaced repetition scheduling for Flashcards.json - a simplified SM-2
algorithm, the same family of algorithm Anki uses, replacing the old
system where the user manually picked a fixed delay ("1 hour", "6 hours",
"tomorrow", ...) on every review. That wasn't real spaced repetition: it
never adapted to how well a specific card is actually remembered. This
module tracks an ease factor and interval per card and grows/shrinks them
based on a 4-button rating (Again/Hard/Good/Easy), exactly like modern
Anki's review screen.

Card schema (Flashcards.json), new fields in bold, old ones unchanged:
{
  "id": "...",
  "q": "...",
  "a": "...",
  "next_review": "YYYY-MM-DD HH:MM:SS",
  "ease": 2.5,            # multiplier applied to the interval on a "Good" review
  "interval_days": 0,     # current interval in days (0 = brand new / just failed)
  "repetitions": 0,       # consecutive successful (non-"Again") reviews
  "source": "resource"    # which agent/entity type generated this card - see ai_pipeline.py
}

Old cards created before this module existed are missing ease/interval_days/
repetitions/source - _ensure_srs_fields() fills in sane defaults for them
the first time they're reviewed, so no migration script is needed.
"""
from datetime import datetime, timedelta

import config
from logging_config import get_logger

logger = get_logger(__name__)

DEFAULT_EASE = 2.5
MIN_EASE = 1.3

RATINGS = ("again", "hard", "good", "easy")

# Labels/emoji for the 4 review buttons, in a consistent order across
# Telegram and the dashboard.
RATING_LABELS = {
    "again": "😵 Again",
    "hard": "😐 Hard",
    "good": "🙂 Good",
    "easy": "😎 Easy",
}


def ensure_srs_fields(card):
    """Fills in default SRS fields on a card that predates this module."""
    card.setdefault("ease", DEFAULT_EASE)
    card.setdefault("interval_days", 0)
    card.setdefault("repetitions", 0)
    card.setdefault("source", "resource")
    return card


def schedule_next_review(card, rating):
    """
    Mutates `card` in place with the next SM-2 step for the given rating
    ("again"/"hard"/"good"/"easy") and returns it. Does not touch Drive -
    callers are responsible for persisting the card (see
    drive_service.update_json_file_on_drive usage in bot_handlers.py/
    dashboard.py).
    """
    if rating not in RATINGS:
        raise ValueError(f"Unknown SRS rating: {rating!r}, expected one of {RATINGS}")

    ensure_srs_fields(card)
    ease = card["ease"]
    interval = card["interval_days"]
    reps = card["repetitions"]

    if rating == "again":
        # Forgot it - reset the learning progress and show it again soon
        # (a short "relearning" step, not a full day) rather than punishing
        # the whole schedule the way a failed review resets repetitions.
        reps = 0
        interval = 0
        ease = max(MIN_EASE, ease - 0.20)
        delta = timedelta(minutes=10)
    else:
        reps += 1
        if rating == "hard":
            ease = max(MIN_EASE, ease - 0.15)
            interval = max(1, round(interval * 1.2)) if interval else 1
        elif rating == "good":
            ease = ease  # unchanged
            if reps == 1:
                interval = 1
            elif reps == 2:
                interval = 6
            else:
                interval = max(1, round(interval * ease))
        elif rating == "easy":
            ease = ease + 0.15
            interval = max(1, round((interval or 1) * ease * 1.3))
        delta = timedelta(days=interval)

    card["ease"] = round(ease, 2)
    card["interval_days"] = interval
    card["repetitions"] = reps
    card["next_review"] = (datetime.now(config.msk_tz) + delta).strftime("%Y-%m-%d %H:%M:%S")
    return card
