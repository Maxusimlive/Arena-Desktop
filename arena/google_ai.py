"""Google AI mode track: the engine window and the message transport.

Google has no public chat API, so the desktop drives the real page the same
way a person would: a WebView window on google.com, the query typed into
Google's own search box, and the rendered AI Mode answer read back from the
DOM. Nothing is synthesized: if Google does not answer, the turn fails
honestly instead of inventing text.

The window is a genuine top-level WebView (same approach as the arena.ai
engine), so sign-in, consent and "unusual traffic" checks are done by the
person in that window, exactly like on arena.ai.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
from typing import Dict, Optional

LOG = logging.getLogger("arena.google")

_NO_CORE = object()  # cache marker: this window has no WebView2 core

def _call_off_thread(window, action, name: str, timeout: float = 3.0):
    """Run a pywebview call on a throwaway thread, never on the caller's.

    pywebview gates ``show``/``hide``/``resize``/``move`` with ``_shown_call``,
    and that wrapper starts with ``self.events.shown.wait(20)`` — a plain
    ``threading.Event.wait`` holding the GIL. Two consequences, both fatal:

    * If the window has never been shown, the call waits the full 20 seconds
      and every Python thread in the process stops dead for that time: the
      freeze the user keeps hitting when the Google window is closed.
    * The wait cannot be interrupted from outside, so a bounded wait around it
      cannot help while it runs on the request thread.

    Parking the call on a daemon thread keeps the GIL free: the helper returns
    immediately, the caller stays responsive, and a window that never answers
    simply never answers. Returns ``(done, error)``.
    """
    box: dict = {}
    done_evt = threading.Event()

    def work():
        try:
            action()
        except BaseException as exc:      # never escape into the caller
            box["error"] = exc
        finally:
            done_evt.set()

    try:
        threading.Thread(target=work, daemon=True, name=name).start()
    except Exception:
        LOG.exception("%s: could not start the worker", name)
        return False, None
    finished = done_evt.wait(timeout)     # Event.wait releases the GIL
    return finished, box.get("error")



def _looks_like_cookie(value) -> bool:
    """True for a cookie object rather than an already-plain value.

    Deliberately duck-typed: a real ``CoreWebView2Cookie`` and a test double
    both expose ``Name``/``Value``, while tuples and strings do not. This is
    what decides whether a list may leave the UI thread as-is or has to be
    flattened there first.
    """
    if isinstance(value, (tuple, list, str, bytes, int, float, bool, dict)):
        return False
    # Inspect the TYPE, never the instance: ``hasattr(cookie, "Name")`` calls
    # the COM property getter, which is the very cross-apartment read that
    # freezes the process. Looking at the class costs nothing and touches no
    # apartment.
    cls = type(value)
    if hasattr(cls, "Name") and hasattr(cls, "Value"):
        # Real CoreWebView2Cookie: pythonnet registers the properties on the
        # class, so this answers without touching any apartment.
        return True
    try:
        # Plain Python doubles keep Name/Value in instance __dict__. Reading
        # the dict directly never invokes a property getter.
        fields = object.__getattribute__(value, "__dict__")
    except Exception:
        return False
    return "Name" in fields and "Value" in fields


def _has_is_completed(task) -> bool:
    """True when ``task.IsCompleted`` can actually be read.

    Uses the type, not the instance: pythonnet exposes .NET properties on the
    proxy class, so this answers without entering any apartment.
    """
    if hasattr(type(task), "IsCompleted"):
        return True
    try:
        return "IsCompleted" in object.__getattribute__(task, "__dict__")
    except Exception:
        return False


def _is_net_collection(value) -> bool:
    """True for a finished .NET collection (iterable, but not a task/list).

    pythonnet's ``List[CoreWebView2Cookie]`` passes neither ``isinstance(x,
    list)`` nor "has IsCompleted", so without this it fell through to the
    "await the task" branch and reported a pending read for ever.
    """
    if isinstance(value, (list, tuple, dict, str, bytes)):
        return False
    return not _has_is_completed(value) and hasattr(value, "__iter__")


def _task_status(task) -> str:
    """``task.Status`` as text, or why it cannot be read.

    Triaging the frozen cookie jar needs to tell "still Running" apart from
    "Faulted" and "RanToCompletion"; without this they all looked like the same
    silent timeout in the log.
    """
    try:
        return str(task.Status)
    except Exception as exc:
        return f"unreadable({type(exc).__name__}: {exc})"


def _task_error(task) -> str:
    """``task.Exception`` if it faulted: the reason a cookie read returned nothing."""
    try:
        err = task.Exception
        return "none" if err is None else str(err)
    except Exception as exc:
        return f"unreadable({type(exc).__name__}: {exc})"


def _session_cookies_ready(value) -> bool:
    """True when ``value`` is a session-cookie list that has actually ARRIVED.

    A WebView2 ``GetCookiesAsync`` result can come back as a bare ``list`` that
    is *empty* while the browser is still enumerating an invisible window's
    jar. Treating that as "no cookies" is indistinguishable from "not signed
    in", which is exactly how this bug stayed hidden: an empty answer and a
    wrong answer look the same. A list with items, or a non-list result (a COM
    collection, a task), is taken at face value.
    """
    if isinstance(value, list):
        return len(value) > 0
    # A .NET collection is not a Python list: `List[CoreWebView2Cookie]` answers
    # `isinstance(..., list) == False` and carries `.Count` instead.
    count = getattr(value, "Count", None)
    if count is not None:
        try:
            return int(count) > 0
        except Exception:
            pass
    return value is not None


def _await_task(task, timeout: float):
    """Wait for a .NET task WITHOUT ever calling ``Task.Wait()``.

    ``Task.Wait()`` blocks the calling thread while pythonnet holds the GIL.
    Python's other threads then cannot run at all: not the Flask worker that
    asked, not the log watchdog, not the heartbeat — the whole process stops as
    one, and nothing more is written to any log. That is exactly the freeze
    seen when opening Google mode: the log stops mid-line, no exception, no
    stuck-call report, an empty crash file.

    Polling ``IsCompleted`` is safe: each call into .NET is short and returns,
    and ``time.sleep()`` between polls releases the GIL, so every other thread
    keeps running. Returns ``(ok, result)``.
    """
    deadline = time.time() + float(timeout)
    # NOT hasattr(): on a pythonnet proxy ``hasattr`` swallows the exception
    # that reading a .NET property can raise outside its apartment and simply
    # returns False. That sent every real WebView2 task down the "plain object"
    # branch below, which returned (False, None) in about 20 ms — the
    # "[google-cookies] task did not complete in time" lines that made the jar
    # look empty and stopped the sign-in window from ever being seen.
    has_flag = _has_is_completed(task)
    while time.time() < deadline:
        try:
            done = task.IsCompleted if has_flag else False
        except Exception as exc:
            done = False
            # Loud on purpose: a task whose IsCompleted cannot be read used to
            # look like "not finished yet" and silently produce an empty jar.
            if not globals().get("_reported_iscompleted_failure"):
                globals()["_reported_iscompleted_failure"] = True
                LOG.warning("task.IsCompleted is unreadable (%s: %s); the task "
                            "cannot be awaited from this thread",
                            type(exc).__name__, exc)
        if done:
            try:
                return True, task.Result
            except Exception as exc:
                # Version (b): the task COMPLETED but reading the result threw.
                # Silently returning (False, None) makes this indistinguishable
                # from "still running", so it is recorded before giving up.
                _note_once("result", "[google-cookies] task completed but its "
                            "Result threw %s: %s" % (type(exc).__name__, exc))
                return False, None
        if not has_flag:
            # A stand-in without IsCompleted (older fakes, plain objects) -- or
            # a value that simply IS the answer: pythonnet hands back a finished
            # `List[CoreWebView2Cookie]` where a task was expected. Returning
            # `(False, None)` there is what made the jar look forever pending.
            if hasattr(type(task), "Result"):
                try:
                    return True, task.Result
                except Exception:
                    return False, None
            return True, task
        time.sleep(0.05)  # releases the GIL: other threads keep working
    return False, None



_NOTED: dict = {}


def _note_once(key: str, message: str) -> None:
    """Log a diagnostic once per session, and always to the diag log.

    The cookie path runs every 0.6 s; repeating the same line 800 times buries
    everything else. One line with the facts is enough to tell the three
    failure modes apart.
    """
    if _NOTED.get(key):
        return
    _NOTED[key] = True
    LOG.warning(message)
    try:
        diag = None
        if diag is not None: diag.note(message)
    except Exception:
        pass


SEARCH_URL = "https://www.google.com/search?{query}&udm=50&hl={hl}"
HOME_URL = "https://www.google.com/"
ACCOUNT_URL = "https://myaccount.google.com/"
MAX_WAIT = 240.0
LOAD_WAIT = 20.0
POLL = 0.7

# Lines the AI Mode page prints around the answer; they are chrome, not answer.
_NOISE = (
    "ai mode is a generative",
    "ai mode is experimental",
    "generative ai is experimental",
    "share",
    "export to docs",
    "copy",
    "show more",
    "sign in",
    "all filters",
    "tools",
)


class GoogleEngine:
    """Owns the google.com WebView and turns a prompt into an answer."""

    def __init__(self):
        self.window = None
        self.main = None
        self.loaded = False
        self.host = ""
        self.visible = False
        self.last_error: Optional[str] = None
        self._nav_marker = 0.0
        self._lock = threading.Lock()
        # pywebview refuses every call on a window that was never shown (it
        # waits 20 s for the `shown` event and then raises "Main window failed
        # to start"). That single fact caused the whole log: the jar export,
        # the cookie clear and every close all failed with the same error.
        self._ever_shown = False
        self._shown_evt = threading.Event()
        self._show_lock = threading.Lock()   # two polls must not start it twice
        # While the UI polls status, the window must not be touched at all:
        # a WebView call from a request thread is exactly what froze the app.
        self._quiet_until = 0.0
        self._show_failed = False            # a failed start is not retried
        self._sign_in_page_opened = False    # the window was sent to sign-in

    # -- lifecycle -------------------------------------------------------
    def attach(self, window, main=None):
        self.window = window
        self.main = main
        def _loaded(*a):
            # Never run on the caller's thread: pywebview fires `loaded` from
            # the UI thread and that thread may already be blocked in a call
            # of ours, which would deadlock.
            threading.Thread(target=self._after_load, daemon=True).start()

        try:
            diag = None
            # The Google window is the worst case: it is created hidden and
            # stays hidden until sign-in, so every gated call would wait the
            # full 20 s with the GIL held.
            if diag is not None: diag.make_window_calls_non_blocking(window)
        except Exception:
            LOG.info("google window calls left as-is")

        window.events.loaded += _loaded
        window.events.closing += self._on_closing
        window.events.closed += self._on_closed
        window.events.shown += self._on_shown

    def _on_shown(self, *a):
        self._ever_shown = True
        self._shown_evt.set()

    def shown_once(self) -> bool:
        """True once the window has really been on screen.

        pywebview's ``shown`` event also fires on hide-then-show and never
        clears, so one gate is enough for the rest of the session.

        A browser window never fires that event at all: it is not pywebview,
        there is no message loop of ours to report anything. Waiting for it
        cost 8 s *per sign-in click* (``ensure_visible``) and 2.5 s on every
        status poll, and made the app believe the window had never appeared --
        which in turn skipped the cookie wipe and the navigation. When the
        window has no native part, "created" is the whole answer.
        """
        if self._shown_evt.is_set():
            return True
        if (self.window is not None
                and getattr(self.window, "is_browser", False) is True):
            self._shown_evt.set()
            return True
        try:
            if self.window is not None and self.window.events.shown.wait(2.5):
                self._shown_evt.set()
        except Exception:
            pass
        return self._shown_evt.is_set()

    def _ensure_shown(self) -> bool:
        """Show (or re-show) the window before touching it.

        A window created with ``hidden=True`` is *real*: it just was never
        shown. Calling ``show()`` first is what makes pywebview accept
        ``load_url`` / ``evaluate_js`` / ``get_cookies`` instead of raising.
        """
        if self.window is None:
            return False
        if self.shown_once():
            return True
        # Start it once per session: retrying a failed start on every status
        # poll is what filled the log with the same error ten times over.
        with self._show_lock:
            if self.shown_once():
                return True
            if self._show_failed:
                return False
            try:
                self.window.show()
            except Exception:
                LOG.info("google window could not be started on demand")
                self._show_failed = True
                return False
            if self.shown_once():
                LOG.info("google window started on demand")
                self.visible = True
                return True
            self._show_failed = True
            LOG.info("google window was shown but never reported itself ready")
            return False

    def _on_closing(self, *a):
        # Closing hides the window; the session (cookies) stays in the profile.
        threading.Timer(0.15, self.hide).start()
        return False

    def _on_closed(self, *a):
        self.loaded = False
        self.visible = False
        self._on_window_closed()

    def _on_window_closed(self) -> None:
        """The person closed the sign-in window: stop believing it is open.

        `_shown_evt` is set once and never cleared -- correct while a window is
        alive, a lie afterwards. Without this reset the second sign-in returned
        from `ensure_visible()` without ever asking for a window, which is the
        report "the button does nothing" all over again.
        """
        self.visible = False
        self.loaded = False
        self._sign_in_page_opened = False
        self._shown_evt.clear()
        self.stop_watch()
        forget = getattr(self.window, "forget", None)
        if callable(forget):
            # Only a single-use window has one; a persistent window keeps its
            # profile and is deliberately held on to.
            forget()

    def _page_facts(self):
        """(host, title) straight from WebView2 — no pywebview gate involved.

        ``evaluate_js`` waits for pywebview's own ``_pywebviewready`` flag,
        which is only set from the UI thread while the page is loading; asking
        for it here both takes 20 s when the flag is late and can deadlock the
        UI thread. The browser answers directly instead.
        """
        def start(core):
            # Only START the scripts here: this runs on the UI thread, and
            # WebView2 finishes its tasks through that thread's own message
            # pump. Waiting for the task inside this call blocks the pump that
            # has to complete it, so the wait never ends — the window stayed
            # invisible and every later UI call queued up behind it.
            tasks = []
            for script in ("location.host", "document.title"):
                try:
                    tasks.append(core.ExecuteScriptAsync(script))
                except Exception:
                    tasks.append(None)
            return tasks

        tasks = self._on_ui(start, limit=6.0)
        if not tasks:
            # No WebView2 to ask. A browser window answers the same two
            # questions over the DevTools protocol, and without this branch the
            # host stayed empty for every browser window -- so `loaded` was
            # never set and nothing downstream believed a page had appeared.
            host = self._eval_on_page("location.host")
            title = self._eval_on_page("document.title")
            return str(host or ""), str(title or "")

        out = []
        for task in tasks:
            text = ""
            try:
                if task is not None:
                    # Awaited here, on the caller's thread — the page facts are
                    # plain strings, so no COM call is made off the UI thread.
                    ok, value = _await_task(task, 2.5)
                    if ok:
                        text = str(value or "")
            except Exception:
                text = ""
            text = text.strip()
            if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
                try:
                    text = json.loads(text)
                except ValueError:
                    text = text.strip('"')
            out.append(text)

        facts = out
        if len(facts) < 2:
            return "", ""
        return (facts[0] or ""), (facts[1] or "")

    def _after_load(self):
        try:
            host, title = self._page_facts()
            if not host:
                host = str(self.window.evaluate_js("location.host") or "")
                title = str(self.window.evaluate_js("document.title") or "")
            self.host, title = str(host or ""), str(title or "")
        except Exception:
            self.host, title = "", ""
        if "unusual traffic" in title.lower():
            self.loaded = False
            self.last_error = "Google is showing an 'unusual traffic' check."
            self.show()
            return
        self.loaded = True
        self.last_error = None

    def ready(self) -> bool:
        return self.window is not None and self.loaded

    def wait_ready(self, timeout: float = 30.0) -> bool:
        end = time.time() + timeout
        while time.time() < end and not self.ready():
            time.sleep(0.25)
        return self.ready()

    def state(self) -> dict:
        return {
            "attached": self.window is not None,
            "ready": self.ready(),
            "host": self.host,
            "open": self.visible,
            "error": self.last_error,
        }

    # -- window ----------------------------------------------------------
    def show(self):
        """Bring the window up without ever stalling the caller.

        ``window.show()`` is a `shown`-gated call: on a hidden window pywebview
        waits 20 s and then throws, and the caller here is usually a request
        thread — the whole app looked frozen for that wait. The attempt runs on
        a worker thread; a window that does not come up quickly is simply
        reported as not ready.
        """
        diag = None

        if not self.window:
            LOG.warning("[google-window] show() called with no window — nothing "
                        "to show; sign-in cannot proceed")
            if diag is not None: diag.note("[google-window] show: нет окна")
            return
        self.visible = True
        box: dict = {}
        done = threading.Event()
        if diag is not None: diag.note("[google-window] show: начало")
        LOG.info("[google-window] show: asking the window to come up")

        def work():
            try:
                self.window.show()
                box["ok"] = True
                LOG.info("[google-window] show: the window accepted show()")
            except Exception as exc:
                box["error"] = exc
                LOG.warning("[google-window] show: the window refused show(): "
                            "%s: %s", type(exc).__name__, exc)
            finally:
                done.set()

        worker = threading.Thread(target=work, daemon=True, name="google-show")
        worker.start()
        # Event.wait, not Thread.join: joining a thread that is itself blocked
        # in pywebview's GIL-holding wait would stall this caller as well.
        if not done.wait(6.0):
            LOG.warning("[google-window] show() still pending after 6s; "
                        "the UI thread is busy — not waiting further")
            if diag is not None: diag.note("[google-window] show: не ответило за 6 с")
            return
        if "error" in box:
            LOG.info("google window show failed: %s", box["error"])
            return
        ready = self.shown_once()
        LOG.info("[google-window] show: shown event reported=%s", ready)
        if ready:
            if not getattr(self, "_placed", False):
                self._place_over_main()
            LOG.info("google window is on screen")
            if diag is not None: diag.note("[google-window] окно на экране")
        else:
            # This is the state that leaves the app with an invisible window and
            # a person clicking a button that appears to do nothing.
            LOG.warning("[google-window] show() returned but the window never "
                        "reported 'shown'; it may be created but invisible")

    def _place_over_main(self):
        """Put the window near the main one; failure here is cosmetic.

        Runs on a worker thread: resize()/move() are gated exactly like show(),
        so a stalled window must never stall the caller.
        """
        w, m = self.window, self.main
        if w is None or m is None:
            return

        def work():
            try:
                # resize/move are _shown_call gated exactly like show(): each
                # may wait 20 s holding the GIL, so they run here, never on the
                # caller's thread.
                _call_off_thread(
                    w, lambda: w.resize(max(int(m.width) - 120, 900),
                                        max(int(m.height) - 100, 640)),
                    "google-resize", timeout=0)
                _call_off_thread(
                    w, lambda: w.move(int(m.x) + 60, int(m.y) + 50),
                    "google-move", timeout=0)
                self._placed = True
            except Exception:
                LOG.info("google window placement skipped")

        threading.Thread(target=work, daemon=True, name="google-place").start()

    def hide(self):
        """Hide the Google window and return at once.

        ``window.hide()`` is a pywebview ``_shown_call``: it first waits up to
        20 s on ``events.shown`` while holding the GIL. Closing the chats drawer
        in Google mode calls this from a request thread, so that wait froze the
        entire application — every thread, not just the window. The call is
        parked on a daemon thread and never waited for: hiding is fire and
        forget by nature.
        """
        self.visible = False
        if not self.window:
            return
        w = self.window

        def work():
            try:
                w.hide()
                LOG.info("[google-window] hide() dispatched to the UI thread")
            except Exception:
                LOG.info("[google-window] hide failed", exc_info=True)

        threading.Thread(target=work, daemon=True, name="google-hide").start()
        LOG.info("google window hide requested")

    def clear_cookies(self) -> bool:
        """Wipe the window's cookie jar, so a new sign-in starts from zero.

        The point of a sign-in window is to log in as somebody *else*: leaving
        the previous session in place makes Google sign the person straight
        back into the account they wanted to leave. The jar is wiped through
        WebView2's own CookieManager and the sites' storage through its
        profile API — both work while the window is up, which the profile
        folders do not (deleting them under a live WebView used to kill it).
        """
        if not self.window:
            return False
        if getattr(self.window, "is_browser", False) is True:
            # A real browser wipes its own jar over the DevTools protocol (the
            # same `Network.clearBrowserCookies` the arena window uses). Without
            # this branch the wipe went to WebView2's CookieManager, which does
            # not exist here, so "fresh sign-in" silently kept the old session
            # and Google signed the person straight back into it.
            wiped = False
            try:
                wiped = bool(self.window.clear_cookies())
            except Exception:
                LOG.info("google window: the browser refused to clear its jar")
            LOG.info("google window cookies cleared (via: %s)",
                     "browser" if wiped else "unavailable")
            return wiped
        # Deleting through WebView2's CookieManager needs no shown window and
        # no navigation, so it is the first and the decisive step: the old
        # code went through pywebview, which waits for the `shown` event and
        # then throws "Main window failed to start" on a hidden window.
        cleared = self._clear_cookies_direct()
        try:
            storage = self.clear_storage()
            cleared = cleared or storage
        except Exception:
            LOG.info("google storage clear skipped")
        # No pywebview fallback here on purpose: window.clear_cookies() is a
        # `loaded`-gated call that can block a request thread for minutes.
        # Without WebView2 (tests, other platforms) the jar cannot be wiped,
        # and saying so is better than hanging.
        LOG.info("google window cookies cleared (via: %s)",
                 "manager" if cleared else "unavailable")
        return cleared

    def _clear_cookies_direct(self) -> bool:
        """WebView2's own DeleteAllCookies: no window state required."""
        def call(core):
            core.CookieManager.DeleteAllCookies()
            return True

        if self._on_ui(call, limit=5.0):
            time.sleep(0.4)
            LOG.info("google window jar wiped via CookieManager")
            return True
        return False

    def clear_storage(self) -> bool:
        """Empty the sites' local storage through the browser itself.

        The profile folders cannot be deleted while WebView2 has them open —
        doing that used to kill the window ("Main window failed to start"
        right after a wipe). Storage is cleared through the API instead.
        """
        def call(core):
            core.Profile.ClearBrowsingDataAsync()
            return True

        ok = bool(self._on_ui(call, limit=5.0))
        LOG.info("google window storage clear: %s", "done" if ok else "unavailable")
        return ok

    # Google hosts whose cookies carry the session. Ask each one explicitly:
    # pywebview's get_cookies() queries *the current page's* URL, and after a
    # sign-in that page is often myaccount.google.com or about:blank, so the
    # old call legitimately returned nothing and the account was saved without
    # a session ("cookies: no" in the log). www.google.com carries SID and
    # __Secure-1PSID, accounts.google.com the same SID plus account cookies.
    COOKIE_URLS = (
        "https://www.google.com/",
        "https://accounts.google.com/",
    )

    def quiet(self, seconds: float = 20.0) -> None:
        """Mark the window as off-limits for a while (status polls call this)."""
        self._quiet_until = max(self._quiet_until, time.time() + float(seconds))

    def quiet_now(self) -> bool:
        return time.time() < float(getattr(self, "_quiet_until", 0.0) or 0.0)

    def _edge_host(self):
        """``(CookieManager, scheduler, native_form)`` or None.

        pywebview 6.x sets ``window.native`` to the WinForms form; its control
        is ``.webview`` and the Edge layer (which owns the scheduler) ``.browser``.

        **``CoreWebView2`` must never be read off the UI thread.** It is a COM
        property of a WinForms control: reading it from a Flask worker is a
        native access violation, and Windows kills the process on the spot with
        no Python traceback. That is exactly how switching to Google mode made
        the app disappear while the log simply stopped mid-line.

        The value is therefore cached the first time it is read *on* the UI
        thread, and every later caller on any thread reuses the cache. The COM
        object itself is apartment-threaded, but reading its reference once is
        what the cache buys us; the calls that actually use it still run through
        ``_on_ui``.
        """
        cached = getattr(self, "_core_cached", None)
        if cached is not None and cached is not _NO_CORE:
            return cached
        native = getattr(self.window, "native", None)
        if native is None:
            return None
        if self._on_ui_thread(native):
            found = self._read_edge_host_on_ui(native)
            self._core_cached = found if found is not None else _NO_CORE
            return found
        # Off the UI thread: hop over, read there, remember, and return.
        found = self._on_ui(lambda _core: self._read_edge_host_on_ui(native), limit=3.0)
        if found is None:
            LOG.info("google window: could not read CoreWebView2 from the UI thread")
            self._core_cached = _NO_CORE
            return None
        self._core_cached = found
        return found

    @staticmethod
    def _on_ui_thread(native) -> bool:
        """True when the caller already runs on the window's UI thread."""
        try:
            return not bool(getattr(native, "InvokeRequired", False))
        except Exception:
            return False

    @staticmethod
    def _read_edge_host_on_ui(native):
        """Read the Edge host. UI thread ONLY — see ``_edge_host``."""
        control = getattr(native, "webview", None)
        core = getattr(control, "CoreWebView2", None) if control is not None else None
        if core is None:
            return None
        browser = getattr(native, "browser", None)
        scheduler = getattr(browser, "syncContextTaskScheduler", None)
        try:
            return (core.CookieManager, scheduler, native)
        except Exception:
            return None

    def _edge_manager(self):
        host = self._edge_host()
        return host[0] if host else None

    def _on_ui(self, start_call, limit: float = 5.0):
        """Run ``start_call(core)`` on the UI thread, wait here for the result.

        Two rules make this safe, and both matter:

        * The *call* must happen on the window's UI thread. WinForms and
          WebView2 are single-threaded; touching them from a Flask thread is a
          native access violation that kills the process with no traceback.
        * The *wait* must not happen there. WebView2 tasks complete through the
          UI thread's own message pump, so ``task.Wait()`` inside an Invoke
          deadlocks the window — and with the tray attached the whole app.
          ``start_call`` therefore only *starts* the async operation and
          returns a future; the waiting happens on the caller's thread, which
          is exactly what pywebview itself does.
        """
        native = getattr(self.window, "native", None) if self.window else None
        if native is None:
            return None
        box: dict = {}

        def on_ui():
            try:
                # Runs on the UI thread by construction (Invoke/BeginInvoke),
                # so reading CoreWebView2 here is legal. Reading it anywhere
                # else is a native crash, which is why this is the only place
                # besides _read_edge_host_on_ui that touches it.
                control = getattr(native, "webview", None)
                core = getattr(control, "CoreWebView2", None)
                if core is None:
                    box["future"] = None
                    return
                box["future"] = start_call(core)
            except BaseException as exc:  # never let it escape into the UI loop
                box["error"] = exc

        try:
            from System.Windows.Forms import MethodInvoker  # type: ignore
            if getattr(native, "InvokeRequired", False):
                invoker = MethodInvoker(on_ui)
                # BeginInvoke returns at once; a wedged UI thread must cost a
                # bounded wait below, not a permanent block in here.
                if hasattr(native, "BeginInvoke"):
                    # Preferred: returns immediately, so a wedged UI thread
                    # costs a bounded wait below instead of a hard block. This
                    # call does not hold the GIL while the UI thread works.
                    native.BeginInvoke(invoker)
                else:
                    # Invoke blocks until the UI thread runs the delegate. If
                    # that thread is itself waiting on us this is a deadlock
                    # that freezes every Python thread at once, so refuse it and
                    # let the caller report "UI thread did not answer".
                    LOG.info("window has no BeginInvoke; refusing a blocking Invoke")
                    return None
            else:
                on_ui()
        except Exception as exc:
            LOG.info("google window UI call could not be queued: %s", exc)
            return None

        deadline = time.time() + float(limit)
        while time.time() < deadline and "future" not in box and "error" not in box:
            time.sleep(0.02)
        if "error" in box:
            LOG.info("google window UI call failed: %s", box["error"])
            return None
        if "future" not in box:
            LOG.info("google window UI thread did not answer in %.1fs", limit)
            return None

        future = box["future"]
        if future is None:
            return None
        if isinstance(future, list):
            return future
        # `List[CoreWebView2Cookie]` is iterable but not a Python list. Turning
        # it into one here keeps the caller's `isinstance(result, list)` honest.
        if (_is_net_collection(future)):
            try:
                return list(future)
            except Exception:
                return future
        if isinstance(future, (tuple, dict, str, int, float, bool)):
            return future          # a plain value: nothing to await
        # An awaitable: wait for it *here*, on the calling thread.
        try:
            remaining = max(0.2, deadline - time.time())
            # _has_is_completed, not hasattr: hasattr on a pythonnet proxy
            # reports False for a property it cannot serve from this apartment,
            # so a real WebView2 task fell through to "return future" and the
            # caller received a raw task it could not read. That is why the
            # cookie jar came back empty on every attempt.
            if _has_is_completed(future):
                ok, result = _await_task(future, remaining)
                if not ok:
                    LOG.info("google window operation did not finish in %.1fs", limit)
                return result
            if hasattr(future, "Result"):
                return future.Result
            return future
        except Exception as exc:
            LOG.info("google window operation failed: %s", exc)
            return None

    def cookie_probe(self) -> dict:
        """Everything needed to explain a failed jar read, in one dict.

        Diagnostic only: it reports what the window claims about itself, which
        is what separates "hidden and asleep" from "shown but wedged".
        """
        return {
            "window": self.window is not None,
            "visible": bool(self.visible),
            "shown_once": bool(self.shown_once()),
            "ready": bool(self.ready()),
            "host": self.host,
            "quiet_until": round(max(0.0, self._quiet_until - time.time()), 2),
            "show_failed": bool(getattr(self, "_show_failed", False)),
        }

    def cookies_readable(self) -> bool:
        """True when reading the cookie jar can actually produce an answer.

        WebView2 only services the tasks it is handed while its renderer is
        running; a window that was never shown — or one that was hidden again —
        leaves ``GetCookiesAsync`` pending forever. Rather than wait out that
        timeout on every poll, the read is skipped and the reason recorded.
        """
        return self.window is not None and bool(self.visible)

    def ensure_visible(self) -> bool:
        """Bring the window on screen if it is not, so reading becomes possible."""
        if self.visible and self.shown_once():
            return True
        self.show()
        if getattr(self.window, "is_browser", False) is True:
            # There is no `shown` event to wait for: a browser either started
            # or it did not, and the window's own state says which.
            return True
        inner = getattr(self.window, "window", self.window)   # LazyWindow -> real
        if getattr(inner, "events", None) is None:
            # A window with no event surface can never report `shown`, so the
            # wait below can only burn its full eight seconds on every press.
            # Asking for it was the whole point; whether it arrived is not
            # something this window is able to say.
            LOG.info("[google-window] the window reports no events; "
                     "not waiting for 'shown'")
            return True
        end = time.time() + 8.0
        while time.time() < end and not self.shown_once():
            time.sleep(0.1)
        return self.shown_once()

    def _cookies_direct(self, url: str, limit: float = 4.0) -> list:
        """Cookies for one URL, fetched and flattened entirely on the UI thread.

        **This is where the app froze.** ``GetCookiesAsync`` returns a .NET task
        whose result is a list of ``CoreWebView2Cookie`` objects — live COM
        proxies, not plain Python values. Unpacking them afterwards
        (``cookie.Name``) on a Flask thread is a cross-apartment COM call: it
        does not raise, it does not return. The thread sits in a DCOM wait
        while pythonnet holds the GIL, so every other thread in the process
        stops, the log ends mid-line and the crash file stays empty. The stack
        dumps in ``desktop-crash-2.log`` show exactly that, four times over:
        ``google_ai.py line 630 in _cookies_direct``.

        So the values are read where they legally can be — on the window's UI
        thread — and only plain ``(name, value, domain)`` tuples cross back.
        """
        diag = None

        def start(core):
            # Runs ON the UI thread. Only *start* the read here: WebView2
            # completes its tasks through this thread's own message pump, so
            # waiting for one from inside this call would block the pump that
            # has to finish it — a self-deadlock that times out empty.
            manager = getattr(core, "CookieManager", None)
            if manager is None:
                return None
            return manager.GetCookiesAsync(url)

        # Version (c): a window that is hidden never runs the tasks WebView2
        # queues for it — its renderer is frozen by the browser. Asking anyway
        # produces exactly "task did not complete in time", forever. Skipping
        # the call is not a guess: it is the documented behaviour of a
        # backgrounded WebView2, and it turns an infinite timeout into one
        # honest line in the log.
        if not self.cookies_readable():
            _note_once(
                "paused",
                "[google-cookies] reading is paused: window not on screen "
                f"(visible={self.visible}, shown_once={self.shown_once()}, "
                f"host={self.host!r}). Show the Google window and sign in "
                "there; the jar cannot be read from a hidden window.")
            return []

        result = self._on_ui(start, limit=limit)
        if result is None:
            _note_once("on_ui", "[google-cookies] the UI thread did not answer "
                                "in %.1fs (window wedged?)" % limit)
            return []

        ok = True
        if isinstance(result, list):
            # _on_ui already awaited the task, so a resolved value arrives as
            # the list itself. An EMPTY list here is still ambiguous, and the
            # retry below covers it too.
            jar = result
        elif _is_net_collection(result):
            # A .NET collection that came back as itself. Iterating it here is
            # plain enumeration, not a COM property call, so it is safe off the
            # UI thread -- and it keeps the jar from looking "pending".
            jar = list(result)
        else:
            # The task is pending. Await it here, on the caller's thread, where
            # polling IsCompleted is safe and time.sleep releases the GIL.
            ok, jar = _await_task(result, max(0.5, limit))

        if ok and jar is not None and not _session_cookies_ready(jar):
            # An empty read is not proof of an empty jar: the first one
            # routinely races the window's first navigation, and a silent zero
            # is what made every Google sign-in look like "not signed in".
            _note_once("empty", "[google-cookies] first read returned an empty "
                                "list; retrying once")
            again = self._on_ui(start, limit=limit)
            if isinstance(again, list):
                if _session_cookies_ready(again):
                    jar = again
            elif again is not None:
                ok2, jar2 = _await_task(again, max(0.5, limit))
                if ok2 and jar2 is not None:
                    jar = jar2

        if not isinstance(jar, list):
            if not ok or jar is None:
                # One line of forensics per failure, because "did not complete
                # in time" alone cannot be acted on: it is identical whether the
                # task never finished, finished with an exception, or was never
                # a real .NET task. These four facts separate those cases.
                probe = self.cookie_probe()
                if diag is not None: diag.note(
                    "[google-cookies] task did not complete in time"
                    f" | type={type(result).__name__}"
                    f" | has_is_completed={_has_is_completed(result)}"
                    f" | status={_task_status(result)}"
                    f" | result_error={_task_error(result)}"
                    f" | window={probe['window']} | visible={probe['visible']}"
                    f" | shown_once={probe['shown_once']} | ready={probe['ready']}"
                    f" | show_failed={probe['show_failed']}"
                    f" | host={probe['host']!r} | url={url}")
                return []
            return []

        if jar and _looks_like_cookie(jar[0]):
            # COM: the fields may only be read on the UI thread. This is the
            # second hop, and it is what stops the freeze.
            if diag is not None: diag.note(f"[google-cookies] {url}: unpacking {len(jar)} COM objects "
                      "on the UI thread")
            flat = self._flatten_cookies_on_ui(jar, limit=limit)
            if diag is not None: diag.note(f"[google-cookies] {url}: unpacked {len(flat)}")
            return flat
        if _is_net_collection(jar):
            # A .NET collection stays a live COM view of the browser's jar, and
            # the caller is a Flask thread. Letting it fall out of scope there
            # runs the collection's release on the wrong apartment -- a native
            # fault that kills the process with nothing in desktop.log. Handing
            # back a plain list means the only reference dies where Python can
            # account for it.
            try:
                return list(jar)
            except Exception:
                return []
        return jar

    def _flatten_cookies_on_ui(self, cookies, limit: float = 4.0) -> list:
        """Turn COM cookie objects into plain tuples — on the UI thread only.

        ``cookie.Name``/``.Value``/``.Domain`` are COM property calls on
        apartment-threaded objects. Off the owning thread they block forever
        instead of failing, which is precisely the reported freeze.
        """
        diag = None

        def read(_core):
            out = []
            try:
                for cookie in cookies:
                    try:
                        out.append((str(cookie.Name), str(cookie.Value),
                                    str(cookie.Domain)))
                    except Exception:
                        continue
            except Exception:
                return []
            return out

        result = self._on_ui(read, limit=limit)
        if isinstance(result, list):
            return result
        LOG.warning("[google-cookies] the UI thread did not unpack the cookie "
                    "fields within %.1fs; returning an empty jar", limit)
        if diag is not None: diag.note("[google-cookies] unpack went unanswered")
        return []

    def cookies(self, tries: int = 3, wait: float = 0.8) -> str:
        """The window's cookie jar as a ``Cookie:`` header value.

        This is the piece that lets the plain-HTTPS transport answer for the
        session the person just created in the window: without it the app had
        an account it could not actually use.

        WebView2 writes the jar a moment *after* the page settles, so a single
        read right after detection legitimately came back empty ("cookies: no"
        in the log, followed by every request failing). Retried on purpose.
        """
        if not self.window:
            LOG.info("[google-cookies] no window; returning empty jar")
            return ""
        # A window that has never been shown has no usable WebView2 session, and
        # asking for one is the expensive, deadlock-prone path. Google mode is
        # opened long before sign-in, so this is the common case: answer "no
        # cookies" instantly instead of reaching into a window that is not there.
        # Deliberately a flag check, not shown_once(): that one waits up to
        # 2.5 s for an event, and this runs inside a request handler.
        # The gate applies only to a window that has a real WinForms host: a
        # window with no native part cannot produce WebView2 cookies anyway,
        # but stand-in windows in tests (and the pure-HTTP path) still must be
        # able to answer through their own jar reader.
        if (getattr(self.window, "native", None) is not None
                and not getattr(self, "_ever_shown", False)):
            # WebView2 only services its cookie API while its renderer runs --
            # but a browser has no such gate: its jar is readable as soon as the
            # debugging port answers, whether or not a person ever saw it.
            LOG.info("[google-cookies] window never shown yet; no jar to read")
            return ""
        diag = None
        if diag is not None: diag.note("[google-cookies] start")
        pairs = []
        # A single wall-clock budget for the whole read. Every inner step has
        # its own bound, but one of them can still be slow on a busy machine;
        # the total must stay small because this runs inside a request.
        overall_deadline = time.time() + 12.0
        for attempt in range(max(1, tries)):
            if time.time() >= overall_deadline:
                if diag is not None: diag.note("[google-cookies] overall budget spent; giving up")
                break
            if diag is not None: diag.note(f"[google-cookies] attempt {attempt + 1}/{max(1, tries)}")
            pairs = (self._cookies_from_browser()
                     or self._cookies_from_edge()
                     or self._cookies_from_pywebview())
            if diag is not None: diag.note(f"[google-cookies] attempt {attempt + 1}: {len(pairs) if pairs else 0} pairs")
            if pairs:
                break
            if attempt + 1 < max(1, tries):
                time.sleep(wait)
        if not pairs:
            return ""
        seen = set()
        out = []
        for name, value in pairs:
            if name in seen:
                continue
            seen.add(name)
            out.append(f"{name}={value}")
        return "; ".join(out)

    def _cookies_from_browser(self) -> list:
        """The jar of the browser window the app actually opened.

        Every other reader here speaks WebView2 (``GetCookiesAsync`` on the UI
        thread). Since the Google window became a real browser, those readers
        had nothing to talk to: ``_edge_manager()`` is None, so ``cookies()``
        answered "" and ``jar_signed_in()`` answered False no matter what the
        person did in the window. Signing in worked; the app just could not see
        it, and the tick never appeared. This is the reader that matches the
        window that is actually running.
        """
        # Strictly `is True`: a stand-in window whose every attribute is a
        # hanging stub would otherwise answer with a function -- which is
        # truthy -- and send us into a four-second wait for a jar that does not
        # exist. The flag has to be the flag.
        if getattr(self.window, "is_browser", False) is not True:
            # Not a browser window: `cookie_tuples` on a wedged stand-in would
            # block the attribute lookup itself, and on pywebview it does not
            # exist at all.
            return []
        reader = getattr(self.window, "cookie_tuples", None)
        if reader is None:
            return []
        # Bounded on purpose. Reading a browser's jar talks to another process
        # over its debugging socket, and a wedged window must not hold a request
        # thread: every other reader here already carries a limit for the same
        # reason. A late answer is dropped, not waited for.
        box: dict = {}

        def work():
            try:
                box["pairs"] = reader() or []
            except Exception as exc:
                box["error"] = exc

        worker = threading.Thread(target=work, daemon=True, name="google-jar")
        worker.start()
        worker.join(4.0)
        if worker.is_alive() or "error" in box:
            LOG.info("google cookies: the browser jar could not be read")
            return []
        tuples = box.get("pairs") or []
        pairs = []
        for name, value, domain in tuples:
            host = str(domain or "").lstrip(".")
            if "google." in host or host.endswith("google.com"):
                pairs.append((str(name), str(value)))
        return pairs

    def _cookies_from_edge(self) -> list:
        """Ask WebView2 for each Google host's cookies explicitly.

        ``GetCookiesAsync(url)`` answers for *that* URL only, which is exactly
        why the wrapper's single call (current page) kept coming back empty.
        Reading happens on the window's UI thread — doing it from a worker
        thread is a native crash, not an exception.
        """
        if self._edge_manager() is None:
            return []
        found = []
        diag = None
        if diag is not None: diag.note(f"[google-cookies] edge manager ok; urls={len(self.COOKIE_URLS)}")
        for url in self.COOKIE_URLS:
            if diag is not None: diag.note(f"[google-cookies] reading {url}")
            pairs = self._cookies_direct(url)
            if diag is not None: diag.note(f"[google-cookies] {url}: {len(pairs) if pairs else 0} pairs")
            host_pairs = [(n, v) for n, v, d in pairs if "google." in d or not d]
            if pairs:
                LOG.info("google cookies from %s: %s pairs (of %s in the jar)",
                         url, len(host_pairs), len(pairs))
            found.extend(host_pairs)
        return found

    def _cookies_from_pywebview(self) -> list:
        """Never touch pywebview's reader — it can wait forever.

        ``window.get_cookies()`` ends in ``semaphore.acquire()`` with no
        timeout, and its callback is queued on the UI thread. When the window
        is hidden or the UI thread is busy the callback never runs, so the
        caller waits for good: that is what froze the app the moment Google
        mode was opened. A build with no WebView2 simply has no jar to read.
        """
        return []

    def open_sign_in(self, fresh: bool = True):
        """Send the window to Google so the person can sign in there.

        ``fresh`` wipes the jar and the sites' storage first: a sign-in window
        must never arrive on Google already carrying somebody's session. The
        page is then loaded with a cache-busting parameter, so Google is asked
        again instead of serving the previous signed-in copy.
        """
        # Wiping the jar is only right when the sign-in window is not already
        # up. Clicking the button twice used to clear the cookies under a person
        # who was mid-password, throw them back to a blank sign-in page, and
        # look exactly like "the screen reloaded and lost everything".
        # Only a *live* window counts as already open. `self.visible` is merely
        # "we asked for it once": a browser window closed by the person leaves
        # that flag set, and the sign-in click then did nothing at all -- the
        # exact complaint ("the button opens nothing"), because the code
        # decided the window was already there.
        already_open = self._sign_in_in_progress()
        if fresh and already_open:
            LOG.info("[google-window] sign-in already open; keeping the started session")
            self.ensure_visible()
            return
        if fresh and not already_open:
            # Stale flag from an earlier window: clear it, or every later poll
            # in this session keeps believing a window exists.
            self.visible = False
        if fresh:
            # No parking page: the jar is wiped through WebView2's own manager,
            # which works on a hidden window and does not need the shown event
            # that pywebview's wrapper insists on.
            self.clear_cookies()
        # ORDER IS THE WHOLE BUG. pywebview 6 wraps load_url in @_shown_call:
        # it first waits 20 s for `shown` and then raises "Main window failed
        # to start". A window created with hidden=True is built as Show();
        # Hide(), so at this moment it has never been on screen and the wait
        # can only time out -- the navigation is thrown away and the sign-in
        # window never opens. Showing first makes the event real, and only
        # then does the load have something to wait for.
        #
        # A browser window has no `shown` event at all, so waiting for one here
        # cost eight seconds on every press (desktop-23.log, 11:09:08 ->
        # 11:09:17) before the browser was even started. `ensure_visible` is
        # skipped for it: the navigation below is what brings it to the front.
        if getattr(self.window, "is_browser", False) is not True:
            self.ensure_visible()
        else:
            # A browser window is not shown, it is *started*: the window object
            # exists from the first click, while the browser process and its
            # debugging port come up a second later. Marking the pane open here
            # is what keeps a second click from starting a second window, and
            # `load_url` waits for the port itself.
            self.show()
            self.visible = True
        self.load(HOME_URL + ("?authuser=0&hl=" + self._hl_suffix() if fresh else ""))
        self._sign_in_page_opened = True
        own = self.window
        if getattr(own, "ephemeral", False) is True:
            # A single-use window is closed as soon as the jar is out, which is
            # what makes the next sign-in start from nothing instead of landing
            # inside a finished one. The watcher below is what notices the end.
            LOG.info("[google-window] single-use sign-in window opened")

    def _sign_in_in_progress(self) -> bool:
        """True only when a sign-in the person is using is *already on screen*.

        `_window_is_live()` answers "a browser process exists", which is true
        from the moment the window object is created — and that made
        `open_sign_in` return early on the very first click: the pane decided
        the sign-in was already open, so nothing was shown, nothing was
        navigated, and the jar was never wiped. The person saw a click that did
        nothing at all, which is exactly the report.
        """
        if not self._window_is_live():
            return False
        if getattr(self.window, "is_browser", False) is True:
            # A browser cannot tell us about a page it has not been given yet:
            # the only honest signal is whether we navigated it ourselves.
            return bool(self._sign_in_page_opened)
        return True

    def _window_is_live(self) -> bool:
        """True when a window object exists and its browser is still running.

        Asking the window rather than the engine's own `visible` flag is the
        difference between "the person is signing in right now" and "someone
        pressed the button an hour ago".
        """
        if self.window is None:
            return False
        inner = getattr(self.window, "window", self.window)   # LazyWindow -> real
        if inner is None:
            return False
        try:
            state = inner.state() or {}
        except Exception:
            return bool(self.visible)
        if not isinstance(state, dict):
            return bool(self.visible)
        # A window that reports no state at all (tests, stand-ins) keeps the
        # old meaning: the engine's own flag.
        if "running" not in state and "platform" not in state:
            return bool(self.visible)
        return bool(state.get("running")) and not state.get("build_error")

    def _hl_suffix(self) -> str:
        return "ru"

    def open_account_menu(self):
        self.load(ACCOUNT_URL)
        self.show()

    def load(self, url: str):
        if not self.window:
            return False
        self.loaded = False
        self._nav_marker = time.time()
        try:
            self.window.load_url(url)
        except Exception as exc:
            # pywebview's own timeout says the window never reached the screen.
            # Retrying the wrapper repeats the same 20-second wait, so the
            # navigation goes straight to WebView2 through the UI thread, which
            # works on a window pywebview thinks is not up yet. Without this,
            # a window that fails its first `shown` loses every later load too.
            if self._navigate_direct(url):
                LOG.info("google window loaded through WebView2 after: %s",
                         type(exc).__name__)
                return True
            LOG.exception("google window load failed: %s", url)
            return False
        if getattr(self.window, "is_browser", False) is True:
            # The browser navigates asynchronously and fires no `loaded` event,
            # and the engine has always learnt "the page is up" from that event
            # alone. Without this wait every later step -- the sign-in watcher,
            # the account read, the poll that ends a sign-in -- asked a window
            # that reported itself as never loaded.
            self._wait_for_page(url)
        return True

    def _window_cannot_answer(self) -> bool:
        """True when this window has no JS bridge at all, so asking is pointless.

        Both transports the app uses answer `evaluate_js`; a stand-in without it
        (a bare object, a window whose native part failed to load) fails every
        call the same way. That is different from a page that has not painted
        yet and answers with an empty string.
        """
        window = self.window
        if window is None:
            return True
        for candidate in (window, getattr(window, "window", None)):
            # The window itself first, then whatever a wrapper holds: a
            # `LazyWindow` has no JS of its own and passes the call through.
            if candidate is None:
                continue
            if (callable(getattr(candidate, "evaluate_js", None))
                    or callable(getattr(candidate, "evaluate_js_value", None))):
                return False
        # Nothing in the chain can run script. A wrapper with nothing behind
        # it counts here too: `ensure()` would create a window only to answer
        # a question about a window that does not exist yet.
        return True

    def _wait_for_page(self, url: str, timeout: float = 20.0) -> bool:
        """Wait until the browser really shows something, then say `loaded`.

        The loop stops early when the window cannot answer at all. Polling a
        window with no JS bridge used to spin the full twenty seconds -- eighty
        attempts, each one failing the same way -- and the only thing it ever
        produced was a slower test suite and a slower sign-in. Two failures in
        a row with nothing learned means the window does not speak; waiting
        longer cannot change that.
        """
        wanted = (urllib.parse.urlparse(url).hostname or "").lower()
        end = time.time() + timeout
        blind = self._window_cannot_answer()
        while time.time() < end:
            host = str(self._eval_on_page("location.host") or "")
            href = str(self._eval_on_page("location.href") or "")
            if not host and not href and blind:
                # Only a window with no way to answer at all is hopeless. A
                # window that answers "nothing yet" is a page still loading,
                # and cutting that wait short would declare a slow page dead.
                LOG.info("google window: the window cannot answer; "
                         "not waiting the full %.0fs", timeout)
                break
            if host and host not in ("", "about:blank") and (
                    not wanted or wanted.endswith(host) or host.endswith(wanted)
                    or host.split(".")[-2:] == wanted.split(".")[-2:]):
                self.host = host
                self.loaded = True
                self.last_error = None
                LOG.info("google window: page is %s", href or host)
                return True
            time.sleep(0.25)
        LOG.info("google window: no page confirmed within %.0fs (%s)", timeout, url)
        # Still mark it loaded if the window answers at all: a page that took
        # too long to confirm is not a broken window, and refusing to proceed
        # would leave the person signed in with an app that says otherwise.
        host = str(self._eval_on_page("location.host") or "")
        if host:
            self.host, self.loaded = host, True
        return self.loaded

    def _navigate_direct(self, url: str) -> bool:
        """Point the WebView at ``url`` without pywebview's `shown` gate."""
        def call(core):
            core.Navigate(url)
            return True
        try:
            return bool(self._on_ui(call, limit=6.0))
        except Exception:
            return False

    def _eval_on_page(self, script: str):
        """One value from the page, whichever transport this window speaks.

        The window is either pywebview+WebView2 or a real browser; the caller
        should not have to know which. Returns None when neither answers.
        """
        if not self.window:
            return None
        value = None
        runner = getattr(self.window, "evaluate_js_value", None)
        try:
            if runner is not None:
                value = runner(script, timeout=5.0)
            else:
                value = self.window.evaluate_js(script)
        except Exception:
            value = None
        if value is None:
            try:
                value = self.run_script_safe(script, limit=4.0)
            except Exception:
                value = None
        return value

    def run_script_safe(self, script: str, limit: float = 6.0):
        """pywebview's evaluate_js with a hard ceiling on the wait.

        ``window.evaluate_js`` blocks until the page answers; on a window that
        is hidden, broken or mid-navigation that wait has no upper bound, and
        a request thread stuck there never answers the UI again. The call is
        moved to a worker thread that is simply abandoned past ``limit``.
        """
        if not self.window:
            return None
        box: dict = {}

        def work():
            try:
                box["value"] = self.window.evaluate_js(script)
            except Exception as exc:
                box["error"] = exc

        worker = threading.Thread(target=work, daemon=True, name="google-js")
        worker.start()
        worker.join(limit)
        if worker.is_alive():
            LOG.info("google js call abandoned after %.0fs (window not answering)", limit)
            return None
        if "error" in box:
            LOG.info("google js failed: %s", box["error"])
            return None
        return box.get("value")

    def run_script(self, script: str):
        """Deprecated path kept for callers that expect the old signature.

        It delegates to ``run_script_safe`` so that no code path can block a
        request thread forever on pywebview's unbounded waits.
        """
        return self.run_script_safe(script)

    # -- cookies / account ----------------------------------------------
    def apply_cookies(self, cookie_str: str) -> int:
        """Write pasted cookies into the google.com page context.

        Google's authentication cookies are httpOnly, so a pasted
        ``document.cookie`` string cannot restore a session on its own; this
        exists for the cookies a person *can* export (full header strings from
        a cookie editor). Returns how many pairs were written.
        """
        pairs = []
        for chunk in str(cookie_str or "").split(";"):
            chunk = chunk.strip()
            if "=" in chunk:
                name, value = chunk.split("=", 1)
                if name.strip() and " " not in name.strip():
                    pairs.append((name.strip(), value.strip()))
        if not pairs:
            return 0
        script = "(() => { let n = 0; for (const [k, v] of %s) { try { document.cookie = k + '=' + v + '; domain=.google.com; path=/; secure'; n++; } catch (e) {} } return n; })()" % json.dumps(pairs)
        if not self.host.endswith("google.com"):
            self.load(HOME_URL)
            self.wait_ready(LOAD_WAIT)
        written = self.run_script(script)
        try:
            written = int(written or 0)
        except (TypeError, ValueError):
            written = 0
        LOG.info("google cookies: wrote %s of %s pairs", written, len(pairs))
        return written

    ACCOUNT_JS = r"""(() => {
      const btn = document.querySelector(
        'a[href*="SignOutOptions"], a[aria-label*="Google Account"], a[href*="EditUserOptions"]');
      const signIn = document.querySelector(
        'a[href*="ServiceLogin"], a[aria-label*="Sign in"], a[aria-label*="Войти"]');
      const label = btn ? (btn.getAttribute('aria-label') || btn.title || '') : '';
      const m = label.match(/[\w.+-]+@[\w-]+\.[\w.-]+/);
      let name = label.replace(/^Google Account:\s*/, '');
      if (m) name = name.replace('(' + m[0] + ')', '');
      name = name.replace(/[()]/g, ' ').replace(/\s+/g, ' ').trim();
      return JSON.stringify({ signed_in: !!btn, email: m ? m[0] : '', name: name,
                              has_signin_link: !!signIn });
    })()"""

    HISTORY_JS = r"""(() => {
      const out = [];
      const seen = new Set();
      const take = (a) => {
        const href = a.href || '';
        const title = (a.innerText || a.textContent || '').replace(/\s+/g, ' ').trim();
        if (!href || !title || title.length < 3) return;
        if (!/udm=50|\/aimode|ai_mode/.test(href)) return;
        if (seen.has(href)) return;
        seen.add(href);
        out.push({ title: title.slice(0, 120), url: href });
      };
      document.querySelectorAll('a[href]').forEach(take);
      return JSON.stringify(out.slice(0, 60));
    })()"""

    OPEN_HISTORY_JS = r"""(() => {
      const label = /(history|recent|recents|история|недавн)/i;
      const buttons = [...document.querySelectorAll('button, [role="button"], a')];
      const target = buttons.find((b) => label.test(
        (b.getAttribute('aria-label') || '') + ' ' + (b.innerText || '')));
      if (!target) return 'no-history-button';
      target.click();
      return 'clicked';
    })()"""

    def script_result(self, script: str):
        """Run JS straight in WebView2 and return the decoded value.

        ``window.evaluate_js`` is gated on pywebview's ``_pywebviewready``
        event, which on a freshly shown window costs a 20 s wait and then the
        "Main window failed to start" error. WebView2's own ExecuteScriptAsync
        has no such gate, so the sign-in watcher uses it and polls quickly.
        """
        task = self._on_ui(lambda core: core.ExecuteScriptAsync(script), limit=6.0)
        if task is None:
            # No native WebView2 here: this is a real browser, and its page is
            # reachable through the DevTools protocol the window already
            # exposes. Without this branch the account reader simply answered
            # None for every browser window -- so the screen never learned who
            # was signed in.
            runner = getattr(self.window, "evaluate_js_value", None) \
                or self.window.evaluate_js
            result = runner(script, timeout=6.0)
            if result is None:
                return None
            text = str(result).strip()
            if not text or text == "null":
                return None
            try:
                return json.loads(text)
            except ValueError:
                return text.strip('"')
        ok, value = _await_task(task, 4.0)
        if not ok:
            return None
        text = str(value or "").strip()
        if not text or text == "null":
            return None
        try:
            return json.loads(text)
        except ValueError:
            return text.strip('"')

    AUTH_COOKIE_NAMES = ("SID", "__Secure-1PSID", "__Secure-3PSID", "SAPISID")

    def jar_signed_in(self) -> bool:
        """True when the browser jar itself carries Google's auth cookies.

        The DOM anchor is primary (it also names the account), but Google is
        free to restyle its header. The jar cannot be restyled: a live session
        always has SID or __Secure-1PSID.
        """
        browser_pairs = (self._cookies_from_browser()
                         if getattr(self.window, "is_browser", False) is True
                         else [])
        if browser_pairs:
            names = {name for name, _value in browser_pairs}
            return bool(names & set(self.AUTH_COOKIE_NAMES))
        if self._edge_manager() is None:
            return False
        names = set()
        for name, _value, _domain in self._cookies_direct("https://www.google.com/", limit=3.0):
            names.add(name)
        return bool(names & set(self.AUTH_COOKIE_NAMES))

    def _email_via_http(self) -> str:
        """Ask Google who this cookie jar belongs to (slow path, rare)."""
        try:
            from arena import google_http  # local import: no cycle at load time
            raw = self.cookies(tries=1)
            if not raw:
                return ""
            client = google_http.GoogleHTTP(cookie=raw)
            return str((client.account() or {}).get("email") or "")
        except Exception:
            LOG.info("google email lookup over HTTPS failed")
            return ""

    def account(self) -> dict:
        """Read the signed-in Google identity from the page.

        Google's own account button (`a[href*=SignOutOptions]`) is the proof:
        a stray email-looking string in the body is not enough, because search
        results quote addresses all the time.
        """
        # WebView2 first. The pywebview fallback is deliberately last and only
        # when no native control exists: window.evaluate_js waits on an event
        # that a never-shown window never fires, and on a *shown* window it can
        # hang on a blocking wait — from an HTTP thread that freezes the app.
        info = self.script_result(self.ACCOUNT_JS)
        if info is None and getattr(self.window, "is_browser", False) is True:
            # A browser window answers through the DevTools protocol, which is
            # `evaluate_js` on this class already; nothing else to do here, but
            # the call must not fall through to a WebView2-only path.
            pass
        data = {}
        if isinstance(info, str) and info.strip():
            try:
                data = json.loads(info)
            except ValueError:
                data = {}
        elif isinstance(info, dict):
            data = info
        email = str(data.get("email") or "").strip()
        name = str(data.get("name") or "").strip()
        signed = bool(data.get("signed_in"))
        if not signed and self.jar_signed_in():
            # Session is real even if the header anchor was not found. The
            # address is then asked of Google over HTTPS once, so the account
            # card still shows a person instead of a blank line.
            signed = True
            if not email:
                email = self._email_via_http()
                name = name or email
        return {"signed_in": signed, "email": email if signed else "",
                "name": (name or email) if signed else "",
                "email_known": bool(email) if signed else False,
                "signin_link": bool(data.get("has_signin_link"))}

    def page_url(self) -> str:
        """The address the window is on, read from the browser itself."""
        value = self._on_ui(lambda core: str(core.Source or ""), limit=2.0)
        return value if isinstance(value, str) else ""

    def verify(self, timeout: float = None) -> dict:
        """Reload Google and confirm who is signed in — no blind trust."""
        if not self.window:
            return {"signed_in": False, "email": "", "name": "",
                    "error": "The Google window is not open."}
        try:
            # Do not navigate away from a sign-in page: verify() is called from
            # the settings panel while the person may still be typing their
            # password. Read the identity where the window already is.
            current = self.page_url()
            if not current or "google." not in current:
                self.load(HOME_URL)
                self.wait_ready(LOAD_WAIT if timeout is None else timeout)
            time.sleep(0.5)
            info = self.account()
            info["checked_at"] = time.time()
            return info
        except Exception as e:  # a broken window must not crash the endpoint
            LOG.exception("google verify failed")
            return {"signed_in": False, "email": "", "name": "", "error": str(e)}

    def close_sign_in_window(self) -> None:
        """Close a single-use sign-in window and delete its profile.

        Called once the cookies are out. A persistent window is left alone: the
        person may still be looking at it.
        """
        window = self.window
        if window is None or getattr(window, "ephemeral", False) is not True:
            return
        LOG.info("[google-window] the single-use window has done its job; closing it")
        try:
            window.close_and_forget()
        except Exception:
            LOG.info("[google-window] could not close the single-use window")
        self.loaded = False
        self.visible = False
        self._sign_in_page_opened = False
        self._shown_evt.clear()

    def watch_sign_in(self, on_signed_in=None, interval: float = 0.6,
                      timeout: float = 900.0):
        """While the person signs in, poll the window; report when it's done.

        The callback closes the window, so sign-in ends the same way
        everywhere: you sign in, the window goes away, the app shows the
        account.
        """
        self.stop_watch()
        stop = threading.Event()
        self._watch_stop = stop

        def loop():
            end = time.time() + timeout
            while not stop.is_set() and time.time() < end:
                try:
                    if not self.window:
                        return
                    # Poll the page itself: the old guard required both the
                    # loaded flag and a google.com host, so detection waited
                    # for two separate events instead of just looking.
                    if self.ready():
                        info = self.account()
                        if info.get("signed_in"):
                            LOG.info("google sign-in detected: %s", info.get("email"))
                            try:
                                if on_signed_in:
                                    on_signed_in(info)
                            except Exception:
                                LOG.exception("google sign-in callback failed")
                            return
                except Exception:
                    LOG.debug("google sign-in watch tick failed", exc_info=True)
                stop.wait(interval)

        threading.Thread(target=loop, daemon=True, name="google-signin-watch").start()
        return True

    def stop_watch(self):
        stop = getattr(self, "_watch_stop", None)
        if stop is not None:
            stop.set()

    # -- history ---------------------------------------------------------
    def open_url(self, url: str):
        """Open one of Google's own threads (used by imported chats)."""
        url = str(url or "").strip()
        if not url.startswith("https://www.google.com/"):
            return False
        self.load(url)
        self.wait_ready(LOAD_WAIT)
        self.show()
        return True

    def history(self, limit: int = 40):  # pragma: no cover - superseded by HTTPS
        """Best-effort DOM scrape of the AI mode history (kept as a fallback).

        The sidebar list no longer uses this: ``GET /api/google/threads`` asks
        Google's own thread service over HTTPS, which returns every thread
        instead of whatever links happen to be on screen.
        """
        """Best-effort list of the person's Google AI mode threads.

        Google publishes no API for this, so the list is read from the AI mode
        page itself: open its history panel when there is one, then collect
        links that point at AI mode threads.  An empty list is a real answer
        ("nothing found"), never an invented one.
        """
        if not self.window:
            return {"items": [], "reason": "The Google window is not open."}
        try:
            if not self.host.endswith("google.com"):
                self.load(HOME_URL)
                self.wait_ready(LOAD_WAIT)
            if not self.ready():
                return {"items": [], "reason": "The Google window is still loading."}
            info = self.account()
            if not info.get("signed_in"):
                return {"items": [],
                        "reason": "Sign in to Google first: history lives in the account."}
            opened = self.run_script(self.OPEN_HISTORY_JS)
            if opened == "clicked":
                time.sleep(1.5)
            raw = self.run_script(self.HISTORY_JS)
            items = []
            if isinstance(raw, str) and raw.strip():
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        items = [i for i in parsed if isinstance(i, dict) and i.get("url")]
                except ValueError:
                    items = []
            elif isinstance(raw, list):
                items = raw
            reason = "" if items else "Google did not show any AI mode links on that page."
            return {"items": items[:limit], "reason": reason}
        except Exception as e:
            LOG.exception("google history failed")
            return {"items": [], "reason": str(e)}
    # -- transport -------------------------------------------------------
    @staticmethod
    def search_url(prompt: str, hl: str = "en") -> str:
        query = urllib.parse.urlencode({"q": prompt})
        return SEARCH_URL.format(query=query, hl=hl or "en")

    def _submit_in_page(self, prompt: str) -> bool:
        """Type the follow-up into Google's own box so the thread continues."""
        script = """(() => {
          const q = %s;
          const box = document.querySelector('textarea[name="q"], input[name="q"]');
          if (!box) return 'no-input';
          box.focus();
          const proto = box.tagName === 'TEXTAREA'
            ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
          const setter = Object.getOwnPropertyDescriptor(proto, 'value');
          if (setter && setter.set) setter.set.call(box, q); else box.value = q;
          box.dispatchEvent(new Event('input', { bubbles: true }));
          box.dispatchEvent(new Event('change', { bubbles: true }));
          const form = box.closest('form');
          const send = document.querySelector(
            'button[aria-label*="Send"], button[aria-label*="Отправить"], button[type="submit"]');
          if (send && form && form.contains(send)) { send.click(); return 'clicked'; }
          if (form && form.requestSubmit) { form.requestSubmit(); return 'submitted'; }
          if (form) { form.submit(); return 'submitted'; }
          const ev = new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true});
          box.dispatchEvent(ev);
          return 'enter';
        })()""" % json.dumps(prompt)
        result = self.run_script(script)
        LOG.info("google submit in page: %s", result)
        return result in ("clicked", "submitted", "enter")

    def _answer_text(self) -> Optional[str]:
        script = """(() => {
          const main = document.querySelector('#rcnt') || document.querySelector('main') || document.body;
          if (!main) return '';
          const t = (main.innerText || '').trim();
          return t;
        })()"""
        value = self.run_script(script)
        if value is None:
            return None
        return str(value)

    @staticmethod
    def clean_answer(text: str, prompt: str = "") -> str:
        lines = []
        for raw in (text or "").splitlines():
            line = raw.rstrip()
            low = line.strip().lower()
            if not low:
                if lines and lines[-1] != "":
                    lines.append("")
                continue
            if low in _NOISE or any(low.startswith(n) for n in _NOISE):
                continue
            if prompt and low == prompt.strip().lower():
                continue
            lines.append(line)
        out = "\n".join(lines).strip()
        while "\n\n\n" in out:
            out = out.replace("\n\n\n", "\n\n")
        return out

    def ask(self, prompt: str, *, first: bool = False, hl: str = "en"):
        """Generator of SSE-ready dicts for one Google AI mode turn."""
        if not self.window:
            yield {"type": "error", "code": "google_window",
                   "message": "Google AI mode works in the desktop app: the Google window "
                              "is not available in this build."}
            return
        if not self.ready():
            yield {"type": "error", "code": "google_ready",
                   "message": "The Google window is still loading. Wait for it to finish, then send again."}
            return
        self.last_error = None
        submitted = False
        if not first and self.host.endswith("google.com"):
            submitted = self._submit_in_page(prompt)
        if not submitted:
            self.load(self.search_url(prompt, hl))
            time.sleep(0.8)
            # A brand-new page needs its own load event. This wait is bounded
            # and happens *before* the answer clock starts: a slow navigation
            # must not eat the time reserved for Google to write.
            self.wait_ready(LOAD_WAIT)
        # The answer clock starts here, once the page is in place.
        started = time.time()
        yield {"type": "stage", "text": "Google AI mode is writing…"}

        best = ""
        stable = 0
        while time.time() - started < MAX_WAIT:
            time.sleep(POLL)
            raw = self._answer_text()
            if raw is None:
                continue
            text = self.clean_answer(raw, prompt)
            if len(text) > len(best) + 12:
                best = text
                stable = 0
                continue
            if text == best and best:
                stable += 1
                if stable >= 3:
                    break
            elif text and not best:
                best = text
                stable = 0
        elapsed = round(time.time() - started, 1)
        if not best:
            hint = self.last_error or "Google did not render an answer in this window."
            yield {"type": "error", "code": "google_no_answer",
                   "message": f"{hint} The window is open — finish any check there and send again."}
            return
        LOG.info("google answer: %s chars in %ss", len(best), elapsed)
        yield {"type": "text", "text": best}
        yield {"type": "done", "text": best, "elapsed": elapsed}


ENGINE = GoogleEngine()
