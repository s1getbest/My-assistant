"""
Index.json - a lightweight registry of known tags/people/projects/media so
the Archivist agent (ARCHITECTURE.md step 3) can reuse existing entities
and tags instead of creating duplicates (e.g. a second card for the same
person, or "Продуктивность" and "productivity" as two different tags).

Not wired into the AI pipeline yet - these are just the read/update
primitives, built on top of drive_service's existing atomic
update_json_file_on_drive (per-file locking already handles concurrent
writers, see ARCHITECTURE.md step 2 / the earlier security-hardening
session).

Schema (08-System/Index.json):
{
  "tags": ["health", "study", ...],
  "people": [{"name": "...", "file": "06-People/....md"}],
  "projects": [{"name": "...", "file": "02-Projects/..."}],
  "media": [{"title": "...", "file": "05-Media/....md"}]
}
"""
import vault_files
from drive_service import read_json_from_drive, update_json_file_on_drive
from logging_config import get_logger

logger = get_logger(__name__)

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
    Case-insensitive lookup by name/title within a category
    ("people"/"projects"/"media"). Returns the matching entry dict, or
    None if not found or the category is unknown.
    """
    key_field = _ENTITY_CATEGORIES.get(category)
    if not key_field or not name:
        return None
    needle = name.strip().lower()
    for entry in read_index().get(category, []):
        if str(entry.get(key_field, "")).strip().lower() == needle:
            return entry
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
        new_entry = {key_field: name.strip(), "file": file_path}
        data[category].append(new_entry)
        result["entry"] = new_entry
        result["created"] = True
        return data

    update_json_file_on_drive(vault_files.INDEX, mutate, default_factory=_empty_index)
    return result["entry"], result["created"]
