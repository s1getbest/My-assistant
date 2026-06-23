import io
import time
import threading
import json
import re
from datetime import datetime
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
        print("[Drive] Successfully authorized with Google Drive!")
    else:
        print("[Drive] Warning: GOOGLE_TOKEN_JSON environment variable is empty.")
except Exception as e:
    print(f"[Drive] Error authorizing with Google Drive: {e}")


def get_drive_service():
    if _drive_creds is None:
        raise RuntimeError("Google Drive credentials not initialized.")
    return build('drive', 'v3', credentials=_drive_creds)


# === GOOGLE DRIVE CACHING ===
_FILE_CACHE = {}
_CACHE_TIME = {}
_CACHE_LOCK = threading.Lock()


def get_file_id_by_name(filename):
    try:
        service = get_drive_service()
        query = f"name = '{filename}' and '{config.FOLDER_ID}' in parents and trashed = false"
        results = service.files().list(q=query, spaces='drive', fields='files(id)').execute()
        files = results.get('files', [])
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
            file_id = get_file_id_by_name(filename)
            media = MediaIoBaseUpload(
                io.BytesIO(content.encode('utf-8')), 
                mimetype='text/markdown', 
                resumable=True
            )
            if file_id:
                service.files().update(fileId=file_id, media_body=media).execute()
            else:
                file_metadata = {'name': filename, 'parents': [config.FOLDER_ID]}
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
