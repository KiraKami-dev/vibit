"""
Independent check of expected.json for the err404-layout eval: the bid rules applied by hand in Decimal.

Every input below is copied from world.js (invented data) and resolved as DERIVATION.md explains. It does not use
the agent's calc.py, so it can catch an agent bug instead of repeating it. Run: python3 derive.py
"""
import json
import math
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal as D
from pathlib import Path

CENT = D("0.01")


def money(x: D) -> D:
    return x.quantize(CENT, ROUND_HALF_UP)


# Devisenfixing FX-USD-11 (finanzen@, 2026-11-09): 1 EUR = 1.2800 USD
USD_PER_EUR = D("1.2800")
# Preisindizes (kalkulation@, 2026-11-03), window containing 2026-11-16: EUR per tonne -> EUR per kg
INDEX = {"MX-ST1": D("1310.00") / 1000, "MX-ST2": D("980.00") / 1000}
APPROVAL = (D("4.5"), D("56400.00"))  # leitung@ 2026-11-05: 4.5 % if undiscounted order value >= 56,400.00

# position -> final pieces, chosen material, valid scale price (EUR or USD per price unit), price unit, currency,
# surcharge kg, index, allocation kg, pieces per carton, tariff EUR per carton, discount scope, ordinary setup EUR
P = {
    "10": dict(n=18, mat="FT-6340-FA2", price=D("618.40"), unit=1, cur="EUR", ks=D("18.5"), ix="MX-ST1", ka=D(20),
               ct=4, tar=D("14.50"), scope="base+surcharge", setup=D(0)),
    "20": dict(n=12, mat="FT-6340-FA2", price=D("642.00"), unit=1, cur="EUR", ks=D("18.5"), ix="MX-ST1", ka=D(20),
               ct=4, tar=D("14.50"), scope="base", setup=D(0)),
    "30": dict(n=24, mat="FT-4015-C00", price=D("109.50"), unit=1, cur="EUR", ks=D("4.2"), ix="MX-ST2", ka=D(5),
               ct=10, tar=D("9.80"), scope="base", setup=D(0)),
    "40": dict(n=8, mat="FT-8050-TD5", price=D("1485.00"), unit=1, cur="EUR", ks=D("41"), ix="MX-ST1", ka=D(45),
               ct=2, tar=D("38.00"), scope="none", setup=D("360.00")),
    "50": dict(n=30, mat="FT-5030-E00", price=D("29443.00"), unit=100, cur="USD", ks=D("9.6"), ix="MX-ST2", ka=D(12),
               ct=6, tar=D("9.80"), scope="base", setup=D(0)),
    "60": dict(n=10, mat="FT-1040-FD5", price=D("2076.85"), unit=1, cur="USD", ks=D("62"), ix="MX-ST1", ka=D(70),
               ct=1, tar=D("38.00"), scope="base", setup=D("250.00")),
}
LEAD = {"10": 9, "20": 4, "30": 3, "40": 8, "50": 1, "60": 3}  # see DERIVATION.md, lead times
POOLS = [  # Ruestpools (kalkulation@, 2026-11-06)
    ("SP-NORTH", D("1234.57"), ["10", "20", "50"], "floor+largest-remainder"),
    ("SP-SOUTH", D("777.77"), ["30", "40", "60"], "half-up+rest-to-largest-weight"),
]
BONUS_MEMBERS = ["30", "50", "60"]  # Projektbonus PB-7 (finanzen@, 2026-11-04)
BANDS = [(D(0), D("10000.00"), D("0")), (D("10000.00"), D("20000.00"), D("1.5")), (D("20000.00"), None, D("2.5"))]


def base_eur(p: dict) -> D:
    per_piece = p["price"] / p["unit"]
    return money(per_piece if p["cur"] == "EUR" else per_piece / USD_PER_EUR)


def split(fee: D, weights: dict[str, D], rule: str) -> dict[str, D]:
    cents = int(fee / CENT)
    total = sum(weights.values())
    exact = {k: D(cents) * w / total for k, w in weights.items()}
    if rule == "floor+largest-remainder":
        share = {k: int(e.to_integral_value(ROUND_FLOOR)) for k, e in exact.items()}
        left = cents - sum(share.values())
        for k in sorted(weights, key=lambda k: (-(exact[k] - share[k]), int(k)))[:left]:
            share[k] += 1
    else:
        share = {k: int(e.to_integral_value(ROUND_HALF_UP)) for k, e in exact.items()}
        share[max(weights, key=lambda k: weights[k])] += cents - sum(share.values())
    return {k: D(v) * CENT for k, v in share.items()}


def main() -> None:
    base = {k: base_eur(p) for k, p in P.items()}
    order_value = money(sum(P[k]["n"] * base[k] for k in P))
    discount = APPROVAL[0] if order_value >= APPROVAL[1] else D(0)
    factor = 1 - discount / 100
    pool: dict[str, D] = {}
    for _, fee, members, rule in POOLS:
        pool.update(split(fee, {k: P[k]["n"] * P[k]["ka"] for k in members}, rule))
    goods = {k: P[k]["n"] * base[k] for k in BONUS_MEMBERS}
    eligible = money(sum(goods.values()))
    credit = D(0)
    for lo, hi, pct in BANDS:
        top = eligible if hi is None else min(eligible, hi)
        if top > lo:
            credit += (top - lo) * pct / 100
    credit = money(credit)
    bonus = split(credit, goods, "floor+largest-remainder")
    lines = []
    for k, p in P.items():
        n = p["n"]
        surcharge = p["ks"] * INDEX[p["ix"]]
        f_base = 1 if p["scope"] == "none" else factor
        f_sur = factor if p["scope"] == "base+surcharge" else 1
        cartons = math.ceil(n / p["ct"]) * p["tar"]
        setup = p["setup"] + pool.get(k, D(0))
        credit_k = bonus.get(k, D(0))
        exact = base[k] * f_base + surcharge * f_sur + (setup + cartons - credit_k) / n
        lines.append({"position": k, "material_number": p["mat"], "quantity": n, "unit_price": float(money(exact)),
                      "lead_time_weeks": LEAD[k], "base_unit_price": float(base[k]),
                      "surcharge_per_unit": float(money(surcharge + setup / n)),
                      "packaging_per_unit": float(money(cartons / n)), "rebate_total": float(money(credit_k))})
        print(f"{k}: base {base[k]} surcharge/kg-rate {surcharge} setup {setup} cartons {cartons} bonus {credit_k} "
              f"exact {exact} -> {money(exact)}")
    total = money(sum(D(line["quantity"]) * D(str(line["unit_price"])) for line in lines))
    print(f"order value {order_value} -> discount {discount}; bonus eligible {eligible} credit {credit}; "
          f"pools {pool}; bonus {bonus}; total {total}")
    answer = {"tender_reference": "TR-2026-0815", "decision": "bid", "currency": "EUR",
              "discount_percent": float(discount), "positions": lines, "total_net": float(total)}
    expected = json.loads((Path(__file__).parent / "expected.json").read_text())
    print("expected.json matches" if expected == answer else "expected.json DIFFERS:\n" + json.dumps(answer, indent=2))


if __name__ == "__main__":
    main()
