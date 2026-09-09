"""
Filenames used in the Obsidian vault stored on Google Drive.

Centralizing these avoids the same string literal ("Tasks.md", "Health.md", ...)
being duplicated across drive_service.py, bot_handlers.py, scheduler_jobs.py and
dashboard.py, where a typo in one place would silently create a second,
divergent file instead of raising an error.
"""

TASKS = "Tasks.md"
HEALTH = "Health.md"
FINANCE = "Finance.md"
MEMORY = "Memory.md"
GOALS = "Goals.md"
INBOX = "Inbox.md"
ICEBOX = "Icebox.md"
RAW_INBOX = "Raw_Inbox.md"
QUESTIONS = "Questions.md"
FLASHCARDS = "Flashcards.json"
PROFILE = "Profile.json"

# Files that live in the "01-Daily" Obsidian folder.
DAILY_FILES = (TASKS, HEALTH, FINANCE)

# Files that live in the "03-System" Obsidian folder.
SYSTEM_FILES = (INBOX, FLASHCARDS, PROFILE, GOALS, ICEBOX, MEMORY)
