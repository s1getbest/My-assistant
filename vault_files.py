"""
Filenames and folder names used in the Obsidian vault stored on Google Drive.

Centralizing these avoids the same string literal ("Tasks.md", "Health.md", ...)
being duplicated across drive_service.py, bot_handlers.py, scheduler_jobs.py and
dashboard.py, where a typo in one place would silently create a second,
divergent file instead of raising an error.

Folder layout follows a PARA + Zettelkasten hybrid (see ARCHITECTURE.md):
  00-Inbox      - unprocessed raw input
  01-Daily      - Tasks.md / Health.md / Finance.md / Journal.md / Location.md
  02-Projects   - one subfolder per project with a deadline
  03-Areas      - ongoing areas of life without a deadline (e.g. university)
  04-Resources  - Zettelkasten notes (knowledge, articles, ideas)
  05-Media      - watched/read/played media entries
  06-People     - person cards
  07-Archive    - closed/stale items (e.g. Icebox.md)
  08-System     - Profile.json, Flashcards.json, Memory.md, Questions.md, Index.json
"""

# === FILENAMES ===
TASKS = "Tasks.md"
HEALTH = "Health.md"
FINANCE = "Finance.md"
JOURNAL = "Journal.md"
LOCATION = "Location.md"
MEMORY = "Memory.md"
GOALS = "Goals.md"
INBOX = "Inbox.md"
ICEBOX = "Icebox.md"
RAW_INBOX = "Raw_Inbox.md"
QUESTIONS = "Questions.md"
FLASHCARDS = "Flashcards.json"
PROFILE = "Profile.json"
INDEX = "Index.json"  # entity/tag index - see ARCHITECTURE.md step 2, not populated yet
SCHEDULE = "Расписание.md"  # university weekly schedule - see university_schedule.py / ARCHITECTURE.md step 6

# === FOLDER NAMES (top-level, under config.FOLDER_ID) ===
FOLDER_INBOX = "00-Inbox"
FOLDER_DAILY = "01-Daily"
FOLDER_PROJECTS = "02-Projects"
FOLDER_AREAS = "03-Areas"
FOLDER_RESOURCES = "04-Resources"
FOLDER_MEDIA = "05-Media"
FOLDER_PEOPLE = "06-People"
FOLDER_ARCHIVE = "07-Archive"
FOLDER_SYSTEM = "08-System"

ALL_FOLDERS = (
    FOLDER_INBOX,
    FOLDER_DAILY,
    FOLDER_PROJECTS,
    FOLDER_AREAS,
    FOLDER_RESOURCES,
    FOLDER_MEDIA,
    FOLDER_PEOPLE,
    FOLDER_ARCHIVE,
    FOLDER_SYSTEM,
)

# === WHICH FILES LIVE IN WHICH TOP-LEVEL FOLDER ===
# Files that live in "01-Daily".
DAILY_FILES = (TASKS, HEALTH, FINANCE, JOURNAL, LOCATION)

# Files that live in "00-Inbox" - unprocessed input, not yet triaged.
INBOX_FILES = (INBOX, RAW_INBOX, QUESTIONS)

# Files that live in "07-Archive".
ARCHIVE_FILES = (ICEBOX,)

# Files that live in "08-System".
SYSTEM_FILES = (FLASHCARDS, PROFILE, GOALS, MEMORY, INDEX)

# Files that live in "03-Areas". ARCHITECTURE.md originally sketched
# Расписание.md as living in a nested "03-Areas/Учёба/" subfolder, but
# _get_or_create_folder only creates folders directly under config.FOLDER_ID
# (no nested-folder support yet) - simplified to live directly in 03-Areas
# for this one file rather than building nested-folder creation for it.
AREAS_FILES = (SCHEDULE,)
