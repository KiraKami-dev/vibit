"""
configure_position goes on with the next position pages by the mapping the model gave, while each page shows one
bid position with the same controls; any other page goes back to the model. An invented one-page wizard.
"""
import asyncio
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import submit  # noqa: E402
from submit import PositionDesk  # noqa: E402
from tests.test_site_tools import _Browser  # noqa: E402

WIZARD = """
<main id="main"></main>
<script>
const order = %s;
const extra = %s;
let at = 0;
const saved = {};
function render() {
  const main = document.getElementById('main');
  if (at >= order.length) { main.innerHTML = '<h1>Konditionen</h1><p>' + JSON.stringify(saved) + '</p>'; return; }
  const p = order[at];
  main.innerHTML = `<h1>Position ${p}</h1>
    <label>Material <select id="m"><option value="">-</option><option>M-1</option><option>M-2</option></select></label>
    <label>Menge <input id="q"></label>
    <label>Preis <input id="pr"></label>
    <label>Lieferzeit <select id="w"><option value="">-</option><option>2</option><option>4</option></select></label>
    ${extra.includes(p) ? '<label>Zertifikat <input id="c"></label>' : ''}
    <button id="next">Weiter</button> <button>Position auslassen</button>`;
  document.getElementById('next').onclick = () => {
    saved[p] = [m.value, q.value, pr.value, w.value].join('/');
    at += 1; render();
  };
}
render();
</script>
"""

ENTRIES = {p: {"position": p, "material_number": m, "quantity": q, "unit_price": price, "lead_time_weeks": w}
           for p, m, q, price, w in (("01.01", "M-1", 5, 10.5, 2), ("01.02", "M-2", 3, 7.25, 4),
                                     ("01.03", "M-1", 1, 99, 4))}


def run_wizard(order: list[str], extra: list[str] = (), auto: bool = True):
    async def go():
        async with _Browser() as b:
            session = await b.session(WIZARD % (order, list(extra)))
            desk = PositionDesk(ENTRIES, deadline=time.monotonic() + 600)
            ctx = SimpleNamespace(deps=SimpleNamespace(session=session))
            submit.AUTO_POSITIONS = auto
            try:
                text = await desk.configure_position(
                    ctx, order[0], fields={"Material": "material_number", "Menge": "quantity", "Preis": "unit_price",
                                           "Lieferzeit": "lead_time_weeks"}, next_button="Weiter")
            finally:
                submit.AUTO_POSITIONS = True
            return text, desk, await session.page.inner_text("main")
    return asyncio.run(go())


class AutoPositions(unittest.TestCase):
    def test_same_pages_are_entered_in_one_call(self):
        text, desk, page = run_wizard(["01.02", "01.01", "01.03"])
        self.assertEqual(desk.entered, {"01.01", "01.02", "01.03"})
        self.assertIn("Konditionen", page)
        self.assertIn('"01.01":"M-1/5/10,50/2"', page)
        self.assertIn("Entered with the same mapping as well: 01.01, 01.03", text)

    def test_a_page_with_another_control_goes_back_to_the_model(self):
        text, desk, page = run_wizard(["01.01", "01.02", "01.03"], extra=["01.02"])
        self.assertEqual(desk.entered, {"01.01"})
        self.assertIn("Position 01.02", page)
        self.assertNotIn("as well", text)

    def test_off_switch_enters_one_position_per_call(self):
        text, desk, page = run_wizard(["01.01", "01.02"], auto=False)
        self.assertEqual(desk.entered, {"01.01"})
        self.assertIn("Position 01.02", page)


class _Trace:
    def __init__(self):
        self.events = []

    def event(self, worker, kind, quiet=False, **data):
        self.events.append((kind, data))


class _Page:
    """A form page without a browser: look() shows `view`."""

    def __init__(self, view: str):
        self.view, self.name, self.trace, self.restarts = view, "bid", _Trace(), 0
        self.lock = asyncio.Lock()

    async def look(self, *args, **kwargs):
        return self.view

    async def settle(self, *args, **kwargs):
        return None


def refused_calls(view: str, calls: int):
    desk = PositionDesk(ENTRIES, deadline=time.monotonic() + 600)
    page = _Page(view)
    ctx = SimpleNamespace(deps=SimpleNamespace(session=page))
    fields = {"Material": "material_number", "Menge": "quantity"}

    async def go():
        out = []
        for _ in range(calls):
            try:
                out.append(await desk.configure_position(ctx, "01.02", fields=fields, next_button="Weiter"))
            except submit.UnexpectedModelBehavior as error:
                out.append(error)
        return out

    return asyncio.run(go()), page


class Refusals(unittest.TestCase):
    OVERVIEW = "# Angebot\nSchritt 3 von 8: Konditionen\n[e4 button \"Weiter\"]\n"

    def test_a_page_without_positions_says_so_and_points_to_the_list(self):
        texts, _ = refused_calls(self.OVERVIEW, 1)
        self.assertIn("shows none of the bid's positions", texts[0])
        self.assertIn("position list or overview", texts[0])

    def test_the_refusal_names_the_positions_the_page_shows(self):
        text = submit.not_on_page("01.02", ["01.03"], "01.02, 01.03")
        self.assertIn("the page shows position(s) 01.03", text)

    def test_three_refusals_in_a_row_send_the_model_away_and_six_end_the_worker(self):
        texts, page = refused_calls(self.OVERVIEW, 7)
        self.assertNotIn("in a row", texts[1])
        self.assertIn("do not call configure_position again on this page", texts[2])
        self.assertIsInstance(texts[5], submit.UnexpectedModelBehavior)
        self.assertIsInstance(texts[6], str)  # the count starts again for the next pass
        self.assertEqual([d["reason"] for k, d in page.trace.events if k == "configure_refused"][5], "stop")


class ReviewCheck(unittest.TestCase):
    def test_a_position_missing_on_the_review_is_named(self):
        review = "# Prüfen\n| 01.01 | M-1 | 5 |\n| 01.02 | M-2 | 3 |\nSumme 74,25\n"
        self.assertEqual(submit.missing_on_review(review, ENTRIES), ["01.03"])
        self.assertEqual(submit.missing_on_review(review + "| 01.03 | M-1 | 1 |\n", ENTRIES), [])

    def test_a_review_that_lists_no_position_is_no_evidence(self):
        self.assertEqual(submit.missing_on_review("# Prüfen\nSumme 74,25\n", ENTRIES), [])

    def test_a_longer_number_is_not_the_position(self):
        review = "| 01.01 | 01.02 | 101.03 | 01.031 |\n"
        self.assertEqual(submit.missing_on_review(review, ENTRIES), ["01.03"])


if __name__ == "__main__":
    unittest.main()
