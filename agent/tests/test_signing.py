"""Signing and its fallbacks in submit.py against fakes (scripted workers instead of the model, no browser)."""
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import submit  # noqa: E402
import worker  # noqa: E402
from submit import (BidFlow, FormReady, Mailbox, OfferTypeStep, PositionDesk, SigningTan,  # noqa: E402
                    SubmissionResult, submit_bid)
from tests.test_bid_flow import BRIEFING, FakeMailbox, FakeSession  # noqa: E402

NO_BID = {"decision": "no_bid", "discount_percent": 0, "positions": []}
DONE = SubmissionResult(submitted=True, confirmation="Bid received", omitted_positions=[])


class Script:
    """Stands in for run_worker: answers each call with the next scripted output and records the calls."""

    def __init__(self, *outputs):
        self.outputs, self.calls = list(outputs), []

    async def __call__(self, session, goal, output_type, **kwargs):
        self.calls.append(SimpleNamespace(goal=goal, type=output_type, **kwargs))
        output = self.outputs.pop(0) if self.outputs else None
        return output, {}


async def no_check(view, requirements):
    return None


class SigningNeverStopsAtAStepCap(unittest.IsolatedAsyncioTestCase):
    async def test_sign_passes_repeat_until_the_bid_is_in(self):
        script = Script(FormReady(at_signing_step=False, open_points="positions not done"), None, None, DONE)
        with mock.patch.object(submit, "run_worker", script), mock.patch.object(submit, "check_summary", no_check):
            result = await submit_bid(FakeSession(), FakeMailbox(["bids@example.com"]), BRIEFING,
                                      time.monotonic() + 900, None)
        self.assertTrue(result.submitted)
        self.assertEqual([c.type for c in script.calls], [FormReady] + [SubmissionResult] * 3)
        last = script.calls[-1]
        self.assertIn("omit a position", last.goal)  # an incomplete form is finished, not abandoned
        self.assertIn(submit.fill_in, last.tools)

    async def test_no_further_pass_without_time(self):
        script = Script(FormReady(at_signing_step=True, open_points=""), None, None, DONE)
        with mock.patch.object(submit, "run_worker", script), mock.patch.object(submit, "check_summary", no_check):
            result = await submit_bid(FakeSession(), FakeMailbox(["bids@example.com"]), BRIEFING,
                                      time.monotonic() + submit.LAST_CHANCE - 5, None)
        self.assertIsNone(result)
        self.assertEqual(len(script.calls), 2)

    async def test_a_revoked_model_token_ends_signing(self):
        calls = []

        async def revoked(session, goal, output_type, **kwargs):
            calls.append(output_type)
            if output_type is FormReady:
                return FormReady(at_signing_step=True, open_points=""), {}
            raise RuntimeError("status_code: 401, model_name: m, body: {'type': 'authentication_error'}")

        with mock.patch.object(submit, "run_worker", revoked), mock.patch.object(submit, "check_summary", no_check):
            await submit_bid(FakeSession(), FakeMailbox(["bids@example.com"]), BRIEFING, time.monotonic() + 900, None)
        self.assertEqual(calls, [FormReady, SubmissionResult])

    def test_reserve_grows_with_slow_model_calls(self):
        with mock.patch.object(worker, "PACE", []):
            self.assertEqual(submit.signing_reserve(1000), submit.SIGNING_RESERVE)  # no pace known yet
            worker.PACE.append((300.0, 10))  # 30 s per step
            self.assertEqual(submit.signing_reserve(1000), 300)
            self.assertEqual(submit.signing_reserve(400), 240)  # the form keeps 40 % of what is left
            self.assertEqual(submit.signing_reserve(200), submit.SIGNING_RESERVE)


class NoBidAfterThePreparedMainOffer(unittest.IsolatedAsyncioTestCase):
    def flow(self):
        flow = BidFlow(FakeSession(), FakeMailbox(["bids@example.com"]), BRIEFING, time.monotonic() + 1200)
        flow.ready = True  # the early pass chose the main offer and stopped at the positions
        return flow

    async def test_goes_back_to_the_offer_type_then_declares_and_signs(self):
        script = Script(OfferTypeStep(at_offer_type=True, step="offer type"),
                        FormReady(at_signing_step=True, open_points=""), DONE)
        with mock.patch.object(submit, "run_worker", script), mock.patch.object(submit, "check_summary", no_check):
            result = await self.flow().finish(NO_BID, None)
        self.assertTrue(result.submitted)
        rewind, form, sign = script.calls
        self.assertIs(rewind.type, OfferTypeStep)
        self.assertIn("Go back to the step where the offer type is chosen", rewind.goal)
        self.assertIn("choose the declaration not to bid", form.goal)
        self.assertIn("declaration that you do not submit an offer", form.goal)
        self.assertNotIn("An earlier pass already did the steps before the positions", form.goal)
        self.assertIn("request_signing_tan", sign.goal)

    async def test_a_failed_way_back_leaves_it_to_the_form_pass(self):
        script = Script(OfferTypeStep(at_offer_type=False, step="item 1.1"),
                        FormReady(at_signing_step=False, open_points=""), DONE)
        with mock.patch.object(submit, "run_worker", script), mock.patch.object(submit, "check_summary", no_check):
            result = await self.flow().finish(NO_BID, None)
        self.assertTrue(result.submitted)
        form = script.calls[1]
        self.assertIn("go back with the form's back button to the offer-type step", form.goal)

    async def test_a_bid_needs_no_way_back(self):
        script = Script(FormReady(at_signing_step=True, open_points=""), DONE)
        answer = {"decision": "bid", "discount_percent": 0, "positions": []}
        with mock.patch.object(submit, "run_worker", script), mock.patch.object(submit, "check_summary", no_check):
            await self.flow().finish(answer, None)
        self.assertIs(script.calls[0].type, FormReady)


class PositionPage(FakeSession):
    VIEW = '## Item 1.1\nQty [e3 textbox "Qty" value=""]\n[e4 select "Article" options: a | b]'

    async def look(self, fresh=False):
        return self.VIEW

    async def settle(self):
        pass


class HandOver(unittest.IsolatedAsyncioTestCase):
    def desk(self):
        desk = PositionDesk({"1.1": {"position": "1.1", "material_number": "M-1", "quantity": 2,
                                     "unit_price": 3.5, "lead_time_weeks": 4}}, deadline=time.monotonic() + 10**4)
        desk.remember({"Qty": "quantity", "Article": "material_number", "Price": "unit_price",
                       "Weeks": "lead_time_weeks"})
        return desk

    async def test_two_form_failures_hand_the_position_to_the_model(self):
        desk = self.desk()
        ctx = SimpleNamespace(deps=SimpleNamespace(session=PositionPage()))

        async def broken(session, entry, plan, log, choices=None):
            return "'Article' is disabled: an earlier choice was not confirmed"

        with mock.patch.object(submit, "fill_position", broken):
            first = await desk.configure_position(ctx, "1.1")
            second = await desk.configure_position(ctx, "1.1")
            third = await desk.configure_position(ctx, "1.1")
        self.assertIn("not fully entered", first)
        self.assertIn("enter it yourself now with fill_in()", second)
        self.assertIn("unit_price: 3,50", second)
        self.assertIn("enter it yourself now", third)
        self.assertNotIn("1.1", desk.entered)
        done = await desk.configure_position(ctx, "1.1", entered_yourself=True)
        self.assertIn("counted as entered", done)
        self.assertEqual(desk.total(), 7)

    async def test_a_value_the_form_does_not_offer_is_never_handed_over(self):
        desk = self.desk()
        ctx = SimpleNamespace(deps=SimpleNamespace(session=PositionPage()))

        async def no_option(session, entry, plan, log, choices=None):
            return f"'Article': no option means 'M-1'. Options: a | b. {submit.NUMBER_ONLY}"

        with mock.patch.object(submit, "fill_position", no_option):
            for _ in range(3):
                text = await desk.configure_position(ctx, "1.1")
        self.assertNotIn("yourself", text)
        self.assertIn("leave it out", (await desk.configure_position(ctx, "1.1")))
        refused = await desk.configure_position(ctx, "1.1", entered_yourself=True)
        self.assertIn("Only a position this tool handed to you", refused)


class MailSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.page = SimpleNamespace(url="https://mail.example.com/")

    async def revive(self):
        pass

    async def _goto(self, url):
        pass


class RequestAnswer(unittest.TestCase):
    def test_a_notice_with_a_link_keeps_its_words(self):
        line = 'Code 2 was sent to your mailbox ([e9 link "open the inbox" -> http://mail.example/]).'
        self.assertEqual(submit.WORDS_ONLY.sub(r"\1", line), "Code 2 was sent to your mailbox (open the inbox).")
        self.assertEqual(submit.WORDS_ONLY.sub(r"\1", '[e5 button "Request code"]'), '[e5 button "Request code"]')


class TanLookup(unittest.IsolatedAsyncioTestCase):
    def test_goal_describes_what_not_where(self):
        goal = submit.tan_goal("T-1", "", "")
        self.assertIn("T-1", goal)
        self.assertIn("search", goal)
        self.assertIn("never compare dates with today's clock", goal)
        self.assertNotIn("listed without a search", goal)

    async def test_second_try_thinks(self):
        script = Script(None, SigningTan(tan="000000", subject="Signing TAN", received="today"))
        mailbox = Mailbox(MailSession(), BRIEFING, time.monotonic() + 600)
        with mock.patch.object(submit, "run_worker", script):
            found = await mailbox.tan("Request 7 sent")
        self.assertEqual(found.tan, "000000")
        self.assertEqual([c.thinking for c in script.calls], [False, True])
        self.assertEqual({c.max_steps for c in script.calls}, {submit.TAN_STEPS})
        self.assertIn("Request 7 sent", script.calls[0].goal)


if __name__ == "__main__":
    unittest.main()
