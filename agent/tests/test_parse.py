"""Reading shown values: numbers, dates, zones and sender addresses, on invented strings."""
import sys
import unittest
from datetime import UTC, date, datetime
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parse import (  # noqa: E402
    ParseError,
    Unit,
    parse_date,
    parse_datetime,
    parse_decimal,
    parse_int,
    parse_timezone,
    parse_unit,
    split_address,
)


class Numbers(unittest.TestCase):
    def test_stated_format(self):
        self.assertEqual(parse_decimal("1.234,56", "de"), D("1234.56"))
        self.assertEqual(parse_decimal("1,234.56", "en"), D("1234.56"))
        # the same digits read differently per format
        self.assertEqual(parse_decimal("1.234", "de"), D(1234))
        self.assertEqual(parse_decimal("1.234", "en"), D("1.234"))
        self.assertEqual(parse_decimal("1,234", "de"), D("1.234"))
        self.assertEqual(parse_decimal("1,234", "en"), D(1234))

    def test_unambiguous_without_format(self):
        for text, value in (("12", "12"), ("12,50", "12.50"), ("1.234,5", "1234.5"), ("1,234.5", "1234.5"),
                            ("0,125", "0.125"), ("1234.567", "1234.567"), ("1.234.567", "1234567"),
                            ("-3,5", "-3.5"), ("12.5", "12.5")):
            self.assertEqual(parse_decimal(text), D(value), text)

    def test_shape_beats_stated_format(self):
        self.assertEqual(parse_decimal("1.234,56", "en"), D("1234.56"))
        self.assertEqual(parse_decimal("12,50", "en"), D("12.50"))

    def test_ambiguous_without_format(self):
        for text in ("1.234", "1,234", "12.345 kg"):
            with self.assertRaises(ParseError, msg=text):
                parse_decimal(text)

    def test_units_currency_and_spaces(self):
        self.assertEqual(parse_decimal(" 160 bar "), D(160))
        self.assertEqual(parse_decimal("€ 12,50"), D("12.50"))
        self.assertEqual(parse_decimal("12,50 EUR"), D("12.50"))
        self.assertEqual(parse_decimal("5 %"), D(5))
        self.assertEqual(parse_decimal("1 234,56 EUR"), D("1234.56"))  # grouping space
        self.assertEqual(parse_decimal("1'234.56"), D("1234.56"))
        self.assertEqual(parse_decimal("0,75 m3"), D("0.75"))  # the digit in the unit is not a number

    def test_locale_tag_decides(self):
        self.assertEqual(parse_decimal("1.234 (de-DE)"), D(1234))
        self.assertEqual(parse_decimal("1.234 (en-US)"), D("1.234"))
        self.assertEqual(parse_decimal("1,234 en-GB", "de"), D(1234))  # the tag wins over the record's format

    def test_rejects(self):
        for text in ("", "n/a", "M12", "10 - 20", "1.23.45", "1,2,3"):
            with self.assertRaises(ParseError, msg=text):
                parse_decimal(text, "de")

    def test_int(self):
        self.assertEqual(parse_int("1.200 Stück", "de"), 1200)
        self.assertEqual(parse_int("12,0"), 12)
        with self.assertRaises(ParseError):
            parse_int("12,5")

    def test_unit(self):
        self.assertEqual((parse_unit("Satz"), parse_unit("St.")), (Unit.SET, Unit.PIECE))


class Dates(unittest.TestCase):
    def test_iso(self):
        self.assertEqual(parse_datetime("2026-05-15T10:30:00Z"), datetime(2026, 5, 15, 10, 30, tzinfo=UTC))
        # 10:30 at +02:00 is 08:30 UTC
        self.assertEqual(parse_datetime("2026-05-15T10:30:00+02:00"), datetime(2026, 5, 15, 8, 30, tzinfo=UTC))
        self.assertEqual(parse_datetime("2026-05-15 10:30"), datetime(2026, 5, 15, 10, 30, tzinfo=UTC))
        # an offset in the string wins over the stated zone
        self.assertEqual(parse_datetime("2026-05-15T10:30:00Z", "", "+05:00"),
                         datetime(2026, 5, 15, 10, 30, tzinfo=UTC))

    def test_date_only_is_start_of_day(self):
        self.assertEqual(parse_datetime("2026-05-15"), datetime(2026, 5, 15, tzinfo=UTC))  # no zone: a UTC date
        # midnight at +02:00 is 22:00 UTC the day before
        self.assertEqual(parse_datetime("15.05.2026", "dmy", "+02:00"), datetime(2026, 5, 14, 22, 0, tzinfo=UTC))

    def test_stated_order(self):
        self.assertEqual(parse_datetime("03/04/2026", "dmy"), datetime(2026, 4, 3, tzinfo=UTC))
        self.assertEqual(parse_datetime("03/04/2026", "mdy"), datetime(2026, 3, 4, tzinfo=UTC))
        self.assertEqual(parse_datetime("2026/04/03", "ymd"), datetime(2026, 4, 3, tzinfo=UTC))

    def test_order_from_the_string(self):
        self.assertEqual(parse_datetime("13/04/2026"), datetime(2026, 4, 13, tzinfo=UTC))
        self.assertEqual(parse_datetime("04/13/2026"), datetime(2026, 4, 13, tzinfo=UTC))
        self.assertEqual(parse_datetime("04/13/2026", "dmy"), datetime(2026, 4, 13, tzinfo=UTC))
        self.assertEqual(parse_datetime("05.05.2026"), datetime(2026, 5, 5, tzinfo=UTC))  # same either way
        self.assertEqual(parse_datetime("3. Mai 2026"), datetime(2026, 5, 3, tzinfo=UTC))
        self.assertEqual(parse_datetime("March 4, 2026 2:15 PM"), datetime(2026, 3, 4, 14, 15, tzinfo=UTC))

    def test_ambiguous_order(self):
        for text in ("03/04/2026", "03.04.26 10:00"):
            with self.assertRaises(ParseError, msg=text):
                parse_datetime(text)

    def test_zones(self):
        # 23:30 at UTC+2 is 21:30 UTC
        self.assertEqual(parse_datetime("15.05.2026 23:30", "dmy", "UTC+2"), datetime(2026, 5, 15, 21, 30, tzinfo=UTC))
        self.assertEqual(parse_datetime("15.05.2026, 23:30 Uhr (UTC+02:00)", "dmy"),
                         datetime(2026, 5, 15, 21, 30, tzinfo=UTC))
        self.assertEqual(parse_datetime("15.05.2026 01:30 MESZ", "dmy"), datetime(2026, 5, 14, 23, 30, tzinfo=UTC))
        # Athens is UTC+3 in May and UTC+2 in January
        self.assertEqual(parse_datetime("15.05.2026 12:00", "dmy", "Europe/Athens"),
                         datetime(2026, 5, 15, 9, 0, tzinfo=UTC))
        self.assertEqual(parse_datetime("15.01.2026 12:00", "dmy", "Europe/Athens"),
                         datetime(2026, 1, 15, 10, 0, tzinfo=UTC))
        self.assertEqual(parse_datetime("01/02/2026 08:00", "mdy", "-05:00"), datetime(2026, 1, 2, 13, 0, tzinfo=UTC))
        self.assertIsNone(parse_timezone(""))
        with self.assertRaises(ParseError):
            parse_timezone("somewhere")

    def test_rejects(self):
        for text in ("", "soon", "31.02.2026", "15.05.2026 25:00"):
            with self.assertRaises(ParseError, msg=text):
                parse_datetime(text, "dmy")

    def test_plain_date(self):
        self.assertEqual(parse_date("31.12.2026", "dmy"), date(2026, 12, 31))
        self.assertEqual(parse_date("2026-01-01"), date(2026, 1, 1))
        self.assertEqual(parse_date("2026-01-01T23:30:00-08:00"), date(2026, 1, 1))  # as written, not converted
        with self.assertRaises(ParseError):
            parse_date("01.02.2026")


class Addresses(unittest.TestCase):
    def test_split(self):
        self.assertEqual(split_address("Finanzen@Example.Test"), ("finanzen", "example.test"))
        self.assertEqual(split_address("Some Name <some.name@mail.example.test>"), ("some.name", "mail.example.test"))
        for text in ("", "nobody", "a@example.test, b@example.test"):
            with self.assertRaises(ParseError, msg=text):
                split_address(text)


if __name__ == "__main__":
    unittest.main()
