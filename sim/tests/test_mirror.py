"""
The mirror against its recording: every recorded page comes back as recorded, and the bid form, replayed with
the entries of the recorded walks, shows the pages the website showed.

Needs a recording in sim/captured/ (see sim/capture.py); skipped without one. From the repo root:
    python3 -m unittest discover -s sim/tests
"""
import http.client
import re
import sys
import unittest
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bidform  # noqa: E402
import mirror  # noqa: E402

RECORDED = any(mirror.CAPTURED.glob("*/exchanges.jsonl")) if mirror.CAPTURED.exists() else False
plain = mirror.plain


class Visitor:
    """A browser's worth of HTTP against one mirrored website: keeps the cookie, follows redirects."""

    def __init__(self, site: mirror.Site):
        self.site, self.cookie = site, ""

    def request(self, method: str, target: str, form: dict | None = None, follow: bool = True):
        connection = http.client.HTTPConnection("127.0.0.1", self.site.port, timeout=20)
        body = urlencode(form) if form is not None else None
        headers = {"Cookie": self.cookie, "Content-Type": "application/x-www-form-urlencoded"}
        connection.request(method, target, body, headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        if response.getheader("Set-Cookie"):
            self.cookie = response.getheader("Set-Cookie").split(";")[0]
        if follow and response.status == 303:
            return self.request("GET", response.getheader("Location"))
        return response.status, data.decode("utf-8", "replace"), target

    def enter(self) -> None:
        """Sign in, accept the cookie notice and pass the entry checks, the way the agent's helpers do."""
        self.request("POST", "/login", {**self.site.credentials, "returnTo": "/"})
        self.request("POST", "/consent", {"returnTo": "/"})
        for _ in range(3):
            status, text, _ = self.request("GET", "/")
            shown = mirror.NUMBER.search(text)
            if status == 200 and not (self.site.rec.entry and shown and 'name="receipt"' in text):
                break
            if shown:
                self.request("POST", self.site.rec.entry, {**self.site.rec.passes[self.site.rec.entry],
                                                           "receipt": shown.group(1)})
        for checked, form_path in self.site.rec.checks.items():
            _, text, _ = self.request("GET", checked)
            self.request("POST", form_path, {**self.site.rec.passes[form_path],
                                             "receipt": mirror.NUMBER.search(text).group(1)})


@unittest.skipUnless(RECORDED, "no recording in sim/captured/")
class MirrorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        mirror.BASE_PORT = 8851
        cls.world = mirror.World("test")
        cls.servers = cls.world.serve()

    @classmethod
    def tearDownClass(cls) -> None:
        for server in cls.servers:
            server.shutdown()

    def test_signed_out_goes_to_sign_in(self) -> None:
        for site in self.world.sites.values():
            status, text, _ = Visitor(site).request("GET", "/")
            self.assertEqual(status, 200)
            self.assertIn('type="password"', text, site.id)

    def test_recorded_pages_come_back_as_recorded(self) -> None:
        for site in self.world.sites.values():
            visitor = Visitor(site)
            visitor.enter()
            skip = {site.rec.entry, "/login", "/consent", *(site.rec.checks.values())}
            if site.bidform:
                skip.add(site.bidform.path)
            checked = differing = 0
            for (path, query), found in site.rec.pages.items():
                last = found[-1]
                if path in skip or not last.body.endswith(".html"):
                    continue
                if site.id == self.world.mail_site and path == "/" and not dict(query).get("q"):
                    continue  # the TAN list changes with the TANs handed out
                target = path + ("?" + urlencode(query) if query else "")
                status, text, _ = visitor.request("GET", target, follow=False)
                want = site.render(site.rec.text(site.rec.page(path, query, without=next(
                    (f'action="{f}"' for c, f in site.rec.checks.items() if path == c), ""))), None)
                checked += 1
                differing += status != 200 or plain(text) != plain(want)
            self.assertGreater(checked, 5, site.id)
            self.assertEqual(differing, 0, f"{site.id}: {differing} of {checked} pages differ")

    def test_unrecorded_address_says_so(self) -> None:
        site = next(iter(self.world.sites.values()))
        visitor = Visitor(site)
        visitor.enter()
        status, text, _ = visitor.request("GET", "/no-such-page-was-recorded")
        self.assertEqual(status, 404)
        self.assertIn("Not recorded", text)

    def test_form_rules_explain_every_recorded_refusal(self) -> None:
        forms = [site.bidform for site in self.world.sites.values() if site.bidform]
        self.assertTrue(forms)
        for form in forms:
            self.assertEqual(form.conflicts, [])

    def test_bid_form_replays_the_recorded_walks(self) -> None:
        """Send the recorded entries again; each page must equal the page the website answered with."""
        site = next(s for s in self.world.sites.values() if s.bidform)
        form, rec = site.bidform, site.rec
        walks, walk = [], []
        for e in rec.exchanges:  # a walk: the posts of one session from step one, with the pages they led to
            if e.path != form.path:
                continue
            if e.method == "POST":
                if e.form.get("revision") == "0" and walk:  # a new session starts the form over
                    walks.append(walk)
                    walk = []
                if e.status == 303 and (e.location or "").startswith("/login"):
                    continue  # the recorded session had timed out: this post was never applied
                walk.append([e, e if e.status == 400 else None])
            elif e.ok and walk and walk[-1][1] is None and bidform.kind_of(rec.text(e)):
                walk[-1][1] = e
        walks.append(walk)
        compared = differing = 0
        examples = []
        for walk in walks:
            visitor = Visitor(site)
            visitor.enter()
            _, text, _ = visitor.request("GET", form.path)
            session = site.sessions[visitor.cookie.split("=", 1)[1]]
            shown = [e for _, e in walk if e is not None]
            order, seen = [], set()
            for e in shown:  # put this session's positions in the order the recorded session had them
                title = re.search(r"<h2>[^<]*?(\S+)</h2>", rec.text(e)).group(1)
                if title in form.positions and title not in seen:
                    seen.add(title)
                    order.append(title)
            session.form.order = order + [p for p in form.positions if p not in seen]
            for post, page in walk:
                if post.form.get("action") in ("tan", "submit") or page is None:
                    break  # TANs differ by nature; the walk's pages up to the summary are what is compared
                status, text, _ = visitor.request("POST", form.path, {k: v for k, v in post.form.items() if k != "csrf"})
                compared += 1
                want = plain(site.render(rec.text(page), None))
                if plain(text) != want:
                    differing += 1
                    if len(examples) < 3:
                        got = plain(text)
                        at = next((i for i, (a, b) in enumerate(zip(got, want)) if a != b), min(len(got), len(want)))
                        examples.append(f"after {sorted(post.form.items())[:6]}:\n  got  …{got[max(0, at - 80):at + 120]!r}\n"
                                        f"  want …{want[max(0, at - 80):at + 120]!r}")
        self.assertGreater(compared, 20)
        self.assertEqual(differing, 0, f"{differing} of {compared} form pages differ\n" + "\n".join(examples))

    def test_signing_with_the_tan_from_the_mailbox(self) -> None:
        site = next(s for s in self.world.sites.values() if s.bidform)
        form = site.bidform
        visitor = Visitor(site)
        visitor.enter()
        visitor.request("GET", form.path)
        post = lambda **fields: visitor.request("POST", form.path, fields)  # noqa: E731
        post(**form.bidder, email="someone@bidder.example", action="next")
        refusing = next(iter(form.refused))
        declining = next(k for k in form.kinds if k not in form.refused and k != "bid")
        self.assertEqual(post(kind=refusing, action="next")[0], 400)
        post(kind=declining, action="next")
        post(discount="0", total="0,00", action="next")
        status, text, _ = post(acknowledge="on", action="next")
        self.assertIn('name="tan"', text)
        self.assertEqual(post(tan="WRONG", confirm_token="", action="submit")[0], 400)
        status, text, _ = post(action="tan", tan="", confirm_token="")
        token = re.search(r'data-confirm-token="([^"]*)"', text).group(1)
        tan = self.world.tans[-1]
        mailbox = Visitor(self.world.sites[self.world.mail_site])
        mailbox.enter()
        _, listing, _ = mailbox.request("GET", "/")
        prefix = self.world.tan_message.prefix
        self.assertIn(f"{prefix}{tan['number']}", listing)
        _, message, _ = mailbox.request("GET", f"/messages/{prefix}{tan['number']}")
        self.assertIn(tan["value"], message)
        status, text, _ = post(tan=tan["value"], confirm_token=token, action="submit")
        self.assertEqual(status, 200)
        self.assertNotIn('name="revision"', text)
        self.assertEqual(self.world.signed[-1]["decision"], declining)
        self.assertIn('type="password"', visitor.request("GET", form.path)[1])  # signing ended the session


if __name__ == "__main__":
    unittest.main()
