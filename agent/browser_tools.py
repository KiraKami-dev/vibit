"""
One website in one tab, and the actions a model may take on it.

`SiteSession` owns the tab (and popups it opens) for one website. After every action it settles the page with
generic helpers that make no decisions: sign in with the run's credentials when a login form shows, accept only
the necessary cookies, pass "slide to unlock / not a robot" checks, close modal popups through their close button,
reload transient error pages and sign in again after a session timeout. What to open, search, read and enter is
left to the model through `TOOLS`.
"""
import asyncio
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from extract import locate, observe
from playwright.async_api import BrowserContext, Dialog, Page
from pydantic_ai import RunContext
from tracing import Trace

MAX_VIEW = 14000  # characters of a page shown at once; read_more pages through the rest
ACTION_MS = 8000  # default limit of one browser operation (reading a label, typing, a scripted click)
HELPER_SECONDS = 20  # a helper or a page reading that takes longer hangs on a stuck page and is given up
MAX_PAGES = 15  # pages read_all_pages follows in one call
MAX_READ = 8  # pages read_pages opens in one call
MAX_PAGED = 45000  # characters read_all_pages or read_pages return in one call

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
    label: str = ""  # names this tab in the trace when a website is worked in several tabs
    runs: list[dict] = field(default_factory=list)  # per worker goal: requests, seconds, outcome

    @property
    def name(self) -> str:
        return self.label or self.site["siteId"]

    async def close(self) -> None:
        """Close this session's tab and the windows it opened; the website's sign-in stays with the context."""
        for page in self.pages:
            try:
                await page.close()
            except Exception:
                pass
        self.pages = []

    @property
    def page(self) -> Page:
        open_pages = [p for p in self.pages if not p.is_closed()]
        self.pages = open_pages
        return open_pages[-1]

    async def start(self) -> str:
        """Open the site's start page in a new tab, signed in."""
        await self.context.add_init_script(AUTO_CLOSE_JS)
        page = await self.context.new_page()
        self._watch(page)
        await self._goto(self.site["url"])
        return await self.look()

    def _watch(self, page: Page) -> None:
        self.pages.append(page)
        page.set_default_timeout(ACTION_MS)  # an operation on a stuck page fails soon instead of hanging
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
        """Let the page finish, then run the generic helpers until nothing changes."""
        page = self.page
        self.logins = 0  # a timeout later in the run may need a fresh sign-in
        unlocked = False
        for _ in range(6):
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                await page.wait_for_load_state("networkidle", timeout=2500)
                for frame in page.frames[1:]:  # an embedded document may still be loading its next page
                    await frame.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                pass
            if (await self._safely(self._login, page) or await self._safely(self._cookies, page)
                    or await self._safely(self._error_page, page)):
                page = self.page
                continue
            if await self._safely(self._human_check, page):
                unlocked = True
                continue
            break
        if unlocked:
            await self.finish_loading()
        await self._collect_closed(page)

    async def finish_loading(self, rounds: int = 4, pause: float = 0.5) -> None:
        """Let a page that fills in step by step finish (a pane that loads once a check is passed, a text that loads
        once the pane is there, a further check inside it): pause until two readings in a row are the same."""
        before = None
        for _ in range(rounds):
            await asyncio.sleep(pause)
            page = self.page
            try:
                await page.wait_for_load_state("networkidle", timeout=2500)
            except Exception:
                pass
            await self._safely(self._human_check, page)
            try:
                now = _plain(await asyncio.wait_for(observe(page, "settle"), HELPER_SECONDS))
            except Exception:
                return
            if now == before:
                return
            before = now

    async def _safely(self, helper, page: Page) -> bool:
        """A helper that fails (a slow page, a detached element) just did nothing; the model sees the page as is."""
        try:
            return await asyncio.wait_for(helper(page), HELPER_SECONDS)
        except Exception as error:
            reason = (str(error).splitlines() or [type(error).__name__])[0][:160]
            self.trace.event(self.name, "helper_error", helper=helper.__name__, error=reason)
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
                closed = await asyncio.wait_for(frame.evaluate("(window.__arClosed || []).splice(0)"), 5)
            except Exception:
                continue
            for title in closed:
                self.events.append(f"closed popup {title!r} with its close button")
                self.trace.event(self.name, "helper", action="close_popup", title=title)

    async def look(self, offset: int = 0) -> str:
        """What the model sees: the helpers' events, the tab, then the page text with refs."""
        page = self.page
        await self._collect_closed(page)
        self.stamp += 1
        if offset == 0:
            try:
                self.view = await asyncio.wait_for(observe(page, str(self.stamp)), HELPER_SECONDS)
            except Exception as error:
                await asyncio.sleep(1.0)
                try:
                    self.view = await asyncio.wait_for(observe(page, str(self.stamp)), HELPER_SECONDS)
                except Exception:
                    self.view = f"(the page could not be read: {type(error).__name__}; reload it or go back)"
        try:
            title = await asyncio.wait_for(page.title(), 5)
        except Exception:
            title = ""
        header = [f"URL: {page.url}", f"Title: {title}"]
        if len(self.pages) > 1:
            header.append(f"Windows open: {len(self.pages)} (you are in the newest)")
        if self.events:
            header.append("Since your last action: " + "; ".join(self.events))
            self.events.clear()
        if offset == 0:
            self.trace.event(self.name, "view", quiet=True, url=page.url, text=self.view)
            self.seen += "\n" + self.view
        text = self.view[offset:offset + MAX_VIEW]
        rest = len(self.view) - offset - len(text)
        if rest > 0:
            text += f"\n[... {rest} more characters: read_more(offset={offset + len(text)})]"
        return "\n".join(header) + "\n\n" + text

    async def act(self, kind: str, ref: str, fn, typed: str = "") -> str:
        """Run one action on the element `ref`, settle, and return the new view. `typed` goes into the trace."""
        async with self.lock:
            element = await locate(self.page, ref)
            if element is None:
                return f"No element {ref} on this page (refs change with every view).\n\n" + await self.look()
            started = time.monotonic()
            before = (self.page.url, self.view)
            try:
                await fn(element)
            except Exception as error:
                first = str(error).splitlines()[0][:200]
                self.trace.event(self.name, "action_error", action=kind, ref=ref, error=first)
                return f"{kind} {ref} failed: {first}\n\n" + await self.look()
            await asyncio.sleep(0.25)
            await self.settle()
            if kind == "click" and time.monotonic() - started < 0.5:
                # nothing was loading yet: a click that loads (an embedded document's next page) may start late
                await asyncio.sleep(0.4)
                await self.settle()
            self.trace.event(self.name, "action", action=kind, ref=ref, seconds=round(time.monotonic() - started, 2),
                             **({"typed": typed[:80]} if typed else {}))
            view = await self.look()
            if (self.page.url, self.view) == before:
                view = (f"Nothing on the page changed after {kind} {ref}. Do not repeat it; check the page for what "
                        f"is still missing (a required field, a checkbox, another button).\n\n") + view
            return view


async def _click(element) -> None:
    try:
        await element.click(timeout=5000)
    except Exception:  # covered by a banner or outside the scroll box
        await asyncio.wait_for(element.evaluate("e => e.click()"), 5)


@dataclass
class Deps:
    """What a worker's tools reach: its website and the notes it keeps for the orchestrator."""

    session: SiteSession
    notes: dict[str, str] = field(default_factory=dict)
    refused: dict[str, int] = field(default_factory=dict)
    deadline: float | None = None  # time.monotonic() value the worker has to be done by


async def goto(ctx: RunContext[Deps], url: str) -> str:
    """Open a URL or a path on this website (e.g. a link's target from the page)."""
    session = ctx.deps.session
    target = urljoin(session.page.url, url)
    if urlparse(target).netloc != urlparse(session.site["url"]).netloc:
        return f"{target} is not on this website ({session.site['url']}); stay on it."
    async with session.lock:
        await session._goto(target)
        session.trace.event(session.name, "action", action="goto", url=target)
        return await session.look()


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
    return await ctx.deps.session.act("fill", ref, run, typed=text)


async def select(ctx: RunContext[Deps], ref: str, option: str) -> str:
    """Choose an option of a select by its visible text."""
    async def run(element):
        try:
            await element.select_option(label=option, timeout=5000)
        except Exception:
            await element.select_option(option, timeout=5000)
    return await ctx.deps.session.act("select", ref, run)


async def check(ctx: RunContext[Deps], ref: str, checked: bool = True) -> str:
    """Tick (checked=true) or untick a checkbox or radio button."""
    async def run(element):
        try:
            await element.set_checked(checked, timeout=5000)
        except Exception:
            await element.evaluate("(e, c) => { if (e.checked !== c) e.click(); }", checked)
    return await ctx.deps.session.act("check", ref, run)


async def back(ctx: RunContext[Deps], url: str = "", ref: str = "") -> str:
    """Go back to the previous page. It takes no arguments; with a url it opens that page instead."""
    if url:
        return await goto(ctx, url)
    session = ctx.deps.session
    async with session.lock:
        try:
            await session.page.go_back(wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass
        await session.settle()
        return await session.look()


async def read_more(ctx: RunContext[Deps], offset: int) -> str:
    """Show the rest of a long page from the offset the view gave."""
    return await ctx.deps.session.look(offset)


CONTROL = re.compile(r'\[(e\d+) ([\w-]+) "([^"\n]*)"((?: -> [^\]\n]*)?(?: expanded=\w+)?)( disabled)?\]')
REF = re.compile(r"\[e\d+ ")
FRAME_OPEN, FRAME_CLOSE = "[frame ", "[/frame]"


def _plain(text: str) -> str:
    """A view without its refs, which change with every view: for comparing pages and for pages no longer open."""
    return REF.sub("[", text)


def _content(text: str) -> str:
    """What a page says, without refs, link targets and frame addresses (those count pages up while the content
    repeats): two pages with the same content are the same page."""
    lines = [line for line in _plain(text).splitlines() if not line.startswith(FRAME_OPEN)]
    return re.sub(r'" -> [^\]\n]*\]', '"]', "\n".join(lines))


def _controls(view: str, kind: str, name: str) -> list[re.Match]:
    return [m for m in CONTROL.finditer(view) if m.group(2) == kind and m.group(3) == name]


def _paged_part(view: str, at: int) -> tuple[str, bool]:
    """The part of `view` a paging control at offset `at` belongs to: the innermost embedded document around it
    (and True), or the whole view when the control is on the page itself."""
    open_at: list[int] = []
    position = 0
    for line in view.splitlines(keepends=True):
        stripped = line.strip()
        if position > at and not open_at:
            break
        if stripped.startswith(FRAME_OPEN) and not stripped.endswith("(separate)]"):
            open_at.append(position)
        elif stripped == FRAME_CLOSE and open_at:
            start = open_at.pop()
            if start <= at < position:
                return view[start:position + len(line)].strip(), True
        position += len(line)
    return view, False


def _new_lines(part: str, previous: str, framed: bool) -> tuple[str, int]:
    """`part` without the leading lines it shares with the previous page, and how many were dropped. An embedded
    document is cut only when it grew (a "load more" list); a whole page always loses its repeated top."""
    lines, old = part.splitlines(), _plain(previous).splitlines()
    same = 0
    while same < len(lines) and same < len(old) and _plain(lines[same]) == old[same]:
        same += 1
    if framed and same < max(3, len(old) - 3):  # a new page of the document, not the old one with more rows
        return part, 0
    return "\n".join(lines[same:]), same


async def read_all_pages(ctx: RunContext[Deps], ref: str) -> str:
    """Read every further page of the list, table or document you are looking at, in one step.

    Pass the ref of the control that leads to its next page ("next", "more", ">", a page number). It is followed
    again and again and all pages come back as one text. Use it instead of clicking through pages one by one.
    """
    session, deadline = ctx.deps.session, ctx.deps.deadline
    view = session.view
    first = next((m for m in CONTROL.finditer(view) if m.group(1) == ref.strip()), None)
    if first is None:
        return f"No link or button {ref} on this page (refs change with every view).\n\n" + await session.look()
    kind, name = first.group(2), first.group(3)
    same = _controls(view, kind, name)
    ordinal = next(i for i, m in enumerate(same) if m.group(1) == first.group(1))
    part, framed = _paged_part(view, first.start())
    pages, plain, control, reason = [part], [_content(part)], first, ""
    open_page = 0  # index of the page the browser shows when the loop ends
    used = min(len(part), MAX_VIEW)
    while True:
        if control is None:
            reason = f"the page shows no {kind} {name!r} any more: this was the last page"
        elif control.group(5):
            reason = f"the {kind} {name!r} is disabled: this was the last page"
        elif len(pages) >= MAX_PAGES:
            reason = f"{MAX_PAGES} pages are read per call; call read_all_pages again to continue from here"
        elif used >= MAX_PAGED:
            reason = "the output limit is reached; call read_all_pages again to continue from here"
        elif deadline is not None and time.monotonic() > deadline - 5:
            reason = "time is running out"
        if reason:
            break
        before = (session.page.url, _plain(session.view))
        await session.act("click", control.group(1), _click)
        if (session.page.url, _plain(session.view)) == before:  # a slow page: give it a moment before concluding
            await asyncio.sleep(1.5)
            async with session.lock:
                await session.settle()
                await session.look()
        view = session.view
        if name.isdigit():  # numbered page links: the next number leads on
            name = str(int(name) + 1)
        same = _controls(view, kind, name)
        control = same[min(ordinal, len(same) - 1)] if same else None
        if not framed:
            part = view
        elif control:
            part = _paged_part(view, control.start())[0]
        else:  # the control went with the last page: take the embedded document that changed
            blocks = [_paged_part(view, m.start() + 1)[0] for m in re.finditer(re.escape(FRAME_OPEN), view)]
            fresh = [block for block in blocks if _content(block) not in plain]
            part = min(fresh, key=len) if fresh else pages[open_page]
        if (session.page.url, _plain(view)) == before:
            reason = f"following {first.group(3)!r} changed nothing: this was the last page"
        elif _content(part) in plain:
            open_page = plain.index(_content(part))
            reason = f"the content of page {open_page + 1} came again: there are no further pages"
        if reason:
            if _content(part) == plain[open_page]:
                pages[open_page] = part  # the same content with the refs that work now
            break
        pages.append(part)
        plain.append(_content(part))
        open_page = len(pages) - 1
        used += min(len(part), MAX_VIEW)
    out = [f"URL: {session.page.url}", f"Read {len(pages)} page(s) by following {kind} {first.group(3)!r}; stopped "
           f"because {reason}.", "Only the paged part is shown, and only the refs of the page marked open work; "
           "read_more(offset=0) shows the whole current page."]
    if session.events:
        out.append("Since your last action: " + "; ".join(session.events))
        session.events.clear()
    for number, page_text in enumerate(pages, 1):
        cut_note = ""
        if number > 1:
            page_text, dropped = _new_lines(page_text, pages[number - 2], framed)
            if dropped:
                cut_note = f" ({dropped} leading lines are the same as on the page before and not repeated)"
        if number - 1 != open_page:
            page_text = _plain(page_text)
        if len(page_text) > MAX_VIEW:
            cut = len(page_text) - MAX_VIEW
            page_text = page_text[:MAX_VIEW] + f"\n[... page {number} is cut here, {cut} more characters]"
        out.append(f"\n===== page {number}{' (open now)' if number - 1 == open_page else ''}{cut_note} =====\n"
                   f"{page_text}")
    session.trace.event(session.name, "paged", pages=len(pages), reason=reason, control=first.group(3))
    session.trace.event(session.name, "paged_text", quiet=True, text="\n".join(out))
    return "\n".join(out)


async def read_pages(ctx: RunContext[Deps], urls: list[str]) -> str:
    """Open several pages of this website one after the other and read them all in one step.

    Pass link targets the current page shows (the part after "->"), e.g. the detail pages or tabs of several
    records you want to compare. Use it instead of opening them one by one.
    """
    session, deadline = ctx.deps.session, ctx.deps.deadline
    out, used, opened = [], 0, 0
    base = session.page.url
    for number, url in enumerate(urls[:MAX_READ], 1):
        target = urljoin(base, url)
        if urlparse(target).netloc != urlparse(session.site["url"]).netloc:
            out.append(f"\n===== {url}: not on this website, not opened")
            continue
        if used >= MAX_PAGED or (deadline is not None and time.monotonic() > deadline - 5):
            out.append(f"\n===== {url}: not opened (output limit or time); open it in a further call")
            continue
        async with session.lock:
            await session._goto(target)
            session.trace.event(session.name, "action", action="goto", url=target)
            await session.look()
        opened += 1
        text = session.view if number == min(len(urls), MAX_READ) else _plain(session.view)
        if len(text) > MAX_VIEW:
            text = text[:MAX_VIEW] + f"\n[... cut here, {len(text) - MAX_VIEW} more characters; open this page alone]"
        used += len(text)
        events = ""
        if session.events:
            events = "\nSince your last action: " + "; ".join(session.events)
            session.events.clear()
        out.append(f"\n===== {session.page.url}{events}\n{text}")
    head = (f"Opened {opened} of {len(urls)} pages. The last one is open now and only its refs work."
            + (f" At most {MAX_READ} pages are opened per call." if len(urls) > MAX_READ else ""))
    return head + "\n".join(out)


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


TOOLS = [goto, click, fill, select, check, back, read_more, read_all_pages, read_pages, note]
