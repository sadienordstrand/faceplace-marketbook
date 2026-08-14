#!/usr/bin/env python3
"""
settings_ui.py
--------------
The pre-flight settings window for fb_marketplace_sweep.py.

Rendered as a small HTML page in a Chromium window driven by Playwright, which
is already a dependency, so there's nothing extra to install and the styling can
share the gallery's palette and typewriter faces exactly. The page calls back
into Python through an exposed function, so no local web server is involved.
Chromium runs it in an app window — no address bar, no tabs — so that what
opens is a window belonging to this app rather than a browser showing a page.

The page itself is three files in ui/ — markup, stylesheet, script — assembled
here. They're substituted into one string rather than linked, because the window
is loaded with set_content() and so has no base URL for a relative href to
resolve against.
"""
import json
import shutil
import tempfile
import webbrowser
from pathlib import Path

from playwright.sync_api import sync_playwright

UI_DIR = Path(__file__).resolve().parent / "ui"

FONTS = ("https://fonts.googleapis.com/css2?family=Lato:ital,wght@0,400;0,700;"
         "1,400&family=Courier+Prime:wght@400;700&display=swap")

# The document the window opens on, before its real contents are written in.
# It exists to be a valid address for --app and to put the right name in the
# title bar for the moment before the page arrives.
APP_URL_TITLE = "<title>Faceplace%20Marketbook</title>"


def _asset(name):
    return (UI_DIR / name).read_text(encoding="utf-8")


def _json(value):
    """json.dumps for a <script> block. "</" would end the block early — the
    same escape build_gallery applies to its data, for the same reason."""
    return json.dumps(value).replace("</", "<\\/")



def _call(hooks, name, default=None):
    """Reads a value out of a hook for the initial page render. A missing or
    broken hook must not stop the settings window from opening at all."""
    fn = hooks.get(name)
    if not fn:
        return default
    try:
        return fn()
    except Exception:
        return default


def open_link(url):
    """A link in the window, opened in the everyday browser. Never in the
    window itself: that one is Playwright's, so it has no address bar to come
    back from, it isn't logged into Facebook, and it closes as soon as a search
    starts. Anything that isn't a web address is refused, because this is
    reached from the page and ends in a call to the shell's URL handler."""
    if not str(url).startswith(("http://", "https://")):
        return
    try:
        webbrowser.open(url, new=1)
    except Exception as e:
        # The terminal behind the window is where everything else says so, and
        # the address is written out in the window either way.
        print(f"Couldn't open {url} in a browser ({e}).")


def render(locations, paces, defaults, saved=(), email=None,
           units=("hours", "days"), builtins=(), shortcut=None, update=None,
           schedule=None):
    defaults = dict(defaults or {})
    # The update banner is three files of its own rather than lines added to the
    # three below, because it's a self-contained thing that either appears or
    # doesn't. They're joined on rather than linked for the same reason as
    # everything else here: the page is loaded with set_content() and has no
    # base URL for an href to resolve against.
    #
    # The script and stylesheet go in first, because the script is what carries
    # the data placeholders below. The data goes in last, so a search whose text
    # happens to look like a placeholder is never treated as one.
    return (_asset("settings.html")
            .replace("__UPDATE_BAR__", _asset("update.html"))
            .replace("__JS__", _asset("settings.js") + _asset("update.js"))
            .replace("__TOKENS__", _asset("tokens.css"))
            .replace("__CSS__", _asset("settings.css") + _asset("update.css"))
            .replace("__UPDATE__", _json(update or {}))
            .replace("__SHORTCUT__", _json(shortcut or {"ask": False}))
            .replace("__FONTS__", FONTS)
            .replace("__BUILTINS__", _json(list(builtins)))
            .replace("__LOCATIONS__", _json(list(locations)))
            .replace("__PACES__", _json(paces))
            .replace("__SAVED__", _json(list(saved)))
            .replace("__EMAIL__", _json(email or {}))
            .replace("__SCHEDULE__", _json(schedule or {}))
            .replace("__UNITS__", _json(list(units)))
            .replace("__DEFAULTS__", _json(defaults)))


def collect_settings(locations, paces, defaults=None, headless=False,
                     on_add=None, on_remove=None, hooks=None, on_ready=None,
                     builtins=()):
    """Opens the settings window and blocks until it's done.

    `on_add(label, text)` and `on_remove(label)` should both return
    (labels, error) and persist the change. Without them the city list is
    read-only. Cities named in `builtins` get no remove button.

    `hooks` wires up the saved-search, past-search and email tabs. Every entry
    is optional; whatever is missing simply leaves that part of the window
    inert, so this module still works with nothing but the search form. See
    scheduling.ui_hooks and past_runs.ui_hooks for the implementations.

    `on_ready(page)` is called once the page is loaded, which is how the test
    suite clicks through this window without a person in front of it.

    Returns whatever the page submitted — a dict with an "action" key — or None
    if cancelled or the window was closed.
    """
    hooks = dict(hooks or {})
    html = render(locations, paces, defaults,
                  saved=_call(hooks, "list_searches", default={}).get("searches", []),
                  email=_call(hooks, "email_config", default={}),
                  # Same reason as the email config: a scheduled search needs
                  # automatic runs as well, and the block that says so is on the
                  # tab the window opens on. Asked once here rather than when the
                  # Email & Setup tab is first opened, which is far too late.
                  schedule=_call(hooks, "schedule_state", default={}),
                  units=hooks.get("units") or ("hours", "days"),
                  builtins=builtins,
                  shortcut=_call(hooks, "shortcut_offer"),
                  update=_call(hooks, "update_offer"))
    state = {}
    known = list(locations)

    def add_city(label, text):
        if not on_add:
            return {"error": "Adding cities isn't available here."}
        try:
            labels, error = on_add(label, text)
        except Exception as e:
            return {"error": f"Couldn't save that: {e}"}
        if error:
            return {"error": error}
        labels = list(labels)
        added = next((c for c in labels if c not in known), label)
        known[:] = labels
        return {"cities": labels, "added": added}

    def remove_city(label):
        if not on_remove:
            return {"cities": list(known)}
        try:
            labels, error = on_remove(label)
        except Exception as e:
            return {"cities": list(known), "error": f"Couldn't remove that: {e}"}
        known[:] = list(labels)
        return {"cities": list(known), "error": error}

    def hook(name):
        """Wraps a hook so a bug in it shows up as a message in the window
        rather than an exception the page never hears back from — an unanswered
        expose_function call leaves the button spinning forever."""
        def call(*args):
            fn = hooks.get(name)
            if not fn:
                return {"error": "That isn't available in this window."}
            try:
                return fn(*args)
            except Exception as e:
                return {"error": f"{type(e).__name__}: {e}"}
        return call

    # An app window: no address bar, no tab strip, nothing but the page. What
    # this opens is a settings window, and the browser toolbar was the reason it
    # didn't behave like one — Chromium gives the keyboard to the address bar
    # when a window opens, so the caret would sit blinking in the query box
    # while whatever was typed went to the omnibox. The page can't take it
    # back, and can't even tell: Playwright emulates focus, so as far as the
    # document knows it has the keyboard already. A window with no omnibox
    # leaves the keyboard nowhere else to be.
    #
    # App mode is a startup flag, so the window has to be the one Chromium opens
    # for itself, which is what a persistent context gives us. Nothing about
    # this window is worth keeping between launches, so its profile is a
    # temporary one that goes when it does. The address it starts on has to be a
    # real one — Chromium ignores --app for about:blank and hands back an
    # ordinary browser window — so it opens on an empty document of our own that
    # set_content replaces before anyone sees it.
    profile = tempfile.mkdtemp(prefix="faceplace-window-")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            profile, headless=headless,
            args=[f"--app=data:text/html,{APP_URL_TITLE}",
                  "--window-size=1000,1180"],
            **({"viewport": {"width": 1000, "height": 1180}} if headless
               else {"no_viewport": True}))
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        # First answer wins: a stray second click, or a close that races a
        # submit, must not replace what the user already asked for.
        def finish(data):
            if not state.get("done"):
                state.update(done=True, data=data)

        page.expose_function("pySubmit", finish)
        page.expose_function("pyCancel", lambda: finish(None))
        page.expose_function("pyAddCity", add_city)
        page.expose_function("pyRemoveCity", remove_city)
        page.expose_function("pyOpenLink", open_link)
        for js_name, hook_name in (
                ("pyListSearches", "list_searches"),
                ("pySaveSearch", "save_search"),
                ("pyUpdateSearch", "update_search"),
                ("pyDeleteSearch", "delete_search"),
                ("pyCheckSchedule", "check_schedule"),
                ("pySaveEmail", "save_email"),
                ("pyTestEmail", "test_email"),
                ("pyScheduleState", "schedule_state"),
                ("pySetSchedule", "set_schedule"),
                ("pyRenewWakes", "renew_wakes"),
                ("pyListRuns", "list_runs"),
                ("pyOpenRun", "open_run"),
                ("pyDeleteRun", "delete_run"),
                ("pyAddShortcut", "add_shortcut"),
                ("pyShortcutNever", "shortcut_never"),
                ("pyUpdateNow", "update_now")):
            page.expose_function(js_name, hook(hook_name))
        page.set_content(html)
        if on_ready:
            on_ready(page)
        try:
            while not state.get("done"):
                if page.is_closed():
                    break
                page.wait_for_timeout(120)
        except Exception:
            pass  # window closed mid-wait
        try:
            ctx.close()
        except Exception:
            pass
    # Chromium can still be letting go of the profile as this runs, and on
    # Windows that leaves a file undeletable for a moment. A window that closed
    # properly must not fail over its own scratch directory.
    shutil.rmtree(profile, ignore_errors=True)
    return state.get("data")
