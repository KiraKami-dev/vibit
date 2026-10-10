"""
A local mirror of the run's websites, built from what `sim/capture.py` recorded.

Every recorded page is served as it was received. Around that the mirror behaves like the websites where the
recording shows how: sign-in, the cookie notice, entry checks with a number to copy, transient error pages, session
timeouts, and the bid form with its signing TAN (`sim/bidform.py`). It never guesses: an address or search that
was not recorded gets a plain "not recorded" page and a line in `captured/_missing.log`, which is the list of
paths still to record.

Each website gets its own port, since the agent keeps a tab on its website's host. From the repo root:
    python3 sim/mirror.py            # serves until stopped; prints the run to put into CHALLENGE_SCOUTING
"""
import json
import os
import random
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bidform  # noqa: E402

CAPTURED = Path(__file__).resolve().parent / "captured"
BASE_PORT = int(os.environ.get("SIM_PORT", "8801"))
SESSION_SECONDS = float(os.environ.get("SIM_SESSION_SECONDS", "0"))  # 0: sessions do not time out
FLAKY = float(os.environ.get("SIM_FLAKY", "0"))  # share of pages answered with the recorded error page first
CSRF = re.compile(r'name="csrf" value="([^"]+)"')
NUMBER = re.compile(r'"value":"([^"]+)"')  # the number an entry check shows, in its drawing script
MISSING = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Not recorded</title></head><body>
<h1>Not recorded</h1><p>The mirror has no recording of <code>{what}</code>. It is logged in
captured/_missing.log; record it with sim/drive.py.</p></body></html>"""


def canon(query: str) -> tuple:
    return tuple(sorted(parse_qsl(query, keep_blank_values=True)))


@dataclass
class Exchange:
    method: str
    path: str
    query: tuple
    form: dict
    status: int
    kind: str
    location: str | None
    body: str | None  # file name under bodies/
    resource: str

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.body)


class Recording:
    """One website's recorded exchanges, and what they show about how it behaves."""

    def __init__(self, directory: Path):
        self.dir = directory
        self.exchanges: list[Exchange] = []
        for line in (directory / "exchanges.jsonl").read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            parts = urlsplit(e["url"].replace("{{origin}}", ""))
            self.exchanges.append(Exchange(e["method"], parts.path or "/", canon(parts.query),
                                           dict(parse_qsl(e["post"] or "", keep_blank_values=True)), e["status"],
                                           e["type"], e["location"], e["body"], e["resource"]))
        self._texts: dict[str, str] = {}
        self.tokens = set()
        for e in self.exchanges:
            if e.body and e.body.endswith(".html"):
                self.tokens.update(t for t in CSRF.findall(self.text(e)) if not t.startswith("login:"))
        gets = [e for e in self.exchanges if e.method == "GET"]
        self.pages: dict[tuple, list[Exchange]] = {}
        for e in gets:
            if e.ok:
                self.pages.setdefault((e.path, e.query), []).append(e)
        self.errors = {e.path: e for e in gets if e.status >= 500 and e.body}
        self.login = next(e for e in gets if e.path == "/login" and e.ok)
        self.consent = next((e for e in gets if e.path == "/consent" and e.ok), None)
        self.cookie = "sim_" + directory.name.replace("-", "_")
        # an entry check: the start page sends there until its form was sent correctly
        first = [e.location for e in gets if e.path == "/" and e.status == 303 and e.location
                 and not e.location.startswith(("/login", "/consent"))]
        self.entry = first[0] if first else None
        # checks in front of a page: a form on the page itself that leads back to it once sent correctly
        self.checks: dict[str, str] = {}  # the checked page's path -> the path its form is sent to
        self.passes: dict[str, dict] = {}  # form path -> the fields a correct answer carried
        self.refusals: dict[str, Exchange] = {}
        for e in self.exchanges:
            if e.method != "POST" or e.path in ("/login", "/consent", "/logout") or "receipt" not in e.form:
                continue
            if e.status == 303 and e.location:
                self.passes[e.path] = {k: v for k, v in e.form.items() if k not in ("csrf", "receipt")}
                if e.path != self.entry:
                    self.checks[urlsplit(e.location).path] = e.path
            elif e.body:
                self.refusals[e.path] = e

    def text(self, e: Exchange) -> str:
        if e.body not in self._texts:
            self._texts[e.body] = (self.dir / "bodies" / e.body).read_text(encoding="utf-8")
        return self._texts[e.body]

    def raw(self, e: Exchange) -> bytes:
        return (self.dir / "bodies" / e.body).read_bytes()

    def page(self, path: str, query: tuple, having: str = "", without: str = "") -> Exchange | None:
        """The latest recording of an address, optionally one that has (or lacks) a piece of text."""
        for e in reversed(self.pages.get((path, query), [])):
            text = self.text(e) if (having or without) and e.body.endswith(".html") else ""
            if (not having or having in text) and (not without or without not in text):
                return e
        return None


@dataclass
class Session:
    csrf: str = field(default_factory=lambda: secrets.token_hex(16))
    started: float = field(default_factory=time.monotonic)
    signed_in: bool = False
    consented: bool = False
    passed: set = field(default_factory=set)  # form paths of the checks passed
    numbers: dict = field(default_factory=dict)  # form path -> the number its check shows now
    erred: set = field(default_factory=set)  # paths that showed their error page already
    navigations: int = 0
    form: object = None  # the bid form's state


@dataclass
class Reply:
    status: int = 200
    body: bytes | str = b""
    kind: str = "text/html; charset=UTF-8"
    location: str | None = None
    cookie: str | None = None


class Site:
    """One mirrored website: recorded pages plus the behaviour around them."""

    def __init__(self, site_id: str, port: int, world: "World"):
        self.id, self.port, self.world = site_id, port, world
        self.rec = Recording(CAPTURED / site_id)
        self.origin = f"http://127.0.0.1:{port}"
        self.credentials = {"username": "sim-" + site_id, "password": secrets.token_urlsafe(12)}
        self.sessions: dict[str, Session] = {}
        self.lock = threading.RLock()
        self.random = random.Random(world.seed + site_id)
        self.bidform = bidform.find(self.rec)

    # -- what is sent -------------------------------------------------------------------------------------------

    def render(self, text: str, session: Session | None) -> str:
        """A recorded page with this mirror's address, login name and the session's form token."""
        text = text.replace("{{origin}}", self.origin).replace("{{username}}", self.credentials["username"])
        if session:
            for token in self.rec.tokens:
                text = text.replace(token, session.csrf)
            text = re.sub(r'"navigation":\d+', f'"navigation":{session.navigations}', text)
        return text

    def recorded(self, e: Exchange, session: Session | None) -> Reply:
        if e.body.endswith((".html", ".css", ".js", ".json", ".txt", ".svg")):
            return Reply(e.status, self.render(self.rec.text(e), session), e.kind)
        return Reply(e.status, self.rec.raw(e), e.kind)

    def missing(self, what: str) -> Reply:
        with (CAPTURED / "_missing.log").open("a", encoding="utf-8") as log:
            log.write(f"{self.id} {what}\n")
        return Reply(404, MISSING.format(what=what.replace("<", "&lt;")))

    def check_page(self, session: Session, form_path: str, e: Exchange) -> Reply:
        """A page with a number to copy: shown with a fresh number, which the form's answer has to carry."""
        text = self.rec.text(e)
        shown = NUMBER.search(text)
        if shown:
            fresh = "".join(self.random.choice("0123456789") if c.isdigit() else c for c in shown.group(1))
            text = text.replace(shown.group(1), fresh)
            session.numbers[form_path] = fresh
        return Reply(e.status, self.render(text, session), e.kind)

    def check_answer(self, session: Session, form_path: str, form: dict, back: str) -> Reply:
        wanted = self.rec.passes[form_path]
        if form.get("receipt", "").strip() == session.numbers.get(form_path) and all(
                form.get(k) == v for k, v in wanted.items() if k != "returnTo"):
            session.passed.add(form_path)
            return Reply(303, location=back)
        return self.check_page(session, form_path, self.rec.refusals[form_path])

    # -- one request --------------------------------------------------------------------------------------------

    def handle(self, method: str, target: str, form: dict, cookie: str | None) -> Reply:
        parts = urlsplit(target)
        path, query = parts.path or "/", canon(parts.query)
        with self.lock:
            session = self.sessions.get(cookie or "")
            if session and SESSION_SECONDS and time.monotonic() - session.started > SESSION_SECONDS:
                del self.sessions[cookie]  # a timed-out session loses what was entered, as on the website
                session = None
            reply = self.answer(method, path, query, parts.query, form, session, cookie)
            return reply

    def answer(self, method, path, query, raw_query, form, session, cookie) -> Reply:
        rec, here = self.rec, path + ("?" + raw_query if raw_query else "")
        back = dict(query).get("returnTo") or form.get("returnTo") or "/"
        if not session or not session.signed_in:
            if path == "/login" and method == "POST":
                if (form.get("username"), form.get("password")) != tuple(self.credentials.values()):
                    return self.sign_in_page(back)
                key = secrets.token_hex(16)
                self.sessions[key] = Session(signed_in=True)
                return Reply(303, location=back, cookie=f"{rec.cookie}={key}; Path=/; HttpOnly; SameSite=Lax")
            if path == "/login":
                return self.sign_in_page(back)
            return Reply(303, location="/login?returnTo=" + quote(here, safe=""))
        if path == "/logout" and method == "POST":
            self.sessions.pop(cookie, None)
            return Reply(303, location="/login?returnTo=%2F")
        if rec.consent and not session.consented:
            if path == "/consent" and method == "POST":
                session.consented = True
                return Reply(303, location=back)
            if path == "/consent":
                text = re.sub(r'(name="returnTo" value=")[^"]*', lambda m: m.group(1) + back, rec.text(rec.consent))
                return Reply(200, self.render(text, session))
            return Reply(303, location="/consent?returnTo=" + quote(here, safe=""))
        if rec.entry and rec.entry not in session.passed:
            if path != rec.entry:
                return Reply(303, location=rec.entry)
            if method == "POST":
                return self.check_answer(session, rec.entry, form, "/")
            if path in rec.errors and path not in session.erred:
                session.erred.add(path)
                return self.recorded(rec.errors[path], session)
            return self.check_page(session, rec.entry, rec.page(path, ()))
        if method == "GET":
            session.navigations += 1
        for checked, form_path in rec.checks.items():
            if path == form_path and method == "POST":
                return self.check_answer(session, form_path, form, checked)
            if path.startswith(checked) and form_path not in session.passed and method == "GET":
                gate = rec.page(checked, (), having=f'action="{form_path}"')
                return self.check_page(session, form_path, gate)
        if self.bidform and path == self.bidform.path:
            return self.bidform.handle(self, session, method, form)
        if method != "GET":
            return self.missing(f"{method} {here} {sorted(k for k in form if k != 'csrf')}")
        if self.world.mail_site == self.id:
            reply = self.world.mailbox(self, session, path, query)
            if reply:
                return reply
        if FLAKY and rec.errors and path not in session.erred and self.random.random() < FLAKY:
            session.erred.add(path)
            error = next(iter(rec.errors.values()))
            return Reply(error.status, self.render(self.rec.text(error).replace(
                f'href="{error.path}"', f'href="{here}"'), session), error.kind)
        without = next((f'action="{f}"' for c, f in rec.checks.items() if path == c), "")
        found = rec.page(path, query, without=without) or self.loosely(path, query)
        return self.recorded(found, session) if found else self.missing(f"GET {here}")

    def loosely(self, path: str, query: tuple) -> Exchange | None:
        """A search recorded with the same words in another order or case is the same search."""
        def words(q):
            return tuple((k, " ".join(sorted(v.lower().split())) if k == "q" else v) for k, v in q
                         if not (k == "page" and v == "1"))
        for (p, q), found in self.rec.pages.items():
            if p == path and q != query and words(q) == words(query):
                return found[-1]
        return None

    def sign_in_page(self, back: str) -> Reply:
        text = re.sub(r'(name="returnTo" value=")[^"]*', lambda m: m.group(1) + back, self.rec.text(self.rec.login))
        return Reply(200, self.render(text, None))


class World:
    """The mirrored websites together: what one of them does can show on another (a TAN arrives in the mailbox)."""

    def __init__(self, seed: str = ""):
        self.seed = seed or secrets.token_hex(4)
        ids = sorted(p.name for p in CAPTURED.iterdir() if (p / "exchanges.jsonl").exists())
        self.sites = {site_id: Site(site_id, BASE_PORT + i, self) for i, site_id in enumerate(ids)}
        self.signed: list[dict] = []  # the bids signed on the mirror
        self.tans: list[dict] = []  # every TAN handed out: number, value, reference, when
        self.mail_site, self.tan_list, self.tan_message = bidform.find_tan_mail(
            {s.id: s.rec for s in self.sites.values()})

    def mailbox(self, site: Site, session: Session, path: str, query: tuple) -> Reply | None:
        """The start page's TAN messages and the messages themselves, for the TANs this mirror handed out."""
        return bidform.tan_mail(self, site, session, path, query)

    def new_tan(self, reference: str) -> dict:
        """A signing TAN; request numbers go on from the highest one the recorded mailbox shows."""
        base = self.tan_message.highest if self.tan_message else 0
        tan = {"number": base + len(self.tans) + 1, "value": secrets.token_hex(4).upper(), "reference": reference,
               "at": time.time()}
        self.tans.append(tan)
        return tan

    def sign(self, bid: dict) -> None:
        self.signed.append(bid)
        bidform.write_signed(CAPTURED, self.signed)

    def run(self, brief: str = "", extra: dict | None = None) -> dict:
        """The run as an agent gets it: the mirror's addresses and logins in place of the websites'."""
        access = [{"siteId": s.id, "url": s.origin, "credentials": s.credentials} for s in self.sites.values()]
        submit = next((s for s in self.sites.values() if s.bidform), None)
        task = dict(extra or {})
        task.update(brief=brief, access=access)
        if submit:
            task["submission"] = {"kind": "site", "siteId": submit.id, "url": submit.origin}
        return task

    def serve(self) -> list[ThreadingHTTPServer]:
        servers = []
        for site in self.sites.values():
            server = ThreadingHTTPServer(("127.0.0.1", site.port), handler(site))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            servers.append(server)
        return servers


def handler(site: Site):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:
            pass

        def respond(self, method: str) -> None:
            length = int(self.headers.get("content-length") or 0)
            form = dict(parse_qsl(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)) if length else {}
            cookies = dict(c.strip().split("=", 1) for c in (self.headers.get("cookie") or "").split(";") if "=" in c)
            reply = site.handle(method, self.path, form, cookies.get(site.rec.cookie))
            body = reply.body.encode("utf-8") if isinstance(reply.body, str) else reply.body
            self.send_response(reply.status)
            self.send_header("Content-Type", reply.kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            if reply.location:
                self.send_header("Location", reply.location)
            if reply.cookie:
                self.send_header("Set-Cookie", reply.cookie)
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(body)

        def do_GET(self) -> None:
            self.respond("GET")

        def do_POST(self) -> None:
            self.respond("POST")

        def do_HEAD(self) -> None:
            self.respond("HEAD")

    return Handler


def main() -> None:
    world = World(os.environ.get("SIM_SEED", ""))
    world.serve()
    print(json.dumps(world.run(), indent=1), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
