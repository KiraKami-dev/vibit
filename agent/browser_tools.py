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

    @property
    def name(self) -> str:
        return self.site["siteId"]

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
        for _ in range(6):
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                await page.wait_for_load_state("networkidle", timeout=2500)
            except Exception:
                pass
            if (await self._login(page) or await self._cookies(page) or await self._error_page(page)
                    or await self._human_check(page)):
                page = self.page
                continue
            break
        await self._collect_closed(page)

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
        await password.first.press("Enter")
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

    async def look(self, offset: int = 0) -> str:
        """What the model sees: the helpers' events, the tab, then the page text with refs."""
        page = self.page
        await self._collect_closed(page)
        self.stamp += 1
        if offset == 0:
            try:
                self.view = await observe(page, str(self.stamp))
            except Exception as error:
                await asyncio.sleep(1.0)
                self.view = await observe(page, str(self.stamp)) if not page.is_closed() else f"(page error {error})"
        header = [f"URL: {page.url}", f"Title: {await page.title()}"]
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

    async def act(self, kind: str, ref: str, fn) -> str:
        """Run one action on the element `ref`, settle, and return the new view."""
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
            self.trace.event(self.name, "action", action=kind, ref=ref, seconds=round(time.monotonic() - started, 2))
            view = await self.look()
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
    return await ctx.deps.session.act("fill", ref, run)


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


async def back(ctx: RunContext[Deps]) -> str:
    """Go back to the previous page."""
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
        return (f"Not noted: {', '.join(missing[:10])} appear on no page you have seen. Note only values copied "
                f"from a page; open the page again if you need to check.")
    ctx.deps.notes[key] = value
    ctx.deps.session.trace.event(ctx.deps.session.name, "note", key=key, value=value)
    return f"noted {key}"


TOOLS = [goto, click, fill, select, check, back, read_more, note]
