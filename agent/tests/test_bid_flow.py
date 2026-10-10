"""The bid form overlapped with the research (submit.BidFlow) and its prompts, against fakes: no browser, no model."""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import submit  # noqa: E402
from briefing import Briefing  # noqa: E402
from submit import BidFlow, PositionDesk, _positions  # noqa: E402

BRIEFING = Briefing(tender_reference="T-1", company_name="Example GmbH", vat_id="DE000000000",
                    scenario_date="2030-01-01", bid_site="bids", mail_site="mail", submission_steps="Fill the form.")
SIGNED_IN = "<html><body><h1>Tenders</h1><form><input type='search'></form></body></html>"
SIGNED_OUT = "<html><body><form><input name=u><input type=\"password\" name=p></form></body></html>"


class FakeTrace:
    def __init__(self):
        self.events = []

    def event(self, worker, kind, quiet=False, **data):
        self.events.append((worker, kind, data))


class FakePage:
    """The tab: evaluate() answers the keepalive fetch with [url, html]."""

    def __init__(self, bodies):
        self.bodies, self.calls = list(bodies), 0

    async def evaluate(self, script):
        self.calls += 1
        return ["https://bids.example.com/", self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]]


class FakeSession:
    def __init__(self, bodies=(SIGNED_IN,)):
        self.trace = FakeTrace()
        self.page = FakePage(bodies)
        self.site = {"siteId": "bids", "url": "https://bids.example.com/"}
        self.name = "bids"
        self.lock = asyncio.Lock()
        self.seen = self.view = ""


class FakeMailbox:
    def __init__(self, addresses):
        self.addresses, self.calls = list(addresses), 0

    async def address(self):
        self.calls += 1
        return self.addresses.pop(0) if self.addresses else ""


class KeepAlive(unittest.IsolatedAsyncioTestCase):
    async def test_returns_when_signed_out(self):
        session = FakeSession([SIGNED_IN, SIGNED_IN, SIGNED_OUT])
        flow = BidFlow(session, FakeMailbox([]), BRIEFING, deadline=10**9)
        flow.ready = True
        with mock.patch.object(submit, "KEEPALIVE_SECONDS", 0.01):
            await asyncio.wait_for(flow._keep_alive(), 2)
        self.assertEqual(session.page.calls, 3)
        self.assertFalse(flow.ready)
        self.assertIn("session_lost", [kind for _, kind, _ in session.trace.events])

    async def test_signed_in_page_keeps_going(self):
        session = FakeSession([SIGNED_IN])
        flow = BidFlow(session, FakeMailbox([]), BRIEFING, deadline=10**9)
        with mock.patch.object(submit, "KEEPALIVE_SECONDS", 0.01):
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(flow._keep_alive(), 0.2)
        self.assertGreater(session.page.calls, 3)


class Handover(unittest.IsolatedAsyncioTestCase):
    async def test_finish_waits_for_a_pass_then_hands_the_form_over(self):
        flow = BidFlow(FakeSession(), FakeMailbox(["user@example.com"]), BRIEFING, deadline=10**9)

        async def prepare_then_keep_alive():
            flow.preparing = True
            await asyncio.sleep(0.2)
            flow.preparing, flow.ready = False, True
            await asyncio.sleep(3600)

        flow.task = asyncio.create_task(prepare_then_keep_alive())
        await asyncio.sleep(0.01)
        submitted = mock.AsyncMock(return_value="signed")
        with mock.patch.object(submit, "submit_bid", submitted):
            self.assertEqual(await flow.finish({"decision": "bid"}, {}), "signed")
        self.assertTrue(flow.task.cancelled())
        self.assertIs(submitted.call_args.kwargs["flow"], flow)
        self.assertTrue(flow.ready)

    async def test_address_looked_up_again_when_the_first_found_nothing(self):
        mailbox = FakeMailbox(["", "user@example.com"])
        flow = BidFlow(FakeSession(), mailbox, BRIEFING, deadline=10**9)
        with mock.patch.dict("os.environ", {"AGENT_FORM_OVERLAP": "0"}):
            flow.start()
        self.assertIsNone(flow.task)
        self.assertEqual(await flow.address, "")
        self.assertEqual(await flow.contact_address(), "user@example.com")
        self.assertEqual(await flow.contact_address(), "user@example.com")  # kept once found
        self.assertEqual(mailbox.calls, 2)


class Prompts(unittest.TestCase):
    ANSWER = {"decision": "bid", "discount_percent": 0, "positions": []}

    def desk(self):
        return PositionDesk({"1.1": {"position": "1.1", "material_number": "M-1", "quantity": 2, "unit_price": 3.5,
                                     "lead_time_weeks": 4}}, deadline=10**9)

    def test_mapped_form_needs_no_mapping_again(self):
        desk = self.desk()
        self.assertIn("map each control", _positions(self.ANSWER, desk, filled=False))
        desk.remember({"Catalogue no.": "material_number"}, "Apply choice", "Next")
        text = _positions(self.ANSWER, desk, filled=False)
        self.assertIn("already knows", text)
        self.assertNotIn("map each control", text)

    def test_no_bid_after_a_prepared_main_offer_goes_back(self):
        answer = {**self.ANSWER, "decision": "no_bid"}
        self.assertIn("go back", _positions(answer, PositionDesk({}, 0), filled=False, prepared=True))
        self.assertNotIn("go back", _positions(answer, PositionDesk({}, 0), filled=False))
        self.assertNotIn("go back", _positions(answer, PositionDesk({}, 0), filled=True, prepared=True))

    def test_a_named_option_carries_to_the_same_value_on_later_positions(self):
        desk = self.desk()
        desk.remember({"Finish": "certification", "Bore": "bore"})
        first = {"bore": "10", "certification": "None"}
        self.assertEqual(desk.picks("1.1", first, {"Finish": "Without finish"}), {"Finish": "Without finish"})
        self.assertEqual(desk.picks("1.2", {"bore": "20", "certification": "none"}, None),
                         {"Finish": "Without finish"})
        self.assertEqual(desk.picks("1.3", {"bore": "10", "certification": "Coated"}, None), {})
        self.assertEqual(desk.picks("1.1", first, None), {"Finish": "Without finish"})  # a revisit

    def test_remember_drops_unknown_names_and_keeps_earlier_ones(self):
        desk = self.desk()
        self.assertIn("Unknown value names", desk.remember({"Colour": "colour"}))  # nothing usable at all
        self.assertEqual(desk.remember({"Bore": "bore", "Qty": "quantity", "Colour": "colour"}), "")
        self.assertEqual(desk.remember({"Shade": "colour"}), "")  # the mapping so far still counts
        desk.remember({"Quantity": "quantity"})
        self.assertEqual(desk.plan.fields, {"Bore": "bore", "Quantity": "quantity"})


if __name__ == "__main__":
    unittest.main()
