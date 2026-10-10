"""
Records what a browser receives from the run's websites, for the local mirror (`sim/replay.py`).

A `Recorder` stores every response of a browser context: method, address, form data, status, headers and body.
`crawl` then walks one website the way a visitor can: it opens the start page signed in and follows the links
and embedded documents the pages show, one request about every second, never a sign-out link and never a form.
Paths that need input (searches, paging buttons, the bid form) are recorded by `sim/flows.py` on top of this.

Everything goes to `sim/captured/<siteId>/` (gitignored: it is website content, which stays on this machine).
The run's login never reaches the disk: it is replaced by `{{username}}` / `{{password}}` wherever it occurs.

From the repo root (the scouting logins come from `atira scout` unless the run is already in the environment):
    HEADLESS=1 uv run --project agent python sim/capture.py [siteId ...]
"""
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, deque
from pathlib import Path
from urllib.parse import parse_qs, urldefrag, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "agent"))

import browsers  # noqa: E402
import run as contract  # noqa: E402
from browser_tools import SiteSession  # noqa: E402
from extract import observe  # noqa: E402
from playwright.async_api import BrowserContext, Response, async_playwright  # noqa: E402
from tracing import Trace  # noqa: E402

CAPTURED = ROOT / "captured"
PAUSE = float(os.environ.get("CAPTURE_PAUSE", "0.8"))  # seconds between two pages of one website
MAX_PAGES = int(os.environ.get("CAPTURE_PAGES", "400"))
PER_SHAPE = 16  # addresses followed per path and set of parameter names
LEAVES = re.compile(r"logout|log-out|signout|sign-out|abmeld|ausloggen", re.IGNORECASE)
TEXT = re.compile(r"text/|json|javascript|xml|x-www-form-urlencoded", re.IGNORECASE)
EXTENSIONS = {"text/html": "html", "text/css": "css", "application/javascript": "js", "text/javascript": "js",
              "application/json": "json", "image/png": "png", "image/svg+xml": "svg", "image/jpeg": "jpg",
              "application/zip": "zip", "application/pdf": "pdf", "text/plain": "txt"}

# What a page offers besides its text: where its links and embedded documents lead, and its forms.
OFFERS_JS = r"""
() => {
  const abs = u => { try { return new URL(u, document.baseURI).href; } catch (e) { return null; } };
  const shown = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const links = Array.from(document.querySelectorAll('a[href]')).map(a => (
    {url: abs(a.getAttribute('href')), text: (a.textContent || '').trim().slice(0, 80), shown: shown(a)}));
  const frames = Array.from(document.querySelectorAll('iframe[src],frame[src]')).map(f => abs(f.getAttribute('src')));
  const forms = Array.from(document.querySelectorAll('form')).map(f => ({
    method: (f.getAttribute('method') || 'get').toLowerCase(), action: abs(f.getAttribute('action') || ''),
    fields: Array.from(f.querySelectorAll('input,select,textarea,button')).map(e => ({
      tag: e.tagName.toLowerCase(), type: e.getAttribute('type'), name: e.getAttribute('name'),
      value: e.tagName === 'SELECT' ? null : e.getAttribute('value'), shown: shown(e),
      text: e.tagName === 'BUTTON' ? (e.textContent || '').trim().slice(0, 80) : undefined,
      options: e.tagName === 'SELECT' ? Array.from(e.options).map(o => [o.value, o.text.trim()]) : undefined}))}));
  return {links: links.filter(l => l.url), frames: frames.filter(Boolean), forms};
}
"""


class Recorder:
    """Writes the responses of one website's browser context to `captured/<siteId>/`."""

    def __init__(self, site: dict, directory: Path | None = None):
        self.site = site
        self.origin = _origin(site["url"])
        self.dir = directory or CAPTURED / site["siteId"]
        (self.dir / "bodies").mkdir(parents=True, exist_ok=True)
        (self.dir / "pages").mkdir(exist_ok=True)
        self.exchanges = (self.dir / "exchanges.jsonl").open("a", encoding="utf-8")
        self.started = time.monotonic()
        self.count = 0
        self.label = ""  # the flow step under way, written with each exchange
        self.halt = False  # set to end a crawl after the page it is on
        self.pending: set[asyncio.Task] = set()

    def attach(self, context: BrowserContext) -> None:
        context.on("response", self._on_response)

    def _on_response(self, response: Response) -> None:
        task = asyncio.ensure_future(self._store(response))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def drain(self) -> None:
        while self.pending:
            await asyncio.gather(*list(self.pending), return_exceptions=True)

    def scrub(self, text: str) -> str:
        """The run's login and the website's own address, as placeholders the mirror fills in again."""
        creds = self.site["credentials"]
        for key in ("password", "username"):
            if creds.get(key):
                text = text.replace(creds[key], "{{%s}}" % key)
        return text.replace(self.origin, "{{origin}}")

    def keep(self, body: bytes, kind: str) -> str:
        """Store a body once under its hash and return the file's name."""
        if TEXT.search(kind):
            try:
                body = self.scrub(body.decode("utf-8")).encode("utf-8")
            except UnicodeDecodeError:
                pass
        name = hashlib.sha256(body).hexdigest()[:20] + "." + EXTENSIONS.get(kind.split(";")[0].strip(), "bin")
        path = self.dir / "bodies" / name
        if not path.exists():
            path.write_bytes(body)
        return name

    async def _store(self, response: Response) -> None:
        request = response.request
        if _origin(request.url) != self.origin:
            return
        headers = await response.all_headers()
        kind = headers.get("content-type", "")
        try:
            body = await response.body()
        except Exception:  # a redirect has none
            body = b""
        try:
            frame = request.frame
            where = "main" if frame.parent_frame is None else "frame"
            page_url = frame.page.url
        except Exception:
            where, page_url = "none", ""
        self.count += 1
        self.exchanges.write(json.dumps({
            "n": self.count, "t": round(time.monotonic() - self.started, 2), "label": self.label,
            "method": request.method, "url": self.scrub(request.url),
            "post": self.scrub(request.post_data or "") or None,
            "resource": request.resource_type, "navigation": request.is_navigation_request(), "in": where,
            "page": self.scrub(page_url), "referer": self.scrub(request.headers.get("referer", "")) or None,
            "status": response.status, "type": kind, "location": self.scrub(headers.get("location", "")) or None,
            "cookies": sorted({c.split("=")[0].strip() for c in headers.get("set-cookie", "").split("\n") if c}),
            "headers": {k: self.scrub(v) for k, v in headers.items()
                        if k in ("cache-control", "content-disposition", "x-frame-options", "refresh",
                                 "content-security-policy")},
            "body": self.keep(body, kind) if body else None, "size": len(body),
        }, ensure_ascii=False) + "\n")
        self.exchanges.flush()

    async def snapshot(self, session: SiteSession, requested: str, note: str = "") -> dict:
        """The page as it stands: every frame's document, the agent's view of it, a screenshot, what it offers."""
        page = session.page
        await self.drain()
        number = len(list((self.dir / "pages").glob("*.json"))) + 1
        stem = self.dir / "pages" / f"{number:04d}"
        frames, offers = [], {"links": [], "frames": [], "forms": []}
        for index, frame in enumerate(page.frames):
            try:
                html = await asyncio.wait_for(frame.content(), 10)
                found = await asyncio.wait_for(frame.evaluate(OFFERS_JS), 10)
            except Exception:
                continue
            name = f"{stem.name}-{index}.html"
            (self.dir / "pages" / name).write_text(self.scrub(html), encoding="utf-8")
            frames.append({"url": self.scrub(frame.url), "dom": name, "top": frame.parent_frame is None})
            for key in offers:
                offers[key] += found[key]
        try:
            view = await asyncio.wait_for(observe(page, f"capture{number}"), 20)
        except Exception as error:
            view = f"(not readable: {type(error).__name__})"
        try:
            await page.screenshot(path=str(stem) + ".png", full_page=True, timeout=10000)
        except Exception:
            pass
        try:
            title = await asyncio.wait_for(page.title(), 5)
        except Exception:
            title = ""
        events, session.events = list(session.events), []
        record = {"n": number, "requested": self.scrub(requested), "url": self.scrub(page.url), "title": title,
                  "note": note, "label": self.label, "frames": frames, "view": self.scrub(view), "events": events,
                  "links": [{**link, "url": self.scrub(link["url"])} for link in offers["links"]],
                  "embedded": [self.scrub(url) for url in offers["frames"]],
                  "forms": json.loads(self.scrub(json.dumps(offers["forms"], ensure_ascii=False)))}
        Path(str(stem) + ".json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        return record


def _origin(url: str) -> str:
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc}"


async def open_site(playwright, site: dict, trace: Trace) -> tuple[SiteSession, Recorder]:
    """A signed-in tab on the website whose responses are recorded."""
    (context,) = await browsers.contexts(playwright, 1)
    recorder = Recorder(site)
    recorder.attach(context)
    session = SiteSession(site, context, trace)
    recorder.label = "start"
    await session.start()
    return session, recorder


async def fetch(session: SiteSession, recorder: Recorder, url: str) -> None:
    """A file the browser would download instead of showing: fetched with the tab's sign-in and stored."""
    response = await session.context.request.get(url, max_redirects=0)
    headers = response.headers
    kind = headers.get("content-type", "")
    body = await response.body()
    recorder.count += 1
    recorder.exchanges.write(json.dumps({
        "n": recorder.count, "t": round(time.monotonic() - recorder.started, 2), "label": recorder.label,
        "method": "GET", "url": recorder.scrub(url), "post": None, "resource": "download", "navigation": True,
        "in": "main", "page": "", "referer": None, "status": response.status, "type": kind,
        "location": recorder.scrub(headers.get("location", "")) or None, "cookies": [],
        "headers": {k: v for k, v in headers.items() if k in ("content-disposition", "cache-control")},
        "body": recorder.keep(body, kind) if body else None, "size": len(body)}, ensure_ascii=False) + "\n")
    recorder.exchanges.flush()


async def crawl(session: SiteSession, recorder: Recorder, limit: int = MAX_PAGES) -> int:
    """Follow the links and embedded documents the website shows, breadth first, and snapshot each page."""
    origin = recorder.origin
    start = urldefrag(session.page.url)[0]
    queue, seen, shapes = deque(), {start}, Counter()
    for line in (recorder.dir / "exchanges.jsonl").read_text(encoding="utf-8").splitlines():
        earlier = json.loads(line)  # an earlier recording's pages are not opened again
        if earlier["method"] == "GET" and earlier["status"] == 200:
            seen.add(earlier["url"].replace("{{origin}}", origin))

    def offer(record: dict) -> None:
        found = [link["url"] for link in record["links"]] + record["embedded"]
        for url in found:
            url = urldefrag(url.replace("{{origin}}", origin))[0]
            parts = urlparse(url)
            shape = (parts.path, tuple(sorted(parse_qs(parts.query, keep_blank_values=True))))
            if _origin(url) != origin or url in seen or LEAVES.search(url) or shapes[shape] >= PER_SHAPE:
                continue  # one kind of address in every combination of its parameters is not walked through
            shapes[shape] += 1
            seen.add(url)
            queue.append(url)

    recorder.label, recorder.halt = "crawl", False
    offer(await recorder.snapshot(session, start, "start page"))
    done = 1
    while queue and done < limit and not recorder.halt:
        url = queue.popleft()
        await asyncio.sleep(PAUSE)
        async with session.lock:
            session.events.clear()
            await session._goto(url)
            failed = [e for e in session.events if e.startswith("navigation was slow or failed")]
            if failed and "ownload" in failed[0]:
                await fetch(session, recorder, url)
                done += 1
                continue
            offer(await recorder.snapshot(session, url))
        done += 1
        if done % 10 == 0:
            print(f"{recorder.site['siteId']}: {done} pages, {len(queue)} waiting", flush=True)
    await recorder.drain()
    return done


def scouting_run() -> dict:
    """The run to record: from the environment as the agent gets it, else straight from `atira scout`."""
    if os.environ.get("CHALLENGE_SCOUTING") or os.environ.get("CHALLENGE_RUN_TOKEN"):
        return contract.load_run()[0]
    return json.loads(subprocess.run(["atira", "scout", "--json"], check=True, capture_output=True, text=True).stdout)


async def main() -> None:
    task = scouting_run()
    wanted = sys.argv[1:]
    sites = [site for site in task["access"] if not wanted or site["siteId"] in wanted]
    trace = Trace(str(CAPTURED / "_traces"))
    async with async_playwright() as playwright:
        async def one(site: dict) -> None:
            session, recorder = await open_site(playwright, site, trace)
            pages = await crawl(session, recorder)
            print(f"{site['siteId']}: {pages} pages, {recorder.count} responses -> {recorder.dir}", flush=True)
        await asyncio.gather(*(one(site) for site in sites))


if __name__ == "__main__":
    asyncio.run(main())
