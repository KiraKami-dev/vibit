"""
One website in one tab, and the actions a model may take on it.

`SiteSession` owns the tab (and popups it opens) for one website. After every action it settles the page with
generic helpers that make no decisions: sign in with the run's credentials when a login form shows, accept only
the necessary cookies, pass "slide to unlock / not a robot" checks, close modal popups through their close button,
reload transient error pages and sign in again after a session timeout. What to open, search, read and enter is
left to the model through `TOOLS`.
"""
import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from extract import changes, locate, observe, select_options
from playwright.async_api import BrowserContext, Dialog, Page
from pydantic_ai import RunContext
from tracing import Trace

SETTLE_SECONDS = 45  # upper bound for waiting on a page and its helpers after one action
MAX_VIEW = 14000  # characters of a page shown at once; read_more pages through the rest
START_SECONDS = float(os.environ.get("SESSION_START_SECONDS", "90"))  # one sign-in attempt
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
    scripted: bool = False  # the popup closer is installed in this context

    @property
    def name(self) -> str:
        return self.site["siteId"]

    @property
    def page(self) -> Page:
        open_pages = [p for p in self.pages if not p.is_closed()]
        self.pages = open_pages
        return open_pages[-1]

    async def start(self, attempts: int = 2) -> str:
        """
        Open the site's start page in a new tab, signed in, within START_SECONDS, once more on failure: a hung
        sign-in (a cookie button that never answers, a page that never loads) must not hold a worker, or the
        whole bid, for minutes. A tab that lands on the browser's own error page counts as failed.
        """
        for attempt in range(attempts):
            try:
                view = await asyncio.wait_for(self._open(), START_SECONDS)
                if self.page.url.startswith("chrome-error:") or not self.view.strip():
                    raise ConnectionError(f"the start page did not load ({self.page.url[:80]})")
                return view
            except Exception as error:
                self.trace.event(self.name, "start_failed", attempt=attempt + 1,
                                 error=f"{type(error).__name__}: {str(error)[:160]}")
                for page in self.pages:
                    try:
                        await page.close()
                    except Exception:
                        pass
                self.pages = []
        raise TimeoutError(f"{self.name}: the site could not be opened in {attempts} attempts")

    async def _open(self) -> str:
        if not self.scripted:
            await self.context.add_init_script(AUTO_CLOSE_JS)
            self.scripted = True
        page = await self.context.new_page()
        self._watch(page)
        await self._goto(self.site["url"])
        return await self.look()

    def _watch(self, page: Page) -> None:
        self.pages.append(page)
        page.on("dialog", self._on_dialog)
        page.on("popup", self._on_popup)

    def _on_popup(self, popup: Page) -> None:
        self._watch(popup)
        self.events.append(f"a new window opened ({popup.url}); you are now in it, it closes back to the last one")

    async def _on_dialog(self, dialog: Dialog) -> None:
        self.events.append(f"browser {dialog.type} said: {dialog.message!r}; accepted")
        await dialog.accept()

    async def _goto(self, url: str) -> None:
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as error:  # a slow load event still leaves a usable page
            self.events.append(f"navigation was slow or failed: {str(error).splitlines()[0][:160]}")
        await self.settle()

    async def settle(self) -> None:
        """Let the page finish and run the generic helpers, but never longer than SETTLE_SECONDS: a click that never
        returns (seen on a cookie notice) must not block the tab for minutes."""
        try:
            await asyncio.wait_for(self._settle(), SETTLE_SECONDS)
        except asyncio.TimeoutError:
            self.events.append("the page kept loading; continuing with what it shows")
            self.trace.event(self.name, "helper_error", helper="settle", error=f"timeout after {SETTLE_SECONDS} s")

    async def _settle(self) -> None:
        page = self.page
        self.logins = 0  # a timeout later in the run may need a fresh sign-in
        for _ in range(6):
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                await page.wait_for_load_state("networkidle", timeout=2500)
            except Exception:
                pass
            if (await self._safely(self._login, page) or await self._safely(self._cookies, page)
                    or await self._safely(self._error_page, page) or await self._safely(self._human_check, page)):
                page = self.page
                continue
            break
        await self._collect_closed(page)

    async def _safely(self, helper, page: Page) -> bool:
        """A helper that fails (a slow page, a detached element) just did nothing; the model sees the page as is."""
        try:
            return await helper(page)
        except Exception as error:
            self.trace.event(self.name, "helper_error", helper=helper.__name__, error=str(error).splitlines()[0][:160])
            return False

    async def _login(self, page: Page) -> bool:
        password = page.locator("input[type=password]:visible")
        if not await password.count() or self.logins >= 2:
            return False
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
                done += await frame.evaluate(HUMAN_CHECK_JS)
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
                closed = await frame.evaluate("(window.__arClosed || []).splice(0)")
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
        page = self.page
        await self._collect_closed(page)
        self.stamp += 1
        if offset == 0:
            try:
                self.view = await observe(page, str(self.stamp))
            except Exception as error:
                await asyncio.sleep(1.0)
                self.view = await observe(page, str(self.stamp)) if not page.is_closed() else f"(page error {error})"
        title = await page.title()
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


VALUE = re.compile(r"[\w/.:,+-]*\d[\w/.:,+-]*")


def unseen_values(text: str, seen: str) -> list[str]:
    """The values in `text` (anything with a digit: numbers, references, dates) that `seen` does not contain."""
    haystack = " ".join(seen.split())
    missing = []
    for token in VALUE.findall(text):
        token = token.strip(".,:;/-+")
        if token and token not in haystack and token not in missing:
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
