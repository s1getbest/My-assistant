"""
YAML-frontmatter note templates for the vault (ARCHITECTURE.md step 2).

Defines the fixed set of note types (resource/media/person/project) and
their expected frontmatter fields, plus small render/parse helpers for
that frontmatter. Wired into the AI pipeline via ai_pipeline.save_entity_note
(the [MEDIA]/[PERSON]/[PROJECT] tags) since ARCHITECTURE.md step 3.

Frontmatter here is a deliberately simple `key: value` / `key: [a, b]`
subset - not full YAML - so we don't need to add a PyYAML dependency for
what is a handful of flat fields per note. Obsidian itself only needs
this subset to recognize frontmatter and for Dataview to query it.
"""
import re

import vault_files

# One entry per note type: which PARA folder it lives in, and the
# frontmatter fields the Archivist agent should fill in (order preserved
# for rendering). "type" is always first and always equals the dict key.
NOTE_TYPES = {
    "resource": {
        "folder": vault_files.FOLDER_RESOURCES,
        "fields": ["type", "category", "tags", "source", "created"],
    },
    "media": {
        "folder": vault_files.FOLDER_MEDIA,
        "fields": ["type", "category", "status", "rating", "date_finished", "tags"],
    },
    "person": {
        "folder": vault_files.FOLDER_PEOPLE,
        "fields": ["type", "relationship", "met_where", "met_date", "tags"],
    },
    "project": {
        "folder": vault_files.FOLDER_PROJECTS,
        "fields": ["type", "status", "deadline", "area", "tags"],
    },
}

# Allowed values for fields where the vocabulary must stay closed (keeps
# Index.json/Dataview queries meaningful - see ARCHITECTURE.md).
FIELD_ENUMS = {
    "media.category": ("anime", "movie", "series", "book", "game"),
    "media.status": ("planned", "watching", "watched", "dropped"),
    "person.relationship": ("friend", "family", "colleague", "acquaintance", "romantic"),
    "project.status": ("active", "paused", "done"),
}

_FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n?(.*)$', re.DOTALL)


def render_frontmatter(fields):
    """
    fields: dict of str -> str | list[str] | None. None/empty values are
    omitted rather than rendered as blank.
    """
    lines = ["---"]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        if isinstance(value, (list, tuple)):
            if not value:
                continue
            items = ", ".join(str(v) for v in value)
            lines.append(f"{key}: [{items}]")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


def render_note(fields, body):
    """Full note text: frontmatter block + blank line + body."""
    return f"{render_frontmatter(fields)}\n\n{(body or '').strip()}\n"


def parse_note(content):
    """
    Returns (fields: dict, body: str). fields is {} if the content has no
    frontmatter block (e.g. a legacy plain-text note).
    """
    match = _FRONTMATTER_RE.match(content or "")
    if not match:
        return {}, content or ""

    raw_fields, body = match.groups()
    fields = {}
    for line in raw_fields.split("\n"):
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            fields[key] = [v.strip() for v in value[1:-1].split(",") if v.strip()]
        else:
            fields[key] = value
    return fields, body.strip()


def is_known_type(note_type):
    return note_type in NOTE_TYPES


def folder_for_type(note_type):
    entry = NOTE_TYPES.get(note_type)
    return entry["folder"] if entry else None
