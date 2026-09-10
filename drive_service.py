import io
import hashlib
import time
import threading
import json
import re
from datetime import datetime, timedelta
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
import config
import vault_files
from logging_config import get_logger

logger = get_logger(__name__)

# === GOOGLE DRIVE CREDENTIALS INITIALIZATION ===
_drive_creds = None
try:
    if config.GOOGLE_TOKEN_JSON:
        token_data = json.loads(config.GOOGLE_TOKEN_JSON)
        _drive_creds = Credentials.from_authorized_user_info(
            token_data,
            scopes=["https://www.googleapis.com/auth/drive"]
        )
        logger.info("[Drive] Successfully authorized with Google Drive credentials!")
    else:
        logger.warning("[Drive] Warning: GOOGLE_TOKEN_JSON environment variable is empty.")
except Exception as e:
    logger.error(f"[Drive] Error authorizing with Google Drive: {e}")


def get_drive_service():
    if _drive_creds is None:
        raise RuntimeError("Google Drive credentials not initialized.")
    return build('drive', 'v3', credentials=_drive_creds)


def _escape_drive_query_value(value):
    """
    Escapes a value for safe interpolation into a Google Drive API `q` search
    string. Drive query strings use single-quoted literals; without escaping,
    a filename/category containing a single quote (which a user can trigger
    via a note title or [NOTE] tag) could break out of the intended literal
    and alter the query.
    See: https://developers.google.com/drive/api/guides/ref-search-terms
    """
    return (value or "").replace("\\", "\\\\").replace("'", "\\'")


# === OBSIDIAN FOLDER MAPPING (PARA + Zettelkasten, see ARCHITECTURE.md) ===
_FOLDER_IDS = {name: None for name in vault_files.ALL_FOLDERS}
_FOLDER_LOCK = threading.Lock()


def _get_or_create_folder(folder_name):
    """
    Get folder ID by name within the main FOLDER_ID.
    If it doesn't exist, create it.
    """
    try:
        service = get_drive_service()
        safe_name = _escape_drive_query_value(folder_name)
        query = f"name = '{safe_name}' and '{config.FOLDER_ID}' in parents and trashed = false and mimeType = 'application/vnd.google-apps.folder'"
        results = service.files().list(q=query, spaces='drive', fields='files(id)').execute()
        files = results.get('files', [])

        if files:
            folder_id = files[0]['id']
            logger.info(f"[Drive] Found existing folder: {folder_name} (ID: {folder_id})")
            return folder_id

        # Create folder if it doesn't exist
        folder_metadata = {
            'name': folder_name,
            'parents': [config.FOLDER_ID],
            'mimeType': 'application/vnd.google-apps.folder'
        }
        folder = service.files().create(body=folder_metadata, fields='id').execute()
        folder_id = folder.get('id')
        logger.info(f"[Drive] Created new folder: {folder_name} (ID: {folder_id})")
        return folder_id
    except Exception as e:
        logger.error(f"[Drive] Error getting/creating folder {folder_name}: {e}")
        return None


def initialize_folder_mapping():
    """
    Initialize folder IDs for Obsidian structure on startup.
    """
    with _FOLDER_LOCK:
        for folder_name in _FOLDER_IDS.keys():
            _FOLDER_IDS[folder_name] = _get_or_create_folder(folder_name)
    logger.info(f"[Drive] Folder mapping initialized: {_FOLDER_IDS}")


def _get_folder_for_file(filename):
    """
    Determine which folder a file should be stored in based on its name.
    """
    if filename in vault_files.DAILY_FILES:
        return _FOLDER_IDS.get(vault_files.FOLDER_DAILY)
    if filename in vault_files.INBOX_FILES:
        return _FOLDER_IDS.get(vault_files.FOLDER_INBOX)
    if filename in vault_files.ARCHIVE_FILES:
        return _FOLDER_IDS.get(vault_files.FOLDER_ARCHIVE)
    if filename in vault_files.SYSTEM_FILES:
        return _FOLDER_IDS.get(vault_files.FOLDER_SYSTEM)
    if filename in vault_files.AREAS_FILES:
        return _FOLDER_IDS.get(vault_files.FOLDER_AREAS)
    # Freeform Zettelkasten notes created via the [NOTE] tag. Dedicated
    # routing for Media/People/Project note types lands in
    # ARCHITECTURE.md step 3 - until then everything else ends up here.
    if filename.endswith(".md"):
        return _FOLDER_IDS.get(vault_files.FOLDER_RESOURCES)
    # Default to main folder
    return config.FOLDER_ID


# === GOOGLE DRIVE CACHING ===
_FILE_CACHE = {}
_CACHE_TIME = {}
_CACHE_LOCK = threading.Lock()

# === PER-FILE LOCKS (race-condition protection) ===
# Every read-modify-write cycle against a given vault file (e.g. two Telegram
# messages arriving back-to-back, or the startup reminder-restore thread
# running while a message is being processed) must be serialized per
# filename, otherwise the second writer can silently overwrite the first
# writer's change ("lost update"). This only protects against concurrent
# access from *this* process (which is how the bot actually runs on Render -
# a single Flask/APScheduler process) - it does not protect against someone
# editing the same file directly in Obsidian at the exact same moment via
# Drive sync; that would require optimistic concurrency against Drive
# revision IDs, which is out of scope for this fix.
_FILE_LOCKS = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _get_file_lock(filename):
    with _FILE_LOCKS_GUARD:
        lock = _FILE_LOCKS.get(filename)
        if lock is None:
            lock = threading.RLock()
            _FILE_LOCKS[filename] = lock
        return lock


def get_file_id_by_name(filename, folder_id=None):
    try:
        if folder_id is None:
            folder_id = _get_folder_for_file(filename)
        service = get_drive_service()
        safe_name = _escape_drive_query_value(filename)
        query = f"name = '{safe_name}' and '{folder_id}' in parents and trashed = false"
        results = service.files().list(q=query, spaces='drive', fields='files(id)').execute()
        files = results.get('files', [])
        return files[0]['id'] if files else None
    except Exception as e:
        logger.error(f"[Drive] Error looking up file ID for {filename}: {e}")
        return None


def get_folder_id(folder_name):
    """
    Returns the Drive folder ID for a top-level PARA folder (see
    vault_files.ALL_FOLDERS), or None if folder mapping hasn't
    initialized yet / that folder failed to create.
    """
    return _FOLDER_IDS.get(folder_name)


def _is_missing_file_error(exc):
    # Google Drive returns 404 when a create/update targets a parent folder
    # ID that no longer exists - e.g. the user deleted a PARA folder (and
    # everything in it) directly in Drive, bypassing the bot entirely. Our
    # in-memory _FOLDER_IDS cache (set once at process startup) has no way
    # to know that happened on its own.
    return getattr(getattr(exc, 'resp', None), 'status', None) == 404


def _refresh_stale_folder(stale_folder_id):
    """
    If stale_folder_id is one of our tracked PARA folder IDs, re-resolve
    that folder by name - recreating it on Drive if it's gone - and update
    the cache so every future lookup (and the caller's retry) picks up the
    fresh ID. Returns the fresh ID, or None if stale_folder_id isn't one of
    ours or the recreation attempt itself failed.
    """
    with _FOLDER_LOCK:
        folder_name = next((name for name, fid in _FOLDER_IDS.items() if fid == stale_folder_id), None)
        if folder_name is None:
            return None
        fresh_id = _get_or_create_folder(folder_name)
        _FOLDER_IDS[folder_name] = fresh_id
        if fresh_id:
            logger.warning(f"[Drive] Folder '{folder_name}' (ID: {stale_folder_id}) was missing - recreated as {fresh_id}.")
        return fresh_id


def _cache_key(filename, folder_id):
    # Filenames are only unique within a folder (e.g. a Media note and a
    # Person note could coincidentally share a name) - keying the cache on
    # (folder_id, filename) instead of just filename avoids one shadowing
    # the other. folder_id is None for the common case (folder inferred
    # from the filename itself, e.g. Tasks.md), which is fine since those
    # filenames are already globally unique in the vault.
    return f"{folder_id or ''}:{filename}"


def read_file_from_drive(filename, bypass_cache=False, folder_id=None):
    """
    folder_id: explicit Drive folder ID to look in, overriding the normal
    filename-based inference (_get_folder_for_file). Needed for freeform
    entity notes (Media/People/Projects) whose filename is a user-chosen
    name and can't be mapped to a folder by name alone.
    """
    cache_key = _cache_key(filename, folder_id)
    # Check cache first (unless bypassed)
    if not bypass_cache:
        with _CACHE_LOCK:
            if cache_key in _FILE_CACHE and (time.time() - _CACHE_TIME.get(cache_key, 0) < 300):
                return _FILE_CACHE[cache_key]

    last_err = None
    for attempt in range(3):
        try:
            target_folder_id = folder_id if folder_id is not None else _get_folder_for_file(filename)
            file_id = get_file_id_by_name(filename, folder_id=target_folder_id)
            if not file_id:
                return ""
            service = get_drive_service()
            request = service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()

            content = fh.getvalue().decode('utf-8')

            # Save to cache
            with _CACHE_LOCK:
                _FILE_CACHE[cache_key] = content
                _CACHE_TIME[cache_key] = time.time()

            return content
        except Exception as e:
            last_err = e
            logger.warning(f"[Drive] Read error for {filename} (attempt {attempt + 1}/3): {e}")
            if attempt < 2:
                time.sleep(1)
    logger.error(f"[Drive] Read failed for {filename}: {last_err}")
    return ""


def write_file_to_drive(filename, content, folder_id=None):
    """
    folder_id: see read_file_from_drive - explicit override for freeform
    entity notes that can't be routed by filename alone.
    """
    last_err = None
    for attempt in range(3):
        try:
            service = get_drive_service()
            target_folder_id = folder_id if folder_id is not None else _get_folder_for_file(filename)
            file_id = get_file_id_by_name(filename, target_folder_id)
            media = MediaIoBaseUpload(
                io.BytesIO(content.encode('utf-8')),
                mimetype='text/markdown',
                resumable=True
            )
            if file_id:
                service.files().update(fileId=file_id, media_body=media).execute()
            else:
                file_metadata = {'name': filename, 'parents': [target_folder_id]}
                service.files().create(body=file_metadata, media_body=media, fields='id').execute()

            # Forcefully update cache upon successful write
            cache_key = _cache_key(filename, folder_id)
            with _CACHE_LOCK:
                _FILE_CACHE[cache_key] = content
                _CACHE_TIME[cache_key] = time.time()
            return
        except Exception as e:
            last_err = e
            # A PARA folder deleted directly in Drive (not through the bot)
            # leaves target_folder_id pointing at nothing - recreate it and
            # retry immediately instead of failing every write until the
            # process happens to restart.
            if _is_missing_file_error(e):
                fresh_id = _refresh_stale_folder(target_folder_id)
                if fresh_id:
                    folder_id = fresh_id
            logger.warning(f"[Drive] Write error for {filename} (attempt {attempt + 1}/3): {e}")
            if attempt < 2:
                time.sleep(1)
    logger.error(f"[Drive] Write failed for {filename}: {last_err}")
    raise last_err


def read_json_from_drive(filename, bypass_cache=False):
    try:
        content = read_file_from_drive(filename, bypass_cache=bypass_cache).strip()
        if not content:
            default_data = [] if filename == vault_files.FLASHCARDS else {}
            write_json_to_drive(filename, default_data)
            return default_data
        return json.loads(content)
    except Exception as e:
        logger.error(f"[Drive] Read JSON error for {filename}: {e}")
        default_data = [] if filename == vault_files.FLASHCARDS else {}
        try:
            write_json_to_drive(filename, default_data)
        except Exception as write_err:
            logger.error(f"[Drive] Failed to initialize JSON file {filename}: {write_err}")
        return default_data


def write_json_to_drive(filename, data):
    try:
        write_file_to_drive(filename, json.dumps(data, ensure_ascii=False, indent=2))
        return True
    except Exception as e:
        logger.error(f"[Drive] Write JSON error for {filename}: {e}")
        return False


def update_file_on_drive(filename, mutate_fn, folder_id=None):
    """
    Atomically read-modify-write a text file on Drive.

    `mutate_fn(current_content: str) -> str | None` receives the freshest
    content (cache bypassed) and returns the new content to write, or None
    to abort without writing. The whole read+mutate+write cycle runs under a
    per-filename lock so two concurrent callers (e.g. a Telegram message
    handler and the startup reminder-restore thread) cannot interleave and
    lose one of the updates.

    folder_id: see read_file_from_drive - explicit override for freeform
    entity notes. Note the lock is still keyed on `filename` alone (not
    filename+folder_id) - two different entity types happening to pick the
    same note name is an edge case not worth a bigger lock key for now.

    Returns the new content that was written, or None if aborted/failed.
    """
    lock = _get_file_lock(filename)
    with lock:
        try:
            current = read_file_from_drive(filename, bypass_cache=True, folder_id=folder_id)
            new_content = mutate_fn(current)
            if new_content is None:
                return None
            write_file_to_drive(filename, new_content, folder_id=folder_id)
            return new_content
        except Exception as e:
            logger.error(f"[Drive] update_file_on_drive failed for {filename}: {e}")
            return None


def update_json_file_on_drive(filename, mutate_fn, default_factory=None):
    """
    Atomically read-modify-write a JSON file on Drive under the same
    per-filename lock used by update_file_on_drive.

    `mutate_fn(current_data) -> new_data | None` receives the freshest parsed
    JSON (list/dict) and returns the data to persist, or None to abort.
    """
    lock = _get_file_lock(filename)
    with lock:
        try:
            data = read_json_from_drive(filename, bypass_cache=True)
            if data is None and default_factory is not None:
                data = default_factory()
            new_data = mutate_fn(data)
            if new_data is None:
                return None
            write_json_to_drive(filename, new_data)
            return new_data
        except Exception as e:
            logger.error(f"[Drive] update_json_file_on_drive failed for {filename}: {e}")
            return None


def append_line_to_drive(filename, line):
    try:
        def mutate(current):
            return f"{current.rstrip()}\n{line}".strip() if current.strip() else line

        result = update_file_on_drive(filename, mutate)
        return result is not None
    except Exception as e:
        logger.error(f"[Drive] Append error for {filename}: {e}")
        return False


def normalize_task_line(task_line):
    return (task_line or "").strip()


def get_task_line_token(task_line):
    normalized = normalize_task_line(task_line)
    if not normalized:
        return None
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()[:16]


def _task_line_matches(line, search_text):
    normalized_line = normalize_task_line(line)
    normalized_search = normalize_task_line(search_text)
    if not normalized_line or not normalized_search:
        return False
    return (
        normalized_line == normalized_search
        or normalized_line.endswith(normalized_search)
    )


def delete_line_from_task_file(search_text):
    try:
        def mutate(content):
            if not content.strip():
                return None
            lines = content.split("\n")
            filtered_lines = []
            removed = False
            for line in lines:
                if not removed and _task_line_matches(line, search_text):
                    removed = True
                    continue
                filtered_lines.append(line)
            if not removed:
                return None
            return "\n".join(filtered_lines).strip()

        result = update_file_on_drive(vault_files.TASKS, mutate)
        return result is not None
    except Exception as e:
        logger.error(f"[Drive] Delete task line error: {e}")
        return False


def edit_line_in_task_file(old_search_text, new_line_text):
    try:
        def mutate(content):
            if not content.strip():
                return None
            lines = content.split("\n")
            updated = False
            for idx, line in enumerate(lines):
                if _task_line_matches(line, old_search_text):
                    lines[idx] = new_line_text.strip()
                    updated = True
                    break
            if not updated:
                return None
            return "\n".join(lines)

        result = update_file_on_drive(vault_files.TASKS, mutate)
        return result is not None
    except Exception as e:
        logger.error(f"[Drive] Edit task line error: {e}")
        return False


def get_task_line_by_token(task_token):
    try:
        if not task_token:
            return None
        content = read_file_from_drive(vault_files.TASKS)
        for line in content.split("\n"):
            if get_task_line_token(line) == task_token:
                return normalize_task_line(line)
        return None
    except Exception as e:
        logger.error(f"[Drive] Get task by token error: {e}")
        return None


def mark_task_done_by_token(task_token):
    try:
        if not task_token:
            return None
        found_line = {"value": None}

        def mutate(content):
            lines = content.split("\n")
            for idx, line in enumerate(lines):
                normalized = normalize_task_line(line)
                if "[ ]" in normalized and get_task_line_token(normalized) == task_token:
                    lines[idx] = line.replace("[ ]", "[x]", 1)
                    found_line["value"] = normalize_task_line(lines[idx])
                    return "\n".join(lines)
            return None

        result = update_file_on_drive(vault_files.TASKS, mutate)
        # Only report the updated line back if the write actually succeeded -
        # `result` is None both when the task wasn't found and when the
        # Drive write itself failed after mutate() ran.
        return found_line["value"] if result is not None else None
    except Exception as e:
        logger.error(f"[Drive] Mark task done by token error: {e}")
        return None


def list_markdown_files(limit=10):
    """
    Lists the most recently modified .md files across the whole vault.

    Bug fix: this used to only query files with config.FOLDER_ID (the
    vault root) as a direct parent - but every markdown file lives inside
    one of the PARA subfolders (01-Daily, 04-Resources, ...), never in the
    root itself, so this returned nothing and /search was effectively
    non-functional. Now queries across every known subfolder in one
    request via Drive's boolean query syntax.
    """
    try:
        service = get_drive_service()
        folder_ids = [fid for fid in _FOLDER_IDS.values() if fid]
        if not folder_ids:
            # Folder mapping hasn't initialized yet (or every folder
            # failed to create) - fall back to the root so this doesn't
            # silently return nothing.
            folder_ids = [config.FOLDER_ID]
        parent_clause = " or ".join(f"'{fid}' in parents" for fid in folder_ids)
        query = f"trashed = false and name contains '.md' and ({parent_clause})"
        results = service.files().list(
            q=query,
            spaces='drive',
            fields='files(id,name)',
            pageSize=limit,
            orderBy='modifiedTime desc'
        ).execute()
        return results.get('files', [])[:limit]
    except Exception as e:
        logger.error(f"[Drive] Error listing markdown files: {e}")
        return []


# === DATA PARSING AND RETRIEVAL FUNCTIONS ===

TASK_TIME_RE = re.compile(r'(\d{2}:\d{2})\s*\|\s*(.+)$')


def parse_finance_amount(line):
    if ":" not in line:
        return 0
    val_part = line.split(":", 1)[1].strip()
    num_part = val_part.split("|")[0].strip()
    num_part = re.sub(r'[₽рруб\s]', '', num_part, flags=re.IGNORECASE)
    try:
        return int(float(num_part.replace(",", ".")))
    except ValueError:
        return 0


def get_monthly_expenses():
    try:
        finance_content = read_file_from_drive(vault_files.FINANCE)
        current_month = datetime.now(config.msk_tz).strftime("%Y-%m")
        total = 0
        recent = []
        for line in finance_content.split("\n"):
            line = line.strip()
            if not line or ":" not in line:
                continue
            date_part = line.split(":", 1)[0].replace("*", "").strip()
            if not date_part.startswith(current_month):
                continue
            amount = parse_finance_amount(line)
            total += amount
            val_part = line.split(":", 1)[1].strip()
            parts = [p.strip() for p in val_part.split("|")]
            recent.append({
                "date": date_part,
                "amount": amount,
                "category": parts[1] if len(parts) > 2 else (parts[1] if len(parts) > 1 else "—"),
                "description": parts[-1] if parts else "—",
            })
        return total, recent[-8:][::-1]
    except Exception as e:
        logger.error(f"[Parser] Finance parse error: {e}")
        return 0, []


# Health.md mixes several kinds of entries, all written as
# "* YYYY-MM-DD: <label> <value>" - each key here is the label (matched
# case-insensitively at the start of the value part), mapped to the
# entry_type _parse_health_line returns for it. Sleep hours are the
# exception: no label at all ("* YYYY-MM-DD: 7.5"), for backwards
# compatibility with every sleep entry ever written before other metrics
# existed - handled as the fallback in _parse_health_line, not listed here.
_HEALTH_LABELED_TYPES = {
    "mood": "mood",
    "steps": "steps",
    "hr": "heart_rate",
    "stress": "stress",
    "distance": "distance",
    "calories": "calories",
}


# Matches "YYYY-MM-DD: <rest of line>" anywhere within a line - used
# instead of "split on the first colon and strip a specific bullet
# character" so _parse_health_line doesn't care what (if anything)
# precedes the date: "* ", "- ", "  - " (indented), "• ", "1. ", or
# nothing at all. A bulk historical import can arrive formatted by a
# notes app or another AI session using any of these list-marker
# conventions, and a mismatch here isn't a loud error - it's a
# date_part that quietly fails every exact-match date comparison
# downstream (has_health_entry_for_date, merge_health_lines' dedup) while
# still "parsing" as something.
_HEALTH_LINE_RE = re.compile(r'(\d{4}-\d{2}-\d{2})\s*:\s*(.+)$')


def _parse_health_line(line):
    """
    Returns (date_part, entry_type, value) for one Health.md line, where
    entry_type is one of "sleep"/"mood"/"steps"/"heart_rate"/"stress"/
    "distance"/"calories", or None if the line doesn't match any
    recognized shape. A trailing
    "/N" (as in "Mood 8/10" or "Stress 3/10") is stripped before parsing
    the number, so labels that use a fixed 1-10 scale don't need any
    special-casing here.
    """
    line = line.strip()
    if not line:
        return None
    match = _HEALTH_LINE_RE.search(line)
    if not match:
        return None
    date_part = match.group(1)
    val_part = match.group(2).strip()
    lowered = val_part.lower()
    for label, entry_type in _HEALTH_LABELED_TYPES.items():
        if lowered.startswith(label):
            score_str = val_part[len(label):].strip().split("/")[0].strip()
            try:
                return date_part, entry_type, float(score_str.replace(",", "."))
            except ValueError:
                return None
    try:
        return date_part, "sleep", float(val_part.replace(",", "."))
    except ValueError:
        return None


def _get_health_series(entry_type, limit=7):
    """
    Returns (values, labels, last_value_str) for the last `limit` entries
    of the given type in Health.md, in chronological order - filtering by
    type first, unlike a naive "last N lines" which would mix different
    entry types together (see _parse_health_line).
    """
    values, labels = [], []
    last_value_str = "—"
    try:
        health_content = read_file_from_drive(vault_files.HEALTH)
        parsed = [_parse_health_line(line) for line in health_content.split("\n")]
        matching = [p for p in parsed if p and p[1] == entry_type]
        if matching:
            last_value_str = str(matching[-1][2])
            for date_part, _, value in matching[-limit:]:
                try:
                    label = datetime.strptime(date_part, "%Y-%m-%d").strftime("%d.%m")
                except ValueError:
                    label = date_part
                values.append(value)
                labels.append(label)
    except Exception as e:
        logger.error(f"[Parser] Health parse error ({entry_type}): {e}")
    if not values:
        values, labels = [0], ["Нет данных"]
    return values, labels, last_value_str


def get_sleep_chart_data():
    return _get_health_series("sleep")


def get_mood_chart_data():
    return _get_health_series("mood")


def get_steps_chart_data():
    return _get_health_series("steps")


def get_heart_rate_chart_data():
    return _get_health_series("heart_rate")


def get_stress_chart_data():
    return _get_health_series("stress")


def get_distance_chart_data():
    return _get_health_series("distance")


def get_calories_chart_data():
    return _get_health_series("calories")


def merge_health_lines(existing_content, new_lines_text):
    """
    Merges freeform Health.md-formatted lines (new_lines_text - one entry
    per line, same "* YYYY-MM-DD: [Label] value" shape _parse_health_line
    understands) into existing_content, for bulk-backfilling historical
    data (e.g. a fitness-app export reformatted into this shape by another
    AI session with a large context window, then sent here as a file via
    /import_health - see bot_handlers.py).

    For a (date, entry_type) key already present in existing_content, the
    new line REPLACES the old one in place (last value wins - reimporting
    after fixing a typo in the source data just overwrites, it doesn't
    duplicate); anything new is appended. Every untouched existing line is
    preserved byte-for-byte rather than the whole file being reformatted
    from re-parsed (date, type, value) tuples, which would silently drop
    formatting a round-trip through the parser can't reconstruct (e.g. the
    "/10" in "Mood 8/10" - _parse_health_line only returns the 8.0).

    Returns (merged_content, stats) where stats is
    {"added": n, "updated": n, "skipped": n} - "skipped" counts lines in
    new_lines_text that don't parse as a valid Health.md entry at all
    (wrong shape, unrecognized label, non-numeric value, ...), so the
    caller can tell the user if part of their import silently didn't
    match rather than claiming full success.
    """
    existing_lines = existing_content.split("\n") if existing_content.strip() else []
    key_to_index = {}
    for i, line in enumerate(existing_lines):
        parsed = _parse_health_line(line)
        if parsed:
            key_to_index[(parsed[0], parsed[1])] = i

    stats = {"added": 0, "updated": 0, "skipped": 0}
    for raw_line in new_lines_text.split("\n"):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        parsed = _parse_health_line(raw_line)
        if not parsed:
            stats["skipped"] += 1
            continue
        key = (parsed[0], parsed[1])
        # Rebuild as a clean "* YYYY-MM-DD: <rest>" line rather than
        # keeping whatever preceded the date in the source (a raw "-",
        # "• ", "1. ", ...) - _HEALTH_LINE_RE match is reused here so the
        # stored line always looks the same regardless of which list-marker
        # convention the import happened to use.
        match = _HEALTH_LINE_RE.search(raw_line)
        normalized_line = f"* {match.group(1)}: {match.group(2).strip()}"
        if key in key_to_index:
            existing_lines[key_to_index[key]] = normalized_line
            stats["updated"] += 1
        else:
            existing_lines.append(normalized_line)
            key_to_index[key] = len(existing_lines) - 1
            stats["added"] += 1

    return "\n".join(existing_lines), stats


def has_health_entry_for_date(entry_type, date_str):
    """
    True if Health.md has an entry of the given type (see
    _HEALTH_LABELED_TYPES, plus "sleep") for date_str (YYYY-MM-DD). Used by
    scheduler_jobs.check_daily_sleep()
    instead of a naive `date_str in health_content` substring check - which
    would wrongly count a same-day *mood* entry (e.g. from an early
    /journal) as "sleep logged", since both entry types' lines start with
    the identical date string. That was a real instance of it: the 10:00
    "did you log your sleep?" ping would silently never fire on a day the
    user journaled before 10:00 but hadn't actually logged sleep yet.
    """
    health_content = read_file_from_drive(vault_files.HEALTH)
    for line in health_content.split("\n"):
        parsed = _parse_health_line(line)
        if parsed and parsed[0] == date_str and parsed[1] == entry_type:
            return True
    return False


def get_today_tasks():
    today = datetime.now(config.msk_tz).strftime("%Y-%m-%d")
    tasks = []
    unchecked_idx = 0
    try:
        content = read_file_from_drive(vault_files.TASKS)
        for line in content.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            is_open = "[ ]" in stripped
            is_done = "[x]" in stripped.lower()
            task_idx = None
            if is_open:
                task_idx = unchecked_idx
                unchecked_idx += 1
            if today not in stripped:
                continue
            m = TASK_TIME_RE.search(stripped)
            time_str = m.group(1) if m else "—"
            text = m.group(2).strip() if m else re.sub(r'^[\*\-\s]*\[[ xX]\]\s*', '', stripped)
            text = re.sub(r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s*\|\s*', '', text).strip()
            tasks.append({
                "time": time_str,
                "text": text or stripped,
                "done": is_done,
                "idx": task_idx,
            })
        tasks.sort(key=lambda t: t["time"])
    except Exception as e:
        logger.error(f"[Parser] Tasks parse error: {e}")
    return tasks


def get_expenses_by_category():
    categories = {}
    try:
        finance_content = read_file_from_drive(vault_files.FINANCE)
        current_month = datetime.now(config.msk_tz).strftime("%Y-%m")
        for line in finance_content.split("\n"):
            line = line.strip()
            if not line or ":" not in line:
                continue
            date_part = line.split(":", 1)[0].replace("*", "").strip()
            if not date_part.startswith(current_month):
                continue
            amount = parse_finance_amount(line)
            val_part = line.split(":", 1)[1].strip()
            parts = [p.strip() for p in val_part.split("|")]
            cat = parts[1] if len(parts) > 2 else (parts[1] if len(parts) > 1 else "Разное")
            if not cat:
                cat = "Разное"
            categories[cat] = categories.get(cat, 0) + amount
    except Exception as e:
        logger.error(f"[Parser] Expenses by category parse error: {e}")
    return categories


def get_habit_completion_array():
    habit_data = []
    try:
        content = read_file_from_drive(vault_files.TASKS)
        lines = content.split("\n")

        routine_keywords = [
            "routine", "habit", "зарядка", "тренировка", "медитация", "чтение",
            "планирование", "workout", "english", "брифинг", "витамины", "вода",
            "спорт", "read", "meditate", "уборка", "чистить зубы", "прогулка", "study"
        ]

        today = datetime.now(config.msk_tz)
        for i in range(13, -1, -1):
            day = today - timedelta(days=i)
            day_str = day.strftime("%Y-%m-%d")
            day_label = day.strftime("%d.%m")

            total_routines = 0
            done_routines = 0

            for line in lines:
                stripped = line.strip()
                if not stripped or day_str not in stripped:
                    continue

                is_routine = any(kw in stripped.lower() for kw in routine_keywords)
                if is_routine:
                    total_routines += 1
                    if "[x]" in stripped.lower():
                        done_routines += 1

            if total_routines == 0:
                for line in lines:
                    stripped = line.strip()
                    if not stripped or day_str not in stripped:
                        continue
                    if "[ ]" in stripped or "[x]" in stripped.lower():
                        total_routines += 1
                        if "[x]" in stripped.lower():
                            done_routines += 1

            completed = False
            if total_routines > 0:
                completed = (done_routines / total_routines) >= 0.5

            habit_data.append({
                "date": day_str,
                "label": day_label,
                "total": total_routines,
                "done": done_routines,
                "completed": completed
            })
    except Exception as e:
        logger.error(f"[Parser] Habit completion error: {e}")
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
    return habit_data


# Seeded into Goals.md the first time it's ever read, before the user has
# stated any real goal. Exposed here (rather than inlined below) so
# ai_pipeline.append_goal() can recognize it and replace it outright once a
# real goal comes in, instead of leaving this generic filler sitting
# alongside (and getting equal weight to) an actual stated goal forever.
DEFAULT_GOALS_CONTENT = "# Мои долгосрочные цели\n\n* Улучшить здоровье и сон\n* Вести учет финансов\n* Повысить продуктивность"


def read_or_create_goals():
    """
    Reads Goals.md from drive. If it doesn't exist, creates it with a default template.
    """
    content = read_file_from_drive(vault_files.GOALS)
    if not content.strip():
        content = DEFAULT_GOALS_CONTENT
        write_file_to_drive(vault_files.GOALS, content)
    return content


def get_user_profile():
    """
    Reads Profile.json from Google Drive. If it doesn't exist, initializes it.
    """
    try:
        content = read_file_from_drive(vault_files.PROFILE)
        if not content.strip():
            profile = {"xp": 0, "level": 1}
            write_file_to_drive(vault_files.PROFILE, json.dumps(profile))
            return profile
        return json.loads(content)
    except Exception as e:
        logger.error(f"[Profile] Error reading profile: {e}")
        return {"xp": 0, "level": 1}


def add_user_xp(amount):
    """
    Adds XP to the user profile and calculates the new level.
    """
    try:
        def mutate(profile):
            if not isinstance(profile, dict):
                profile = {"xp": 0, "level": 1}
            profile["xp"] = profile.get("xp", 0) + amount
            profile["level"] = max(1, int(profile["xp"] / 100))
            return profile

        result = update_json_file_on_drive(vault_files.PROFILE, mutate, default_factory=lambda: {"xp": 0, "level": 1})
        if result is None:
            result = {"xp": 0, "level": 1}
        logger.info(f"[Profile] Added {amount} XP. Current XP: {result.get('xp')}, Level: {result.get('level')}")
        return result
    except Exception as e:
        logger.error(f"[Profile] Error adding XP: {e}")
        return {"xp": 0, "level": 1}
