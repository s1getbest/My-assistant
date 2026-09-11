"""
University weekly schedule (ARCHITECTURE.md step 6).

The schedule is entered by editing Schedule.md directly in Drive (no bot
command for it - see ARCHITECTURE.md 8.19: a single-user vault, edited by
hand for something that changes once a semester, is simpler than
maintaining a dedicated command for it). It's not scraped from the
university's website either - a parsing mistake here is costly (wrong
day/time for a class), so a strict, deterministically-parseable format is
used instead of freeform AI recognition.

Expected format of Schedule.md:

    ## Odd
    Mon: 09:00 Calculus; 10:40 Physics
    Tue: 12:20 English
    ...
    ## Even
    Mon: 09:00 History
    ...

- Section headers start with "odd" (any case) for the odd week, anything
  else starting with a day-section marker for the even week.
- Day lines start with a 3-letter (or longer) English day abbreviation
  (Mon/Tue/Wed/Thu/Fri/Sat/Sun) followed by ":". Multiple classes on one
  day are separated by ";", each as "HH:MM Subject". Teacher/room can be
  included directly in the subject text, e.g. "Calculus (Dr. Smith, room
  305)" - parse_day_classes treats it as free text either way.
- A day with no line simply has no classes that week.

Week parity for a given calendar date is computed from a semester anchor
(the Monday of a known week-1) rather than parsed from the website, since
the user enters/updates the schedule manually (see ARCHITECTURE.md).
Update SEMESTER_ANCHOR_MONDAY/SEMESTER_ANCHOR_PARITY at the start of each
new semester.
"""
import re
from datetime import date, timedelta

import vault_files
from drive_service import read_file_from_drive, write_file_to_drive
from logging_config import get_logger

logger = get_logger(__name__)

# Monday of "week 1" for parity-counting purposes, and that week's parity.
# 2026-08-31 (Monday) was a public holiday with no classes, but it's still
# the Monday of week 1 - actual first classes were Tuesday 2026-09-01,
# which falls in that same week 1 = odd.
SEMESTER_ANCHOR_MONDAY = date(2026, 8, 31)
SEMESTER_ANCHOR_PARITY = "odd"

_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]  # index 0 = Monday, matches date.weekday()

_SECTION_RE = re.compile(r'^##\s*(\S+)', re.MULTILINE)


def get_week_parity(for_date):
    """Returns "odd" or "even" for the ISO week containing `for_date`."""
    monday = for_date - timedelta(days=for_date.weekday())
    weeks_since_anchor = (monday - SEMESTER_ANCHOR_MONDAY).days // 7
    same_as_anchor = weeks_since_anchor % 2 == 0
    if same_as_anchor:
        return SEMESTER_ANCHOR_PARITY
    return "even" if SEMESTER_ANCHOR_PARITY == "odd" else "odd"


def split_sections(text):
    """
    Splits the raw schedule text into {"odd": section_text, "even": section_text}
    by "## ..." headers. A header is treated as "odd" if it starts with "odd"
    (case-insensitive), otherwise "even". Missing sections are simply
    absent from the returned dict. Returns {} if no "## " headers are found
    at all (malformed input).
    """
    sections = {}
    matches = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(matches):
        label = m.group(1).strip().lower()
        parity = "odd" if label.startswith("odd") else "even"
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[parity] = text[start:end].strip()
    return sections


def parse_day_classes(section_text, day_index):
    """
    Returns a list of (time_str, subject) tuples for `day_index`
    (0=Monday..6=Sunday) within one week-section's raw text, or [] if that
    day has no matching line or the section is empty.
    """
    if not section_text or not (0 <= day_index < len(_DAY_NAMES)):
        return []
    day_name = _DAY_NAMES[day_index]
    day_line_re = re.compile(rf'^{day_name}\S*\s*:\s*(.+)$', re.IGNORECASE | re.MULTILINE)
    match = day_line_re.search(section_text)
    if not match:
        return []

    entries = []
    for part in match.group(1).split(";"):
        part = part.strip()
        if not part:
            continue
        time_match = re.match(r'^(\d{1,2}:\d{2})\s+(.+)$', part)
        if time_match:
            entries.append((time_match.group(1), time_match.group(2).strip()))
        else:
            entries.append(("—", part))
    return entries


def save_schedule(raw_text):
    """
    Overwrites Schedule.md wholesale - see module docstring on why no
    merging. Folder routing (03-Areas) is resolved automatically by
    drive_service via vault_files.AREAS_FILES. Kept for any future
    programmatic caller; the bot itself no longer has a command that
    calls this (see ARCHITECTURE.md 8.19) - the user edits Schedule.md
    directly in Drive.
    """
    write_file_to_drive(vault_files.SCHEDULE, raw_text.strip() + "\n")


def read_schedule():
    return read_file_from_drive(vault_files.SCHEDULE)


def get_classes_for_date(for_date):
    """
    Returns [(time_str, subject), ...] for `for_date`, sorted by time,
    using the current Schedule.md content and the correct week-parity
    section. Returns [] if nothing is scheduled or the file is empty/unparseable.
    """
    raw = read_schedule()
    if not raw.strip():
        return []
    sections = split_sections(raw)
    parity = get_week_parity(for_date)
    section_text = sections.get(parity, "")
    classes = parse_day_classes(section_text, for_date.weekday())
    return sorted(classes, key=lambda item: item[0])
