"""Form-filling helpers on invented options and labels."""
import sys
import unittest
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from configurator import (  # noqa: E402
    complete_positions,
    controls,
    find_control,
    form_value,
    german,
    match_option,
    parse_number,
    position_on_page,
    resolve_position,
    unmapped,
)


def opts(*texts, values=None):
    values = values or texts
    return [{"value": "", "text": "Choose"}] + [{"value": v, "text": t} for t, v in zip(texts, values, strict=True)]


class Numbers(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_number("1.234,56"), D("1234.56"))
        self.assertEqual(parse_number("1,234.56"), D("1234.56"))
        self.assertEqual(parse_number("12,5"), D("12.5"))
        self.assertEqual(parse_number("7"), D("7"))
        self.assertIsNone(parse_number("AB-12"))

    def test_german(self):
        self.assertEqual(german(1234.5), "1234,50")
        self.assertEqual(german(D("0.005")), "0,01")  # HALF_UP
        self.assertEqual(german(5, 0), "5")
        self.assertEqual(form_value("unit_price", 3.1), "3,10")
        self.assertEqual(form_value("quantity", 40), "40")
        self.assertEqual(form_value("mounting", "Clevis"), "Clevis")


class Options(unittest.TestCase):
    def test_text_sorted_numbers_match_by_value(self):
        options = opts("10", "100", "12", "120", "16", "8")
        self.assertEqual(match_option(options, "12")[0]["value"], "12")
        self.assertEqual(match_option(options, "8")[0]["value"], "8")
        self.assertIsNone(match_option(options, "11")[0])

    def test_number_with_unit(self):
        options = opts("1 week", "2 weeks", "12 weeks", "21 weeks")
        self.assertEqual(match_option(options, "2")[0]["text"], "2 weeks")
        self.assertEqual(match_option(options, "12")[0]["text"], "12 weeks")

    def test_catalogue_number_with_details(self):
        options = opts("XY-100 · 160 bar", "XY-1000 · 250 bar", "XY-100-B · 160 bar", values=["a", "b", "c"])
        self.assertEqual(match_option(options, "XY-100")[0]["value"], "a")
        self.assertEqual(match_option(options, "XY-1000")[0]["value"], "b")

    def test_text_and_value(self):
        options = opts("Without approval", "Type A", values=["none", "a"])
        self.assertEqual(match_option(options, "none")[0]["text"], "Without approval")
        self.assertEqual(match_option(options, "type a")[0]["value"], "a")
        self.assertIsNone(match_option(options, "Type")[0])  # a word of an option is not the option

    def test_placeholder_never_matches(self):
        self.assertIsNone(match_option(opts("Red"), "Choose")[0])


VIEW = """## Item 2.10
[e3 select "Diameter (mm)" selected="Choose" options: Choose | 10 | 8]
[e4 select "Length" selected="Choose" disabled options: Choose]
[e5 button "Apply choice"]
[e6 textbox "Qty (pcs)" value=""]
[e7 textbox "Net unit price (EUR)" value=""]
[e8 textbox "Unit price before discount (EUR)" value=""]
[e9 button "Add item"]"""


class Controls(unittest.TestCase):
    def test_find(self):
        self.assertEqual([c[0] for c in controls(VIEW)], ["e3", "e4", "e5", "e6", "e7", "e8", "e9"])
        self.assertEqual(find_control(VIEW, "Length")[0], "e4")
        self.assertEqual(find_control(VIEW, "qty (pcs)")[0], "e6")
        self.assertEqual(find_control(VIEW, "Apply choice", ("button",))[0], "e5")
        self.assertIsNone(find_control(VIEW, "unit price")[0])  # two labels contain it
        self.assertEqual(find_control(VIEW, "Net unit price (EUR)")[0], "e7")

    def test_position_on_page(self):
        self.assertTrue(position_on_page(VIEW, "2.10"))
        self.assertFalse(position_on_page(VIEW, "2.1"))
        self.assertFalse(position_on_page(VIEW, "10"))

    def test_unmapped(self):
        page = ('Qty [e7 textbox "Qty" value=""]\nPrice [e8 textbox "Price" value=""]\n'
                'Base [e9 textbox "Base per piece" value=""]\n[e10 button "Next"]')
        entry = {"position": "1", "quantity": 4, "unit_price": 2.5, "base_unit_price": 2.0, "bore": "40"}
        self.assertEqual(unmapped(page, entry, {"Qty": "quantity", "Price": "unit_price"}),
                         (["base_unit_price"], ["Base per piece"]))
        full = {"Qty": "quantity", "Price": "unit_price", "base per piece": "base_unit_price"}
        self.assertEqual(unmapped(page, entry, full), ([], []))

    def test_resolve_position_page_wins(self):
        page = "## Item 3.07\nStep 4\nShape [e7 select \"Shape\" options: a | b]\n[e9 button \"Skip item\"]"
        bid = ["3.01", "3.02", "3.07"]
        # the model names the next item of its own list while the form shows another one of the bid
        self.assertEqual(resolve_position("3.02", bid, page), ("3.07", ["3.07"]))
        self.assertEqual(resolve_position("3.07", bid, page), ("3.07", ["3.07"]))
        self.assertEqual(resolve_position("Item 3.07 (pump)", bid, page), ("3.07", ["3.07"]))
        # the page shows an item outside the bid: nothing to enter there (the caller tells the model to omit it, or
        # that the named bid item is not on the page and only items outside the bid are omitted)
        other = page.replace("3.07", "3.05")
        self.assertEqual(resolve_position("3.05", bid, other), (None, []))
        self.assertEqual(resolve_position("3.02", bid, other), ("3.02", []))
        # no item on the page at all (a page in transition): the named bid item, reported as not on the page
        blank = "## Totals\n[e7 textbox \"Net\" value=\"\"]"
        self.assertEqual(resolve_position("3.02", bid, blank), ("3.02", []))
        self.assertEqual(resolve_position("9.99", bid, blank), (None, []))


class Positions(unittest.TestCase):
    def test_complete_only(self):
        answer = {"decision": "bid", "positions": [
            {"position": "1", "material_number": "M-1", "quantity": 5, "unit_price": 2.5, "lead_time_weeks": 3,
             "base_unit_price": 2.0},
            {"position": "2", "material_number": "M-2", "quantity": 5, "unit_price": 2.5},
        ]}
        entries = complete_positions(answer, {"1": {"diameter": "10"}})
        self.assertEqual(list(entries), ["1"])
        self.assertEqual(entries["1"]["diameter"], "10")
        self.assertEqual(complete_positions({**answer, "decision": "no_bid"}), {})


if __name__ == "__main__":
    unittest.main()
