"""Fixes after the overfitting review: the brief check, revision labels, numbers in form fields, pictures on a page,
unread materials before a no bid, mailboxes without message links, and a form the mapping does not fit."""
import asyncio
import base64
import tempfile
import time
import unittest
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import patch

import configurator
import extract
import orchestrator
import research
import research_mail
import submit
from browser_tools import unseen_values
from playwright.async_api import async_playwright
from tracing import Trace


class BriefValues(unittest.TestCase):
    def test_a_numbered_list_copied_word_for_word_is_in_the_brief(self):
        brief = "Company: Muster GmbH\nSteps:\n1. Open the bid form.\n2. Enter the bidder data.\n3. Sign.\n"
        values = {"company_name": "Muster GmbH", "submission_steps": "1. Open the bid form.\n2. Enter the bidder data.\n3. Sign."}
        self.assertEqual(unseen_values("\n".join(str(v) for v in values.values()), brief), [])


class RevisionLabels(unittest.TestCase):
    def test_whole_tokens_decide(self):
        same = research.same_revision
        self.assertTrue(same("R-2", "Revision R-2"))
        self.assertTrue(same("Rev. C", "C"))
        self.assertTrue(same("R-77", "R-77 (current)"))
        self.assertFalse(same("E", "Revision D"))  # not: the letter e is in the word
        self.assertFalse(same("R-1", "R-10"))
        self.assertFalse(same("v1", "v1.1"))
        self.assertFalse(same("Fassung 3", "Fassung 4"))
        self.assertFalse(same("", "R-2"))


class Field:
    """A form field: `takes` says what it accepts and how it then shows the value."""

    def __init__(self, takes):
        self.takes, self.value = takes, ""

    async def fill(self, text, timeout=0):
        self.value = self.takes(text)

    async def evaluate(self, script):
        return self.value


def number_field(text):  # like input[type=number]: no comma, no thousands separator
    if "," in text:
        raise ValueError("Cannot type text into input[type=number]")
    return text


class NumbersInFormFields(unittest.TestCase):
    def type(self, takes, value="1234,50", numeric=True):
        field = Field(takes)
        return asyncio.run(configurator.type_value(field, value, numeric)), field.value

    def test_a_field_that_takes_the_comma_keeps_it(self):
        self.assertEqual(self.type(lambda t: t), (None, "1234,50"))

    def test_a_number_field_gets_the_decimal_point(self):
        self.assertEqual(self.type(number_field), (None, "1234.50"))

    def test_a_field_that_shows_the_number_its_own_way_holds_it(self):
        self.assertEqual(self.type(lambda t: "1.234,50"), (None, "1.234,50"))
        self.assertEqual(self.type(lambda t: "1234.5" if "." in t else "")[0], None)

    def test_another_number_or_nothing_is_reported(self):
        problem, _ = self.type(lambda t: "12")
        self.assertIn("did not take", problem)
        self.assertIn("did not take", self.type(lambda t: "", value="Text", numeric=False)[0])


def text_png(page_html: str) -> bytes:
    async def shot():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": 500, "height": 200})
            await page.set_content(page_html)
            png = await page.locator("#a").screenshot()
            await browser.close()
            return png
    return asyncio.run(shot())


class PicturesOnAPage(unittest.TestCase):
    def view(self, reader):
        png = text_png('<div id=a style="font:20px Arial;width:260px;height:40px;background:#eee">Doc 77-41</div>')
        src = "data:image/png;base64," + base64.b64encode(png).decode()
        html = (f'<h1>Message</h1><p>The number is in the picture:</p><img src="{src}">'
                f'<img alt="Company logo" src="{src}"><img src="{src}" width=16 height=16><p>End</p>')
        seen: list[int] = []

        async def read(png_bytes: bytes) -> str:
            seen.append(len(png_bytes))
            return await reader(png_bytes)

        async def run():
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page(viewport={"width": 700, "height": 400})
                await page.set_content(html)
                with patch.object(extract, "IMAGE_READER", read if reader else None):
                    text = await extract.observe(page, "1")
                await browser.close()
                return text
        return asyncio.run(run()), seen

    def test_an_unlabelled_picture_is_read_into_the_page_text(self):
        async def reader(png):
            return "Doc 77-41"
        text, seen = self.view(reader)
        self.assertIn("[img: Doc 77-41]", text)
        self.assertIn("[img: Company logo]", text)  # a labelled one keeps its label, a small icon is left alone
        self.assertEqual(len(seen), 1)
        self.assertNotIn("[img #", text)

    def test_a_picture_without_text_or_a_failed_reading_leaves_nothing(self):
        async def nothing(png):
            return ""

        async def broken(png):
            raise RuntimeError("model down")
        for reader in (nothing, broken, None):
            text, _ = self.view(reader)
            self.assertNotIn("[img #", text)
            self.assertNotIn("Doc 77-41", text)
            self.assertIn("End", text)


class PictureReadTwice(unittest.TestCase):
    def read(self, readings: list):
        import vision
        calls = []

        async def reading(png):
            calls.append(1)
            return readings[len(calls) - 1]
        with patch.object(vision, "_reading", reading), patch.dict(vision._cache, {}, clear=True):
            return asyncio.run(vision.read_image(b"png-%d" % id(readings))), len(calls)

    def test_two_equal_readings_stand(self):
        self.assertEqual(self.read(["QX-1", "QX-1"]), ("QX-1", 2))

    def test_a_third_reading_decides_or_the_text_is_marked(self):
        self.assertEqual(self.read(["QX-1", "QX-7", "QX-7"]), ("QX-7", 3))
        text, calls = self.read(["QX-1", "QX-7", "QX-9"])
        self.assertEqual(calls, 3)
        self.assertIn("unsure", text)

    def test_one_failed_reading_leaves_the_other(self):
        self.assertEqual(self.read([None, "QX-1"]), ("QX-1", 2))
        self.assertEqual(self.read([None, None]), ("", 2))


def material(number, **values):
    base = dict(material_number=number, bore=D("50"), stroke=D("100"), pressure=D("160"), mounting="Flange",
                active=True, problems=[])
    base.update(values)
    return SimpleNamespace(**base)


class UnreadMaterials(unittest.TestCase):
    def test_every_candidate_fully_read_is_a_complete_comparison(self):
        research_ = SimpleNamespace(inspected=["M-1"], candidates=[])
        self.assertEqual(orchestrator.unread_attributes(research_, {"M-1": material("M-1")}), [])

    def test_a_number_nobody_showed_or_a_missing_record_is_named(self):
        research_ = SimpleNamespace(inspected=["M-1", "M-2"],
                                    candidates=[SimpleNamespace(material_number="M-1", bore=None, stroke=D("100"),
                                                                pressure=None, mounting="")])
        out = orchestrator.unread_attributes(research_, {"M-1": material("M-1", bore=None, stroke=None, mounting="")})
        self.assertEqual(out, ["M-1: bore, mounting not read", "M-2: record not read"])

    def test_the_hit_list_can_supply_what_the_record_lacks(self):
        research_ = SimpleNamespace(inspected=["M-1"],
                                    candidates=[SimpleNamespace(material_number="M-1", bore=D("50"), stroke=None,
                                                                pressure=None, mounting="Flange")])
        self.assertEqual(orchestrator.unread_attributes(research_, {"M-1": material("M-1", bore=None, mounting="")}), [])


class MailboxWithoutLinks(unittest.TestCase):
    def test_the_open_message_is_kept_once(self):
        kept: list[str] = []
        session = SimpleNamespace(name="mail", trace=Trace(tempfile.mkdtemp()), view="")

        async def look(*a, **k):
            return session.view
        session.look = look
        tool = research_mail.keep_open_tool(kept)
        ctx = SimpleNamespace(deps=SimpleNamespace(session=session))

        async def run():
            session.view = "# Approval\nFrom: a@b.test\nText of the message"
            first = await tool(ctx)
            second = await tool(ctx)
            return first, second
        first, second = asyncio.run(run())
        self.assertIn("Kept the open page", first)
        self.assertIn("already kept", second)
        self.assertEqual(kept, ["# Approval\nFrom: a@b.test\nText of the message"])


class FormTheMappingDoesNotFit(unittest.TestCase):
    def desk(self):
        return submit.PositionDesk({"01.02": {"position": "01.02", "material_number": "M-1", "quantity": 12,
                                              "unit_price": 1234.5, "lead_time_weeks": 3}}, time.monotonic() + 900)

    def test_the_values_of_a_position_can_be_asked_for(self):
        text = asyncio.run(self.desk().position_values(" 01.02 "))
        for part in ("material_number = M-1", "quantity = 12", "unit_price = 1234,50", "lead_time_weeks = 3"):
            self.assertIn(part, text)
        self.assertIn("not one of this bid", asyncio.run(self.desk().position_values("09.99")))

    def test_a_position_entered_by_hand_counts_in_the_total(self):
        desk = self.desk()
        session = SimpleNamespace(name="bid", trace=Trace(tempfile.mkdtemp()), seen="")
        ctx = SimpleNamespace(deps=SimpleNamespace(session=session))
        answer = asyncio.run(desk.position_entered(ctx, "01.02"))
        self.assertEqual(desk.entered, {"01.02"})
        self.assertIn("14814,00", answer)
        self.assertIn("is not one of", asyncio.run(desk.position_entered(ctx, "09.99")))


class StatusWithoutALegend(unittest.TestCase):
    def seen(self, **changes):
        values = dict(material_number="M-1", status_code="PRD", status_meaning="", active=False,
                      free_stock="5", stock_lead_time="2 weeks", production_lead_time="6 weeks", conditions=[])
        values.update(changes)
        return research.MaterialSeen(**values)

    def test_a_status_nobody_explained_is_a_fact_still_to_find(self):
        record = research._material_record(self.seen(status_known=False))
        self.assertIn(research.STATUS_UNREAD, record.problems)
        self.assertTrue(any(name.startswith("status") for name in research.incomplete(record)))
        known = research._material_record(self.seen(status_meaning="blocked", status_known=True))
        self.assertFalse(any(name.startswith("status") for name in research.incomplete(known)))

    def test_an_inactive_guess_does_not_establish_no_material(self):
        research_ = SimpleNamespace(inspected=["M-1"], candidates=[])
        guessed = material("M-1", active=False, problems=[research.STATUS_UNREAD])
        blocked = material("M-1", active=False, problems=[])
        self.assertEqual(orchestrator.unread_attributes(research_, {"M-1": guessed}), ["M-1: status meaning not read"])
        self.assertEqual(orchestrator.unread_attributes(research_, {"M-1": blocked}), [])


class SecondReadingBeforeNoBid(unittest.TestCase):
    def settle(self, first_active: bool, rereads: list):
        o = object.__new__(orchestrator.Orchestrator)
        o.trace = Trace(tempfile.mkdtemp())
        want = SimpleNamespace(attributes=lambda: {"bore": D("50")}, pending=[])
        o.requirements = {"A-1": object()}
        o.effective = lambda req: want
        o.tender_facts = SimpleNamespace(positions=[SimpleNamespace(position="01.01", requirement="A-1")])
        research_ = SimpleNamespace(requirement_id="A-1", inspected=["M-1"], candidates=[])
        o.erp = SimpleNamespace(requirements=[research_], materials={"M-1": material("M-1", active=first_active)})
        calls = []

        async def reread(number):
            calls.append(number)
            answer = rereads[len(calls) - 1]
            return None if answer is None else material(number, active=answer)
        o._erp = SimpleNamespace(reread=reread)
        with patch.object(orchestrator, "mismatches", lambda want, attrs: []), \
                patch.object(orchestrator, "merged_attributes", lambda record, research: {}):
            found = asyncio.run(o.second_reading(["01.01"]))
        return found, len(calls), o.erp.materials["M-1"].active

    def test_two_careful_readings_overturn_a_misread_status(self):
        self.assertEqual(self.settle(False, [True, True]), ({"A-1": "M-1"}, 2, True))

    def test_readings_that_agree_leave_the_no_bid(self):
        self.assertEqual(self.settle(False, [False]), ({}, 1, False))

    def test_one_differing_reading_is_not_enough(self):
        self.assertEqual(self.settle(False, [True, False]), ({}, 2, False))

    def test_a_failed_second_reading_changes_nothing(self):
        self.assertEqual(self.settle(False, [None]), ({}, 1, False))


if __name__ == "__main__":
    unittest.main()
