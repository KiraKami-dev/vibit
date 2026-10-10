"""
The mirror's bid form: the recorded step pages, filled in again for what a visitor enters.

Nothing of the form's wording or markup is written here. `BidForm` takes its step pages, the options each choice
opens up, the messages for wrong entries and the receipt from the recording, and only keeps the form's state:
which step, what was entered, which positions are left out, which TAN is valid. A step, choice or message that
was never recorded cannot be shown; choices nobody walked simply offer nothing further.

Signing writes the bid to `captured/_signed.json` and ends the session, as the website does.
"""
import html
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

LEVELS = ("bore", "stroke", "mounting", "certification")
TYPED = ("quantity", "price", "base_price", "surcharge", "packaging", "rebate")
MONEY = re.compile(r"\d{1,3}(\.\d{3})+(,\d{1,2})?|\d+(,\d{1,2})?")
SELECT = r'(<select name="%s"[^>]*>)(.*?)(</select>)'
OPTION = re.compile(r'<option value="([^"]*)"\s*(selected)?\s*>([^<]*)</option>')
ALERT = re.compile(r'<p role="alert">.*?</p>', re.S)
TAIL = re.compile(r'(value="add" type="submit">[^<]*</button>)(.*?)(</div>)', re.S)
TAN_SECONDS = 300
ZERO_WIDTH = "​"


def kind_of(text: str) -> str | None:
    """Which step a recorded page of the form is, by the fields it asks for."""
    for kind, mark in (("bidder", 'name="company"'), ("kind", 'name="kind"'), ("position", 'name="bore"'),
                       ("conditions", 'name="discount"'), ("declarations", 'name="acknowledge"'),
                       ("review", 'name="tan"')):
        if mark in text:
            return kind
    return None


def money(text: str) -> Decimal:
    return Decimal(text.replace(".", "").replace(",", "."))


def german(value: Decimal) -> str:
    whole, _, cents = f"{value:.2f}".partition(".")
    return re.sub(r"(?<=\d)(?=(\d{3})+$)", ".", whole) + "," + cents


def options_of(text: str, name: str) -> tuple[list[tuple[str, str]], str]:
    """A select's choices (value, label) without the empty first one, and the value chosen."""
    found = re.search(SELECT % name, text, re.S)
    choices = [(m.group(1), m.group(3)) for m in OPTION.finditer(found.group(2))] if found else []
    chosen = next((m.group(1) for m in OPTION.finditer(found.group(2)) if m.group(2)), "") if found else ""
    return [c for c in choices if c[0]], chosen


def set_options(text: str, name: str, choices: list[tuple[str, str]], chosen: str, tags: dict) -> str:
    """Rebuild a select with these choices, in the markup the recording uses for it."""
    def build(m):
        first = re.match(r'\s*<option value="">[^<]*</option>', m.group(2))
        tail = re.search(r"\s*$", m.group(2)).group(0)
        body = "".join(f'<option value="{html.escape(v)}" {"selected" if v == chosen else ""}>{label}</option>'
                       for v, label in choices)
        return tags[name][bool(choices)] + (first.group(0) if first else "") + body + tail + m.group(3)
    return re.sub(SELECT % name, build, text, count=1, flags=re.S)


def choose(text: str, name: str, chosen: str) -> str:
    """Mark one of a fixed select's options as chosen."""
    def mark(m):
        inner = re.sub(r'(<option value="[^"]+")\s*(?:selected)?\s*>', r"\1 >", m.group(2))
        if chosen:
            inner = inner.replace(f'<option value="{html.escape(chosen)}" >', f'<option value="{html.escape(chosen)}" selected>')
        return m.group(1) + inner + m.group(3)
    return re.sub(SELECT % name, mark, text, count=1, flags=re.S)


def set_input(text: str, name: str, value: str) -> str:
    return re.sub(r'(<input name="%s" value=")[^"]*' % name, lambda m: m.group(1) + html.escape(value), text, count=1)


def set_box(text: str, name: str, on: bool) -> str:
    return re.sub(r'(<input type="checkbox" name="%s")[^>]*>' % name,
                  lambda m: m.group(1) + (" checked>" if on else " >"), text, count=1)


@dataclass
class State:
    step: int = 0
    revision: int = 0
    bidder: dict = field(default_factory=dict)
    kind: str = ""
    order: list = field(default_factory=list)  # the positions in this session's order, extras last
    entries: dict = field(default_factory=dict)  # position -> what was chosen and typed; {"skip": True} if left out
    conditions: dict = field(default_factory=dict)
    declarations: dict = field(default_factory=dict)
    alert: str = ""
    cause: str = ""
    tan: dict | None = None
    token: str = ""
    notice: bool = False


class BidForm:
    def __init__(self, rec, path: str):
        self.rec, self.path = rec, path
        self.reference = path.rstrip("/").split("/")[-2]
        pages = [e for e in rec.exchanges if e.path == path and e.body and e.body.endswith(".html")]
        self.templates: dict[str, str] = {}
        self.positions: dict[str, str] = {}  # position -> its short text
        self.tree: dict[tuple, list] = {}  # choices made so far -> the choices of the next level
        self.tags: dict[str, dict] = {name: {} for name in (*LEVELS, "material")}
        self.receipt = ""
        self.tails: dict[bool, str] = {}  # what follows the add button, with and without the remove button
        for e in pages:
            text = rec.text(e)
            kind = kind_of(text)
            if kind is None:
                if 'name="revision"' not in text:
                    self.receipt = text
                continue
            if kind == "position":
                title = re.search(r"<h2>[^<]*?(\S+)</h2>", text).group(1)
                removable = 'value="remove"' in text  # once a position was added, every position page offers this
                self.tails[removable] = TAIL.search(text).group(2)
                if not removable:
                    self.positions[title] = re.search(r'id="tender-fields"[^>]*><p>([^<]*)</p>', text).group(1)
                elif title not in self.positions:
                    kind = "extra"
                text = TAIL.sub(lambda m: m.group(1) + self.tails.get(False, m.group(2)) + m.group(3), text, count=1)
                made = ()
                for level in (*LEVELS, "material"):  # how a select is written with and without choices
                    self.tags[level][bool(options_of(text, level)[0])] = re.search(SELECT % level, text, re.S).group(1)
                for level in (*LEVELS, "material"):
                    choices, chosen = options_of(text, level)
                    if choices:
                        self.tree[made] = choices
                    if not chosen:
                        break
                    made += (chosen,)
            if not ALERT.search(text) or kind not in self.templates:
                if kind == "review" and ("<tbody></tbody>" in text or re.search(r"TAN anfordern</button>\s*<p>", text)):
                    self.templates.setdefault("review", text)  # the plainest review page serves as the pattern
                    continue
                if not ALERT.search(text):
                    self.templates[kind] = text
                else:
                    self.templates.setdefault(kind, ALERT.sub("", text))
        every = [rec.text(e) for e in pages]
        self.kinds = dict(options_of(self.templates["kind"], "kind")[0]) if "kind" in self.templates else {}
        self.leads = dict(options_of(self.templates["position"], "lead")[0])
        self.omitted = next((m.group(1) for t in every for m in [re.search(r"</dl>\s*<p>([^<]*?: )[^<]*</p>", t)] if m), "")
        self.notice = next((m.group(1) for t in every
                            for m in [re.search(r'value="tan" type="submit">[^<]*</button>\s*(<p>[^<]*</p>)', t)] if m), "")
        # what a correct first step carried, which kinds were refused, and each message with what caused it
        posts = [e for e in rec.exchanges if e.path == path and e.method == "POST"]
        self.bidder = next(({k: e.form[k] for k in ("company", "vat")} for e in posts
                            if "company" in e.form and e.status == 303), {})
        self.refused = {e.form["kind"] for e in posts if "kind" in e.form and e.status != 303}
        self.alerts: dict[tuple, str] = {}
        self.conflicts: list[str] = []
        for e in posts:
            if e.status == 303 or not e.body:
                continue
            text = rec.text(e)
            kind, found = kind_of(text), ALERT.search(text)
            if not kind or not found:
                continue
            cause = self.cause(kind, e.form, None)
            if cause and self.alerts.setdefault((kind, cause), found.group(0)) != found.group(0):
                self.conflicts.append(f"{kind}/{cause}: two different messages recorded")
            elif not cause:
                self.conflicts.append(f"{kind}: a refusal the mirror's rules do not explain: {sorted(e.form)}")

    # -- the form's rules, as far as the recording shows them -----------------------------------------------------

    def cause(self, kind: str, form: dict, state: State | None) -> str:
        """Why a step's entries are refused ('' if they are fine)."""
        if kind == "bidder":
            return "" if all(form.get(k, "").strip() == v for k, v in self.bidder.items()) else "unknown"
        if kind == "kind":
            return "refused" if form.get("kind") in self.refused else ""
        if kind == "position":
            if not form.get("material"):
                return "incomplete"
            if not re.fullmatch(r"\d+", form.get("quantity", "").strip()):
                return "quantity"
            if not MONEY.fullmatch(form.get("price", "").strip()) or not form.get("lead"):
                return "format"
            if any(form.get(k, "").strip() and not MONEY.fullmatch(form[k].strip())
                   for k in ("base_price", "surcharge", "packaging", "rebate")):
                return "calculation"
            return ""
        if kind == "conditions":
            fine = re.fullmatch(r"\d+(,\d+)?", form.get("discount", "").strip()) and MONEY.fullmatch(
                form.get("total", "").strip())
            return "" if fine else "format"
        if kind == "declarations":
            return "terms" if form.get("terms") else "" if form.get("acknowledge") else "acknowledge"
        if kind == "review":
            tan = state.tan if state else None
            fine = (tan and form.get("tan", "").strip() == tan["value"] and time.time() - tan["at"] <= TAN_SECONDS
                    and form.get("confirm_token") == state.token)
            return "" if fine else "tan"
        return ""

    def steps(self, state: State) -> list[tuple[str, str]]:
        return [("bidder", ""), ("kind", ""), *((("extra" if p not in self.positions else "position"), p)
                                                for p in state.order),
                ("conditions", ""), ("declarations", ""), ("review", "")]

    def move(self, state: State, by: int) -> None:
        steps = self.steps(state)
        state.step += by
        while state.kind != "bid" and steps[state.step][0] in ("position", "extra"):
            state.step += by  # without a bid there are no positions to walk through
        for kind, position in steps[state.step + 1:] if by < 0 else []:  # going back drops what came after
            if kind in ("position", "extra"):
                state.entries.pop(position, None)
            elif kind in ("conditions", "declarations"):
                getattr(state, kind).clear()
        if by < 0:
            state.tan, state.notice = None, False

    # -- one request ------------------------------------------------------------------------------------------------

    def handle(self, site, session, method: str, form: dict):
        from mirror import Reply
        state = session.form
        if state is None:
            order = list(self.positions)
            site.random.shuffle(order)
            state = session.form = State(order=order, kind=next(iter(self.refused), ""))
        if method != "POST":
            return Reply(200, site.render(self.page(site, state), session))
        state.revision += 1
        state.alert, state.cause, state.notice = "", "", False
        kind, position = self.steps(state)[state.step]
        action = form.get("action", "")
        if action == "back":
            self.move(state, -1)
            return Reply(303, location=self.path)
        if kind in ("position", "extra"):
            entry = state.entries.setdefault(position, {})
            before = tuple(entry.get(k, "") for k in LEVELS)
            entry.update({k: form.get(k, "") for k in (*LEVELS, "material", *TYPED, "lead")})
            entry.pop("skip", None)
            for depth, level in enumerate(LEVELS):  # a changed choice empties the ones that depend on it
                if entry[level] != before[depth] or not entry[level]:
                    for later in (*LEVELS[depth + 1:], "material"):
                        if entry[level] != before[depth] or not entry[level]:
                            entry[later] = "" if later != "material" or action == "configure" else entry[later]
                    break
            if action == "configure":
                return Reply(303, location=self.path)
            if action == "skip":
                state.entries[position] = {"skip": True}
                self.move(state, 1)
                return Reply(303, location=self.path)
            if action == "add":
                if "extra" not in self.templates:
                    return Reply(303, location=self.path)  # adding a position was never recorded
                name = re.search(r"<h2>[^<]*?(\S+?)\d+</h2>", self.templates["extra"]).group(1)
                state.order.append(f"{name}{sum(p not in self.positions for p in state.order) + 1}")
                return Reply(303, location=self.path)
            added = [p for p in state.order if p not in self.positions]
            if action == "remove" and added:
                state.order.remove(added[-1])
                state.entries.pop(added[-1], None)
                if kind == "position":
                    state.entries.pop(position, None) if not any(entry.values()) else None
                return Reply(303, location=self.path)
        if kind == "review" and action == "tan":
            state.tan = site.world.new_tan(self.reference)
            state.notice = True
            return Reply(303, location=self.path)
        cause = self.cause(kind, form, state)
        if cause:
            state.alert, state.cause = self.alerts.get((kind, cause), ""), cause
            if kind == "bidder":
                state.bidder = {}
            return Reply(400, site.render(self.page(site, state, form), session))
        if kind == "bidder":
            state.bidder = {k: form.get(k, "") for k in ("company", "vat", "email")}
        elif kind == "kind":
            state.kind = form["kind"]
        elif kind == "conditions":
            state.conditions = {k: form[k].strip() for k in ("discount", "total")}
        elif kind == "declarations":
            state.declarations = {"acknowledge": True}
        elif kind == "review":
            return self.sign(site, session, state)
        self.move(state, 1)
        return Reply(303, location=self.path)

    def sign(self, site, session, state: State):
        from mirror import Reply
        bid = {"tender_reference": self.reference, "decision": "bid" if state.kind == "bid" else state.kind,
               "bidder": state.bidder, "discount": state.conditions.get("discount"),
               "total": state.conditions.get("total"), "omitted": [], "positions": []}
        for position in state.order:
            entry = state.entries.get(position, {})
            if state.kind != "bid":
                break
            if entry.get("skip"):
                bid["omitted"].append(position)
                continue
            bid["positions"].append({"position": position, **{k: entry.get(k, "") for k in ("material", *TYPED, "lead")}})
        site.world.sign(bid)
        for key, value in list(site.sessions.items()):
            if value is session:
                del site.sessions[key]  # the website ends the session with the receipt
        number = "-".join(p if i == 0 else secrets.token_hex(len(p) // 2).upper()
                          for i, p in enumerate(bid_number(self.receipt).split("-")))
        return Reply(200, site.render(self.receipt.replace(bid_number(self.receipt), number), None))

    # -- a step's page --------------------------------------------------------------------------------------------

    def page(self, site, state: State, form: dict | None = None) -> str:
        steps = self.steps(state)
        kind, position = steps[state.step]
        text = self.templates[kind]
        text = re.sub(r'(name="revision" value=")\d+', lambda m: m.group(1) + str(state.revision), text, count=1)
        number = state.step + 1
        text = re.sub(r"(<p>[^<·]*?)\d+( ·)", lambda m: m.group(1) + str(number) + m.group(2), text, count=1)
        if state.alert:
            text = re.sub(r'(name="revision" value="\d+">\s*)', lambda m: m.group(1) + state.alert, text, count=1)
        shown = form or {}
        if kind == "bidder":
            for name in ("company", "vat", "email"):
                text = set_input(text, name, shown.get(name, state.bidder.get(name, "")))
        elif kind == "kind":
            text = choose(text, "kind", shown.get("kind", state.kind))
        elif kind in ("position", "extra"):
            entry = {**state.entries.get(position, {}), **{k: v for k, v in shown.items() if k in TYPED or k == "lead"}}
            if kind == "position":
                old = re.search(r"<h2>([^<]*?)(\S+)</h2>", text)
                text = text.replace(f"<h2>{old.group(1)}{old.group(2)}</h2>", f"<h2>{old.group(1)}{position}</h2>", 1)
                text = text.replace(f'aria-label="{old.group(1)}{old.group(2)}"', f'aria-label="{old.group(1)}{position}"', 1)
                text = re.sub(r'(id="tender-fields"[^>]*><p>)[^<]*', lambda m: m.group(1) + self.positions[position], text, count=1)
            else:
                old = re.search(r"<h2>([^<]*?)(\S+)</h2>", text)
                text = text.replace(old.group(2), position)
            made = ()
            for level in (*LEVELS, "material"):
                choices = self.tree.get(made, []) if len(made) == (*LEVELS, "material").index(level) else []
                chosen = entry.get(level, "")
                text = set_options(text, level, choices, chosen, self.tags)
                if chosen and any(v == chosen for v, _ in choices):
                    made += (chosen,)
            added = any(p not in self.positions for p in state.order)
            text = TAIL.sub(lambda m: m.group(1) + self.tails.get(added, m.group(2)) + m.group(3), text, count=1)
            opened = "open" if state.cause == "calculation" else ""  # the breakdown opens to show what is wrong
            text = re.sub(r'(<details id="calculation-details" )[^>]*>', lambda m: m.group(1) + opened + ">", text, count=1)
            for name in TYPED:
                text = set_input(text, name, entry.get(name, ""))
            text = choose(text, "lead", entry.get("lead", ""))
        elif kind == "conditions":
            for name in ("discount", "total"):
                text = set_input(text, name, shown.get(name, state.conditions.get(name, "")))
            gross = float(sum(int(e["quantity"]) * money(e["price"]) for e in state.entries.values()
                              if not e.get("skip") and e.get("quantity") and state.kind == "bid")) * 1.19
            label = f"{round(gross, 2):,.2f}".rstrip("0").rstrip(".") if gross else "0"
            text = re.sub(r'aria-label="[^"]*(, [a-z]{2}-[A-Z]{2})">[^<]*( <small>)',
                          lambda m: f'aria-label="{label}{m.group(1)}">{label}{m.group(2)}', text, count=1)
            exact = str(int(gross)) if gross == int(gross) else repr(gross)  # as the page's script writes a number
            text = re.sub(r"(total_net=)[^<]*", lambda m: m.group(1) + exact, text, count=1)
        elif kind == "declarations":
            text = set_box(text, "acknowledge", bool(shown.get("acknowledge", state.declarations.get("acknowledge"))))
            text = set_box(text, "terms", bool(shown.get("terms")))
        elif kind == "review":
            text = self.review(state, text)
        return text

    def review(self, state: State, text: str) -> str:
        discount = state.conditions.get("discount", "0")
        values = [state.bidder.get("company", ""), state.bidder.get("vat", ""), state.bidder.get("email", ""), None,
                  self.kinds.get(state.kind, state.kind), f"{discount} %",
                  german(money(state.conditions.get("total", "0"))) + " EUR"]
        cells = iter(values)

        def fill(m):
            value = next(cells, None)
            return m.group(0) if value is None else f"<dd>{html.escape(value)}</dd>"
        text = re.sub(r"<dd>[^<]*</dd>", fill, text, count=len(values))
        left = [p for p in state.order if state.entries.get(p, {}).get("skip")] if state.kind == "bid" else []
        rows = ""
        for position in sorted(p for p in state.order if state.kind == "bid" and p not in left):
            e = state.entries.get(position, {})
            calc = [german(money(e[k])) if e.get(k, "").strip() else "—" for k in ("base_price", "surcharge", "packaging", "rebate")]
            cells_ = [position, e.get("material", ""), e.get("quantity", ""), german(money(e.get("price", "0"))) + " EUR",
                      self.leads.get(e.get("lead", ""), ""), *calc]
            rows += "<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in cells_) + "</tr>"
        paragraph = f"<p>{self.omitted}{', '.join(sorted(left))}</p>" if left else ""
        text = re.sub(r"(</dl>\s*?\n\s*)(?:<p>[^<]*</p>)?(\s*<table>)", lambda m: m.group(1) + paragraph + m.group(2), text, count=1)
        text = re.sub(r"<tbody>.*?</tbody>", lambda m: f"<tbody>{rows}</tbody>", text, count=1, flags=re.S)
        notice = re.sub(r"\d+(?=\D*</p>$)", str(state.tan["number"]), self.notice) if state.notice and state.tan else ""
        text = re.sub(r'(value="tan" type="submit">[^<]*</button>\s*?\n\s*)(?:<p>[^<]*</p>)?',
                      lambda m: m.group(1) + notice, text, count=1)
        state.token = secrets.token_hex(4).upper()
        return re.sub(r'(data-confirm-token=")[^"]*', lambda m: m.group(1) + state.token, text, count=1)


def bid_number(receipt: str) -> str:
    found = re.search(r"\b[A-Z]{2,}-[0-9A-F]{6,}\b", receipt)
    return found.group(0) if found else ""


def find(rec) -> BidForm | None:
    """The bid form of a recording: the address whose form posts carry a revision counter."""
    paths = {e.path for e in rec.exchanges if e.method == "POST" and "revision" in e.form}
    return BidForm(rec, sorted(paths)[0]) if paths else None


# -- the TAN in the mailbox -------------------------------------------------------------------------------------

@dataclass
class TanMail:
    prefix: str  # what the TAN messages' ids start with
    highest: int  # the highest request number in the recording
    subject: str  # the recorded pattern message's subject, ending in its number
    value: str  # the TAN in the pattern message
    message: str  # the pattern message's page


def find_tan_mail(recordings: dict) -> tuple[str | None, None, TanMail | None]:
    """The website whose messages include a numbered family (the TAN mails), and the newest of them as pattern."""
    for site_id, rec in recordings.items():
        family: dict[str, dict[int, object]] = {}
        for (path, _), found in rec.pages.items():
            m = re.fullmatch(r"/messages/(.*?)(\d+)", path)
            if m:
                family.setdefault(m.group(1), {})[int(m.group(2))] = found[-1]
        for prefix, members in family.items():
            if len(members) < 3:
                continue
            numbers = sorted(members)
            newest, other = rec.text(members[numbers[-1]]), rec.text(members[numbers[-2]])
            unique = [t for t in re.findall(r"\b[0-9A-F]{8}\b", newest) if t not in other]
            subject = re.search(r'aria-label="([^"]*\b%d)"' % numbers[-1], newest)
            listed = max((int(n) for found in rec.pages.values() for e in found[-1:]
                          if e.body.endswith(".html") for n in re.findall(re.escape(prefix) + r"(\d+)", rec.text(e))),
                         default=numbers[-1])
            if unique and subject:
                return site_id, None, TanMail(prefix, listed, subject.group(1), unique[0], newest)
    return None, None, None


def renumber(text: str, mail: TanMail, old: int, new: int) -> str:
    """A page naming TAN message `old`, naming `new` instead (its id, its subject, also spelt with hidden spaces)."""
    was = re.sub(r"\d+$", str(old), mail.subject)
    now = re.sub(r"\d+$", str(new), mail.subject)
    text = text.replace(was, now).replace(ZERO_WIDTH.join(was), ZERO_WIDTH.join(now))
    return re.sub(re.escape(mail.prefix) + str(old) + r"\b", mail.prefix + str(new), text)


def tan_mail(world, site, session, path: str, query: tuple):
    """Pages of the mailbox that depend on the TANs this mirror handed out; None for every other page."""
    from mirror import Reply
    mail = world.tan_message
    if not mail or not world.tans:
        return None
    m = re.fullmatch(r"/messages/" + re.escape(mail.prefix) + r"(\d+)", path)
    if m and int(m.group(1)) > mail.highest:
        tan = next((t for t in world.tans if t["number"] == int(m.group(1))), None)
        if not tan:
            return None
        old = int(re.search(r"\d+$", mail.subject).group(0))
        return Reply(200, site.render(renumber(mail.message, mail, old, tan["number"]).replace(mail.value, tan["value"]), session))
    if path == "/" and not dict(query).get("q"):
        found = site.rec.page(path, query) or site.rec.page(path, ())
        if not found:
            return None
        text, shift = site.rec.text(found), len(world.tans)
        numbers = sorted({int(n) for n in re.findall(re.escape(mail.prefix) + r"(\d+)", text)}, reverse=True)
        for number in numbers:  # the list moves up: the newest shown become the mirror's own TAN messages
            text = renumber(text, mail, number, number + shift + 10 ** 6)
        for number in numbers:
            text = renumber(text, mail, number + shift + 10 ** 6, number + shift + (mail.highest - numbers[0]))
        return Reply(200, site.render(text, session))
    return None


def write_signed(directory: Path, bids: list[dict]) -> None:
    (directory / "_signed.json").write_text(json.dumps(bids, ensure_ascii=False, indent=1), encoding="utf-8")
