"""Revision labels differ between sources: the identifier inside them decides which amendments and reserves apply."""
import unittest
from datetime import date
from decimal import Decimal

import calc
from evidence import Requirement, effective_requirement, revision_id, same_revision


class RevisionLabels(unittest.TestCase):
    def test_identifier_without_label_words_dates_or_status(self):
        for label in ("C", "Rev. C", "Revision C of 1 March 2030", "Index C (current)", "revision c"):
            self.assertEqual(revision_id(label), "c", label)
        self.assertEqual(revision_id("Version 2.1 vom 03.05.2030"), "2.1")
        self.assertEqual(revision_id("Revision Q-4711 (current)"), "q-4711")
        self.assertEqual(revision_id(""), "")

    def test_other_revisions_stay_apart(self):
        self.assertFalse(same_revision("Revision I of 1 March", "III"))
        self.assertFalse(same_revision("Rev. 02", "Revision 03 vom 02.05.2030"))
        self.assertFalse(same_revision("Q-4711", "Q-4712"))
        self.assertFalse(same_revision("A", "Revision B (aktuell)"))
        self.assertTrue(same_revision("Spec Q-4711", "Q-4711"))
        self.assertFalse(same_revision("", ""))

    def test_amendment_applies_across_label_forms_only_to_its_revision(self):
        req = Requirement(requirement_id="Q-7", revision="Revision C of 1 March 2030", bore=Decimal(50),
                          stroke=Decimal(300), pressure=Decimal(160), mounting="flange", certification="none",
                          catalog_group="G-1")
        amendments = [calc.Amendment(date=date(2030, 3, 5), revision="C", requirement="Q-7",
                                     attributes={"pressure": Decimal(210)}),
                      calc.Amendment(date=date(2030, 3, 6), revision="B", requirement="Q-7",
                                     attributes={"pressure": Decimal(250)})]
        self.assertEqual(effective_requirement(req, amendments).pressure, Decimal(210))


if __name__ == "__main__":
    unittest.main()
