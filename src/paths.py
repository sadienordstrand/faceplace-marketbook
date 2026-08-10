"""
paths.py

Every path the rest of the app works from, in one place.

Two of them are easy to confuse:

  CODE_DIR  where the modules and their assets live — src/
  ROOT      the project folder someone opens in Finder, holding the launchers,
            the virtualenv, the runs, and everything the app writes

Under ROOT sits .state/, holding what the app maintains for itself: the browser
profile with the Facebook login, the cumulative database, saved searches, the
email settings, your own cities, the update check's notes, and the scheduler's
lock and log.

Nothing under .state/ is tracked by git.
"""

from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
ROOT = CODE_DIR.parent

# --- Beside the code ---------------------------------------------------------

UI_DIR = CODE_DIR / "ui"
SVG_PATH = UI_DIR / "faceplace_marketbook_icon.svg"
LOC_CACHE = CODE_DIR / "locations.json"
SCHEDULER_ENTRY = CODE_DIR / "scheduling.py"

# --- In the project folder ---------------------------------------------------

VENV_DIR = ROOT / ".venv"
RUNS_DIR = ROOT / "runs"
STATE_DIR = ROOT / ".state"

# The browser profile. Holds the login session, and the disk cache.
PROFILE_DIR = STATE_DIR / "fb_session"
# One cumulative database for every run, so the archive of everything ever seen
# lives in one place while each run's snapshot goes in its own folder.
DB_PATH = STATE_DIR / "marketplace_results.sqlite"
# Cities you added yourself, kept apart from the shipped list so a personal city
# never shows up as a change to a tracked file.
USER_LOC_CACHE = STATE_DIR / "my_locations.json"
SEARCHES_PATH = STATE_DIR / "saved_searches.json"
EMAIL_CONFIG_PATH = STATE_DIR / "email_config.json"
# Which shortcuts have been made, and whether the offer of one was waved away.
SHORTCUTS_PATH = STATE_DIR / "shortcuts.json"
# When the repository was last asked what the newest version is, what it said,
# and any version the user waved away.
UPDATE_STATE_PATH = STATE_DIR / "update.json"
# Scratch space for an update in progress: the copy of the current code that an
# interrupted one is put back from. Empty at rest.
UPDATE_DIR = STATE_DIR / "update"
# Windows shortcuts point at an icon file rather than carrying a copy of the
# picture, so it has to live somewhere permanent. It's generated from the SVG,
# so it belongs with the other generated things rather than beside the drawing.
ICO_PATH = STATE_DIR / "faceplace_marketbook_icon.ico"
# The run lock and the tick log.
SCHEDULE_DIR = STATE_DIR / "schedule"
DEBUG_DIR = STATE_DIR / "debug"


def _ensure():
    """The state folder has to exist before anything writes into it. SQLite and
    the atomic JSON writes both assume their parent folder is already there."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # a read-only folder is reported by whatever tries to write first


_ensure()
