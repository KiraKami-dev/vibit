"""
Keeps the recording complete and current.

`audit` (offline) lists what the mirror could be asked for but has no recording of: addresses the recorded pages
link to, addresses an agent asked the mirror for (`captured/_missing.log`), and choices of the bid form whose
follow-up was never walked.

`refresh` asks the websites again, signed in with the scouting login like a visitor: every recorded address, plus
everything `audit` lists. It reports what changed, what is new and what is gone, and stores the new answers, so
the mirror shows the websites as they are now. `--form` also walks every choice of the bid form (it never signs).
Requests are paced; nothing is asked that the websites' own pages and forms do not offer.

From the repo root:
    uv run --project agent python sim/refresh.py audit
    uv run --project agent python sim/refresh.py refresh [--form] [siteId ...]
"""
import html
import http.client
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bidform  # noqa: E402
import mirror  # noqa: E402

PAUSE = 0.25  # seconds between two requests to one website
LINK = re.compile(r'(?:href|src|data-url)="(/[^"#]*)')
LEAVES = re.compile(r"logout|abmeld", re.IGNORECASE)
AGENT = "error404ai-sim-refresh/1.0"


def links(text: str) -> set[str]:
    return {html.unescape(target) for target in LINK.findall(text) if not LEAVES.search(target)}


def key(target: str) -> tuple:
    parts = urlsplit(target)
    return parts.path or "/", mirror.canon(parts.query)


def target_of(path: str, query: tuple) -> str:
    return path + ("?" + urlencode(query) if query else "")


def gaps(site_id: str, rec: mirror.Recording) -> dict[str, list[str]]:
    """What a visitor of the recorded pages could ask for that has no recording."""
    form = bidform.find(rec)
    skip = {"/login", "/consent", rec.entry, form.path if form else None, *rec.checks.values()}
    known = set(rec.pages) | set(rec.absent)
    tan = bidform.find_tan_mail({site_id: rec})[2]

    def moving(target: str) -> bool:
        """The TAN mails: their list grows with every request on the website; the mirror makes its own."""
        path, query = key(target)
        return bool(tan) and (path.startswith("/messages/" + tan.prefix) or dict(query).get("message", "").startswith(tan.prefix))
    linked = set()
    for found in rec.pages.values():
        if found[-1].body.endswith(".html"):
            linked |= {t for t in links(rec.text(found[-1])) if key(t) not in known and key(t)[0] not in skip
                       and not moving(t)}
    asked = set()
    log = mirror.CAPTURED / "_missing.log"
    for line in log.read_text(encoding="utf-8").splitlines() if log.exists() else []:
        parts = line.split(" ", 2)
        if parts[0] == site_id and parts[1] == "GET" and key(parts[2]) not in known:
            asked.add(parts[2])
    unwalked = []
    if form:
        for made, choices in form.tree.items():
            if len(made) < len(bidform.LEVELS):
                unwalked += [" > ".join((*made, value)) for value, _ in choices if (*made, value) not in form.walked]
    return {"linked": sorted(linked), "asked": sorted(asked), "unwalked": unwalked}


class Live:
    """One website, asked directly over HTTP with the scouting login."""

    def __init__(self, access: dict, rec: mirror.Recording, recorder):
        self.access, self.rec, self.recorder = access, rec, recorder
        self.host = urlsplit(access["url"]).netloc
        self.cookies: dict[str, str] = {}
        self.requests = 0

    def request(self, method: str, target: str, form: dict | None = None) -> tuple[int, dict, bytes]:
        time.sleep(PAUSE)
        self.requests += 1
        connection = http.client.HTTPSConnection(self.host, timeout=30)
        headers = {"User-Agent": AGENT, "Accept": "text/html,*/*",
                   "Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items())}
        body = None
        if form is not None:
            body, headers["Content-Type"] = urlencode(form), "application/x-www-form-urlencoded"
        for attempt in range(4):  # a slow or dropped answer is asked for again
            try:
                connection.request(method, target, body, headers)
                response = connection.getresponse()
                data = response.read()
                break
            except (OSError, http.client.HTTPException):
                connection.close()
                if attempt == 3:
                    raise
                time.sleep(3)
                connection = http.client.HTTPSConnection(self.host, timeout=30)
        for name, value in response.getheaders():
            if name.lower() == "set-cookie":
                cookie = value.split(";")[0]
                self.cookies[cookie.split("=", 1)[0]] = cookie.split("=", 1)[1]
        connection.close()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, data

    def token(self, text: str) -> str:
        found = mirror.CSRF.search(text)
        return found.group(1) if found else ""

    def check(self, form_path: str, text: str) -> None:
        number = mirror.NUMBER.search(text)
        self.request("POST", form_path, {"csrf": self.token(text), "receipt": number.group(1) if number else "",
                                         **self.rec.passes[form_path]})

    def enter(self) -> None:
        """Sign in, accept the cookie notice and pass the checks, as a visitor's browser does."""
        for _ in range(10):
            status, headers, body = self.request("GET", "/")
            where = urlsplit(headers.get("location", "")).path
            if status == 200:
                break
            if status >= 500 or not where:
                continue
            _, _, page = self.request("GET", headers["location"])
            text = page.decode("utf-8", "replace")
            if where == "/login":
                self.request("POST", "/login", {"csrf": self.token(text), "returnTo": "/", **self.access["credentials"]})
            elif where == "/consent":
                self.request("POST", "/consent", {"csrf": self.token(text), "returnTo": "/"})
            elif where == self.rec.entry:
                if mirror.NUMBER.search(text):
                    self.check(where, text)
        else:
            raise RuntimeError(f"{self.access['siteId']}: could not get past the entry pages")
        for checked, form_path in self.rec.checks.items():
            _, _, page = self.request("GET", checked)
            text = page.decode("utf-8", "replace")
            if f'action="{form_path}"' in text:
                self.check(form_path, text)

    def get(self, target: str) -> tuple[int, dict, bytes]:
        """A page, signing in again when the website ended the session meanwhile."""
        for _ in range(3):
            status, headers, body = self.request("GET", target)
            if status == 303 and urlsplit(headers.get("location", "")).path in ("/login", "/consent", self.rec.entry):
                self.enter()
                continue
            if status >= 500:
                continue
            return status, headers, body
        return status, headers, body

    def post(self, target: str, form: dict) -> str:
        """Send a form and return the page that follows (the answer itself, or the page it redirects to)."""
        status, headers, body = self.request("POST", target, form)
        self.recorder.store("POST", self.access["url"].rstrip("/") + target, urlencode(form), status, headers, body)
        if status == 303:
            if urlsplit(headers["location"]).path == "/login":
                raise TimeoutError("the website ended the session")
            status, headers, body = self.request("GET", headers["location"])
            self.recorder.store("GET", self.access["url"].rstrip("/") + headers.get("location", target), "", status,
                                headers, body) if status == 200 else None
        return body.decode("utf-8", "replace")


def refresh_pages(live: Live, rec: mirror.Recording, site_id: str) -> dict:
    form = bidform.find(rec)
    skip = {"/login", "/consent", rec.entry, form.path if form else None, *rec.checks.values()}
    found = gaps(site_id, rec)
    queue = [target_of(*k) for k in rec.pages if k[0] not in skip] + found["linked"] + found["asked"]
    seen, report = set(), {"same": 0, "changed": [], "varies": [], "new": [], "gone": []}
    origin = live.access["url"].rstrip("/")
    while queue:
        target = queue.pop(0)
        if key(target) in seen or key(target)[0] in skip or LEAVES.search(target):
            continue
        seen.add(key(target))
        status, headers, body = live.get(target)
        kind = headers.get("content-type", "")
        before = rec.page(*key(target), without=next((f'action="{f}"' for c, f in rec.checks.items() if key(target)[0] == c), ""))
        if status == 303:
            continue  # a redirect inside the website: its destination is a recorded address of its own
        if status != 200:
            if before:
                report["gone"].append(f"{target} ({status})")
            if key(target) not in rec.absent:
                live.recorder.store("GET", origin + target, "", status, headers, body)
            continue
        text = live.recorder.scrub(body.decode("utf-8")) if "html" in kind else None
        if before is None:
            report["new"].append(target)
        elif text is not None and mirror.plain(text) == mirror.plain(rec.text(before)):
            report["same"] += 1
            continue
        elif text is None and body == rec.raw(before):
            report["same"] += 1
            continue
        else:
            again = live.get(target)  # some pages differ on every load: shuffled links, a date in another format
            if text is not None and again[0] == 200 and mirror.plain(live.recorder.scrub(again[2].decode("utf-8"))) != mirror.plain(text):
                report["varies"].append(target)
                live.recorder.store("GET", origin + target, "", again[0], again[1], again[2])
            else:
                report["changed"].append(target)
        live.recorder.store("GET", origin + target, "", status, headers, body)
        if text is not None and len(seen) < 3000:
            queue += [t for t in links(text) if key(t) not in seen]
    return report


def walk_form(live: Live, rec: mirror.Recording, limit: int = 4000) -> dict:
    """Walk every choice of the bid form's dependent selects on one position page. Never signs, never submits."""
    form = bidform.find(rec)
    tree = dict(form.tree)
    todo = [made for made in sorted(tree, key=len)]  # every known node: its choices may lead further
    done, sessions = 0, 0
    levels = (*bidform.LEVELS, "material")

    def start() -> str:
        text = live.get(form.path)[2].decode("utf-8", "replace")
        for _ in range(6):
            kind = bidform.kind_of(text)
            if kind is None:  # signed out, or a notice in between
                live.enter()
                text = live.get(form.path)[2].decode("utf-8", "replace")
                continue
            common = {"csrf": live.token(text), "revision": re.search(r'name="revision" value="(\d+)"', text).group(1)}
            if kind == "bidder":
                text = live.post(form.path, {**common, **form.bidder, "email": "refresh@bidder.example", "action": "next"})
            elif kind == "kind":
                text = live.post(form.path, {**common, "kind": "bid", "action": "next"})
            elif kind == "position":
                return text
            else:  # a form left further along by an earlier visit: back to the start
                text = live.post(form.path, {**common, "action": "back"})
        return text

    text = start()
    pending = [(*made, value) for made in todo if len(made) < len(bidform.LEVELS)
               for value, _ in tree[made] if (*made, value) not in form.walked]
    pending.sort()  # neighbours share their first choices, which saves round trips

    def chosen(page: str) -> tuple:
        made = ()
        for level in bidform.LEVELS:
            value = bidform.options_of(page, level)[1]
            if not value:
                break
            made += (value,)
        return made

    while pending and done < limit:
        made = pending.pop(0)
        if made in tree:
            continue
        try:
            if bidform.kind_of(text) != "position":  # the website ended the session and shows another page
                raise TimeoutError
            now = chosen(text)
            same = next((i for i, (a, b) in enumerate(zip(now, made)) if a != b), min(len(now), len(made)))
            for depth in range(same + 1, len(made) + 1):  # the form takes one further choice per round trip
                fields = {level: (made[i] if i < depth else "") for i, level in enumerate(bidform.LEVELS)}
                common = {"csrf": live.token(text), "revision": re.search(r'name="revision" value="(\d+)"', text).group(1)}
                text = live.post(form.path, {**common, **fields, "action": "configure",
                                             **{k: "" for k in (*bidform.TYPED, "lead")}})
                done += 1
                if bidform.kind_of(text) != "position":
                    raise TimeoutError
        except TimeoutError:
            if sessions > 60:
                break
            live.enter()
            sessions += 1
            text = start()
            pending.insert(0, made)
            continue
        if chosen(text) != made:
            tree[made] = []  # the website did not take this choice
            continue
        choices = bidform.options_of(text, levels[len(made)])[0]
        tree[made] = choices
        if len(made) < len(bidform.LEVELS):
            pending = [(*made, value) for value, _ in choices if (*made, value) not in tree] + pending
    return {"walked": done, "left": len(pending), "sessions_restarted": sessions}


def main() -> None:
    command = sys.argv[1] if sys.argv[1:] else "audit"
    wanted = [a for a in sys.argv[2:] if not a.startswith("--")]
    ids = [p.name for p in sorted(mirror.CAPTURED.iterdir()) if (p / "exchanges.jsonl").exists()]
    if command == "audit":
        total = 0
        for site_id in ids:
            found = gaps(site_id, mirror.Recording(mirror.CAPTURED / site_id))
            count = sum(len(v) for v in found.values())
            total += count
            print(f"{site_id}: {len(found['linked'])} linked but not recorded, {len(found['asked'])} asked for by an "
                  f"agent, {len(found['unwalked'])} form choices not walked")
            for name, values in found.items():
                for value in values[:8]:
                    print(f"   {name}: {value}")
        print("nothing missing" if not total else f"{total} gaps: run `refresh` (with --form for the form choices)")
        sys.exit(1 if total else 0)
    import capture  # needs the agent's environment
    task = capture.scouting_run()
    summary = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "scenario": task.get("scenario_id"),
               "version": task.get("challenge_version"), "sites": {}}
    for access in task["access"]:
        site_id = access["siteId"]
        if wanted and site_id not in wanted:
            continue
        if site_id not in ids:
            print(f"{site_id}: not recorded yet; record it with sim/drive.py first")
            continue
        rec = mirror.Recording(mirror.CAPTURED / site_id)
        recorder = capture.Recorder(access)
        recorder.label = "refresh"
        live = Live(access, rec, recorder)
        live.enter()
        report = refresh_pages(live, rec, site_id)
        print(f"{site_id}: pages done ({live.requests} requests)", flush=True)
        if "--form" in sys.argv and bidform.find(rec):
            report["form"] = walk_form(live, mirror.Recording(mirror.CAPTURED / site_id))
        report["requests"] = live.requests
        summary["sites"][site_id] = report
        print(f"{site_id}: {report['same']} unchanged, {len(report['varies'])} vary on every load, "
              f"{len(report['changed'])} changed, {len(report['new'])} new, "
              f"{len(report['gone'])} gone" + (f", form {report['form']}" if "form" in report else ""), flush=True)
        for name in ("changed", "new", "gone"):
            for target in report[name][:6]:
                print(f"   {name}: {target}")
    known = mirror.varying()  # pages once seen to vary keep varying on the mirror
    for site_id, report in summary["sites"].items():
        report["varies"] = sorted(set(report["varies"]) | set(known.get(site_id, [])))
    (mirror.CAPTURED / "_refresh.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    missing = mirror.CAPTURED / "_missing.log"
    if missing.exists():
        missing.unlink()  # what an agent asked for has been asked of the websites now


if __name__ == "__main__":
    main()
