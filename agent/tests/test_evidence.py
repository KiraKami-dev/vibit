"""Parsing of values the workers copy from pages, on invented wordings."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from decimal import Decimal as D  # noqa: E402

from evidence import ParseError, parse_number, parse_pieces_per_set  # noqa: E402


class PiecesPerSet(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_pieces_per_set("4"), 4)
        self.assertEqual(parse_pieces_per_set("4 Stück"), 4)
        self.assertEqual(parse_pieces_per_set("6 pieces per kit"), 6)

    def test_definition_counts_the_pieces_not_the_set(self):
        self.assertEqual(parse_pieces_per_set("1 Satz = 4 Stück"), 4)
        self.assertEqual(parse_pieces_per_set("2 sets = 12 pcs"), 6)
        self.assertEqual(parse_pieces_per_set("1 set: 3 pieces"), 3)
        self.assertEqual(parse_pieces_per_set("8 Stück je 1 Satz"), 8)
        self.assertEqual(parse_pieces_per_set("Satz zu 7 Stück"), 7)

    def test_unclear(self):
        for text in ("2 = 3", "3 Stück, 4 Stück", "1 2 3", "ein Satz"):
            with self.assertRaises(ParseError, msg=text):
                parse_pieces_per_set(text)


class NumberFormat(unittest.TestCase):
    def test_groupings_follow_the_format(self):
        self.assertEqual(parse_number("12,345", "en"), D("12345"))
        self.assertEqual(parse_number("12.345", "de"), D("12345"))
        self.assertEqual(parse_number("1.234.567", "de"), D("1234567"))
        self.assertEqual(parse_number("1.234,56", "de"), D("1234.56"))
        self.assertEqual(parse_number("1,234.56", "en"), D("1234.56"))

    def test_separator_that_cannot_group_is_the_decimal_mark(self):
        # a record tagged "de" that quotes a rate with a decimal point (a soak run read such a rate as 10^4 times
        # its value), and the mirror case
        self.assertEqual(parse_number("1.2345", "de"), D("1.2345"))
        self.assertEqual(parse_number("0.95", "de-DE"), D("0.95"))
        self.assertEqual(parse_number("1,2345", "en"), D("1.2345"))
        self.assertEqual(parse_number("3,5", "en"), D("3.5"))
        self.assertEqual(parse_number("1.2345 (de-DE)"), D("1.2345"))


if __name__ == "__main__":
    unittest.main()
