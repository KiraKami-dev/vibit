"""Mail selection without the mailbox address, the per-kind time budget, and the one-step search with its fast
first pass over the rows (no browser, no model)."""
import asyncio
import os
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import research_mail as rm  # noqa: E402


def record(sender: str, recipient: str, fee: str, day: int) -> rm.MailRecord:
    return rm.MailRecord[rm.PackagingContent](
        subject="Tariffs T-1", sender=sender, recipient=recipient, sent_at=datetime(2030, 1, day, tzinfo=UTC),
        reference="T-1", is_record=True, content=rm.PackagingContent(entries=[rm.Tariff(key="P-1",
                                                                                         price_per_carton=fee)]))


KIND = rm.Kind(name="packaging", subject="Tariffs", sender="logistics", sender_form="local_part_at_bidder_domain",
               site="mail", what="tariffs", schema=rm.SCHEMAS["packaging"])


class MailDomainTest(unittest.TestCase):
    def test_records_are_authorised_by_the_recipients_domain_without_an_address(self):
        own = "sales@example.com"
        result = rm.KindResult(KIND, records=[
            record("logistics@example.com", own, "1.50", 2),
            record("logistics@example.org", own, "9.99", 3),  # look-alike domain, newer
            record("logistics@example.com", "sales@example.com", "2.50", 4),
        ])
        facts = rm.resolve_mail([result], "T-1", "", datetime(2030, 2, 1, tzinfo=UTC), "")
        self.assertEqual(str(facts.packaging["P-1"].price_per_carton), "2.50")
        self.assertTrue(any("recipients" in p for p in facts.problems))

    def test_the_address_wins_over_the_recipients(self):
        self.assertEqual(rm.bidder_domain("sales@example.net", [record("a@example.com", "b@example.com", "1", 2)]),
                         "example.net")
        self.assertEqual(rm.bidder_domain("", []), "")


class KindBudgetTest(unittest.TestCase):
    def test_budget_shrinks_near_the_deadline(self):
        self.assertEqual(rm.kind_budget(0.0, None), rm.KIND_SECONDS)
        self.assertEqual(rm.kind_budget(0.0, 1000.0), rm.KIND_SECONDS)
        self.assertEqual(rm.kind_budget(0.0, 200.0 + rm.READ_RESERVE), 100.0)
        self.assertEqual(rm.kind_budget(0.0, 30.0 + rm.READ_RESERVE), 30.0)
        self.assertEqual(rm.kind_budget(0.0, 10.0), 0.0)

    def test_approval_is_planned_first(self):
        kinds = [rm.Kind(name=n, subject=n, sender="x", sender_form="name", site="mail", what=n,
                         schema=rm.SCHEMAS[n]) for n in ("fx", "packaging", "approval")]
        self.assertEqual([k.name for k in sorted(kinds, key=lambda k: k.name not in rm.FIRST)],
                         ["approval", "fx", "packaging"])


class LandedTest(unittest.TestCase):
    def test_a_sign_in_redirect_is_not_the_message(self):
        want = "https://mail.example.com/?folder=inbox&message=m-1"
        self.assertTrue(rm._landed(want, want))
        self.assertFalse(rm._landed("https://mail.example.com/login?returnTo=%2F", want))
        self.assertFalse(rm._landed("https://mail.example.com/?folder=inbox", want))



class SenderTest(unittest.TestCase):
    def kind(self, sender, form):
        return rm.Kind(name="approval", subject="Approval", sender=sender, sender_form=form, site="mail",
                       what="approval", schema=rm.SCHEMAS["approval"])

    def test_a_sender_given_as_name_and_address_is_matched_by_the_address(self):
        own = record("j.doe@example.com", "sales@example.com", "1", 2)
        other = record("j.doe@example.org", "sales@example.com", "1", 2)
        for form in ("address", "name", "local_part_at_bidder_domain"):  # whatever the plan called the form
            kind = self.kind("Jane Doe <j.doe@example.com>", form)
            self.assertTrue(rm.sender_ok(own, kind, "example.com"), form)
            self.assertFalse(rm.sender_ok(other, kind, "example.com"), form)
        # the forms the brief states without an address work as before
        self.assertTrue(rm.sender_ok(own, self.kind("j.doe", "local_part_at_bidder_domain"), "example.com"))
        self.assertFalse(rm.sender_ok(other, self.kind("j.doe", "local_part_at_bidder_domain"), "example.com"))
        self.assertTrue(rm.sender_ok(own, self.kind("j.doe@example.com", "address"), ""))
        # and the page may write the sender itself as a name with the address
        shown = record("Jane Doe <j.doe@example.com>", "sales@example.com", "1", 2)
        self.assertTrue(rm.sender_ok(shown, self.kind("Jane Doe <j.doe@example.com>", "address"), ""))
        self.assertTrue(rm.sender_ok(shown, self.kind("j.doe", "local_part_at_bidder_domain"), "example.com"))
        self.assertFalse(rm.sender_ok(shown, self.kind("j.roe", "local_part_at_bidder_domain"), "example.com"))


class Events:
    def event(self, name, kind, quiet=False, **fields):
        pass


def listing(*rows):
    """A result list: (row text, [(link text, target), ...]) per row."""
    from site_tools import Listing, Row
    return Listing([Row(text, [{"ref": "", "text": t, "href": h} for t, h in links]) for text, links in rows],
                   pages=2, how="main list of the page", notes=[])


class OneStepSearchTest(unittest.TestCase):
    ROWS = (("Tariffs T-1 | logistics | 3 Jan", [("Tariffs T-1", "/messages/7"), ("Mark", "/flag/7")]),
            ("Tariffs T-1-A | logistics | 4 Jan", [("Tariffs T-1-A", "/messages/8")]),
            ("Tariffs T-1 | logistics | 5 Jan", []))  # a row that is not a link

    def find(self, marks, slow: bool = False):
        session = SimpleNamespace(name="mail", site={"url": "https://mail.test/"}, seen="", trace=Events(),
                                  page=SimpleNamespace(url="https://mail.test/?q=x"), view="")
        kept = rm._Reading(KIND, "T-1")
        opened: list[str] = []

        async def search_rows(sess, field_ref, query, submit_ref=None):
            return listing(*self.ROWS), f"Search for {query!r}: 3 rows over 2 pages. URL: {sess.page.url}"

        async def decide(state, questions, images=None, retries=2):
            if slow:
                await asyncio.sleep(5)
            return None if marks is None else {key: {"noul": marks[int(key[1:])]} for key in questions}

        async def open_message(sess, url, tries=2):
            opened.append(url)
            return f"URL: {url}\nTitle: message\n\ntext of {url}"

        async def read_message(view, kind, reference):
            return None

        async def run():
            ctx = SimpleNamespace(deps=SimpleNamespace(session=session))
            first = await rm.find_tool(kept, KIND, "T-1")(ctx, "e8", "T-1 Tariffs", "e9")
            again = await rm.keep_messages_tool(kept)(ctx, ["Tariffs T-1"])
            await asyncio.gather(*kept.reads.values())
            return first, again

        session.seen = '[e3 link "Tariffs T-1" -> /messages/7]'  # as the list showed it to the worker
        with patch.object(rm.site_tools, "search_rows", search_rows), patch.object(rm.clef, "decide", decide), \
                patch.object(rm, "open_message", open_message), patch.object(rm, "read_message", read_message), \
                patch.object(rm, "ROW_FILTER_SECONDS", 0.2), patch.dict(os.environ, {"CLEF_BASE_URL": "http://clef"}):
            first, again = asyncio.run(run())
        return first, again, opened, kept

    def test_rows_the_fast_check_is_sure_of_are_kept_in_the_same_step(self):
        first, again, opened, kept = self.find([0.93, 0.07, 0.88])
        self.assertEqual(opened, ["https://mail.test/messages/7"])  # not the look-alike, not the row without a link
        self.assertEqual((len(kept), kept.urls), (1, {"https://mail.test/messages/7"}))
        self.assertIn("1. Tariffs T-1 | logistics | 3 Jan", first)  # the worker still sees every row
        self.assertIn("2. Tariffs T-1-A", first)
        self.assertIn("Already kept for reading (1)", first)
        self.assertIn("already kept", again)  # naming the same subject again opens nothing twice

    def test_without_an_answer_in_time_the_worker_keeps_as_before(self):
        for marks, slow in ((None, False), ([0.9, 0.9, 0.9], True)):
            first, again, opened, kept = self.find(marks, slow)
            self.assertIn("Nothing was kept yet", first)
            self.assertEqual(opened, ["https://mail.test/messages/7"])  # only by the worker's own keep_results
            self.assertIn("Kept 1 of 1", again)

    def test_the_link_a_row_opens(self):
        row = listing(self.ROWS[0]).rows[0]
        self.assertEqual(rm.row_link(row, "https://mail.test/?q=x", "mail.test"), "https://mail.test/messages/7")
        elsewhere = listing(("Tariffs", [("Open in the old client", "https://old.test/m/7")])).rows[0]
        self.assertIsNone(rm.row_link(elsewhere, "https://mail.test/", "mail.test"))

    def test_the_fast_check_is_skipped_when_it_is_off_or_not_there(self):
        with patch.dict(os.environ, {"CLEF_BASE_URL": ""}):
            self.assertIsNone(asyncio.run(rm.likely_rows(["a row"], KIND, "T-1")))
        with patch.object(rm, "ROW_FILTER", False), patch.dict(os.environ, {"CLEF_BASE_URL": "http://clef"}):
            self.assertIsNone(asyncio.run(rm.likely_rows(["a row"], KIND, "T-1")))


class SurveyListTest(unittest.TestCase):
    VIEW = '[e8 textbox "Search"]\n[e9 button "Go"]'

    def lister(self, found):
        session = SimpleNamespace(name="mail", site={"url": "https://mail.test/"}, trace=Events(), view=self.VIEW,
                                  page=SimpleNamespace(url="https://mail.test/?folder=in"))
        asked: list[tuple[str, str, str | None]] = []

        async def search_rows(sess, field_ref, query, submit_ref=None):
            asked.append((field_ref, query, submit_ref))
            sess.view = '[e3 link "Home"]\n[e11 textbox "Search"]\n[e12 button "Go"]'  # refs change with the view
            return listing(*found[sess.page.url][query]), f"Search for {query!r}"

        async def look():
            return session.view

        async def act(kind, ref, fn):
            if ref != "e20":
                return f"No element {ref} on this page"
            session.page.url = "https://mail.test/?folder=old"
            session.view = '[e5 textbox "Search"]\n[e6 button "Go"]'
            return session.view

        session.look, session.act = look, act
        return session, asked, patch.object(rm.site_tools, "search_rows", search_rows)

    FOUND = {"https://mail.test/?folder=in": {
                 "T-1 Tariffs": [("Tariffs T-1 | 3 Jan", [("Tariffs T-1", "/messages/7")])],
                 "T-1 Rates": [("Rates T-1 | 2 Jan", [("Rates T-1", "/messages/5")]),
                               ("Tariffs T-1 | 3 Jan", [("Tariffs T-1", "/messages/7")])]},
             "https://mail.test/?folder=old": {
                 "T-1 Tariffs": [("Tariffs T-1 | 1 Jan", [("Tariffs T-1", "/messages/2")])],
                 "T-1 Rates": []}}

    def test_one_call_lists_a_folder_for_every_topic(self):
        session, asked, searching = self.lister(self.FOUND)

        async def run():
            index: list[rm.SurveyRow] = []
            tool = rm.list_tool(index)
            ctx = SimpleNamespace(deps=SimpleNamespace(session=session))
            first = await tool(ctx, "e8", ["T-1 Tariffs", "T-1 Rates"], "e9")
            again = await tool(ctx, "e11", ["T-1 Rates"], "e12")  # nothing done on the page in between
            session.view += '\n[e20 link "Old mail"]'  # a folder menu opened, the folder is still the same
            same = await tool(ctx, "e11", ["T-1 Tariffs", "T-1 Rates"], "e12")
            session.view += '\n[e20 link "Old mail"]'
            other = await tool(ctx, "e11", ["T-1 Tariffs", "T-1 Rates"], "e12", open_ref="e20")
            return index, first, again, same, other

        with searching:
            index, first, again, same, other = asyncio.run(run())
        # the field and button are found again by their names after each search and after the folder opened;
        # the repeat on the unchanged page sent no search, the one in the unchanged folder only its first
        self.assertEqual(asked, [("e8", "T-1 Tariffs", "e9"), ("e11", "T-1 Rates", "e12"),
                                 ("e11", "T-1 Tariffs", "e12"),
                                 ("e5", "T-1 Tariffs", "e6"), ("e11", "T-1 Rates", "e12")])
        self.assertEqual([row.url for row in index], ["https://mail.test/messages/7", "https://mail.test/messages/5",
                                                      "https://mail.test/messages/2"])
        self.assertIn("'T-1 Rates': 2 rows, 1 new", first)
        self.assertIn("The page now:", first)  # with the refs for opening the next folder
        self.assertIn("Nothing was done on the page", again)
        self.assertIn("the folder has not changed", same)
        self.assertIn("Listed at https://mail.test/?folder=old", other)

    def test_a_list_asked_for_again_and_again_ends_the_survey(self):
        session, asked, searching = self.lister(self.FOUND)

        async def run():
            tool = rm.list_tool([])
            ctx = SimpleNamespace(deps=SimpleNamespace(session=session))
            await tool(ctx, "e8", ["T-1 Tariffs"], "e9")
            await tool(ctx, "e11", ["T-1 Tariffs"], "e12")
            await tool(ctx, "e11", ["T-1 Tariffs"], "e12")
            with self.assertRaises(rm.Stuck):
                await tool(ctx, "e11", ["T-1 Tariffs"], "e12")

        with searching:
            asyncio.run(run())
        self.assertEqual(len(asked), 1)

    def test_every_version_of_a_record_is_asked_for(self):
        rows = [rm.SurveyRow("Tariffs T-1 | logistics | 3 Jan", "u1", "w"),
                rm.SurveyRow("Tariffs T-1 | logistics | 9 Jan", "u2", "w"),
                rm.SurveyRow("Newsletter", "u3", "w")]
        answers = iter([[2], [1, 2]])
        prompts: list[str] = []

        def answer(messages, info):
            from pydantic_ai.messages import ModelResponse, ToolCallPart
            prompts.append(str(messages[-1]))
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name,
                                                     {"kinds": [{"kind": KIND.name, "rows": next(answers)}]})])

        from pydantic_ai.models.function import FunctionModel
        with patch.object(rm, "reader_model", lambda: FunctionModel(answer)):
            assigned = asyncio.run(rm.assign_rows(rows, [KIND], "T-1"))
        self.assertEqual([row.url for row in assigned[KIND.name]], ["u1", "u2"])
        self.assertIn("Rows [1]", prompts[1])  # the row left out is asked about once


if __name__ == "__main__":
    unittest.main()
