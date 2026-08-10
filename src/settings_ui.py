#!/usr/bin/env python3
"""
settings_ui.py
--------------
The pre-flight settings window for fb_marketplace_sweep.py.

Rendered as a small HTML page in a Chromium window driven by Playwright, which
is already a dependency, so there's nothing extra to install and the styling can
share the gallery's palette and typewriter faces exactly. The page calls back
into Python through an exposed function, so no local web server is involved.

The page itself is three files in ui/ — markup, stylesheet, script — assembled
here. They're substituted into one string rather than linked, because the window
is loaded with set_content() and so has no base URL for a relative href to
resolve against.
"""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

UI_DIR = Path(__file__).resolve().parent / "ui"

FONTS = ("https://fonts.googleapis.com/css2?family=Lato:ital,wght@0,400;0,700;"
         "1,400&family=Courier+Prime:wght@400;700&display=swap")


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


def render(locations, paces, defaults, saved=(), email=None,
           units=("hours", "days"), builtins=(), shortcut=None, update=None):
    defaults = dict(defaults or {})
    # 0 / None means "never ask", which the form shows as an empty field.
    budget = defaults.get("descriptions_budget") or ""
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
            .replace("__BUDGET__", str(budget))
            .replace("__BUILTINS__", _json(list(builtins)))
            .replace("__LOCATIONS__", _json(list(locations)))
            .replace("__PACES__", _json(paces))
            .replace("__SAVED__", _json(list(saved)))
            .replace("__EMAIL__", _json(email or {}))
            .replace("__UNITS__", _json(list(units)))
            .replace("__DEFAULTS__", _json(defaults)))


def collect_settings(locations, paces, defaults=None, headless=False,
                     on_add=None, on_remove=None, hooks=None, on_ready=None,
                     builtins=()):
    """Opens the settings window and blocks until it's done.

    `on_add(label, text)` and `on_remove(label)` should both return
    (labels, error) and persist the change. Without them the city list is
    read-only. Cities named in `builtins` get no remove button.

    `hooks` wires up the saved-search and email tabs. Every entry is optional;
    whatever is missing simply leaves that part of the window inert, so this
    module still works with nothing but the search form. See scheduling.ui_hooks
    for the implementations.

    `on_ready(page)` is called once the page is loaded, which is how the test
    suite clicks through this window without a person in front of it.

    Returns whatever the page submitted — a dict with an "action" key — or None
    if cancelled or the window was closed.
    """
    hooks = dict(hooks or {})
    html = render(locations, paces, defaults,
                  saved=_call(hooks, "list_searches", default={}).get("searches", []),
                  email=_call(hooks, "email_config", default={}),
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

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless,
                                    args=["--window-size=1000,1180"])
        page = browser.new_page(**({} if not headless
                                   else {"viewport": {"width": 1000, "height": 1180}}),
                                no_viewport=not headless)
        # First answer wins: a stray second click, or a close that races a
        # submit, must not replace what the user already asked for.
        def finish(data):
            if not state.get("done"):
                state.update(done=True, data=data)

        page.expose_function("pySubmit", finish)
        page.expose_function("pyCancel", lambda: finish(None))
        page.expose_function("pyAddCity", add_city)
        page.expose_function("pyRemoveCity", remove_city)
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
                ("pyAddShortcut", "add_shortcut"),
                ("pyReopenShortcut", "shortcut_reopen"),
                ("pyShortcutNever", "shortcut_never"),
                ("pyUpdateNow", "update_now"),
                ("pyUpdateSkip", "update_skip")):
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
            browser.close()
        except Exception:
            pass
    return state.get("data")
