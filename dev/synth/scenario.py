"""
Synthetic RFQ scenarios: invented tenders with the same jobs and bid rules as the challenge, on other data.

`generate(seed)` builds one `World`: a tender with positions, a bidder Q&A, a specification with revisions, ERP
materials with stock and price conditions, and a mailbox of authority records, with the traps the 29 rules name
(superseded, future-dated, look-alike and wrong-sender records, withdrawn answers, inactive exact matches, blocked
stock, scale and ceiling boundaries). Every value is invented from the seed; nothing is copied from the challenge
websites. Never part of the package: this lives outside `agent/`.

The world holds raw records only. Which record governs is worked out by `oracle.solve`, which the generator also
uses to place traps around the true values (a threshold just above the order value, a ceiling one piece short).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal as D

UTC = timezone.utc
GENERATION = 3  # raise when a seed no longer means the same scenario; runs record it
MOUNTINGS = ["Flansch vorn", "Flansch hinten", "Schwenkauge", "Fußbefestigung", "Gelenkkopf", "Schwenkzapfen"]
CERTS = ["ohne", "ATEX", "DNV", "Abnahmeprüfzeugnis 3.1"]
BORES = [32, 40, 50, 63, 80, 100, 125]
PRESSURES = [160, 210, 250, 315]
SUBJECTS = {  # record kind -> subject word and authorised sender local part (rules 12, 21, 28)
    "condition_rules": ("Konditionsregeln", "kalkulation"), "metal": ("Preisindizes", "kalkulation"),
    "reserve": ("Technikfreigabe", "technik"), "packaging": ("Verpackungstarife", "logistik"),
    "commitment": ("Terminfreigabe", "logistik"), "fx": ("Devisenfixing", "finanzen"),
    "pool": ("Rüstpools", "kalkulation"), "bonus": ("Projektbonus", "finanzen"),
}
ATTRIBUTE_LABELS = {"bore": ("Kolbendurchmesser", "mm"), "stroke": ("Hub", "mm"), "pressure": ("Nenndruck", "bar"),
                    "mounting": ("Befestigungsart", ""), "certification": ("Zertifizierung", "")}


LOCALE = ["de"]  # the notation numbers are written in; a mail may switch to "en" for its own text


NOTATIONS = {"de": "de-DE (Dezimalkomma, Punkt als Tausendertrenner)", "en": "en-US (Dezimalpunkt, Komma als "
             "Tausendertrenner)", "ch": "de-CH (Dezimalpunkt, Apostroph als Tausendertrenner)",
             "sp": "fr-FR (Dezimalkomma, Leerzeichen als Tausendertrenner)"}


def de(value, places: int = 2) -> str:
    """A number in German notation, 1.234,56, or in the notation a mail is being written in."""
    q = D(str(value)).quantize(D(1).scaleb(-places), rounding=ROUND_HALF_UP)
    text = f"{q:,.{places}f}"
    if LOCALE[0] == "en":
        return text
    if LOCALE[0] == "ch":
        return text.replace(",", "'")
    return text.replace(",", "X").replace(".", ",").replace("X", " " if LOCALE[0] == "sp" else ".")


def plain(value) -> str:
    """A number without trailing zeros (400 or 12,5; 12.5 where the notation has a decimal point)."""
    text = format(D(str(value)).normalize(), "f")
    return text if LOCALE[0] in ("en", "ch") else text.replace(".", ",")


@dataclass
class Position:
    number: str
    text: str
    quantity: int
    unit: str  # "Stück" or "Satz"
    requirement: str


@dataclass
class QA:
    number: int
    date: datetime
    kind: str  # quantity, withdrawal, group, packaging, commitment, pool, bonus, setup, amendment, noise
    data: dict
    question: str
    answer: str


@dataclass
class Req:
    rid: str
    bore: int
    stroke: D  # the usable travel when `usable`
    pressure: int
    mounting: str
    certification: str
    group: str
    pieces_per_set: int | None = None
    usable: bool = False
    reserve_key: str = ""


@dataclass
class Revision:
    name: str
    date: datetime
    status: str  # current, superseded, draft
    reqs: dict[str, Req]


@dataclass
class Condition:
    valid_from: date
    valid_to: date
    currency: str
    fx_key: str
    price_unit: int
    scales: list[tuple[int, D]]


@dataclass
class Material:
    number: str
    group: str
    text: str
    bore: int
    stroke: D
    pressure: int
    mounting: str
    certification: str
    status: str  # active, inactive, blocked
    free_stock: int = 0
    blocked_stock: int = 0
    lt_stock: int = 2
    lt_prod: int = 8
    carton: int = 10
    alloc_mass: D = D(0)
    sur_mass: D = D(0)
    index_code: str = ""
    conditions: list[Condition] = field(default_factory=list)

    def attrs(self) -> dict:
        return {"bore": self.bore, "stroke": D(self.stroke), "pressure": self.pressure, "mounting": self.mounting,
                "certification": self.certification}


@dataclass
class Mail:
    mid: int
    folder: str
    sender_name: str
    sender: str
    date: datetime
    subject: str
    body: str
    kind: str = "noise"
    reference: str = ""
    is_record: bool = False
    data: dict = field(default_factory=dict)


@dataclass
class Tender:
    tid: str
    reference: str
    title: str
    buyer: str
    positions: list[Position]


@dataclass
class World:
    seed: int
    reference: str
    scenario: datetime
    company: str
    vat: str
    domain: str
    mailbox: str
    director_name: str
    director: str
    tender: Tender
    decoys: list[Tender]
    qa: list[QA]
    spec_title: str
    revisions: list[Revision]  # oldest first
    materials: list[Material]
    mails: list[Mail]
    features: dict = field(default_factory=dict)

    def senders(self) -> dict[str, str]:
        out = {kind: f"{local}@{self.domain}" for kind, (_, local) in SUBJECTS.items()}
        out["approval"] = self.director
        return out

    def current(self) -> Revision:
        return next(r for r in self.revisions if r.status == "current")


class Bad(Exception):
    """The draw did not make a clean scenario; `generate` tries the next sub-seed."""


def _reference(r: random.Random, year: int, strange: bool = False) -> str:
    n = r.randint(100, 9899)
    if strange:  # spaces, dots, slashes and words inside the reference
        return r.choice([f"VgV {year}/{n:04d} Los {r.randint(1, 9)}", f"Az. {n:04d}-{year % 100:02d}/HZ",
                         f"ÖA.{n:04d}.{year}", f"{year}-S {n:04d}/b"])
    return r.choice([f"VG-{year}-{n:04d}", f"{year}/{n:04d}-HZ", f"AVA-{n:04d}-{year % 100:02d}",
                     f"B{year % 100:02d}.{n:04d}", f"EU-{year}-{n:04d}-L"])


def _material_number(r: random.Random, scheme: int, used: set[str]) -> str:
    while True:
        n = [f"{r.randint(40000000, 49999999)}", f"M-{r.randint(100000, 999999)}",
             f"HZ{r.randint(1000, 9999)}-{r.choice('ABCDEFGH')}{r.randint(10, 99)}", f"ZY-{r.randint(1000, 9999)}",
             f"HZ {r.randint(1000, 9999)}.{r.choice('ABC')}", f"{r.randint(10, 99)}.{r.randint(100, 999)}.{r.randint(10, 99)}",
             f"ZYL+{r.randint(1000, 9999)}"][scheme]
        if n not in used and not any(n in u or u in n for u in used):
            used.add(n)
            return n


def _vary(attrs: dict, r: random.Random, keys=("bore", "stroke", "pressure", "mounting", "certification")) -> dict:
    out, key = dict(attrs), r.choice(keys)
    pools = {"bore": BORES, "pressure": PRESSURES, "mounting": MOUNTINGS, "certification": CERTS}
    if key == "stroke":
        out["stroke"] = D(out["stroke"]) + r.choice([-50, -25, 25, 50, 100])
    else:
        out[key] = r.choice([v for v in pools[key] if v != out[key]])
    return out


PLAIN = {"no_bid": False, "satz": False, "shared": False, "usable": False, "amend": False, "withdrawn": False,
         "fx": False, "unit100": False, "approval": "plain", "groups": False, "metal": False, "windows": False,
         "packaging": False, "commitment": "none", "pool": "none", "bonus": False, "setup": False}
FULL = {"no_bid": False, "satz": True, "shared": True, "usable": True, "amend": True, "withdrawn": True, "fx": True,
        "unit100": True, "groups": True, "metal": True, "windows": True, "packaging": True, "commitment": "applies",
        "pool": "largest_remainder", "bonus": True, "setup": True, "positions": 5}
REAL_FORMS = {"opaque_ids": True, "tight_dates": True, "small_quantities": True}
TRAPS = [  # one bid rule's trap at a time, on an otherwise plain tender
    ("set-units", {"satz": True}), ("shared-stock", {"shared": True}), ("usable-travel", {"usable": True}),
    ("amendment", {"amend": True}), ("withdrawn-answer", {"withdrawn": True}), ("currency", {"fx": True}),
    ("price-unit", {"unit100": True}), ("no-approval", {"approval": "none"}),
    ("threshold-met", {"approval": "cond_pass"}), ("threshold-missed", {"approval": "cond_fail"}),
    ("condition-groups", {"groups": True, "metal": True}), ("metal-rate", {"metal": True}),
    ("metal-windows", {"metal": True, "windows": True}), ("packaging", {"packaging": True}),
    ("commitment-applies", {"commitment": "applies"}), ("commitment-over-ceiling", {"commitment": "ceiling"}),
    ("commitment-other-material", {"commitment": "material"}), ("setup-pool", {"pool": "largest_remainder"}),
    ("setup-pool-rest", {"pool": "rest_to_largest"}), ("project-bonus", {"bonus": True}),
    ("setup-fee", {"setup": True}), ("missing-material", {"no_bid": True}),
]
DATASETS = {
    "mixed": "every feature drawn at random: the broad regression set",
    "public-like": "every rule in play at once on five positions, in the surface forms of the public data",
    "no-bid": "as public-like, but one position has no active exact match",
    "one-trap": "a plain tender with exactly one rule's trap, to see which rule breaks (seed picks the trap)",
    "plain": "no clause at all: tender quantity, one match, base price, the director's discount",
    "single-position": "a tender with one position: nothing to pool, share or split",
    "twelve-positions": "twelve positions with every rule in play",
    "one-requirement": "five positions that all name the same requirement: one material, pooled stock, own scales",
    "exact-ties": "equal quantities of one material in a pool and a bonus: every cent split is a tie",
    "all-sets": "every position is counted in sets",
    "every-currency": "every price in a foreign currency, per 100 or per 1000 pieces",
    "boundaries-met": "stock, ceiling, threshold, validity and band edge are met exactly",
    "boundaries-missed": "stock, ceiling and threshold are missed by one piece or one cent",
    "minute-precision": "records minutes before and after the scenario time",
    "long-chains": "seven or more versions of every record, several not yet in force",
    "decoy-flood": "look-alike references, wrong senders and fifty unrelated mails around every record",
    "only-fakes": "an approval exists only as look-alike, colleague's note and future mail: 0 %",
    "many-revisions": "eight specification revisions, the governing one near the end, plus a draft",
    "release-taken-back": "the newest release was withdrawn, so the revision before it governs",
    "amend-everything": "every requirement amended in two or three attributes, some more than once",
    "future-withdrawal": "the withdrawal of an answer is dated after the scenario, so the answer still counts",
    "odd-discounts": "approved percentages such as 0, 0.5, 12.75 and 33.333 with condition groups",
    "huge-quantities": "tens of thousands of pieces at cent prices per 100 or 1000",
    "tiny-quantities": "one to three pieces, so setup, cartons and bonus dominate the unit price",
    "strange-ids": "references, positions, requirements, groups and materials with spaces, dots, signs, umlauts",
    "foreign-notations": "mails in Swiss and French number notation beside German and English",
    "reply-chains": "subjects with AW:, WG:, Fwd: prefixes and long version histories",
    "price-history": "eight price records per material with up to nine scale steps",
    "metal-maze": "four index codes with five validity windows each, listed in any order",
    "bonus-bands": "five marginal bonus bands with every position a member",
    "no-bid-discounts": "no-bid cases: a plain approval keeps its percentage, a conditional one gives 0 %",
    "nothing-assigned": "tariffs, pools, bonus plans and commitments exist in the mailbox but no answer assigns one",
    "everything-weird": "strange ids, minute precision, decoy flood, long chains and foreign notations on eight positions",
}


def profile(dataset: str, seed: int) -> dict:
    """The features a dataset fixes for `seed`; everything else is drawn."""
    if dataset == "mixed":
        return {}
    if dataset == "public-like":
        return {**FULL, **REAL_FORMS}
    if dataset == "no-bid":
        return {**FULL, **REAL_FORMS, "no_bid": True}
    if dataset == "plain":
        return dict(PLAIN)
    if dataset == "one-trap":
        name, trap = TRAPS[seed % len(TRAPS)]
        return {**PLAIN, **trap, "trap": name}
    full = {**FULL, **REAL_FORMS}
    special = {
        "single-position": {**full, "positions": 1, "shared": False},
        "twelve-positions": {**full, "positions": 12},
        "one-requirement": {**full, "one_requirement": True, "members_all": True},
        "exact-ties": {**PLAIN, "one_requirement": True, "equal_quantities": True, "members_all": True, "positions": 5,
                       "pool": ("largest_remainder", "rest_to_largest")[seed % 2], "bonus": True, "metal": True},
        "all-sets": {**full, "all_sets": True},
        "every-currency": {**full, "fx_all": True, "unit_all": True},
        "boundaries-met": {**full, "boundary": "exact", "approval": "cond_pass", "commitment": "applies"},
        "boundaries-missed": {**full, "boundary": "miss", "approval": "cond_fail", "commitment": "ceiling"},
        "minute-precision": {**full, "tight_dates": "minutes"},
        "long-chains": {**full, "chain": 6},
        "decoy-flood": {**full, "decoys": "heavy"},
        "only-fakes": {**full, "approval": "fakes_only", "decoys": "heavy"},
        "many-revisions": {**full, "revisions": 8},
        "release-taken-back": {**full, "later_withdrawn": True, "revisions": 3},
        "amend-everything": {**full, "amend": "heavy"},
        "future-withdrawal": {**full, "withdrawn": "future"},
        "odd-discounts": {**full, "odd_discount": True, "approval": ("plain", "cond_pass")[seed % 2]},
        "huge-quantities": {**full, "scale": "huge", "small_quantities": False, "unit_all": True},
        "tiny-quantities": {**full, "scale": "tiny"},
        "strange-ids": {**FULL, "strange_ids": True, "opaque_ids": False, "tight_dates": True,
                        "small_quantities": True},
        "foreign-notations": {**full, "notations": ["ch", "sp", "de", "en"]},
        "reply-chains": {**full, "subject_noise": True, "chain": 4},
        "price-history": {**full, "condition_chain": 8, "scale_steps": True},
        "metal-maze": {**full, "index_codes": 4, "window_count": 5, "metal_all": True},
        "bonus-bands": {**full, "bonus_cuts": ["0.15", "0.35", "0.6", "0.85"], "members_all": True},
        "no-bid-discounts": {**full, "no_bid": True, "approval": ("plain", "cond_pass")[seed % 2]},
        "nothing-assigned": {**full, "unassigned": True, "withdrawn": True},
        "everything-weird": {**FULL, "positions": 8, "strange_ids": True, "opaque_ids": False,
                             "small_quantities": True, "tight_dates": "minutes", "decoys": "heavy", "chain": 4,
                             "notations": ["ch", "sp", "de", "en"], "subject_noise": True, "amend": "heavy",
                             "later_withdrawn": True, "revisions": 5, "window_count": 5},
    }
    if dataset in special:
        return special[dataset]
    raise ValueError(f"unknown dataset {dataset!r}; known: {', '.join(DATASETS)}")


def generate(seed: int, dataset: str = "mixed") -> World:
    """One clean scenario for `seed` (the first of its sub-seeds whose draw passes the oracle's checks)."""
    fixed = profile(dataset, seed)
    salt = list(DATASETS).index(dataset) * 7_000_003
    for attempt in range(200):
        try:
            return _generate(seed, random.Random(seed * 1009 + attempt + salt), {**fixed, "dataset": dataset})
        except (Bad, ValueError):  # ValueError: the oracle found the draw ambiguous
            continue
    raise RuntimeError(f"no clean scenario for seed {seed}")


def _generate(seed: int, r: random.Random, fixed: dict) -> World:
    import oracle  # the generator places traps around true values

    f = {
        "no_bid": r.random() < 0.15, "satz": r.random() < 0.6, "shared": r.random() < 0.6,
        "usable": r.random() < 0.6, "amend": r.random() < 0.7, "withdrawn": r.random() < 0.6,
        "fx": r.random() < 0.6, "unit100": r.random() < 0.5,
        "approval": r.choices(["none", "plain", "cond_pass", "cond_fail"], [15, 35, 25, 25])[0],
        "groups": r.random() < 0.7, "metal": r.random() < 0.85, "windows": r.random() < 0.6,
        "packaging": r.random() < 0.7, "commitment": r.choice(["none", "applies", "ceiling", "material"]),
        "pool": r.choice(["none", "largest_remainder", "rest_to_largest"]), "bonus": r.random() < 0.6,
        "setup": r.random() < 0.7,
    }
    # Surface forms as real business data has them: opaque ids instead of 1, 2, 3 and A, B, C; records dated within
    # hours of each other, so the time of day decides; small quantities.
    opaque, tight, small = r.random() < 0.6, r.random() < 0.6, r.random() < 0.5
    f.update(opaque_ids=opaque, tight_dates=tight, small_quantities=small)
    f.update(fixed)  # what the dataset fixes
    opaque, tight, small = f["opaque_ids"], f["tight_dates"], f["small_quantities"]
    strange, edge, chain = bool(fixed.get("strange_ids")), fixed.get("boundary"), fixed.get("chain", 0)
    flood, unassigned, minutes = fixed.get("decoys") == "heavy", bool(fixed.get("unassigned")), tight == "minutes"
    notations = fixed.get("notations") or ["de", "en"]
    step = timedelta(minutes=1) if minutes else timedelta(hours=1)

    def tag(prefix: str, simple: str) -> str:
        return f"{prefix}-{r.randint(1000, 9999)}" if opaque else simple

    now = datetime(r.choice([2025, 2026, 2027]), r.randint(1, 12), r.randint(3, 26), r.randint(7, 16),
                   r.choice([0, 15, 30, 45]), tzinfo=UTC)
    ref = _reference(r, now.year, strange)
    stem = r.choice(["Nordhydraulik", "Rheinzylinder", "Albtal Fluidtechnik", "Weserpress", "Mainkolben"])
    company = f"{stem} {r.choice(['GmbH', 'AG', 'GmbH & Co. KG'])}"
    domain = stem.lower().replace(" ", "-") + ".example"
    if strange:
        company, domain = r.choice([("Müller & Söhne Hydraulik GmbH", "mueller-soehne.example"),
                                    ("Åkesson-Weiß Zylinderbau e.K.", "akesson-weiss.example"),
                                    ("O'Brien Fluid Power (Deutschland) GmbH", "obrien-fluid.example")])
    first, last = r.choice(["Miriam", "Jonas", "Elif", "Henrik", "Carla"]), r.choice(
        ["Brandt", "Okafor", "Lindqvist", "Yilmaz", "Sommer"])
    director = f"{first[0].lower()}.{last.lower()}@{domain}"
    mailbox = f"{r.choice(['vertrieb', 'angebote', 'sales'])}@{domain}"

    stamps = sorted({now - (timedelta(minutes=r.randint(60, 1500)) if minutes else
                            timedelta(days=2, minutes=5 * r.randint(0, 3 * 288)) if tight else
                            timedelta(days=r.randint(2, 45), minutes=r.randint(0, 600))) for _ in range(600)})
    r.shuffle(stamps)

    def before(recent: bool = False) -> datetime:
        """A distinct timestamp before the scenario date."""
        if not stamps:
            raise Bad
        if recent:
            newest = max(stamps)
            stamps.remove(newest)
            return newest
        return stamps.pop()

    def after() -> datetime:
        if minutes:
            return now + timedelta(minutes=r.randint(1, 59))
        if tight:  # later the same day or the next: only the time of day tells it is in the future
            return now + timedelta(minutes=5 * r.randint(1, 360))
        return now + timedelta(days=r.randint(1, 15), minutes=r.randint(0, 600))

    # ---- specification: revisions oldest first, the governing one marked current
    names = r.choice([list("ABCDEFGHIJ"), [f"{i:02d}" for i in range(1, 11)],
                      [f"{2 + i // 3}.{i % 3}" for i in range(10)],
                      ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]])
    if opaque:  # ids that say nothing about their order
        names = [f"{r.choice(['R', 'SP', 'DOK'])}-{n}" for n in r.sample(range(1000, 9999), 10)]
    count = fixed.get("revisions") or r.randint(2, 3)
    rids = r.sample(r.choice([[f"A-{i:02d}" for i in range(1, 13)], [f"R{i}" for i in range(11, 25)],
                              [f"ANF-{i}" for i in range(101, 115)]]), 12)
    if strange:
        rids = r.sample(r.choice([[f"Pkt. 4.{i}" for i in range(1, 14)], [f"TA/{i:03d}" for i in range(1, 14)],
                                  [f"§{i} Abs. 2" for i in range(3, 16)]]), 12)
    n_pos = fixed.get("positions") or r.randint(3, 5)
    if n_pos < 2:
        f.update(shared=False, pool="none", bonus=False)
    n_req = 1 if fixed.get("one_requirement") else (n_pos - 1 if f["shared"] else n_pos)
    group_pool = r.sample(range(100, 999), 30)
    scheme = r.randrange(3)

    def group(i: int) -> str:
        if strange:
            return [f"HZ/{group_pool[i]}-DW", f"Kat. {group_pool[i]} Z", f"Z.{group_pool[i]}.H"][scheme]
        return [f"HZG-{group_pool[i]}", f"KAT{group_pool[i]}Z", f"Z-{group_pool[i]}-H"][scheme]

    reqs: dict[str, Req] = {}
    for i in range(n_req):
        reqs[rids[i]] = Req(rids[i], r.choice(BORES), D(r.choice(range(100, 850, 50))), r.choice(PRESSURES),
                            r.choice(MOUNTINGS), r.choice(CERTS), group(i))
    order = list(reqs)
    if f["usable"]:
        req = reqs[r.choice(order)]
        req.usable, req.reserve_key = True, tag("ER", f"ER-{r.randint(1, 9)}")
    position_numbers = r.choice([[str(10 * i) for i in range(1, 13)], [str(i) for i in range(1, 13)],
                                 [f"{i // 2 + 1}.{i % 2 + 1}" for i in range(12)], [f"{i:03d}" for i in range(1, 13)],
                                 [f"01.{i:02d}" for i in range(1, 13)]])
    if strange:
        position_numbers = r.choice([list("ABCDEFGHIJKL"), [f"LV {i}" for i in range(1, 13)],
                                     [f"{i:04d}0" for i in range(1, 13)]])
    req_of = order + [r.choice(order) for _ in range(n_pos - len(order))]
    r.shuffle(req_of)
    units = ["Satz" if fixed.get("all_sets") else "Stück"] * n_pos
    if f["satz"]:
        i = r.randrange(n_pos)
        units[i] = "Satz"
        reqs[req_of[i]].pieces_per_set = r.choice([2, 3, 4, 6])
    for req in reqs.values():  # every requirement states its set size, used or not
        req.pieces_per_set = req.pieces_per_set or r.choice([2, 4])
    def tender_quantity(unit: str) -> int:
        low, high = {"huge": (10_000, 200_000), "tiny": (1, 3)}.get(fixed.get("scale"),
                                                                     (5, 60) if small else (20, 600))
        if unit == "Satz":
            low, high = max(1, low // 10), max(2, high // 10)
        return r.randint(low, high)

    positions = [Position(position_numbers[i], f"Hydraulikzylinder {r.choice(['doppeltwirkend', 'einfachwirkend'])}"
                          f", Anforderung {req_of[i]}", tender_quantity(units[i]), units[i], req_of[i])
                 for i in range(n_pos)]
    if fixed.get("equal_quantities"):
        for p in positions:
            p.quantity = positions[0].quantity

    def other(req: Req) -> Req:
        """The same requirement in another revision: one or two attributes differ."""
        attrs = {"bore": req.bore, "stroke": req.stroke, "pressure": req.pressure, "mounting": req.mounting,
                 "certification": req.certification}
        for _ in range(r.randint(1, 2)):
            attrs = _vary(attrs, r)
        return Req(req.rid, attrs["bore"], D(attrs["stroke"]), attrs["pressure"], attrs["mounting"],
                   attrs["certification"], req.group, r.choice([2, 3, 4, 6, 8]), req.usable, req.reserve_key)

    start = now - timedelta(days=r.randint(120, 300) + 30 * count)
    revisions = [Revision(names[i], start + timedelta(days=30 * i), "superseded", {k: other(v) for k, v in reqs.items()})
                 for i in range(count - 1)]
    revisions.append(Revision(names[count - 1], start + timedelta(days=30 * count), "current", reqs))
    if fixed.get("later_withdrawn"):  # a newer release that was taken back: the one before it governs again
        revisions.append(Revision(names[count], now - timedelta(days=r.randint(8, 30)), "withdrawn",
                                  {k: other(v) for k, v in reqs.items()}))
    if r.random() < 0.5:  # a newer draft that is not released
        revisions.append(Revision(names[len(revisions)], now - timedelta(days=r.randint(1, 6)), "draft",
                                  {k: other(v) for k, v in reqs.items()}))
    current = revisions[count - 1]

    # ---- bidder Q&A
    qa: list[dict] = []

    def ask(kind: str, when: datetime, data: dict, question: str, answer: str) -> dict:
        qa.append({"kind": kind, "date": when, "data": data, "question": question, "answer": answer})
        return qa[-1]

    for p in r.sample(positions, min(n_pos, r.randint(1, 2))):  # quantity changes: an older and the governing one, a future one
        unit = "Sätze" if p.unit == "Satz" else "Stück"
        first_qty = max(1, p.quantity + r.choice([-3, 4, 9, 15]))
        ask("quantity", before(), {"position": p.number, "quantity": first_qty},
            f"Bleibt die Menge der Position {p.number} unverändert?",
            f"Nein. Die Menge der Position {p.number} wird auf {first_qty} {unit} geändert.")
        if r.random() < 0.6:
            qty = max(1, p.quantity + r.choice([-2, 6, 12, 25]))
            ask("quantity", before(recent=True), {"position": p.number, "quantity": qty},
                f"Gilt für Position {p.number} weiterhin die zuletzt genannte Menge?",
                f"Die Menge der Position {p.number} beträgt nunmehr {qty} {unit}.")
        if r.random() < 0.5:
            qty = p.quantity + r.choice([40, 75])
            ask("quantity", after(), {"position": p.number, "quantity": qty},
                f"Ist für Position {p.number} eine weitere Mengenanpassung vorgesehen?",
                f"Die Menge der Position {p.number} wird auf {qty} {unit} geändert.")
    if f["withdrawn"]:
        p = r.choice(positions)
        unit = "Sätze" if p.unit == "Satz" else "Stück"
        qty = p.quantity + r.choice([7, 18, 33])
        gone = ask("quantity", now - step * r.randint(30, 40), {"position": p.number, "quantity": qty},
                   f"Kann die Menge der Position {p.number} erhöht werden?",
                   f"Die Menge der Position {p.number} wird auf {qty} {unit} geändert.")
        ask("withdrawal", after() if f["withdrawn"] == "future" else now - step * r.randint(2, 20), {"withdraws": gone},
            "Ist die Mengenänderung noch gültig?", "Antwort Nr. {target} wird hiermit zurückgezogen.")
        f["withdrawn_position"] = p.number
    if f["amend"]:
        heavy, keys = f["amend"] == "heavy", ["pressure", "mounting", "certification", "stroke"]
        pairs = [(q, k) for q in (list(reqs.values()) if heavy else r.sample(list(reqs.values()), min(
            len(reqs), r.randint(1, 2)))) for k in (r.sample(keys, r.randint(2, 3)) if heavy else [r.choice(keys)])]
        for req, key in pairs:
            label, unit = ATTRIBUTE_LABELS[key]
            attrs = {"bore": req.bore, "stroke": req.stroke, "pressure": req.pressure, "mounting": req.mounting,
                     "certification": req.certification}

            def amend(when: datetime, revision: str, value) -> None:
                shown = plain(value) if key in ("stroke", "pressure") else value
                what = "nutzbarer Weg" if key == "stroke" and req.usable else label
                ask("amendment", when, {"revision": revision, "requirement": req.rid, "attribute": key,
                                        "value": value},
                    f"Gibt es technische Änderungen zur Anforderung {req.rid}?",
                    f"Technische Änderung zur Spezifikation Revision {revision}, Anforderung {req.rid}: "
                    f"{what} neu {shown}{' ' + unit if unit else ''}. Alle übrigen Merkmale bleiben unverändert.")

            if r.random() < 0.5:
                amend(before(), current.name, _vary(attrs, r, (key,))[key])
            amend(before(recent=True), current.name, _vary(attrs, r, (key,))[key])
            if r.random() < 0.5:
                amend(after(), current.name, _vary(attrs, r, (key,))[key])
            if r.random() < 0.6:  # an amendment to another revision never applies
                amend(before(), revisions[0].name, _vary(attrs, r, (key,))[key])

    def members(most: int) -> list[Position]:
        if fixed.get("members_all") or n_pos < 2:
            return list(positions)
        return r.sample(positions, r.randint(2, most))

    def assign(kind: str, members: list[Position], key: str, text: str) -> None:
        for p in members:
            ask(kind, before(), {"position": p.number, "key": key}, f"Welche Regelung gilt für Position {p.number}?",
                text.format(p=p.number, k=key))

    group_keys: list[str] = [tag("KG", "KG-A1"), tag("KG", "KG-B2")] if f["groups"] and unassigned else []
    if f["groups"] and not unassigned:
        for i, p in enumerate(r.sample(positions, r.randint(1, min(3, n_pos)))):
            key = tag("KG", f"KG-{r.choice('ABCDEF')}{i + 1}")
            group_keys.append(key)
            if r.random() < 0.3:  # an earlier assignment, replaced by the later answer
                old = tag("KG", f"KG-{r.choice('UVW')}{i + 1}")
                group_keys.append(old)
                ask("group", before(), {"position": p.number, "key": old}, f"Welche Konditionen gelten für Position "
                    f"{p.number}?", f"Position {p.number} wird der Konditionsgruppe {old} zugeordnet.")
                ask("group", before(recent=True), {"position": p.number, "key": key},
                    f"Bleibt es bei der Konditionsgruppe für Position {p.number}?",
                    f"Position {p.number} wird nunmehr der Konditionsgruppe {key} zugeordnet.")
            else:
                assign("group", [p], key, "Position {p} wird der Konditionsgruppe {k} zugeordnet.")
    pack_keys = [f"VP-{i}" for i in (r.sample(range(1000, 9999), 3) if opaque else r.sample(range(1, 9), 2))]
    if f["packaging"] and not unassigned:
        for p in r.sample(positions, r.randint(1, n_pos)):
            assign("packaging", [p], r.choice(pack_keys), "Für Position {p} gilt der Verpackungstarif {k}.")
    commit_key, commit_pos = tag("TF", f"TF-{r.randint(1, 9)}"), r.choice(positions)
    if f["commitment"] != "none" and not unassigned:
        assign("commitment", [commit_pos], commit_key, "Für Position {p} gilt die Terminzusage {k}.")
    pool_key, pool_members = tag("RP", f"RP-{r.randint(1, 9)}"), members(min(3, n_pos))
    if f["pool"] != "none" and not unassigned:
        assign("pool", pool_members, pool_key, "Position {p} gehört zum Rüstpool {k}.")
    bonus_key, bonus_members = tag("PB", f"PB-{r.randint(1, 9)}"), members(n_pos)
    if f["bonus"] and not unassigned:
        assign("bonus", bonus_members, bonus_key, "Position {p} nimmt am Projektbonus {k} teil.")
    if f["setup"] and not unassigned:
        for p in r.sample(positions, min(n_pos, r.randint(1, 2))):
            fee = D(r.randint(80, 600)) + D(r.choice(["0.00", "0.50", "0.90"]))
            ask("setup", before(), {"position": p.number, "amount": fee},
                f"Fallen für Position {p.number} Einmalkosten an?",
                f"Für Position {p.number} wird eine einmalige Rüstpauschale von {de(fee)} EUR netto vereinbart.")
    for question, answer in r.sample([
            ("Sind Teillieferungen zulässig?", "Nein, Teillieferungen sind nicht vorgesehen."),
            ("Ist eine Verlängerung der Angebotsfrist geplant?", "Nein, die Angebotsfrist bleibt unverändert."),
            ("Welche Lackierung ist gefordert?", "Die Lackierung richtet sich nach der Spezifikation."),
            ("Dürfen Nebenangebote abgegeben werden?", "Nebenangebote werden nicht gewertet."),
            ("Ist ein Ortstermin vorgesehen?", "Ein Ortstermin findet nicht statt."),
            ("In welcher Sprache ist das Angebot abzugeben?", "Das Angebot ist in deutscher Sprache abzugeben.")], 4):
        ask("noise", before(), {}, question, answer)
    qa.sort(key=lambda q: q["date"])
    first_number = r.choice([1, 11, 101])
    numbers = r.sample(range(1000, 9999), len(qa)) if opaque else range(first_number, first_number + len(qa))
    for q, n in zip(qa, numbers):  # opaque numbers do not follow the dates
        q["number"] = n
    for q in qa:
        if q["kind"] == "withdrawal":
            q["data"] = {"withdraws": q["data"]["withdraws"]["number"]}
            q["answer"] = q["answer"].format(target=q["data"]["withdraws"])
    answers = [QA(q["number"], q["date"], q["kind"], q["data"], q["question"], q["answer"]) for q in qa]

    # ---- mailbox
    mails: list[Mail] = []
    people = {"kalkulation": "Kalkulation", "technik": "Technik / Konstruktion", "logistik": "Versandlogistik",
              "finanzen": "Finanzen / Treasury"}
    lookalike = r.choice([ref + "-A", ref + "/2", "N-" + ref])
    ref_digits = [i for i, c in enumerate(ref) if c.isdigit()]
    twin = ref[:ref_digits[-1]] + str((int(ref[ref_digits[-1]]) + 1) % 10) + ref[ref_digits[-1] + 1:]

    def mail(kind: str, when: datetime, body: str, data: dict | None = None, *, reference: str = ref,
             sender: str | None = None, name: str | None = None, record: bool = True, subject: str | None = None
             ) -> None:
        word = "Rabattfreigabe" if kind == "approval" else SUBJECTS[kind][0]
        local = "" if kind == "approval" else SUBJECTS[kind][1]
        mails.append(Mail(0, "", name or (f"{first} {last}" if kind == "approval" else people[local]),
                          sender or (director if kind == "approval" else f"{local}@{domain}"), when,
                          subject or (r.choice(["AW: ", "WG: ", "Re: ", "[extern] ", "Fwd: AW: "]) if fixed.get(
                              "subject_noise") and r.random() < 0.7 else "") + r.choice([
                                  f"{word} {reference}", f"{word} zu {reference}",
                                               f"{reference}: {word}"]),
                          f"Guten Tag,\n\nbetrifft Ausschreibung {reference}:\n\n{body}", kind, reference, record,
                          data or {}))

    def versions(kind: str, body, data) -> None:
        """The governing record plus, by chance, an older, a future, a look-alike and a wrong-sender version.

        `body(data)` writes the text; `data(shift)` returns the content for a version (shift 0 = governing)."""
        def send(shift: int, when: datetime, **more) -> None:
            content = data(shift)
            LOCALE[0] = r.choice(notations)  # each message states the notation its numbers use
            note = "Zahlenformat dieser Nachricht: " + NOTATIONS[LOCALE[0]]
            try:
                text = body(content)
            finally:
                LOCALE[0] = "de"
            mail(kind, when, f"{text}\n\n{note}", content, **more)

        send(0, before(recent=True))
        for _ in range(chain):  # a long line of superseded versions, and more that are not yet in force
            send(1, before())
        for _ in range(chain // 2):
            send(2, after())
        if r.random() < 0.6:
            send(1, before())
        if r.random() < 0.4:
            send(2, after())
        for _ in range(2 if flood else int(r.random() < 0.4)):
            send(3, before(recent=True), reference=r.choice([lookalike, twin]))
        for _ in range(2 if flood else int(r.random() < 0.3)):
            local = SUBJECTS[kind][1]
            wrong = r.choice([f"{local}@{domain.replace('.example', '-service.example')}",
                              f"{r.choice(['vertrieb', 'einkauf', 'info'])}@{domain}"])
            send(4, before(recent=True), sender=wrong)

    if f["usable"]:
        keys = sorted({q.reserve_key for q in reqs.values() if q.usable})

        def reserve_data(shift: int) -> dict:
            return {"entries": [{"revision": rev.name, "key": key, "reserve": D(r.choice(
                ["7.5", "10", "12.5", "15", "20", "22.5"]))} for rev in revisions for key in keys]}

        versions("reserve", lambda d: "Freigegebene Endreserven je Hubende:\n" + "\n".join(
            f"- Revision {e['revision']}, Endreserve-Schlüssel {e['key']}: {plain(e['reserve'])} mm je Ende"
            for e in d["entries"]) + "\n\nDer Nennhub ergibt sich aus dem nutzbaren Weg zuzüglich der Endreserve an "
                                     "beiden Enden.", reserve_data)

    world = World(seed, ref, now, company, f"DE{r.randint(100000000, 999999999)}", domain, mailbox,
                  f"{first} {last}", director, Tender("", ref, "", "", positions), [], answers,
                  f"Technische Spezifikation Hydraulikzylinder, Vergabe {ref}", revisions, [], mails, f)

    # ---- ERP materials: per requirement the exact match and near misses built from the traps
    used: set[str] = set()
    mat_scheme = 4 + r.randrange(3) if strange else r.randrange(4)
    index_codes = r.sample(["CU-7", "ST-2", "AL-4", "NI-9", "MS-58"], fixed.get("index_codes", 2))
    materials: list[Material] = []
    targets = {p.number: oracle.requirement(world, p) for p in positions}
    missing = r.choice(positions).requirement if f["no_bid"] else None
    done: set[str] = set()
    for p in positions:
        req = reqs[p.requirement]
        if req.rid in done:
            continue
        done.add(req.rid)
        target = targets[p.number]
        raw = {"bore": req.bore, "stroke": req.stroke, "pressure": req.pressure, "mounting": req.mounting,
               "certification": req.certification}
        variants = [(target, "inactive" if req.rid == missing else "active")]
        if req.rid == missing or r.random() < 0.4:
            variants.append((target, r.choice(["inactive", "blocked"])))
        if raw != target:
            variants.append((raw, "active"))  # the unamended requirement, or the usable travel without its reserve
        old = revisions[0].reqs[req.rid]
        variants.append(({"bore": old.bore, "stroke": old.stroke, "pressure": old.pressure,
                          "mounting": old.mounting, "certification": old.certification}, "active"))
        for _ in range(r.randint(2, 4)):
            variants.append((_vary(target, r), "active"))
        seen: set[tuple] = set()
        for attrs, status in variants:
            ident = (tuple(sorted((k, plain(v) if isinstance(v, D) else str(v)) for k, v in attrs.items())),
                     status == "active")
            if ident in seen or (status == "active" and attrs == target and req.rid == missing):
                continue
            seen.add(ident)
            materials.append(Material(
                _material_number(r, mat_scheme, used), req.group,
                f"Hydraulikzylinder D{attrs['bore']} H{plain(attrs['stroke'])} {attrs['pressure']} bar", attrs["bore"],
                D(attrs["stroke"]), attrs["pressure"], attrs["mounting"], attrs["certification"], status,
                lt_stock=r.randint(1, 3), lt_prod=r.randint(6, 14), carton=r.choice([4, 6, 8, 12, 15, 25]),
                alloc_mass=D(r.randint(1500, 28000)) / 1000,
                sur_mass=D(r.randint(800, 19000)) / 1000 if f["metal"] and (fixed.get("metal_all") or r.random() < 0.85) else D(0),
                index_code=r.choice(index_codes) if f["metal"] else ""))
    for i in range(n_req, n_req + r.randint(7, 10)):  # other catalogue groups, so a broad search is refused
        for _ in range(r.randint(4, 7)):
            attrs = {"bore": r.choice(BORES), "stroke": D(r.choice(range(100, 850, 50))),
                     "pressure": r.choice(PRESSURES), "mounting": r.choice(MOUNTINGS), "certification": r.choice(CERTS)}
            if any(attrs == t for t in targets.values()):
                continue
            materials.append(Material(
                _material_number(r, mat_scheme, used), group(i),
                f"Hydraulikzylinder D{attrs['bore']} H{plain(attrs['stroke'])} {attrs['pressure']} bar", attrs["bore"],
                attrs["stroke"], attrs["pressure"], attrs["mounting"], attrs["certification"],
                r.choice(["active", "active", "inactive"]), free_stock=r.randint(0, 400), lt_stock=r.randint(1, 3),
                lt_prod=r.randint(6, 14), carton=r.choice([4, 6, 8, 12]), alloc_mass=D(r.randint(1500, 28000)) / 1000))
    r.shuffle(materials)
    world.materials = materials

    chosen = {p.number: oracle.material(world, p) for p in positions}
    pieces = {p.number: oracle.pieces(world, p) for p in positions}
    demand: dict[str, int] = {}
    for p in positions:
        if chosen[p.number]:
            demand[chosen[p.number].number] = demand.get(chosen[p.number].number, 0) + pieces[p.number]

    # ---- stock, price conditions and FX
    fx_keys: dict[str, str] = {}
    for m in materials:
        need = demand.get(m.number, r.randint(20, 400))
        if edge == "exact" or (edge != "miss" and r.random() < 0.5):
            m.free_stock, m.blocked_stock = need + (0 if edge else r.choice([0, 0, 5, 40])), r.choice([0, 12, 60])
        else:  # free stock falls short; with the blocked stock it would cover
            m.free_stock = max(0, need - (1 if edge else r.randint(1, max(1, need // 2))))
            m.blocked_stock = need - m.free_stock + r.randint(0, 30)
        currency = "EUR"
        if f["fx"] and (fixed.get("fx_all") or r.random() < 0.4):
            currency = r.choice(["USD", "CHF", "GBP"] + (["SEK", "PLN", "JPY"] if fixed.get("fx_all") else []))
        unit = 1
        if f["unit100"] and (fixed.get("unit_all") or r.random() < 0.5):
            unit = r.choice([100, 1000]) if fixed.get("unit_all") else 100
        own = [pieces[p.number] for p in positions if chosen[p.number] is m] or [r.randint(20, 300)]
        cuts = {1, r.choice(own), max(own) + 1, sum(own) if len(own) > 1 else max(own) * 2}
        if fixed.get("scale_steps"):
            cuts |= {max(own) * k for k in (3, 5, 8)} | {max(1, min(own) // 2), max(1, min(own) - 1)}
        cuts = sorted(cuts)
        price = D(r.randint(7, 950) if fixed.get("scale") == "huge" else r.randint(6000, 95000)) / 100

        def condition(first_day: date, last_day: date, factor: D) -> Condition:
            key = f"FX-{currency}-{r.choice(['M', 'Q', 'T'])}{r.randint(1, 12):02d}" if currency != "EUR" else ""
            if key:
                fx_keys[key] = currency
            scales = [(cut, (price * factor * (D(100) - 4 * i) / 100 * unit).quantize(D("0.01")) + D(
                r.choice(["0.00", "0.03", "0.07"]))) for i, cut in enumerate(cuts)]
            return Condition(first_day, last_day, currency, key, unit, scales)

        today = now.date()
        opens = today - timedelta(days=r.randint(20, 200))
        closes = today + timedelta(days=0 if edge == "exact" else r.choice([0, 10, 90, 200]))
        m.conditions = [condition(opens, closes, D(1)),
                        condition(opens - timedelta(days=365), opens - timedelta(days=1), D("0.93")),
                        condition(closes + timedelta(days=1), closes + timedelta(days=365), D("1.06"))]
        for k in range(2, fixed.get("condition_chain", 0)):  # a long price history, none of it valid any more
            m.conditions.append(condition(opens - timedelta(days=365 * k), opens - timedelta(days=365 * (k - 1) + 1),
                                          D("0.93") ** k))
        r.shuffle(m.conditions)
    if fx_keys:
        versions("fx", lambda d: "Devisenfixing für die Kalkulation:\n" + "\n".join(
            f"- FX-Schlüssel {e['key']} ({e['currency']}): 1 {e['currency']} = {de(e['rate'], 4)} EUR"
            for e in d["entries"]), lambda shift: {"entries": [
                {"key": k, "currency": c, "rate": D(r.randint(*{"JPY": (55, 70), "SEK": (850, 950), "PLN": (2200, 2400)}.get(
                    c, (8200, 12900)))) / 10000} for k, c in sorted(fx_keys.items())]})

    # ---- commercial records
    if group_keys:
        scopes = {"base_only": "Der Auftragsrabatt wird ausschließlich auf den Grundpreis gewährt.",
                  "base_and_surcharge": "Der Auftragsrabatt wird auf den Grundpreis und auf den Massezuschlag "
                                        "(Metallzuschlag) gewährt.",
                  "none": "Für diese Gruppe wird kein Auftragsrabatt gewährt, weder auf den Grundpreis noch auf "
                          "Zuschläge."}
        versions("condition_rules", lambda d: "Konditionsregeln je Konditionsgruppe (Rüstkosten sind stets vom Rabatt "
                 "ausgenommen):\n" + "\n".join(f"- Konditionsgruppe {e['key']}: {scopes[e['scope']]}"
                                               for e in d["entries"]),
                 lambda shift: {"entries": [{"key": k, "scope": r.choice(list(scopes))} for k in sorted(
                     group_keys + [tag("KG", "KG-Z9"), tag("KG", "KG-Y8")])]})
    if f["metal"]:
        today = now.date()

        def index_data(shift: int) -> dict:
            rates = []
            for code in index_codes:
                if f["windows"]:
                    first_day = today - timedelta(days=r.randint(3, 20))
                    last_day = today + timedelta(days=0 if edge == "exact" else r.choice([0, 4, 15]))
                    more = (fixed.get("window_count", 3) - 1) // 2
                    spans = [(first_day - timedelta(days=30 * (j + 1)), first_day - timedelta(days=30 * j + 1))
                             for j in range(more)] + [(first_day, last_day)] + [
                        (last_day + timedelta(days=30 * j + 1), last_day + timedelta(days=30 * (j + 1)))
                        for j in range(more)]
                    r.shuffle(spans)
                else:
                    spans = [(None, None)]
                rates += [{"code": code, "from": a, "to": b, "rate": D(r.randint(180, 990)) / 100} for a, b in spans]
            return {"rates": rates}

        def index_body(d: dict) -> str:
            lines = [f"- Index {e['code']}: {de(e['rate'])} EUR/kg" + (
                f", gültig vom {e['from']:%d.%m.%Y} bis einschließlich {e['to']:%d.%m.%Y}" if e["from"] else "")
                     for e in d["rates"]]
            return "Preisindizes für den Massezuschlag (EUR je kg Zuschlagsmasse):\n" + "\n".join(lines)

        versions("metal", index_body, index_data)
    if f["packaging"]:
        versions("packaging", lambda d: "Verpackungstarife (je angefangenem Karton, netto):\n" + "\n".join(
            f"- Tarif {e['key']}: {de(e['price'])} EUR je Karton" for e in d["entries"]),
                 lambda shift: {"entries": [{"key": k, "price": D(r.randint(450, 3900)) / 100} for k in pack_keys]})
    if f["commitment"] != "none":
        m = chosen[commit_pos.number]
        if m is None:
            f["commitment"] = "none (no material)"
        else:
            total = demand[m.number]
            kind = f["commitment"]
            others = [x.number for x in materials if x.group == m.group and x.number != m.number]

            def commit_data(shift: int) -> dict:
                ceiling = (total + (0 if edge else r.choice([0, 0, 25, 100])) if kind != "ceiling"
                           else max(0, total - (1 if edge else r.randint(1, 10))))
                number = r.choice(others) if kind == "material" and others else m.number
                if shift:  # other versions say something else
                    ceiling, number = total + r.choice([-30, 300]), r.choice([m.number] + others)
                return {"entries": [{"key": commit_key, "material": number, "ceiling": ceiling,
                                     "weeks": r.choice([4, 5]) + (shift and 1)},
                                    {"key": tag("TF", "TF-0"), "material": r.choice([m.number] + others),
                                     "ceiling": total + r.choice([-3, 50]), "weeks": r.choice([2, 7])}]}

            if kind == "material" and not others:
                f["commitment"] = "applies"
            versions("commitment", lambda d: "Terminzusagen der Fertigung:\n" + "\n".join(
                f"- Zusage {e['key']}: Material {e['material']}, gültig bis einschließlich {e['ceiling']} Stück "
                f"Gesamtbedarf dieses Materials, Lieferzeit {e['weeks']} Wochen" for e in d["entries"]), commit_data)
    if f["pool"] != "none":
        rules = {"largest_remainder": "Jeder Anteil wird auf ganze Cent abgerundet; die verbleibenden Cent gehen "
                                      "einzeln an die Positionen mit den größten Nachkommaanteilen (bei Gleichstand "
                                      "aufsteigend nach Positionsnummer).",
                 "rest_to_largest": "Jeder Anteil wird kaufmännisch auf ganze Cent gerundet; eine verbleibende "
                                    "Differenz zur Poolpauschale trägt die Position mit dem größten Verteilgewicht."}

        def pool_data(shift: int) -> dict:
            rule = f["pool"] if not shift else r.choice(list(rules))
            return {"entries": [{"key": pool_key, "fee": D(r.randint(30000, 280000)) / 100 + D(
                r.choice(["0.00", "0.01", "0.37"])), "rule": rule},
                                {"key": tag("RP", "RP-0"), "fee": D(r.randint(20000, 90000)) / 100,
                                 "rule": r.choice(list(rules))}]}

        versions("pool", lambda d: "Gemeinsame Rüstkosten:\n" + "\n".join(
            f"- Rüstpool {e['key']}: Poolpauschale {de(e['fee'])} EUR netto für alle Poolpositionen zusammen. "
            f"Verteilung nach Stückzahl × Verteilmasse. Centregel: {rules[e['rule']]}" for e in d["entries"]),
                 pool_data)
    if f["bonus"]:
        eligible = sum((pieces[p.number] * oracle.base_price(world, p) for p in bonus_members
                        if chosen[p.number]), D(0)) or D(50000)

        def bonus_data(shift: int) -> dict:
            fractions = fixed.get("bonus_cuts") or [r.choice(["0.3", "0.45"]), r.choice(["0.7", "0.85"])]
            cuts = [(eligible * D(x)).quantize(D(100)) for x in fractions]
            if edge == "exact" and not shift:
                cuts[-1] = eligible  # the goods total sits exactly on a band edge
            if cuts != sorted(set(cuts)) or cuts[0] <= 0:
                cuts = [D(1000) * (i + 1) for i in range(len(cuts))]
            rates = sorted(r.sample(["0.5", "1", "1.5", "2", "2.5", "3", "4", "5"], len(cuts)), key=D)
            edges = [D(0), *cuts, None]
            return {"entries": [{"key": bonus_key, "bands": [
                {"lower": edges[i], "upper": edges[i + 1], "percent": D(rates[i - 1]) if i else D(0)}
                for i in range(len(cuts) + 1)]}]}

        def bonus_body(d: dict) -> str:
            out = ["Projektbonus (Staffel, der Satz gilt jeweils nur für den Anteil des Warenwerts im Band; Basis ist "
                   "der Warenwert ohne Zuschläge und vor Rabatt):"]
            for e in d["entries"]:
                out.append(f"Bonusplan {e['key']}:")
                for band in e["bands"]:
                    span = (f"über {de(band['lower'])} EUR" if band["upper"] is None else
                            f"von {de(band['lower'])} EUR bis {de(band['upper'])} EUR")
                    out.append(f"- {span}: {plain(band['percent'])} %")
            return "\n".join(out)

        versions("bonus", bonus_body, bonus_data)

    # ---- the director's approval, placed around the true order value
    value = oracle.order_value(world) if not any(v is None for v in chosen.values()) else D(r.randint(20000, 90000))
    kind = f["approval"]
    percent = D(r.choice(["0", "0.5", "12.75", "33.333", "17.5"] if fixed.get("odd_discount") else
                         ["2", "3", "4", "5", "6.5", "7.5"]))

    def approve(when: datetime, pct: D, minimum: D | None = None, **more) -> None:
        text = f"hiermit genehmige ich für die Ausschreibung {more.get('reference', ref)} einen Auftragsrabatt von " \
               f"{plain(pct)} %."
        if minimum is not None:
            text += (f" Die Freigabe gilt nur, wenn der Nettoauftragswert vor Rabatt (ohne Zuschläge und "
                     f"Rüstkosten) mindestens {de(minimum)} EUR beträgt.")
        mail("approval", when, text + f"\n\nMit freundlichen Grüßen\n{first} {last}\nVertriebsleitung",
             {"percent": pct, "minimum": minimum}, **more)

    if kind == "fakes_only":  # nothing from the director that is in force yet
        approve(after(), percent)
    elif kind != "none":
        minimum = {"plain": None, "cond_pass": value - (D(0) if edge == "exact" else r.choice([D(0), D("0.01"), D(1500)])),
                   "cond_fail": value + (D("0.01") if edge else r.choice([D("0.01"), D(900)]))}[kind]
        approve(before(recent=True), percent, minimum)
        approve(before(), percent + 2)  # superseded; never revived when the newer one's condition fails
        for _ in range(chain):
            approve(before(), percent + r.choice([1, 3, 5, 8]))
        if r.random() < 0.5:
            approve(after(), percent + 4)
    for _ in range(3 if flood else int(kind == "fakes_only" or r.random() < 0.7)):
        approve(before(recent=True), D(r.choice([9, 12, 15])), reference=r.choice([lookalike, twin]))
    if flood or kind == "fakes_only" or r.random() < 0.5:  # a colleague's word is not the director's approval
        mail("approval", before(recent=True), f"wie besprochen können wir bei {ref} mit 10 % Rabatt rechnen.",
             {"percent": D(10), "minimum": None}, sender=f"k.vogel@{domain}", name="Kim Vogel")
    mail("approval", before(), f"können Sie für die Ausschreibung {ref} bitte einen Auftragsrabatt freigeben? "
         "Wir rechnen mit starkem Wettbewerb.", {}, sender=mailbox, name="Vertrieb Innendienst", record=False,
         subject=f"Bitte um Rabattfreigabe {ref}")
    for _ in range(r.randint(40, 60) if flood else r.randint(10, 16)):
        topic = r.choice(["Newsletter Fluidtechnik", "Besprechungsprotokoll", "Auftragsbestätigung", "Reisekosten",
                          "Messeplanung", "Wartungsfenster ERP", "Lieferantenbewertung", "Urlaubsplanung"])
        mails.append(Mail(0, "", r.choice(["Info", "Sekretariat", "IT-Service", "Einkauf"]),
                          f"{r.choice(['info', 'office', 'it', 'einkauf'])}@{domain}", before() if stamps else now,
                          f"{topic} {r.choice([twin, lookalike, 'KW ' + str(r.randint(1, 52))])}",
                          "Zur Information. Keine Auswirkung auf laufende Angebote."))
    r.shuffle(mails)
    for m, mid in zip(mails, r.sample(range(1000, 9999), len(mails))):
        m.mid = mid
        m.folder = "archive" if (m.date < now - timedelta(days=14) or r.random() < (0.4 if tight else 0.15)) \
            else "inbox"

    # ---- the tender and its look-alike neighbours
    buyers = ["Stadtwerke Lindenau", "Hafenbetriebe Nordkai", "Zweckverband Wasser Oberland", "Landesbetrieb Bau"]
    world.tender = Tender("t" + str(r.randint(1000, 9999)), ref, "Lieferung von Hydraulikzylindern", r.choice(buyers),
                          positions)
    for other_ref in (lookalike, twin, _reference(r, now.year), _reference(r, now.year - 1)):
        world.decoys.append(Tender("t" + str(r.randint(1000, 9999)), other_ref, r.choice(
            ["Lieferung von Hydraulikzylindern", "Hydraulikaggregate", "Ersatzteile Hydraulik", "Wartung Pressen"]),
            r.choice(buyers), [Position(n, "Hydraulikzylinder", r.randint(10, 300), "Stück", "A-00")
                               for n in position_numbers[:2]]))
    if len({t.tid for t in [world.tender, *world.decoys]}) != len(world.decoys) + 1:
        raise Bad

    expected = oracle.solve(world)
    if (expected["decision"] == "no_bid") != f["no_bid"]:
        raise Bad
    return world
