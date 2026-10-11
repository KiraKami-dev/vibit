"""
One website in one tab, and the actions a model may take on it.

`SiteSession` owns the tab (and popups it opens) for one website. After every action it settles the page with
generic helpers that make no decisions: sign in with the run's credentials when a login form shows, accept only
the necessary cookies, pass "slide to unlock / not a robot" checks, close modal popups through their close button,
reload transient error pages and sign in again after a session timeout. What to open, search, read and enter is
left to the model through `TOOLS`.
"""
import asyncio
import contextvars
import os
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from extract import FRAME_SECONDS, changes, locate, observe, select_options
from playwright.async_api import BrowserContext, Dialog, Page
from pydantic_ai import RunContext
from tracing import Trace

SETTLE_SECONDS = 45  # upper bound for waiting on a page and its helpers after one action
MAX_VIEW = 14000  # characters of a page shown at once; read_more pages through the rest
# A start waits on its tab while it loads: a slow site under load is still alive, and a new tab only queues behind
# the old request. But a tab can also hang for minutes in a context whose requests never complete while the same
# site opens at once in a fresh context (seen in sandbox runs). So an attempt without a usable page after RACE_AFTER
# gets a second tab in a fresh context next to it; the first usable page wins and the other tab is closed.
ATTEMPT_SECONDS = (45, 60, 90, 120)  # budgets of successive attempts (the last repeats)
START_SECONDS = float(os.environ.get("SESSION_START_SECONDS", "120"))  # no attempt runs longer than this
RACE_AFTER = 15  # an attempt without a usable page this long races a tab in a fresh context against its own
STUCK_SECONDS = 20  # a loaded page that answers no script call this long is dead: its tab is given up
PROBE_SECONDS = 5  # one check whether the page answers a script call
HELPER_SECONDS = 15  # one settle helper (sign-in, cookies, ...) on one page
GOTO_SECONDS = 30  # a navigation may take this long before the page is waited on in smaller steps
SLOW_PAGE = 10  # a navigation slower than this is traced
LOOK_SECONDS = 30  # one try to read a page that is still loading during a start
LOGIN_PATIENCE = 45  # a sign-in form still showing this soon after sending it is the post still pending
START_BACKOFF = (0, 10, 30)  # seconds between failed attempts (the last repeats); retries use a fresh context
START_MARGIN = 300  # by default a start keeps retrying until this long before the run's deadline
MIN_ATTEMPT = 15  # an attempt (or a racing tab) with less time than this is not begun
FRESH_AFTER = 1  # after this many failed attempts, each one runs in a fresh browser context (rules out the context)
DEADLINE: float | None = None  # the run's deadline (time.monotonic()); agent.solve sets it
_STARTING: contextvars.ContextVar = contextvars.ContextVar("starting", default=None)  # the session start() runs for
LOOP_VISITS = 3  # arriving on one page this often in a session gets the model a loop hint

# Closes modal <dialog>s and aria-modal popups through their own close button as soon as they open, and records
# what it closed. Links inside popups ("accept and continue") are never followed.
AUTO_CLOSE_JS = r"""
(() => {
  if (window.__arInit) return;
  window.__arInit = true;
  const CLOSE = /^(×|✕|✖|x|schließen|schliessen|close|dismiss|ausblenden|später|later|nicht jetzt|not now)$/i;
  const closed = (window.__arClosed = window.__arClosed || []);
  function closeButton(d) {
    for (const b of d.querySelectorAll('button,[role=button]')) {
      const name = (b.getAttribute('aria-label') || b.textContent || '').trim();
      if (CLOSE.test(name) || b.hasAttribute('data-close')) return b;
    }
    return null;
  }
  function check() {
    for (const d of document.querySelectorAll('dialog[open],[role=dialog][aria-modal=true],[role=alertdialog]')) {
      const modal = d.tagName !== 'DIALOG' || d.matches(':modal');
      if (!modal || d.dataset.arClosed) continue;
      const b = closeButton(d);
      if (!b) continue;
      d.dataset.arClosed = '1';
      closed.push((d.getAttribute('aria-label') || d.querySelector('h1,h2,h3')?.textContent || 'popup').trim());
      b.click();
      if (d.tagName === 'DIALOG' && d.open) d.close();
    }
  }
  const start = () => {
    check();
    new MutationObserver(check).observe(document.documentElement,
      {subtree: true, childList: true, attributes: true, attributeFilter: ['open', 'aria-modal', 'style', 'class']});
  };
  if (document.documentElement) start(); else document.addEventListener('DOMContentLoaded', start);
})();
"""

# "Slide to unlock" plus "I am not a robot" checks in any frame: drag the slider to its end, tick only boxes that
# are labelled as a robot check. Other checkboxes (terms, declarations) are left to the model.
HUMAN_CHECK_JS = r"""
() => {
  const ROBOT = /roboter|robot|mensch\b|human/i, SLIDE = /entsperr|unlock|zieh|slide|schieb/i;
  const labels = e => e.labels ? Array.from(e.labels).map(l => l.textContent) : [];
  const text = e => [e.getAttribute('aria-label') || '', ...labels(e)].join(' ');
  const done = [];
  for (const r of document.querySelectorAll('input[type=range]')) {
    const around = text(r) + ' ' + (r.parentElement ? r.parentElement.textContent : '');
    const max = r.max || '100';
    if (!SLIDE.test(around) || r.value === max || r.disabled) continue;
    r.value = max;
    r.dispatchEvent(new Event('input', {bubbles: true}));
    r.dispatchEvent(new Event('change', {bubbles: true}));
    done.push('unlock slider');
  }
  for (const c of document.querySelectorAll('input[type=checkbox]')) {
    if (c.checked || c.disabled || !ROBOT.test(text(c))) continue;
    c.click();
    done.push('robot check');
  }
  return done;
}
"""

ERROR_PAGE = re.compile(r"vorübergehend nicht verfügbar|temporarily unavailable|service unavailable|bad gateway|"
                        r"gateway time-?out|internal server error|bitte versuchen sie es (später )?erneut",
                        re.IGNORECASE)
COOKIE = re.compile(r"cookie", re.IGNORECASE)
COOKIE_ANSWER = re.compile(r"akzeptier|accept|zustimm|agree|ablehn|reject|erlaub|allow|bestätig|confirm|ok\b",
                           re.IGNORECASE)
COOKIE_MINIMAL = re.compile(r"notwendig|erforderlich|necessary|essential|nur technisch|ablehnen|reject|decline",
                            re.IGNORECASE)


def _budget(attempt: int) -> float:
    """Seconds start attempt `attempt` (from 1) may take."""
    return min(START_SECONDS, ATTEMPT_SECONDS[min(attempt, len(ATTEMPT_SECONDS)) - 1])


async def _close_context(context: BrowserContext | None) -> None:
    if context is None:
        return
    try:
        await asyncio.wait_for(context.close(), 10)
    except Exception:
        pass


@dataclass
class SiteSession:
    """A website of the run (`siteId`, `url`, `credentials`) worked in its own tab."""

    site: dict
    context: BrowserContext
    trace: Trace
    pages: list[Page] = field(default_factory=list)
    events: list[str] = field(default_factory=list)  # what the helpers did since the model last looked
    view: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stamp: int = 0
    logins: int = 0
    seen: str = ""  # every view's text this session, to check notes against
    shown: tuple = ()  # (url, title, view) the model has now, so an unchanged page is not sent again
    base: tuple = ()  # the same for the latest view it got in full; changes are told against this one
    visits: dict[str, int] = field(default_factory=dict)  # page URL -> times the model arrived on it
    last_url: str = ""
    scripted: object = None  # the context the popup closer is installed in
    until: float | None = None  # a running start retries until then; may be moved while it runs
    ok: bool = False  # a start succeeded
    settled: asyncio.Event = field(default_factory=asyncio.Event)  # the latest start ended, either way
    login_at: tuple = ()  # (time.monotonic(), url) of the latest sign-in the helpers sent
    _start_owner: object = None  # set while start() runs; its own looks (any task under it) must not wait
    _old_context: object = None
    _adopted_by: object = None  # a start contender whose tab won: the session that took its tab over

    @property
    def name(self) -> str:
        return self.site["siteId"]

    @property
    def page(self) -> Page:
        open_pages = [p for p in self.pages if not p.is_closed()]
        self.pages = open_pages
        return open_pages[-1]

    def default_until(self) -> float:
        if DEADLINE is not None:
            return DEADLINE - START_MARGIN
        return time.monotonic() + _budget(1) + START_BACKOFF[0] + _budget(2)

    @property
    def starting(self) -> bool:
        return self._start_owner is not None

    async def start(self, attempts: int | None = None, until: float | None = None) -> str:
        """
        Open the site's start page in a new tab, signed in. One attempt waits on its tab while the page loads (its
        budget grows with the attempts, ATTEMPT_SECONDS); after RACE_AFTER without a usable page a second tab in a
        fresh context races it, and the first usable page wins. A tab ends early on the browser's error page, when
        it closes, or when its page stops answering (STUCK_SECONDS). Failed attempts are retried in a fresh
        context after a short backoff until `until` (default: START_MARGIN before the run's deadline; `self.until`
        may be moved while this runs) or `attempts` attempts.
        """
        if until is not None or self.until is None:
            self.until = until if until is not None else self.default_until()
        self._start_owner = _STARTING.set(self)
        self.ok = False
        self.settled.clear()
        if self._old_context is None:
            self._old_context = self.context  # the context the session was made with
        began = time.monotonic()
        tries = 0
        try:
            while True:
                tries += 1
                end = min(time.monotonic() + _budget(tries), self.until)
                own = (self.context, self.scripted)
                fresh = await self._fresh_context() if tries > FRESH_AFTER else None
                if fresh is not None:
                    self.context, self.scripted = fresh, None
                try:
                    winner, view = await self._race(end, began)
                except asyncio.CancelledError:
                    await self._close_pages()
                    self.context, self.scripted = own
                    await _close_context(fresh)
                    raise
                except Exception as error:
                    self.trace.event(self.name, "start_failed", attempt=tries,
                                     seconds=round(time.monotonic() - began, 1),
                                     error=f"{type(error).__name__}: {str(error)[:160]}")
                    await self._close_pages()
                    self.context, self.scripted = own  # the fresh context did not help: back
                    await _close_context(fresh)
                else:
                    if winner is not self:
                        await self._adopt(winner)
                        await _close_context(fresh)  # this attempt's own context lost the race
                    self.ok = True
                    self.trace.event(self.name, "start_ok", attempt=tries, seconds=round(time.monotonic() - began, 1),
                                     fresh_context=self.context is not self._old_context, raced=winner is not self)
                    return view
                if attempts is not None and tries >= attempts:
                    break
                wait = START_BACKOFF[min(tries, len(START_BACKOFF)) - 1]
                if time.monotonic() + wait + MIN_ATTEMPT > self.until:
                    break
                self.trace.event(self.name, "start_retry", after=wait, attempt=tries + 1)
                await asyncio.sleep(wait)
            raise TimeoutError(f"{self.name}: the site could not be opened in {tries} attempts "
                               f"({round(time.monotonic() - began)} s)")
        finally:
            _STARTING.reset(self._start_owner)
            self._start_owner = None
            self.settled.set()

    async def _race(self, end: float, began: float) -> tuple["SiteSession", str]:
        """
        One attempt: a tab of this session, and after RACE_AFTER without a usable page a second one next to it, in a
        fresh context and a session of its own (nothing is given up: the first tab may still answer first). Returns
        the session whose tab showed a usable page first, and that page; the racer's tab and context are closed
        unless it won. Raises the last error when every tab failed.
        """
        tasks = {asyncio.create_task(self._attempt(end)): self}
        racer_context = None
        error: BaseException | None = None
        winner = None
        raced_at = time.monotonic() + RACE_AFTER
        try:
            while tasks:
                timeout = (None if racer_context is not None or raced_at == float("inf")
                           else max(0.0, raced_at - time.monotonic()))
                done, _ = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    contender = tasks.pop(task)
                    if task.exception() is None:
                        winner = contender
                        return contender, task.result()
                    error = task.exception()
                    if tasks:  # the other tab still runs: this one's failure is only traced
                        self.trace.event(self.name, "start_tab_failed", seconds=round(time.monotonic() - began, 1),
                                         raced=contender is not self,
                                         error=f"{type(error).__name__}: {str(error)[:160]}")
                    if contender is not self:
                        await contender._close_pages()
                if done:
                    continue
                if racer_context is None and end - time.monotonic() > MIN_ATTEMPT:
                    racer_context = await self._fresh_context(why="race")
                    if racer_context is not None:
                        racer = SiteSession(self.site, racer_context, self.trace)
                        racer.stamp = self.stamp
                        tasks[asyncio.create_task(racer._attempt(end))] = racer
                        continue
                raced_at = float("inf")  # no second tab (too little time, no context): wait on the first one
            raise error or TimeoutError("no tab")
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.wait(list(tasks), timeout=15)  # a cancel of this start itself still propagates
            for contender in tasks.values():
                if contender is not self:
                    await contender._close_pages()
            if racer_context is not None and (winner is None or winner.context is not racer_context):
                await _close_context(racer_context)

    async def _adopt(self, racer: "SiteSession") -> None:
        """Take over the racer's tab, context, sign-in state and view; its tab's handlers now report here."""
        await self._close_pages()
        racer._adopted_by = self
        self.context, self.scripted, self.pages, self.view = racer.context, racer.scripted, racer.pages, racer.view
        self.stamp, self.logins, self.login_at = max(self.stamp, racer.stamp), racer.logins, racer.login_at
        self.events.extend(racer.events)
        self.seen += racer.seen
        self.shown, self.base, self.last_url = racer.shown, racer.base, racer.last_url
        for url, count in racer.visits.items():
            self.visits[url] = self.visits.get(url, 0) + count

    async def _fresh_context(self, why: str = "retry") -> BrowserContext | None:
        """A new context of the same browser, so a broken context (a stuck connection, cookies, a hung tab) is
        ruled out. None when the browser cannot make one."""
        try:
            browser = self.context.browser
            if browser is None:
                return None
            fresh = await asyncio.wait_for(browser.new_context(viewport={"width": 1280, "height": 800}), 20)
        except Exception as error:
            self.trace.event(self.name, "helper_error", helper="fresh_context", error=str(error)[:160])
            return None
        self.trace.event(self.name, "helper", action="fresh_context", why=why)
        return fresh

    async def _close_pages(self) -> None:
        for page in self.pages:
            try:
                await asyncio.wait_for(page.close(), 10)
            except Exception:
                pass
        self.pages = []

    async def _attempt(self, end: float) -> str:
        """One tab of a start attempt: navigate, then settle and look until the page shows, the browser's error
        page shows, the tab closes, its page stops answering script calls, or `end`."""
        if self.scripted is not self.context:
            await self.context.add_init_script(AUTO_CLOSE_JS)
            self.scripted = self.context
        page = await self.context.new_page()
        self._watch(page)
        try:
            await page.goto(self.site["url"], wait_until="domcontentloaded",
                            timeout=max(1.0, min(GOTO_SECONDS, end - time.monotonic())) * 1000)
        except Exception as error:  # still loading: the request is not given up, the tab waits on
            self.trace.event(self.name, "start_wait", phase="goto", url=page.url[:120],
                             error=str(error).splitlines()[0][:120])
        self.logins = 0
        view, phase, silent = "", "goto", None

        def waiting(now: str) -> None:  # traced once per phase, not once per check
            nonlocal phase
            if now != phase:
                self.trace.event(self.name, "start_wait", phase=now, url=self.page.url[:120])
            phase = now

        while True:
            left = end - time.monotonic()
            self._check_alive()
            if left <= 1:
                if view:  # it shows something (a sign-in still pending): better than another tab
                    return view
                raise TimeoutError(f"still loading ({self.page.url[:80]})")
            answer = await self._answers()
            if answer != "yes":  # nothing on the tab answers: a navigation not yet committed, or a hung page
                committed = not self.page.url.startswith("about:")
                if answer == "hung" and committed:
                    silent = silent or time.monotonic() - PROBE_SECONDS
                    if time.monotonic() - silent >= STUCK_SECONDS:
                        raise ConnectionError(f"the page does not respond ({self.page.url[:80]})")
                waiting("respond" if committed else "commit")
                await asyncio.sleep(min(1.0, max(0.0, left - 1)))
                continue
            silent = None
            try:
                await asyncio.wait_for(self._settle(), min(SETTLE_SECONDS, left))
            except asyncio.TimeoutError:  # the page answers but keeps loading: look at what it shows anyway
                waiting("settle")
            self._check_alive()
            try:
                view = await asyncio.wait_for(self.look(), max(1.0, min(LOOK_SECONDS, end - time.monotonic())))
            except asyncio.TimeoutError:
                waiting("look")
                view = ""
                continue
            if not self.view.strip() or self.view.startswith(("(the page does not respond", "(page error")):
                view = ""
                waiting("render")
                await asyncio.sleep(1.0)
                continue
            if await self._login_pending():
                waiting("login")
                await asyncio.sleep(2.0)
                continue
            return view

    async def _answers(self, page: Page | None = None) -> str:
        """Whether the page (default: the tab's) answers a script call: "yes", "hung" (no answer within
        PROBE_SECONDS; e.g. a navigation that never commits, or a hung page) or "error" (e.g. it navigated)."""
        try:
            await asyncio.wait_for((page or self.page).evaluate("document.readyState"), PROBE_SECONDS)
            return "yes"
        except asyncio.TimeoutError:
            return "hung"
        except Exception:
            return "error"

    async def _wait_answer(self, page: Page) -> bool:
        """Wait up to STUCK_SECONDS for the page to answer a script call (a navigation still committing)."""
        until = time.monotonic() + STUCK_SECONDS
        while True:
            answer = await self._answers(page)
            if answer == "yes":
                return True
            if time.monotonic() >= until or page.is_closed():
                return False
            if answer == "error":
                await asyncio.sleep(0.5)

    def _check_alive(self) -> None:
        """Raise when the attempt's tab is provably dead: closed, or the browser's own error page."""
        open_pages = [p for p in self.pages if not p.is_closed()]
        if not open_pages:
            raise ConnectionError("the tab closed")
        if open_pages[-1].url.startswith("chrome-error:"):
            raise ConnectionError(f"the start page did not load ({open_pages[-1].url[:80]})")

    async def _login_pending(self) -> bool:
        """The sign-in was just sent and its form still shows: the post-login page is still loading."""
        if not self.login_at or time.monotonic() - self.login_at[0] > START_SECONDS:
            return False
        if self.page.url != self.login_at[1]:
            return False
        try:
            return bool(await asyncio.wait_for(self.page.locator("input[type=password]:visible").count(), 5))
        except Exception:
            return False

    def _watch(self, page: Page) -> None:
        self.pages.append(page)
        page.on("dialog", lambda dialog: self._owner()._on_dialog(dialog))
        page.on("popup", lambda popup: self._owner()._on_popup(popup))

    def _owner(self) -> "SiteSession":
        """The session a tab reports to: a start contender's tab reports to the session that took it over."""
        return self._adopted_by or self

    def _on_popup(self, popup: Page) -> None:
        self._watch(popup)
        self.events.append(f"a new window opened ({popup.url}); you are now in it, it closes back to the last one")

    async def _on_dialog(self, dialog: Dialog) -> None:
        self.events.append(f"browser {dialog.type} said: {dialog.message!r}; accepted")
        await dialog.accept()

    async def revive(self) -> None:
        """A session whose tabs are all gone (e.g. after a failed start) gets a fresh tab on the site's start page,
        so looking and acting never fail on an empty tab list. While a start runs in another task, this waits for
        it (its tab is the one to use), so a session can be handed out before it is up."""
        if self.starting and _STARTING.get() is not self:
            self.trace.event(self.name, "helper", action="wait_for_start")
            await self.settled.wait()
        if any(not page.is_closed() for page in self.pages):
            return
        page = await self.context.new_page()
        self._watch(page)
        self.events.append("the tab was gone; opened the website again")
        self.trace.event(self.name, "helper", action="revive")
        await self._goto(self.site["url"])

    async def _goto(self, url: str) -> None:
        """Open `url` in the tab and settle: at most GOTO_SECONDS plus SETTLE_SECONDS; slow ones are traced."""
        began = time.monotonic()
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=GOTO_SECONDS * 1000)
        except Exception as error:  # a slow load event still leaves a usable page
            first = str(error).splitlines()[0][:160]
            self.events.append(f"navigation was slow or failed: {first}")
            self.trace.event(self.name, "slow_page", phase="goto", url=url[:120],
                             seconds=round(time.monotonic() - began, 1), error=first)
        loaded = time.monotonic()
        await self.settle()
        if time.monotonic() - began > SLOW_PAGE:
            self.trace.event(self.name, "slow_page", phase="done", url=url[:120],
                             goto=round(loaded - began, 1), settle=round(time.monotonic() - loaded, 1))

    async def settle(self) -> None:
        """Let the page finish and run the generic helpers, but never longer than SETTLE_SECONDS: a click that never
        returns (seen on a cookie notice) must not block the tab for minutes."""
        try:
            await asyncio.wait_for(self._settle_fresh(), SETTLE_SECONDS)
        except asyncio.TimeoutError:
            self.events.append("the page kept loading; continuing with what it shows")
            self.trace.event(self.name, "helper_error", helper="settle", error=f"timeout after {SETTLE_SECONDS} s")

    async def _settle_fresh(self) -> None:
        self.logins = 0  # a timeout later in the run may need a fresh sign-in
        await self._settle()

    async def _settle(self) -> None:
        page = self.page
        for _ in range(6):
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                await page.wait_for_load_state("networkidle", timeout=2500)
            except Exception:
                pass
            if not await self._wait_answer(page):  # its helpers would hang on every page call
                self.trace.event(self.name, "helper_error", helper="settle",
                                 error=f"the page answers no script call ({page.url[:80]})")
                return
            if (await self._safely(self._login, page) or await self._safely(self._cookies, page)
                    or await self._safely(self._error_page, page) or await self._safely(self._human_check, page)):
                page = self.page
                continue
            break
        await self._collect_closed(page)

    async def _safely(self, helper, page: Page) -> bool:
        """A helper that fails (a slow page, a detached element) just did nothing; the model sees the page as is. A
        helper never takes longer than HELPER_SECONDS: page calls on a page that stops answering wait for ever."""
        try:
            return await asyncio.wait_for(helper(page), HELPER_SECONDS)
        except Exception as error:
            text = (str(error).splitlines() or [""])[0][:160] or f"{type(error).__name__} after {HELPER_SECONDS} s"
            self.trace.event(self.name, "helper_error", helper=helper.__name__, error=text)
            return False

    async def _login(self, page: Page) -> bool:
        password = page.locator("input[type=password]:visible")
        if not await password.count() or self.logins >= 2:
            return False
        if self.login_at and self.login_at[1] == page.url and time.monotonic() - self.login_at[0] < LOGIN_PATIENCE:
            return False  # sent moments ago and the old page still shows: the post is pending, do not resend it
        self.logins += 1
        form = password.first.locator("xpath=ancestor::form[1]")
        scope = form if await form.count() else page.locator("body")
        user = scope.locator("input:visible:not([type=password]):not([type=hidden]):not([type=checkbox])"
                             ":not([type=radio]):not([type=submit]):not([type=button])")
        if not await user.count():
            return False
        creds = self.site["credentials"]
        await user.first.fill(creds["username"])
        await password.first.fill(creds["password"])
        try:
            await password.first.press("Enter", timeout=5000)
        except Exception:
            pass  # the form was sent; the page is still loading, which settle waits for
        self.login_at = (time.monotonic(), page.url)
        self.events.append("signed in with this run's login")
        self.trace.event(self.name, "helper", action="login", url=page.url)
        await asyncio.sleep(0.3)
        return True

    async def _cookies(self, page: Page) -> bool:
        buttons = page.locator("button:visible, input[type=submit]:visible")
        candidates = []
        for i in range(min(await buttons.count(), 20)):
            button = buttons.nth(i)
            text = (await button.inner_text() if await button.evaluate("e => e.tagName") == "BUTTON"
                    else await button.get_attribute("value")) or ""
            if COOKIE.search(text) and COOKIE_ANSWER.search(text):
                candidates.append((not COOKIE_MINIMAL.search(text), text.strip(), button))
        if not candidates:
            return False
        candidates.sort(key=lambda c: c[0])  # the necessary-only choice first
        _, text, button = candidates[0]
        await button.click()
        self.events.append(f"cookie notice answered with {text!r}")
        self.trace.event(self.name, "helper", action="cookies", choice=text)
        await asyncio.sleep(0.3)
        return True

    async def _human_check(self, page: Page) -> bool:
        done = []
        for frame in page.frames:
            try:
                done += await asyncio.wait_for(frame.evaluate(HUMAN_CHECK_JS), FRAME_SECONDS)
            except Exception:
                continue
        if not done:
            return False
        self.events.append("unlocked a human check (" + ", ".join(sorted(set(done))) + ")")
        self.trace.event(self.name, "helper", action="human_check", done=done)
        await asyncio.sleep(0.4)
        return True

    async def _error_page(self, page: Page) -> bool:
        try:
            body = await page.locator("body").inner_text(timeout=3000)
        except Exception:
            return False
        if len(body) > 1500 or not ERROR_PAGE.search(body):
            return False
        self.events.append("a transient error page showed; reloaded")
        self.trace.event(self.name, "helper", action="reload", url=page.url)
        await asyncio.sleep(1.0)
        try:
            await page.reload(wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        return True

    async def _collect_closed(self, page: Page) -> None:
        for frame in page.frames:
            try:
                closed = await asyncio.wait_for(frame.evaluate("(window.__arClosed || []).splice(0)"),
                                                FRAME_SECONDS)
            except Exception:
                continue
            for title in closed:
                self.events.append(f"closed popup {title!r} with its close button")
                self.trace.event(self.name, "helper", action="close_popup", title=title)

    async def look(self, offset: int = 0, brief: bool = False, fresh: bool = False) -> str:
        """What the model sees: the helpers' events, the tab, then the page text with refs.

        After an action on the same page, only the changed lines are sent when that is much shorter: the model still
        has its latest full view (worker.py keeps it), and unchanged lines keep their refs. Only with `brief` (the
        browser tools, whose result the model gets as is). `fresh` marks a whole view the model now holds (a
        worker's first prompt); other callers (helpers that reshape the text) get it whole and reset the baseline.
        """
        await self.revive()
        page = self.page
        await self._collect_closed(page)
        self.stamp += 1
        if offset == 0:
            try:
                self.view = await observe(page, str(self.stamp))
            except Exception as error:
                await asyncio.sleep(1.0)
                try:
                    self.view = (await observe(page, str(self.stamp)) if not page.is_closed()
                                 else f"(page error {error})")
                except Exception as again:  # e.g. a script on the page hangs: the tab does not answer at all
                    self.view = f"(the page does not respond: {type(again).__name__} {str(again)[:160]})"
        try:
            title = await asyncio.wait_for(page.title(), FRAME_SECONDS)
        except Exception:
            title = ""
        header = [f"URL: {page.url}", f"Title: {title}"]
        notes = []
        if len(self.pages) > 1:
            notes.append(f"Windows open: {len(self.pages)} (you are in the newest)")
        if offset == 0 and page.url != self.last_url:
            self.last_url = page.url
            self.visits[page.url] = self.visits.get(page.url, 0) + 1
            if self.visits[page.url] >= LOOP_VISITS:  # a loop guard: paging back and forth finds nothing new
                notes.append(f"Note: you have arrived on this page {self.visits[page.url]} times; going back and "
                             f"forth between the same pages shows nothing new. Act on what this page offers (open "
                             f"an entry, its menu or its actions) or take another route.")
        if self.events:
            notes.append("Since your last action: " + "; ".join(self.events))
            self.events.clear()
        key = (page.url, title, self.view)
        if offset == 0 and brief and key == self.shown:
            return "\n".join([*notes, "The page is unchanged since your last action (same text and refs)."])
        if offset == 0:
            self.trace.event(self.name, "view", quiet=True, url=page.url, text=self.view)
            self.seen += "\n" + self.view
            same_page = brief and self.base and self.base[:2] == key[:2] and len(self.view) <= MAX_VIEW
            diff = changes(self.base[2], self.view) if same_page else None
            if diff is not None:
                self.shown = key
                return "\n".join([*notes, "The page changed. Against your latest full view of it, only these parts "
                                   "differ now (- was there, + is new); everything else, refs included, is as there:",
                                   diff])
        text = self.view[offset:offset + MAX_VIEW]
        rest = len(self.view) - offset - len(text)
        if rest > 0:
            text += f"\n[... {rest} more characters: read_more(offset={offset + len(text)})]"
        self.shown = self.base = key if offset == 0 and rest <= 0 and (brief or fresh) else ()
        return "\n".join(header + notes) + "\n\n" + text

    async def act(self, kind: str, ref: str, fn) -> str:
        """Run one action on the element `ref`, settle, and return the new view."""
        async with self.lock:
            await self.revive()
            element = await locate(self.page, ref)
            if element is None:
                return (f"No element {ref} on this page (refs change with every view).\n\n"
                        + await self.look(brief=True))
            started = time.monotonic()
            before = (self.page.url, self.view)
            try:
                await fn(element)
            except Exception as error:
                first = str(error).splitlines()[0][:300]
                self.trace.event(self.name, "action_error", action=kind, ref=ref, error=first)
                return f"{kind} {ref} failed: {first}\n\n" + await self.look(brief=True)
            await asyncio.sleep(0.25)
            await self.settle()
            self.trace.event(self.name, "action", action=kind, ref=ref, seconds=round(time.monotonic() - started, 2))
            view = await self.look(brief=True)
            if (self.page.url, self.view) == before:
                view = (f"Nothing on the page changed after {kind} {ref}. Do not repeat it; check the page for what "
                        f"is still missing (a required field, a checkbox, another button).\n\n") + view
            return view


async def _click(element) -> None:
    try:
        await element.click(timeout=5000)
    except Exception:
        await element.evaluate("e => e.click()")  # covered by a banner or outside the scroll box


@dataclass
class Deps:
    """What a worker's tools reach: its website and the notes it keeps for the orchestrator."""

    session: SiteSession
    notes: dict[str, str] = field(default_factory=dict)
    refused: dict[str, int] = field(default_factory=dict)


async def goto(ctx: RunContext[Deps], url: str) -> str:
    """Open a URL or a path on this website (e.g. a link's target from the page)."""
    session = ctx.deps.session
    await session.revive()
    target = urljoin(session.page.url, url)
    if urlparse(target).netloc != urlparse(session.site["url"]).netloc:
        return f"{target} is not on this website ({session.site['url']}); stay on it."
    async with session.lock:
        await session._goto(target)
        session.trace.event(session.name, "action", action="goto", url=target)
        return await session.look(brief=True)


async def click(ctx: RunContext[Deps], ref: str) -> str:
    """Click the element with this ref (link, button, tab, summary, checkbox)."""
    return await ctx.deps.session.act("click", ref, _click)


async def fill(ctx: RunContext[Deps], ref: str, text: str, submit: bool = False) -> str:
    """Type text into a textbox or textarea (replaces its content), or set a slider to a number.

    submit=true presses Enter afterwards, which runs a search or sends the field's form.
    """
    async def run(element):
        if await element.evaluate("e => e.type") == "range":
            await element.evaluate("(e, v) => { e.value = v; e.dispatchEvent(new Event('input', {bubbles: true}));"
                                   " e.dispatchEvent(new Event('change', {bubbles: true})); }", text)
        else:
            await element.fill(text, timeout=5000)
        if submit:
            await element.press("Enter")
    return await ctx.deps.session.act("fill", ref, run)


LABELS_JS = r"""
e => Array.from(e.options || []).map(o =>
  [o.textContent.replace(/[\u200b\u200c\u200d\u2060\ufeff\u00ad]/g, '').replace(/\s+/g, ' ').trim(), o.value])
"""


def _option_index(choices: list[list[str]], option: str) -> int | None:
    """The option whose visible text (else value) is `option`, ignoring spacing and then case."""
    want = " ".join(option.split())
    for match in (lambda t, v: t == want, lambda t, v: v == option, lambda t, v: t.casefold() == want.casefold()):
        for i, (text, value) in enumerate(choices):
            if match(text, value):
                return i
    return None


def _close_options(texts: list[str], option: str, limit: int = 8) -> list[str]:
    """Options that contain `option`, else any of its words, for a helpful error."""
    want = option.casefold().strip()
    hits = [t for t in texts if want and want in t.casefold()]
    if not hits:
        words = [w for w in want.replace(",", " ").split() if len(w) > 1]
        hits = [t for t in texts if any(w in t.casefold() for w in words)]
    return hits[:limit]


async def select(ctx: RunContext[Deps], ref: str, option: str) -> str:
    """Choose an option of a select by its visible text."""
    async def run(element):
        choices = await element.evaluate(LABELS_JS)
        index = _option_index(choices, option)
        if index is None:
            texts = [text for text, _ in choices]
            close = _close_options(texts, option)
            hint = f"options like it: {' | '.join(close)}" if close else f"options({ref}, contains) searches them"
            raise ValueError(f"no option {option!r} among {len(texts)}; {hint}")
        await element.select_option(index=index, timeout=5000)
    return await ctx.deps.session.act("select", ref, run)


async def options(ctx: RunContext[Deps], ref: str, contains: str = "") -> str:
    """List the options of a select (views shorten long lists), or only those whose text contains `contains`."""
    session = ctx.deps.session
    async with session.lock:
        found = await select_options(session.page, ref, contains)
    if found is None:
        return f"No select {ref} on this page (refs change with every view)."
    total, hits = found
    which = f" containing {contains!r}" if contains else ""
    text = f"{len(hits)} of {total} options of {ref}{which}:\n" + "\n".join(hits[:300])
    if len(hits) > 300:
        text += f"\n... {len(hits) - 300} more; narrow with contains"
    session.seen += "\n" + text
    return text


async def check(ctx: RunContext[Deps], ref: str, checked: bool = True) -> str:
    """Tick (checked=true) or untick a checkbox or radio button."""
    async def run(element):
        try:
            await element.set_checked(checked, timeout=5000)
        except Exception:
            await element.evaluate("(e, c) => { if (e.checked !== c) e.click(); }", checked)
    return await ctx.deps.session.act("check", ref, run)


async def back(ctx: RunContext[Deps], reason: str = "") -> str:
    """Go back to the previous page."""
    session = ctx.deps.session
    async with session.lock:
        try:
            await session.page.go_back(wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass
        await session.settle()
        return await session.look(brief=True)


async def read_more(ctx: RunContext[Deps], offset: int) -> str:
    """Show the rest of a long page from the offset the view gave."""
    return await ctx.deps.session.look(offset)


# A run of value characters; those with a digit are values. (The former single pattern, [..]*\d[..]*, retried
# every start of a long run without a digit: quadratic, minutes on one long token.)
VALUE_RUN = re.compile(r"[\w/.:,+-]+")
DIGIT = re.compile(r"\d")


def unseen_values(text: str, seen: str) -> list[str]:
    """The values in `text` (anything with a digit: numbers, references, dates) that `seen` does not contain."""
    missing = []  # a value holds no whitespace, so searching `seen` as is matches its whitespace-normalised form
    for token in VALUE_RUN.findall(text):
        if not DIGIT.search(token):
            continue
        token = token.strip(".,:;/-+")
        if token and token not in seen and token not in missing:
            missing.append(token)
    return missing


async def note(ctx: RunContext[Deps], key: str, value: str) -> str:
    """Record a fact you can see on the current page (copy values exactly), so it survives if you run out of time."""
    missing = unseen_values(value, ctx.deps.session.seen)
    if missing:
        ctx.deps.session.trace.event(ctx.deps.session.name, "note_refused", key=key, missing=missing[:10])
        ctx.deps.refused[key] = ctx.deps.refused.get(key, 0) + 1
        if ctx.deps.refused[key] > 2:
            return (f"Not noted, again. Stop trying to note {key!r}; continue with your goal from what the current "
                    f"page shows.")
        return (f"Not noted: {', '.join(missing[:10])} appear on no page you have seen. Note only values copied "
                f"from a page; open the page again if you need to check.")
    ctx.deps.notes[key] = value
    ctx.deps.session.trace.event(ctx.deps.session.name, "note", key=key, value=value)
    return f"noted {key}"


TOOLS = [goto, click, fill, select, options, check, back, read_more, note]
