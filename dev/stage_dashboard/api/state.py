"""Vercel function: one JSON snapshot of the public stage data.

Reads only public, unauthenticated pages: the stage fragment the stage page
itself polls, plus /v1/challenges/<id>/{stats,tokens,leaderboard,activity}.
Stateless; the page builds run history in the viewer's browser. Responses are
cached at the edge for a few seconds so many viewers cost one upstream poll.
"""
import html, json, os, re, ssl, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler

BASE = "https://ehl-challenge.atira.ai"
CHALLENGE = os.environ.get("CHALLENGE", "rfq")
UA = "error404ai-dashboard/0.1 (team monitor)"
ROW = re.compile(r'<li data-row="(run_\w+)">(.*?)</li>', re.S)
METER = re.compile(r"(\d+):(\d\d):(\d\d)\s*·\s*(\d+)\s*calls\s*·\s*\$([\d.]+)")

# python.org builds ship without CA roots; use the macOS bundle when running locally.
_CA = "/etc/ssl/cert.pem"
CTX = ssl.create_default_context(
    cafile=_CA if os.path.exists(_CA) and not os.environ.get("SSL_CERT_FILE") else None)


def get(url, as_json=True):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=8, context=CTX) as r:
        body = r.read().decode()
    return json.loads(body) if as_json else body


def text(fragment):
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_fragment(markup):
    """-> (running_now_total or None, [row dicts]); pulls team, kind, phase and
    the 'hh:mm:ss · N calls · $x' meter from each row."""
    m = re.search(r"Running now\s*·\s*(\d+)", text(markup))
    total = int(m.group(1)) if m else None
    rows = []
    for rid, inner in ROW.findall(markup):
        who = re.search(r'class="who">(.*?)</span>', inner, re.S)
        kind = re.search(r'class="kind">(.*?)</span>', inner, re.S)
        phase = re.search(r'class="phase">(.*?)</span>\s*(?:<span class="meter"|$)', inner, re.S)
        row = {"id": rid, "team": text(who.group(1)) if who else "?",
               "kind": text(kind.group(1)) if kind else "", "phase": ""}
        if phase:
            row["phase"] = text(re.sub(r'<span class="kind">.*?</span>', "", phase.group(1))).lstrip("· ").strip()
        mt = METER.search(text(inner))
        if mt:
            h, mi, s, calls, cost = mt.groups()
            row.update(secs=int(h) * 3600 + int(mi) * 60 + int(s), calls=int(calls), cost=float(cost))
        rows.append(row)
    return total, rows


def snapshot():
    api = lambda n: get(f"{BASE}/v1/challenges/{CHALLENGE}/{n}")
    jobs = {
        "fragment": lambda: get(f"{BASE}/challenges/{CHALLENGE}/stage.fragment", False),
        **{n: (lambda n=n: api(n)) for n in ("stats", "tokens", "leaderboard", "activity")},
    }
    out, errors = {}, {}
    with ThreadPoolExecutor(5) as pool:
        futs = {k: pool.submit(f) for k, f in jobs.items()}
    for k, f in futs.items():
        try:
            out[k] = f.result()
        except Exception as e:
            errors[k] = str(e)
    total, rows = parse_fragment(out.pop("fragment", ""))
    for e in out.get("leaderboard", {}).get("entries", []):
        e.pop("members", None)
        e.pop("history", None)
    return {"at": time.time(), "running_total": total, "rows": rows, "live": out, "errors": errors}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(snapshot()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "public, s-maxage=3, stale-while-revalidate=5")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":      # local check: prints one snapshot
    print(json.dumps(snapshot(), indent=1)[:1500])
