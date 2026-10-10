"""Parsing of values the workers copy from pages, on invented wordings."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evidence import ParseError, parse_pieces_per_set  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
