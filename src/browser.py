"""
browser.py

Driving Chromium: the persistent profile that holds the Facebook login, the
login check, navigation retries, and the pacing of a run.

The profile is a real Chromium user-data directory, so the session survives
between runs and is shared by the sweep, the settings window and the scheduler.
Only one process can hold it at a time.
"""
import contextlib
import json
import os
import random
import re
import subprocess
import sys
import time

import paths

PROFILE_DIR = paths.PROFILE_DIR

# A sweep scrolls past thousands of photos and Chromium caches every one, so an
# uncapped profile grows without bound. A cap keeps the speed of a warm cache
# without the hoarding: Chromium evicts its oldest entries once full. The login
# lives in cookies and local storage, not here, so emptying the cache never logs
# anyone out.
DISK_CACHE_BYTES = 256 * 1024 * 1024


def human_pause(a=2.0, b=4.5):
    time.sleep(random.uniform(a, b))


def fmt_dur(seconds):
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s" if m else f"{s}s"


def _prevent_sleep():
    """Ask the OS not to sleep. Returns a function that lifts the request, or
    None if this platform has no mechanism we know about."""
    if sys.platform == "darwin":
        # -w ties caffeinate's lifetime to ours, so it can't outlive the run
        # even if we're killed outright.
        proc = subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())])

        def release():
            if proc.poll() is None:
                proc.terminate()
        return release
    if os.name == "nt":
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        import ctypes
        kernel32 = ctypes.windll.kernel32
        if not kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
            return None
        return lambda: kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    return None


@contextlib.contextmanager
def keep_awake(announce=True):
    """Keep the machine awake for the duration.

    A full run is hours long, and a laptop that sleeps in the middle drops the
    browser connection, which costs everything not yet written to disk. Failing
    to arrange this is never fatal: the run just proceeds without it.
    """
    try:
        release = _prevent_sleep()
    except Exception:
        release = None
    if release and announce:
        print("Keeping this computer awake until the run finishes. Closing a "
              "laptop lid still sleeps it.")
    try:
        yield
    finally:
        if release:
            try:
                release()
            except Exception:
                pass


class WindowClosed(KeyboardInterrupt):
    """The Facebook window was shut by hand while a run was using it.

    A subclass of KeyboardInterrupt because that is exactly what it means to the
    run: stop here, keep everything gathered, write the outputs. Every stage
    already knows how to end that way, so this needs no second set of handlers.

    What it replaces is worse than a lost run. The error a closed window raises
    used to come out of whichever Playwright call was in flight and end the
    process without unwinding, which left the run lock held and the browser
    profile locked — so the next launch couldn't start at all until the stale
    lock aged out.
    """


# How Playwright words it once the page, the context or the whole browser has
# gone. Matched on the text because the exception class differs by version and
# by which call happened to be in flight when the window went.
CLOSED_SIGNS = ("has been closed", "target closed", "browser closed",
                "connection closed", "browser has disconnected")


def window_was_closed(e):
    text = str(e).lower()
    return any(sign in text for sign in CLOSED_SIGNS)


def stop_if_window_closed(e):
    """Turn a closed-window error into the interrupt it amounts to.

    Called from the handlers that would otherwise shrug an error off and carry
    on — a failed navigation, a card snapshot that came back empty. Once the
    window is gone every one of those fails, so retrying thousands of times in
    silence is the one response worse than stopping. Anything that isn't a
    closed window is left for the caller to deal with.
    """
    if not window_was_closed(e):
        return
    # What was kept is the next line out of whichever stage was running, so this
    # says only what happened.
    print("\n  The Facebook window was closed, so the search stops here.")
    raise WindowClosed("The Facebook window was closed.") from e


# A run drives this window for hours, and closing it is the one thing that ends
# a search from the outside, so the window says so the whole time it's working.
# It also says what to do instead, because Ctrl-C in the terminal is the same
# ending at a tidier moment.
#
# Wording is a constant of its own so a test can hold it against the divider
# pattern in fb_marketplace_sweep.CARDS_JS. That pattern looks for a short element
# whose text says results came from outside the search, and this is a short element
# on every page that mentions searching — close enough to be worth pinning down.
NOTICE_TEXT = ("Faceplace Marketbook is using this window \u2014 closing it ends "
               "the search. To stop early and keep what has been found, press "
               "Ctrl-C in the Terminal window instead.")

# pointer-events: none matters: the login, the popups and the radius control all
# need clicking, and nothing this app draws over Facebook may ever be in the way.
# Re-added on DOMContentLoaded as well as immediately, because an init script
# runs before there is a body to add it to.
#
# Written as a call, not a function: an init script is source the page runs as
# written, so a function expression on its own would be built and thrown away.
NOTICE_JS = """
(() => {
  const text = __NOTICE__;
  const show = () => {
    if (window.top !== window || !document.body) return;
    if (document.getElementById('faceplace-notice')) return;
    const el = document.createElement('div');
    el.id = 'faceplace-notice';
    el.textContent = text;
    el.style.cssText = 'position:fixed;left:50%;bottom:14px;'
      + 'transform:translateX(-50%);z-index:2147483647;pointer-events:none;'
      + 'width:max-content;max-width:min(620px,92vw);padding:9px 15px;'
      + 'border-radius:9px;background:rgba(18,18,18,.9);color:#fff;'
      + 'font:13px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,'
      + 'sans-serif;text-align:center;box-shadow:0 2px 12px rgba(0,0,0,.35)';
    document.body.appendChild(el);
  };
  show();
  document.addEventListener('DOMContentLoaded', show);
})();
""".replace("__NOTICE__", json.dumps(NOTICE_TEXT))


def launch_context(p, headless=False, notice=True):
    """Chromium on the saved Facebook profile.

    `notice` draws the note above over every page, and belongs on any window
    opened to do hours of work by itself. Logging in and setting the radius are
    the windows it's wrong for: those are opened for someone to use, and closing
    one when they're finished is how they're meant to end.
    """
    try:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), headless=headless,
            args=[f"--disk-cache-size={DISK_CACHE_BYTES}"],
            viewport={"width": 1280, "height": 900})
        if notice:
            try:
                ctx.add_init_script(NOTICE_JS)
            except Exception:
                pass  # a missing note is no reason to lose the run
        return ctx
    except Exception as e:
        if "already in use" in str(e) or "existing browser session" in str(e):
            raise SystemExit(
                "A leftover browser window is still using this app's saved "
                "Facebook login.\n"
                "Close any Chromium window this app opened earlier, then try "
                "again.\n"
                + ("If you can't find it, open Task Manager, end any "
                   "'chrome.exe' process whose command line mentions "
                   "fb_session, and try again."
                   if os.name == "nt" else
                   "If you can't find it, run this in Terminal and try again:\n"
                   "  pkill -f 'user-data-dir=.*fb_session'"))
        raise


AUTH_PAGE_RE = re.compile(r"login|checkpoint|two_step_verification|recover", re.I)


def is_logged_in(page):
    """True only for a fully authenticated session. Password/2FA/checkpoint
    pages don't count, and the c_user cookie only exists after full auth."""
    try:
        if page.query_selector('input[name="email"]') or page.query_selector('input[name="pass"]'):
            return False
        if AUTH_PAGE_RE.search(page.url):
            return False
        return any(c["name"] == "c_user"
                   for c in page.context.cookies("https://www.facebook.com"))
    except Exception:
        return False


class SessionExpired(Exception):
    """Raised instead of waiting when nobody is at the keyboard to log in."""


def ensure_logged_in(page, timeout_s=600, unattended=False):
    """Waits (polling, no terminal input needed) until the session is logged in.

    A scheduled run passes a short timeout and unattended=True: there is no one
    to type a password at 5am, so it gives up quickly and raises, which the
    scheduler turns into an email asking you to log in again."""
    page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
    human_pause()
    deadline = time.time() + timeout_s
    warned = False
    while not is_logged_in(page):
        # is_logged_in answers False for a window that isn't there any more, so
        # without this the wait would run its full ten minutes against nothing.
        if page.is_closed():
            raise WindowClosed("The Facebook window was closed before login.")
        if not warned and not unattended:
            print("\n>> Not logged in. Log in to Facebook BY HAND in the browser "
                  "window (including any two-factor code) — the script continues "
                  "automatically once you're fully in.")
            warned = True
        if time.time() > deadline:
            if unattended:
                raise SessionExpired(
                    "The saved Facebook session is no longer valid.")
            raise SystemExit("Timed out waiting for Facebook login.")
        time.sleep(3)
    if warned:
        print(">> Login detected, continuing.")


def goto_with_retry(page, url, retries=1):
    """Navigate to `url`, retrying transient failures. Returns True on success."""
    for attempt in range(retries + 1):
        try:
            page.goto(url, wait_until="domcontentloaded")
            return True
        except Exception as e:
            stop_if_window_closed(e)
            if attempt < retries:
                print(f"  navigation retry {attempt + 1}/{retries} after error: {e}")
                human_pause(3.0, 6.0)
            else:
                print(f"  navigation failed after {retries} retries: {e}")
    return False
