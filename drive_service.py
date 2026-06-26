import io
import hashlib
import time
import threading
import json
import re
from datetime import datetime, timedelta
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
import config

# === GOOGLE DRIVE CREDENTIALS INITIALIZATION ===
_drive_creds = None
try:
    if config.GOOGLE_TOKEN_JSON:
        token_data = json.loads(config.GOOGLE_TOKEN_JSON)
        _drive_creds = Credentials.from_authorized_user_info(
            token_data, 
            scopes=["https://www.googleapis.com/auth/drive"]
        )
        print("[Drive] Successfully authorized with Google Drive credentials!")
    else:
        print("[Drive] Warning: GOOGLE_TOKEN_JSON environment variable is empty.")
except Exception as e:
    print(f"[Drive] Error authorizing with Google Drive: {e}")


def get_drive_service():
    if _drive_creds is None:
        raise RuntimeError("Google Drive credentials not initialized.")
    return build('drive', 'v3', credentials=_drive_creds)

# === OBSIDIAN FOLDER MAPPING ===
_FOLDER_IDS = {
    "01-Daily": None,
    "02-Brain": None,
    "03-System": None
}
_FOLDER_LOCK = threading.Lock()


def _get_or_create_folder(folder_name):
    """
    Get folder ID by name within the main FOLDER_ID.
    If it doesn't exist, create it.
    """
    try:
        service = get_drive_service()
        query = f"name = '{folder_name}' and '{config.FOLDER_ID}' in parents and trashed = false and mimeType = 'application/vnd.google-apps.folder'"
        results = service.files().list(q=query, spaces='drive', fields='files(id)').execute()
        files = results.get('files', [])
        
        if files:
            folder_id = files[0]['id']
            print(f"[Drive] Found existing folder: {folder_name} (ID: {folder_id})")
            return folder_id
        
        # Create folder if it doesn't exist
        folder_metadata = {
            'name': folder_name,
            'parents': [config.FOLDER_ID],
            'mimeType': 'application/vnd.google-apps.folder'
        }
        folder = service.files().create(body=folder_metadata, fields='id').execute()
        folder_id = folder.get('id')
        print(f"[Drive] Created new folder: {folder_name} (ID: {folder_id})")
        return folder_id
    except Exception as e:
        print(f"[Drive] Error getting/creating folder {folder_name}: {e}")
        return None


def initialize_folder_mapping():
    """
    Initialize folder IDs for Obsidian structure on startup.
    """
    with _FOLDER_LOCK:
        for folder_name in _FOLDER_IDS.keys():
            _FOLDER_IDS[folder_name] = _get_or_create_folder(folder_name)
    print(f"[Drive] Folder mapping initialized: {_FOLDER_IDS}")


def _get_folder_for_file(filename):
    """
    Determine which folder a file should be stored in based on its name.
    """
    # Daily files
    if filename in ["Tasks.md", "Health.md", "Finance.md"]:
        return _FOLDER_IDS.get("01-Daily")
    # Brain files (Zettelkasten notes)
    if filename.endswith(".md") and filename not in ["Tasks.md", "Health.md", "Finance.md", "Goals.md", "Inbox.md", "Icebox.md", "Memory.md"]:
        return _FOLDER_IDS.get("02-Brain")
    # System files
    if filename in ["Inbox.md", "Flashcards.json", "Profile.json", "Goals.md", "Icebox.md", "Memory.md"]:
        return _FOLDER_IDS.get("03-System")
    # Default to main folder
    return config.FOLDER_ID


# === GOOGLE DRIVE CACHING ===
_FILE_CACHE = {}
_CACHE_TIME = {}
_CACHE_LOCK = threading.Lock()


def get_file_id_by_name(filename, folder_id=None):
    try:
        if folder_id is None:
            folder_id = _get_folder_for_file(filename)
        service = get_drive_service()
        query = f"name = '{filename}' and '{folder_id}' in parents and trashed = false"
        return files[0]['id'] if files else None
    except Exception as e:
        print(f"[Drive] Error looking up file ID for {filename}: {e}")
        return None


def read_file_from_drive(filename):
    # Check cache first
    with _CACHE_LOCK:
        if filename in _FILE_CACHE and (time.time() - _CACHE_TIME.get(filename, 0) < 300):
            return _FILE_CACHE[filename]

    last_err = None
    for attempt in range(3):
        try:
            file_id = get_file_id_by_name(filename)
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
                _FILE_CACHE[filename] = content
                _CACHE_TIME[filename] = time.time()
                
            return content
        except Exception as e:
            last_err = e
            print(f"[Drive] Read error for {filename} (attempt {attempt + 1}/3): {e}")
            if attempt < 2:
                time.sleep(1)
    print(f"[Drive] Read failed for {filename}: {last_err}")
    return ""


def write_file_to_drive(filename, content):
    last_err = None
    for attempt in range(3):
        try:
            service = get_drive_service()
            folder_id = _get_folder_for_file(filename)
            file_id = get_file_id_by_name(filename, folder_id)
            media = MediaIoBaseUpload(
                io.BytesIO(content.encode('utf-8')), 
                mimetype='text/markdown', 
                resumable=True
            )
            if file_id:
                service.files().update(fileId=file_id, media_body=media).execute()
            else:
                file_metadata = {'name': filename, 'parents': [folder_id]}
                service.files().create(body=file_metadata, media_body=media, fields='id').execute()
            
            # Forcefully update cache upon successful write
            with _CACHE_LOCK:
                _FILE_CACHE[filename] = content
                _CACHE_TIME[filename] = time.time()
            return
        except Exception as e:
            last_err = e
            print(f"[Drive] Write error for {filename} (attempt {attempt + 1}/3): {e}")
            if attempt < 2:
                time.sleep(1)
    print(f"[Drive] Write failed for {filename}: {last_err}")
    raise last_err

def read_json_from_drive(filename):
    try:
        content = read_file_from_drive(filename).strip()
        if not content:
            default_data = [] if filename == "Flashcards.json" else {}
            write_json_to_drive(filename, default_data)
            return default_data
        return json.loads(content)
    except Exception as e:
        print(f"[Drive] Read JSON error for {filename}: {e}")
        default_data = [] if filename == "Flashcards.json" else {}
        try:
            write_json_to_drive(filename, default_data)
        except Exception as write_err:
            print(f"[Drive] Failed to initialize JSON file {filename}: {write_err}")
        return default_data


def write_json_to_drive(filename, data):
    try:
        write_file_to_drive(filename, json.dumps(data, ensure_ascii=False, indent=2))
        return True
    except Exception as e:
        print(f"[Drive] Write JSON error for {filename}: {e}")
        return False


def append_line_to_drive(filename, line):
    try:
        current = read_file_from_drive(filename)
        new_content = f"{current.rstrip()}\n{line}".strip() if current.strip() else line
        write_file_to_drive(filename, new_content)
        # Forcefully update cache upon successful append (though write_file_to_drive already does)
        with _CACHE_LOCK:
            _FILE_CACHE[filename] = new_content
            _CACHE_TIME[filename] = time.time()
        return True
    except Exception as e:
        print(f"[Drive] Append error for {filename}: {e}")
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
        content = read_file_from_drive("Tasks.md")
        if not content.strip():
            return False
        lines = content.split("\n")
        filtered_lines = []
        removed = False
        for line in lines:
            if not removed and _task_line_matches(line, search_text):
                removed = True
                continue
            filtered_lines.append(line)
        if not removed:
            return False
        write_file_to_drive("Tasks.md", "\n".join(filtered_lines).strip())
        return True
    except Exception as e:
        print(f"[Drive] Delete task line error: {e}")
        return False


def edit_line_in_task_file(old_search_text, new_line_text):
    try:
        content = read_file_from_drive("Tasks.md")
        if not content.strip():
            return False
        lines = content.split("\n")
        updated = False
        for idx, line in enumerate(lines):
            if _task_line_matches(line, old_search_text):
                lines[idx] = new_line_text.strip()
                updated = True
                break
        if not updated:
            return False
        write_file_to_drive("Tasks.md", "\n".join(lines))
        return True
    except Exception as e:
        print(f"[Drive] Edit task line error: {e}")
        return False


def get_task_line_by_token(task_token):
    try:
        if not task_token:
            return None
        content = read_file_from_drive("Tasks.md")
        for line in content.split("\n"):
            if get_task_line_token(line) == task_token:
                return normalize_task_line(line)
        return None
    except Exception as e:
        print(f"[Drive] Get task by token error: {e}")
        return None


def mark_task_done_by_token(task_token):
    try:
        if not task_token:
            return None
        content = read_file_from_drive("Tasks.md")
        lines = content.split("\n")
        for idx, line in enumerate(lines):
            normalized = normalize_task_line(line)
            if "[ ]" in normalized and get_task_line_token(normalized) == task_token:
                lines[idx] = line.replace("[ ]", "[x]", 1)
                write_file_to_drive("Tasks.md", "\n".join(lines))
                return normalize_task_line(lines[idx])
        return None
    except Exception as e:
        print(f"[Drive] Mark task done by token error: {e}")
        return None


def list_markdown_files(limit=10):
    try:
        service = get_drive_service()
        query = (
            f"'{config.FOLDER_ID}' in parents and trashed = false "
            "and name contains '.md'"
        )
        results = service.files().list(
            q=query,
            spaces='drive',
            fields='files(id,name)',
            pageSize=limit,
            orderBy='modifiedTime desc'
        ).execute()
        return results.get('files', [])[:limit]
    except Exception as e:
        print(f"[Drive] Error listing markdown files: {e}")
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
        finance_content = read_file_from_drive("Finance.md")
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
        print(f"[Parser] Finance parse error: {e}")
        return 0, []


def get_sleep_chart_data():
    sleep_data, sleep_labels = [], []
    last_sleep = "—"
    try:
        health_content = read_file_from_drive("Health.md")
        health_lines = [l.strip() for l in health_content.split("\n") if l.strip()]
        if health_lines:
            last_sleep = health_lines[-1].split(":", 1)[-1].strip()
            for line in health_lines[-7:]:
                if ":" not in line:
                    continue
                date_part = line.split(":", 1)[0].replace("*", "").strip()
                val_part = line.split(":", 1)[1].strip()
                try:
                    formatted_date = datetime.strptime(date_part, "%Y-%m-%d").strftime("%d.%m")
                except ValueError:
                    formatted_date = date_part
                try:
                    sleep_data.append(float(val_part.replace(",", ".")))
                    sleep_labels.append(formatted_date)
                except ValueError:
                    pass
    except Exception as e:
        print(f"[Parser] Health parse error: {e}")
    if not sleep_data:
        sleep_data, sleep_labels = [0], ["Нет данных"]
    return sleep_data, sleep_labels, last_sleep


def get_today_tasks():
    today = datetime.now(config.msk_tz).strftime("%Y-%m-%d")
    tasks = []
    unchecked_idx = 0
    try:
        content = read_file_from_drive("Tasks.md")
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
        print(f"[Parser] Tasks parse error: {e}")
    return tasks


def get_expenses_by_category():
    categories = {}
    try:
        finance_content = read_file_from_drive("Finance.md")
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
        print(f"[Parser] Expenses by category parse error: {e}")
    return categories


def get_habit_completion_array():
    habit_data = []
    try:
        content = read_file_from_drive("Tasks.md")
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
        print(f"[Parser] Habit completion error: {e}")
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


def read_or_create_goals():
    """
    Reads Goals.md from drive. If it doesn't exist, creates it with a default template.
    """
    content = read_file_from_drive("Goals.md")
    if not content.strip():
        content = "# Мои долгосрочные цели\n\n* Улучшить здоровье и сон\n* Вести учет финансов\n* Повысить продуктивность"
        write_file_to_drive("Goals.md", content)
    return content


def get_user_profile():
    """
    Reads Profile.json from Google Drive. If it doesn't exist, initializes it.
    """
    try:
        content = read_file_from_drive("Profile.json")
        if not content.strip():
            profile = {"xp": 0, "level": 1}
            write_file_to_drive("Profile.json", json.dumps(profile))
            return profile
        return json.loads(content)
    except Exception as e:
        print(f"[Profile] Error reading profile: {e}")
        return {"xp": 0, "level": 1}


def add_user_xp(amount):
    """
    Adds XP to the user profile and calculates the new level.
    """
    try:
        profile = get_user_profile()
        profile["xp"] = profile.get("xp", 0) + amount
        profile["level"] = max(1, int(profile["xp"] / 100))
        write_file_to_drive("Profile.json", json.dumps(profile))
        print(f"[Profile] Added {amount} XP. Current XP: {profile['xp']}, Level: {profile['level']}")
        return profile
    except Exception as e:
        print(f"[Profile] Error adding XP: {e}")
        return {"xp": 0, "level": 1}
