"""Mail selection without the mailbox address, and the per-kind time budget (no browser, no model)."""
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
