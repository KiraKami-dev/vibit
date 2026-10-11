"""
The one correct answer of a synthetic scenario, a scorer, and a cross-check against the agent's calc.py.

`solve(world)` applies the 29 bid rules to the world's raw records, written here a second time and on purpose
without importing the agent's code, so `via_calc` (the same world through `agent/calc.py`) is a real comparison:
where the two disagree, one of them misreads a rule. `score` follows docs/scoring.md (bid and no_bid weights,
positions matched by number, extras cost one position's share). The two "when present" checks are not specified
in detail by the platform; here they are the quantity of the position behind a withdrawn answer and the
surcharge of positions whose index has validity windows.
"""
from __future__ import annotations

import math
import sys
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal as D
from pathlib import Path

from scenario import Material, Position, World

CENT = D("0.01")


def money(value) -> D:
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


def number_key(number: str) -> tuple:
    return tuple(int(part) if part.isdigit() else part for part in number.replace(".", " ").split())


# ---------------------------------------------------------------- which record governs


def authority(world: World, kind: str):
    """Rule 21: the latest record of this kind for the exact reference from its authorised sender, not later than
    the scenario date. A newer record replaces the older one whatever it says."""
    sender = world.senders()[kind].casefold()
    valid = [m for m in world.mails if m.kind == kind and m.is_record and m.reference == world.reference
             and m.sender.casefold() == sender and m.date <= world.scenario]
    return max(valid, key=lambda m: m.date) if valid else None


def entry(world: World, kind: str, **where):
    record = authority(world, kind)
    for e in (record.data.get("entries", []) if record else []):
        if all(e.get(k) == v for k, v in where.items()):
            return e
    return None


def qa_key(world: World, kind: str, position: Position):
    """The latest answer of this kind for the position, not later than the scenario date."""
    valid = [q for q in world.qa if q.kind == kind and q.data.get("position") == position.number
             and q.date <= world.scenario]
    return max(valid, key=lambda q: (q.date, q.number)).data if valid else None


def quantity(world: World, position: Position) -> int:
    """Rules 1 and 13: the latest change that is not withdrawn, else the tender quantity."""
    gone = {q.data["withdraws"] for q in world.qa if q.kind == "withdrawal" and q.date <= world.scenario}
    live = [q for q in world.qa if q.kind == "quantity" and q.data["position"] == position.number
            and q.number not in gone and q.date <= world.scenario]
    return max(live, key=lambda q: (q.date, q.number)).data["quantity"] if live else position.quantity


def pieces(world: World, position: Position) -> int:
    """Rule 17: sets times the governing revision's pieces per set."""
    n = quantity(world, position)
    if position.unit == "Satz":
        n *= world.current().reqs[position.requirement].pieces_per_set
    return n


def requirement(world: World, position: Position) -> dict:
    """Rules 2, 14, 20: the current revision's requirement, its scoped amendments, then the stroke reserve."""
    revision = world.current()
    req = revision.reqs[position.requirement]
    attrs = {"bore": req.bore, "stroke": D(req.stroke), "pressure": req.pressure, "mounting": req.mounting,
             "certification": req.certification}
    scoped = [q for q in world.qa if q.kind == "amendment" and q.data["revision"] == revision.name
              and q.data["requirement"] == req.rid and q.date <= world.scenario]
    for q in sorted(scoped, key=lambda q: (q.date, q.number)):
        value = q.data["value"]
        attrs[q.data["attribute"]] = D(value) if q.data["attribute"] == "stroke" else value
    if req.usable:
        reserve = entry(world, "reserve", revision=revision.name, key=req.reserve_key)
        if reserve is None:
            raise ValueError(f"no reserve for {revision.name}/{req.reserve_key}")
        attrs["stroke"] += 2 * D(reserve["reserve"])
    return attrs


def material(world: World, position: Position) -> Material | None:
    """Rules 3 and 10: the active material of the requirement's catalogue group that matches exactly."""
    want = requirement(world, position)
    group = world.current().reqs[position.requirement].group
    hits = [m for m in world.materials if m.group == group and m.status == "active" and m.attrs() == want]
    if len(hits) > 1:
        raise ValueError(f"position {position.number}: two matching materials")
    return hits[0] if hits else None


def base_price(world: World, position: Position) -> D:
    """Rules 4 and 22: the condition valid on the scenario day, the scale the position's own pieces reach, the
    price unit normalised, converted to EUR, HALF_UP to cents."""
    m = material(world, position)
    today = world.scenario.date()
    valid = [c for c in m.conditions if c.valid_from <= today <= c.valid_to]
    if len(valid) != 1:
        raise ValueError(f"{m.number}: {len(valid)} valid conditions")
    cond = valid[0]
    n = pieces(world, position)
    price = max((s for s in cond.scales if s[0] <= n), key=lambda s: s[0])[1]
    rate = D(1)
    if cond.currency != "EUR":
        rate = D(entry(world, "fx", key=cond.fx_key)["rate"])
    return money(price / cond.price_unit * rate)


def order_value(world: World) -> D:
    """Rule 16: final pieces times EUR base price over all positions, before discount, HALF_UP to cents."""
    return money(sum((pieces(world, p) * base_price(world, p) for p in world.tender.positions), D(0)))


def discount(world: World, value: D | None) -> D:
    """Rules 5 and 16: the latest approval; 0 when its inclusive minimum fails, never an older approval."""
    approval = authority(world, "approval")
    if approval is None:
        return D(0)
    minimum = approval.data.get("minimum")
    if minimum is not None and (value is None or value < D(minimum)):
        return D(0)
    return D(approval.data["percent"])


def split_cents(amount: D, weights: dict[str, D], rule: str = "largest_remainder") -> dict[str, D]:
    """An amount in whole cents over positions by weight: largest remainders, or round each and hand the
    difference to the largest weight. Ties go to the lower position number."""
    cents = int(money(amount) / CENT)
    total = sum(weights.values(), D(0))
    order = sorted(weights, key=number_key)
    exact = {k: D(cents) * weights[k] / total for k in order}
    if rule == "largest_remainder":
        share = {k: int(exact[k].to_integral_value(ROUND_FLOOR)) for k in order}
        for k in sorted(order, key=lambda k: exact[k] - share[k], reverse=True)[:cents - sum(share.values())]:
            share[k] += 1
    else:
        share = {k: int(exact[k].to_integral_value(ROUND_HALF_UP)) for k in order}
        share[max(order, key=lambda k: weights[k])] += cents - sum(share.values())
    return {k: D(v) * CENT for k, v in share.items()}


# ---------------------------------------------------------------- the answer


def solve(world: World, detail: bool = False) -> dict:
    positions = world.tender.positions
    chosen = {p.number: material(world, p) for p in positions}
    if any(m is None for m in chosen.values()):
        approval = authority(world, "approval")
        percent = D(approval.data["percent"]) if approval and approval.data.get("minimum") is None else D(0)
        return {"tender_reference": world.reference, "decision": "no_bid", "currency": "EUR",
                "discount_percent": float(percent), "positions": [], "total_net": 0.0}
    n = {p.number: pieces(world, p) for p in positions}
    base = {p.number: base_price(world, p) for p in positions}
    value = money(sum((n[k] * base[k] for k in n), D(0)))
    factor = 1 - discount(world, value) / 100
    demand: dict[str, int] = {}
    for p in positions:
        demand[chosen[p.number].number] = demand.get(chosen[p.number].number, 0) + n[p.number]

    today = world.scenario.date()
    surcharge = {}
    for p in positions:  # rule 28: the rate whose inclusive window holds the scenario day
        m = chosen[p.number]
        rate = D(0)
        if m.sur_mass:
            rates = [e for e in authority(world, "metal").data["rates"] if e["code"] == m.index_code
                     and (e["from"] is None or e["from"] <= today <= e["to"])]
            if len(rates) != 1:
                raise ValueError(f"{m.index_code}: {len(rates)} rates apply")
            rate = D(rates[0]["rate"])
        surcharge[p.number] = m.sur_mass * rate

    pool: dict[str, D] = {}
    members = [p for p in positions if qa_key(world, "pool", p)]
    if members:  # rule 25
        rule = entry(world, "pool", key=qa_key(world, "pool", members[0])["key"])
        pool = split_cents(rule["fee"], {p.number: n[p.number] * chosen[p.number].alloc_mass for p in members},
                           rule["rule"])
    bonus: dict[str, D] = {}
    members = [p for p in positions if qa_key(world, "bonus", p)]
    if members:  # rule 26
        plan = entry(world, "bonus", key=qa_key(world, "bonus", members[0])["key"])
        goods = {p.number: n[p.number] * base[p.number] for p in members}
        total = money(sum(goods.values(), D(0)))
        credit = D(0)
        for band in plan["bands"]:
            top = total if band["upper"] is None else min(total, band["upper"])
            if top > band["lower"]:
                credit += (top - band["lower"]) * band["percent"] / 100
        if money(credit) > 0:
            bonus = split_cents(money(credit), goods)

    out, notes = [], {}
    for p in positions:
        k, m, count = p.number, chosen[p.number], n[p.number]
        scope = "base_only"
        group = qa_key(world, "group", p)
        if group:  # rule 12
            scope = entry(world, "condition_rules", key=group["key"])["scope"]
        f_base = factor if scope != "none" else D(1)
        f_sur = factor if scope == "base_and_surcharge" else D(1)
        setup = D((qa_key(world, "setup", p) or {}).get("amount", 0)) + pool.get(k, D(0))
        cartons = D(0)
        tariff = qa_key(world, "packaging", p)
        if tariff:  # rule 23
            cartons = math.ceil(count / m.carton) * D(entry(world, "packaging", key=tariff["key"])["price"])
        credit = bonus.get(k, D(0))
        unit = base[k] * f_base + surcharge[k] * f_sur + (setup + cartons - credit) / count  # rule 27
        weeks = m.lt_stock if m.free_stock >= demand[m.number] else m.lt_prod  # rules 6, 15
        promise = qa_key(world, "commitment", p)
        if promise:  # rule 24
            e = entry(world, "commitment", key=promise["key"])
            if e and e["material"] == m.number and demand[m.number] <= e["ceiling"]:
                weeks = e["weeks"]
        out.append({"position": k, "material_number": m.number, "quantity": count, "unit_price": float(money(unit)),
                    "lead_time_weeks": weeks, "base_unit_price": float(base[k]),
                    "surcharge_per_unit": float(money(surcharge[k] + setup / count)),
                    "packaging_per_unit": float(money(cartons / count)), "rebate_total": float(money(credit))})
        notes[k] = {"scope": scope, "setup": str(setup), "demand": demand[m.number], "exact": str(unit)}
    total = money(sum((D(str(o["unit_price"])) * o["quantity"] for o in out), D(0)))
    answer = {"tender_reference": world.reference, "decision": "bid", "currency": "EUR",
              "discount_percent": float(discount(world, value)), "positions": out, "total_net": float(total)}
    if detail:
        answer["_detail"] = {"order_value": str(value), "positions": notes}
    return answer


# ---------------------------------------------------------------- scoring (docs/scoring.md)

POSITION_WEIGHTS = {"material_number": 20, "quantity": 15, "base_unit_price": 15, "surcharge_per_unit": 10,
                    "packaging_per_unit": 10, "rebate_total": 10, "unit_price": 10, "lead_time_weeks": 10}
MONEY_FIELDS = {"unit_price", "base_unit_price", "surcharge_per_unit", "packaging_per_unit", "rebate_total"}


def _same(field: str, got, want) -> bool:
    if got is None:
        return False
    if field in MONEY_FIELDS:
        return money(str(got)) == money(str(want))
    return str(got).strip() == str(want).strip()


def score(world: World, expected: dict, got: dict | None) -> dict:
    """Quality 0 to 100 with its parts; `got` is the signed bid as the answer schema writes it (None = none)."""
    if not got:
        return {"quality": 0.0, "eligible": False, "parts": {}, "positions": {}, "wrong": ["no signed bid"]}
    parts: dict[str, float] = {}
    wrong: list[str] = []

    def part(name: str, points: float, ok: bool, note: str = "") -> None:
        parts[name] = points if ok else 0.0
        if not ok:
            wrong.append(note or name)

    eligible = got.get("tender_reference") == expected["tender_reference"]
    rows = got.get("positions") or []
    total_ok = money(str(got.get("total_net", -1))) == money(str(expected["total_net"]))
    if expected["decision"] == "no_bid":
        part("decision", 75, got.get("decision") == "no_bid")
        part("reference", 5, eligible)
        part("no_positions", 5, not rows)
        part("total_zero", 15, total_ok)
        return {"quality": sum(parts.values()), "eligible": eligible, "parts": parts, "positions": {}, "wrong": wrong}

    features = world.features
    checks = {}
    if features.get("withdrawn_position"):
        checks["withdrawn_check"] = features["withdrawn_position"]
    windowed = [p["position"] for p in expected["positions"] if p["surcharge_per_unit"] and features.get("windows")
                and features.get("metal")]
    by_number: dict[str, dict] = {}
    extras = 0
    for row in rows:
        number = str(row.get("position"))
        if number in by_number or number not in {p["position"] for p in expected["positions"]}:
            extras += 1
        else:
            by_number[number] = row
    part("decision", 5, got.get("decision") == "bid")
    part("reference", 5, eligible)
    part("discount", 5, _same("discount_percent", float(got.get("discount_percent", -1)),
                              float(expected["discount_percent"])))
    summed = money(sum((D(str(r.get("unit_price", 0))) * int(r.get("quantity", 0)) for r in rows), D(0)))
    part("total", 5, total_ok and summed == money(str(got.get("total_net", -1))))
    part("arithmetic", 5, bool(rows) and summed == money(str(got.get("total_net", -1)))
         and len(by_number) == len(expected["positions"]) and not extras)
    if "withdrawn_check" in checks:
        row = by_number.get(checks["withdrawn_check"], {})
        want = next(p for p in expected["positions"] if p["position"] == checks["withdrawn_check"])
        part("withdrawn_check", 5, _same("quantity", row.get("quantity"), want["quantity"]))
    if windowed:
        part("metal_index_check", 5, all(_same("surcharge_per_unit", by_number.get(k, {}).get("surcharge_per_unit"),
                                               next(p for p in expected["positions"] if p["position"] == k)
                                               ["surcharge_per_unit"]) for k in windowed))
    pool = 75 - 5 * (("withdrawn_check" in parts) + ("metal_index_check" in parts))
    share = pool / len(expected["positions"])
    per_position: dict[str, dict] = {}
    earned = 0.0
    for want in expected["positions"]:
        row = by_number.get(want["position"])
        fields = {f: bool(row) and _same(f, row.get(f), want[f]) for f in POSITION_WEIGHTS}
        per_position[want["position"]] = fields
        earned += share * sum(w for f, w in POSITION_WEIGHTS.items() if fields[f]) / 100
        wrong += [f"position {want['position']}: {f}" for f, ok in fields.items() if not ok]
    parts["positions"] = max(0.0, earned - extras * share)
    if extras:
        wrong.append(f"{extras} extra or duplicate position(s)")
    return {"quality": round(sum(parts.values()), 2), "eligible": eligible, "parts": parts,
            "positions": per_position, "wrong": wrong}


# ---------------------------------------------------------------- the same world through agent/calc.py


def via_calc(world: World) -> dict:
    """The answer agent/calc.py gives for the world's records, selected with calc's own helpers."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent"))
    import calc

    now, ref = world.scenario, world.reference

    def latest(kind: str):
        local, _, domain = world.senders()[kind].partition("@")
        records = [calc.DatedRecord(reference=m.reference, sender=m.sender, date=m.date, key=str(i))
                   for i, m in enumerate(world.mails) if m.kind == kind and m.is_record]
        hit = calc.latest_authoritative(records, ref, now, local, domain)
        return world.mails[int(hit.key)] if hit else None

    def entries(kind: str) -> list[dict]:
        record = latest(kind)
        return record.data.get("entries", []) if record else []

    def key_of(kind: str, p) -> str | None:
        return (qa_key(world, kind, p) or {}).get("key")

    revision = world.current()
    changes = [calc.QuantityChange(number=str(q.number), date=q.date, position=q.data["position"],
                                   quantity=q.data["quantity"]) for q in world.qa if q.kind == "quantity"]
    withdrawals = [calc.Withdrawal(number=str(q.number), date=q.date, withdraws=str(q.data["withdraws"]))
                   for q in world.qa if q.kind == "withdrawal"]
    amendments = [calc.Amendment(date=q.date, revision=q.data["revision"], requirement=q.data["requirement"],
                                 attributes={q.data["attribute"]: q.data["value"]})
                  for q in world.qa if q.kind == "amendment"]
    reserves = {(e["revision"], e["key"]): e["reserve"] for e in entries("reserve")}
    scopes = {e["key"]: e["scope"] for e in entries("condition_rules")}
    tariffs = {e["key"]: e["price"] for e in entries("packaging")}
    promises = {e["key"]: e for e in entries("commitment")}
    inputs = []
    for p in world.tender.positions:
        req = revision.reqs[p.requirement]
        attrs = calc.apply_amendments({"bore": req.bore, "stroke": req.stroke, "pressure": req.pressure,
                                       "mounting": req.mounting, "certification": req.certification}, amendments,
                                      revision.name, req.rid, now)
        if req.usable:
            attrs["stroke"] = calc.nominal_stroke(D(str(attrs["stroke"])), reserves[(revision.name, req.reserve_key)])
        candidates = [(m.number, m.attrs()) for m in world.materials if m.group == req.group and m.status == "active"]
        hits = calc.matching_materials(attrs, candidates)
        m = next((x for x in world.materials if hits and x.number == hits[0]), None)
        promise = promises.get(key_of("commitment", p) or "")
        inputs.append(calc.PositionInput(
            position=p.number, quantity=calc.resolve_quantity(p.quantity, p.number, changes, withdrawals, now),
            unit=calc.parse_unit(p.unit), pieces_per_set=req.pieces_per_set,
            material=m and calc.Material(material_number=m.number, free_stock=m.free_stock,
                                         stock_lead_time_weeks=m.lt_stock, production_lead_time_weeks=m.lt_prod,
                                         pieces_per_carton=m.carton, allocation_mass=m.alloc_mass,
                                         surcharge_mass=m.sur_mass, metal_index=m.index_code or None),
            conditions=[calc.PriceCondition(valid_from=c.valid_from, valid_to=c.valid_to, currency=c.currency,
                                            fx_key=c.fx_key or None, price_unit=c.price_unit,
                                            scales=[calc.PriceScale(min_quantity=a, price=b) for a, b in c.scales])
                        for c in (m.conditions if m else [])],
            discount_scope=calc.DiscountScope(scopes[key_of("group", p)]) if key_of("group", p) else
            calc.DiscountScope.BASE_ONLY,
            setup_fee=D((qa_key(world, "setup", p) or {}).get("amount", 0)),
            packaging=calc.PackagingTariff(price_per_carton=tariffs[key_of("packaging", p)])
            if key_of("packaging", p) else None,
            commitment=promise and calc.DeliveryCommitment(material_number=promise["material"],
                                                           ceiling=promise["ceiling"],
                                                           lead_time_weeks=promise["weeks"])))
    approval = latest("approval")
    indices: dict[str, list] = {}
    record = latest("metal")
    for e in (record.data["rates"] if record else []):
        indices.setdefault(e["code"], []).append(calc.MetalIndexRate(rate=e["rate"], valid_from=e["from"],
                                                                     valid_to=e["to"]))
    pool_members = [p.number for p in world.tender.positions if key_of("pool", p)]
    pool_keys = {key_of("pool", p) for p in world.tender.positions} - {None}
    pool = next((e for e in entries("pool") if e["key"] in pool_keys), None)
    bonus_members = [p.number for p in world.tender.positions if key_of("bonus", p)]
    bonus_keys = {key_of("bonus", p) for p in world.tender.positions} - {None}
    plan = next((e for e in entries("bonus") if e["key"] in bonus_keys), None)
    bid = calc.BidInput(
        tender_reference=ref, scenario_date=now, positions=inputs,
        approval=approval and calc.Approval(percent=approval.data["percent"],
                                            min_order_value=approval.data.get("minimum")),
        fx_rates={e["key"]: e["rate"] for e in entries("fx")}, metal_indices=indices,
        setup_pool=calc.SetupPool(fee=pool["fee"], members=pool_members,
                                  allocation=calc.CentAllocation(pool["rule"])) if pool and pool_members else None,
        project_bonus=calc.ProjectBonus(bands=[calc.BonusBand(**b) for b in plan["bands"]], members=bonus_members)
        if plan and bonus_members else None)
    return calc.calculate(bid).answer()
