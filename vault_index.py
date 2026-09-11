"""
Index.json - a lightweight registry of known tags/people/projects/media so
the Archivist agent (ARCHITECTURE.md step 3) can reuse existing entities
and tags instead of creating duplicates (e.g. a second card for the same
person, or "Productivity" and "productivity" as two different tags).

Wired into apply_gemini_tags() (ai_pipeline.py, ARCHITECTURE.md step 3) for
Media/Person/Project entity resolution, and into the /who Telegram command
for direct lookup. Built on top of drive_service's atomic
update_json_file_on_drive (per-file locking already handles concurrent
writers).

Schema (08-System/Index.json):
{
  "tags": ["health", "study", ...],
  "people": [{"name": "...", "file": "06-People/....md", "added": "YYYY-MM-DD"}],
  "projects": [{"name": "...", "file": "02-Projects/...", "added": "YYYY-MM-DD"}],
  "media": [{"title": "...", "file": "05-Media/....md", "added": "YYYY-MM-DD"}]
}
"added" is the date the entity was first created (used by scheduler_jobs.py's
weekly_audit to report what's new that week) - absent on entries created
before this field existed.
"""
import difflib
import re
from datetime import datetime

import config
import vault_files
from drive_service import read_json_from_drive, update_json_file_on_drive
from logging_config import get_logger

logger = get_logger(__name__)

# Similarity ratio (difflib.SequenceMatcher) above which a name/title with
# no exact match is treated as the same entity - catches typos and minor
# spelling variants ("Attak on Titan" vs "Attack on Titan"). This is a plain
# string-similarity check, not a semantic one - it will NOT catch
# diminutives or aliases ("Vanya" vs "Ivan"), which are too dissimilar as
# strings; that class of duplicate would need an LLM or a curated alias
# list, deliberately out of scope here (see ARCHITECTURE.md).
#
# Chosen deliberately low-ish (as string-similarity thresholds go) because
# of the trailing-number guard below: short strings differing by a single
# character can score surprisingly high (e.g. "Title1" vs "Title2" is
# 0.83), which would otherwise merge genuinely distinct entities - a
# sequel, a new season, a second attempt at a project - into one. That
# specific, very real class of false positive is excluded explicitly by
# _differs_only_by_trailing_number() rather than by raising this threshold
# so high it stops catching real typos in longer titles.
FUZZY_MATCH_THRESHOLD = 0.82

_TRAILING_NUMBER_RE = re.compile(r'\s*\d+\s*$')


def _differs_only_by_trailing_number(a, b):
    """
    True if `a` and `b` are identical once a trailing number is stripped
    from each, but the original strings differ - e.g. "Thesis" vs
    "Thesis2", "Naruto 2" vs "Naruto 3", "Attack on Titan" vs "Attack
    on Titan 2". These are almost always a different season/sequel/
    revision, not a typo of the same entity, so they're excluded from the
    fuzzy match regardless of how high their raw similarity ratio is.
    """
    if a == b:
        return False
    return _TRAILING_NUMBER_RE.sub('', a).strip() == _TRAILING_NUMBER_RE.sub('', b).strip()

# Entity categories keyed by the field that identifies an entry within them.
_ENTITY_CATEGORIES = {
    "people": "name",
    "projects": "name",
    "media": "title",
}


def _empty_index():
    return {"tags": [], "people": [], "projects": [], "media": []}


def _normalize(data):
    """Coerce whatever was read back into a dict with all expected keys."""
    result = _empty_index()
    if isinstance(data, dict):
        for key in result:
            if isinstance(data.get(key), list):
                result[key] = data[key]
    return result


def read_index():
    """Read-only snapshot of the index, safe to feed into a prompt."""
    return _normalize(read_json_from_drive(vault_files.INDEX))


def find_entity(category, name):
    """
    Lookup by name/title within a category ("people"/"projects"/"media"):
    exact case-insensitive match first, falling back to a fuzzy match
    (see FUZZY_MATCH_THRESHOLD) if nothing matched exactly - catches a
    typo'd repeat mention instead of silently creating a near-duplicate
    entity. Returns the matching entry dict, or None if nothing matched
    closely enough, or the category is unknown.
    """
    key_field = _ENTITY_CATEGORIES.get(category)
    if not key_field or not name:
        return None
    needle = name.strip().lower()
    entries = read_index().get(category, [])

    for entry in entries:
        if str(entry.get(key_field, "")).strip().lower() == needle:
            return entry

    best_entry, best_ratio = None, 0.0
    for entry in entries:
        candidate = str(entry.get(key_field, "")).strip().lower()
        if not candidate or _differs_only_by_trailing_number(needle, candidate):
            continue
        ratio = difflib.SequenceMatcher(None, needle, candidate).ratio()
        if ratio > best_ratio:
            best_entry, best_ratio = entry, ratio

    if best_entry is not None and best_ratio >= FUZZY_MATCH_THRESHOLD:
        logger.info(
            f"[Index] Fuzzy-matched {name!r} to existing {category} entry "
            f"{best_entry.get(key_field)!r} (similarity={best_ratio:.2f})"
        )
        return best_entry
    return None


def add_tags(tags):
    """Add any new tags (deduplicated, case-insensitive) to the index."""
    tags = [t.strip() for t in (tags or []) if t and t.strip()]
    if not tags:
        return

    def mutate(data):
        data = _normalize(data)
        existing_lower = {t.lower() for t in data["tags"]}
        added = False
        for tag in tags:
            if tag.lower() not in existing_lower:
                data["tags"].append(tag)
                existing_lower.add(tag.lower())
                added = True
        return data if added else None

    update_json_file_on_drive(vault_files.INDEX, mutate, default_factory=_empty_index)


def upsert_entity(category, name, file_path):
    """
    Ensure an entry for `name` exists in `category` pointing at
    `file_path`. Returns (entry, created) - created is False if an entry
    with that name already existed (its file_path is left untouched, since
    the caller should already know it if it's updating an existing note).
    """
    key_field = _ENTITY_CATEGORIES.get(category)
    if not key_field:
        raise ValueError(f"Unknown entity category: {category}")

    result = {"entry": None, "created": False}

    def mutate(data):
        data = _normalize(data)
        needle = name.strip().lower()
        for entry in data[category]:
            if str(entry.get(key_field, "")).strip().lower() == needle:
                result["entry"] = entry
                result["created"] = False
                return None  # nothing to write, already present
        new_entry = {
            key_field: name.strip(),
            "file": file_path,
            "added": datetime.now(config.msk_tz).strftime("%Y-%m-%d"),
        }
        data[category].append(new_entry)
        result["entry"] = new_entry
        result["created"] = True
        return data

    update_json_file_on_drive(vault_files.INDEX, mutate, default_factory=_empty_index)
    return result["entry"], result["created"]
