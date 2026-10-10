"""
v2 research check: read the brief, then the specification worker and the ERP workers, and print their records.

Run from the repo root with the scouting logins and the models proxy:
    eval "$(atira scout --env)"; HEADLESS=1 uv run --project agent python dev/research_check.py \
        [--reserve KEY=MM ...] [--only ID ...]

Usable-travel reserves come from the Technikfreigabe mail (another worker); pass them with --reserve to see the final
selection, otherwise those strokes stay pending and the ERP stage inspects every hit that matches the rest.
"""
import argparse
import asyncio
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

from playwright.async_api import async_playwright  # noqa: E402

import browsers  # noqa: E402
import calc  # noqa: E402
import run as contract  # noqa: E402
from briefing import read_brief  # noqa: E402
from browser_tools import SiteSession  # noqa: E402
from evidence import effective_requirement, parse_date  # noqa: E402
from research import Erp, research_sites, research_spec, reselect  # noqa: E402
from tracing import Trace  # noqa: E402


def _drop_raw(value):
    if isinstance(value, dict):
        return {k: _drop_raw(v) for k, v in value.items() if k != "raw"}
    if isinstance(value, list):
        return [_drop_raw(v) for v in value]
    return value


def dump(model) -> str:
    return json.dumps(_drop_raw(json.loads(model.model_dump_json())), ensure_ascii=False)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reserve", action="append", default=[], help="KEY=per-end reserve in mm")
    parser.add_argument("--only", action="append", default=[], help="only these requirement ids in the ERP stage")
    args = parser.parse_args()
    reserves = {k: Decimal(v) for k, v in (r.split("=", 1) for r in args.reserve)}

    task, _ = contract.load_run()
    trace = Trace()
    t0 = time.monotonic()
    deadline = t0 + 15 * 60
    context = task["brief"].split("## Rules")[0].strip()
    briefing, sites = await asyncio.gather(read_brief(task), research_sites(task))
    t_brief = time.monotonic() - t0
    print(f"\nbrief read in {t_brief:.1f}s: tender {briefing.tender_reference}, spec site {sites.spec_site}, "
          f"ERP site {sites.erp_site}", flush=True)
    access = {s["siteId"]: s for s in task["access"]}
    async with async_playwright() as playwright:
        spec_ctx, erp_ctx = await browsers.contexts(playwright, 2)
        spec_session = SiteSession(access[sites.spec_site], spec_ctx, trace)
        erp = Erp(access[sites.erp_site], erp_ctx, trace, deadline)
        erp.warm_up()  # signs in to the ERP while the specification is read
        await spec_session.start()
        scenario = parse_date(briefing.scenario_date)
        effective = {}

        def on_requirement(req):  # ERP research starts as soon as the specification yields a requirement
            if args.only and req.requirement_id not in args.only:
                return
            effective[req.requirement_id] = effective_requirement(req, amendments=[], reserves=reserves,
                                                                  scenario_date=scenario)
            print(f"[{time.monotonic() - t0:7.1f}s] requirement {req.requirement_id} -> ERP", flush=True)
            erp.start(effective[req.requirement_id])

        spec = await research_spec(spec_session, briefing, context=context, deadline=deadline,
                                   on_requirement=on_requirement)
        t_spec = time.monotonic() - t0
        print(f"\n===== specification ({spec.seconds}s, at {t_spec:.1f}s)\n{dump(spec)}", flush=True)
        wanted = [r for r in spec.requirements if not args.only or r.requirement_id in args.only]
        for req in wanted:  # anything a page reported only after a disagreement check
            if req.requirement_id not in effective:
                on_requirement(req)
        result = await erp.collect()
        t_erp = time.monotonic() - t0
    print(f"\n===== ERP ({result.seconds}s, at {t_erp:.1f}s)")
    for research in result.requirements:
        print(dump(research))
    for number, record in sorted(result.materials.items()):
        print(f"--- material {number}\n{dump(record)}")

    print("\n===== mapped to calc inputs")
    chosen = await reselect(list(effective.values()), result, trace)
    for req in wanted:
        number = chosen.get(req.requirement_id)
        record = result.materials.get(number) if number else None
        line = f"{req.requirement_id}: group {req.catalog_group} -> {number or 'NO MATERIAL (pending or none)'}"
        if record:
            try:
                material = record.to_material()
                cond = calc.valid_condition(record.conditions, scenario)
                line += (f"; stock {material.free_stock}, lead {material.stock_lead_time_weeks}/"
                         f"{material.production_lead_time_weeks} w, carton {material.pieces_per_carton}, masses "
                         f"{material.allocation_mass}/{material.surcharge_mass} kg, index {material.metal_index}; "
                         f"valid condition {cond.valid_from}..{cond.valid_to} {cond.currency} {cond.fx_key or ''} "
                         f"per {cond.price_unit}: " + ", ".join(f"{s.min_quantity}+ {s.price}" for s in cond.scales))
            except calc.CalcError as error:
                line += f"; calc error: {error}"
        print(line)
    print(f"\ntimings: brief {t_brief:.1f}s, spec done at {t_spec:.1f}s, ERP done at {t_erp:.1f}s "
          f"(ERP stage {result.seconds}s)\ntrace: {trace.path}")


if __name__ == "__main__":
    asyncio.run(main())
