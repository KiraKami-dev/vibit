"""Bid forms of other shapes than one position per page with native controls: invented local pages, real browser."""
import asyncio
import sys
import unittest
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from configurator import (  # noqa: E402
    VALUE_NAMES,
    FillPlan,
    controls,
    decimal_mark,
    fill_position,
    form_value,
    mapping_problems,
    position_on_page,
    same_number,
)
from extract import observe  # noqa: E402
from orchestrator import material_attributes  # noqa: E402


class Session:
    def __init__(self, page):
        self.page = page

    async def look(self):
        return await observe(self.page, "1")

    async def settle(self):
        pass


def run_page(html: str, work):
    """Open `html` in a headless browser and run `work(session)`."""
    async def run():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html)
                return await work(Session(page))
            finally:
                await browser.close()
    return asyncio.run(run())


def row(n: int, named: bool) -> str:
    """One table row per position; labels the same in every row, or only row-specific names."""
    def field(kind, name):
        label = "" if named else f' aria-label="{kind}"'
        return f'<td><input name="{name}_{n}"{label}></td>'
    return (f"<tr><td>1.0{n}</td><td>Cylinder</td>{field('Material', 'mat')}{field('Menge', 'qty')}"
            f"{field('Preis', 'price')}{field('Wochen', 'weeks')}</tr>")


def table(named: bool) -> str:
    return ("<h2>Positions</h2><table><tr><th>Pos</th><th>Text</th><th>Material</th><th>Menge</th><th>Preis</th>"
            "<th>Wochen</th></tr>" + "".join(row(n, named) for n in (1, 2, 3)) + "</table><button>Weiter</button>")


ENTRY = {"position": "1.02", "material_number": "M-2", "quantity": 7, "unit_price": D("12.5"), "lead_time_weeks": 3}


async def values(page, names):
    return [await page.input_value(f"[name={n}]") for n in names]


class TablePage(unittest.TestCase):
    def test_repeated_labels_take_the_row_of_the_position(self):
        async def work(session):
            view = await session.look()
            self.assertTrue(position_on_page(view, "1.02"))
            self.assertTrue(position_on_page(view, "1.03"))
            plan = FillPlan(fields={"Material": "material_number", "Menge": "quantity", "Preis": "unit_price",
                                    "Wochen": "lead_time_weeks"})
            log: list[str] = []
            error = await fill_position(session, ENTRY, plan, log)
            return error, await values(session.page, ["mat_1", "qty_1", "mat_2", "qty_2", "price_2", "weeks_2"])

        error, got = run_page(table(named=False), work)
        self.assertIsNone(error)
        self.assertEqual(got, ["", "", "M-2", "7", "12,50", "3"])

    def test_row_specific_names_map_to_the_same_column(self):
        async def work(session):
            # the model mapped the first row's fields; the third position goes into the third row
            plan = FillPlan(fields={"mat_1": "material_number", "qty_1": "quantity", "price_1": "unit_price",
                                    "weeks_1": "lead_time_weeks"})
            error = await fill_position(session, {**ENTRY, "position": "1.03"}, plan, [])
            return error, await values(session.page, ["qty_1", "mat_3", "qty_3", "price_3", "weeks_3"])

        error, got = run_page(table(named=True), work)
        self.assertIsNone(error)
        self.assertEqual(got, ["", "M-2", "7", "12,50", "3"])

    def test_a_list_of_positions_without_fields_is_not_a_position_page(self):
        view = ("## Item 1.01\nShape [e2 select \"Shape\" options: a | b]\nQty [e3 textbox \"Qty\" value=\"\"]\n"
                "Other items: 1.02, 1.03\n[e4 button \"Next\"]")
        self.assertFalse(position_on_page(view, "1.02"))
        self.assertTrue(position_on_page(view, "1.01"))


ARIA = """<h2>Item 4.02</h2>
<fieldset><legend>Series</legend>
  <label><input type="radio" name="s" value="a"> Series A</label>
  <label><input type="radio" name="s" value="b"> Series B</label>
</fieldset>
<div id="lb">Mounting</div>
<div role="listbox" aria-labelledby="lb" tabindex="0">
  <div role="option" aria-selected="false">Flange</div><div role="option" aria-selected="false">Clevis</div>
</div>
<span id="cb">Catalogue no.</span>
<div role="combobox" aria-labelledby="cb" aria-controls="pop" aria-expanded="false" tabindex="0">Choose</div>
<ul id="pop" role="listbox" hidden><li role="option">M-10 / 16 bar</li><li role="option">M-11 / 25 bar</li></ul>
<label>Qty <input type="number" name="q"></label>
<label>Unit price <input type="number" step="0.01" name="p"></label>
<label>Weeks <input name="w"></label>
<script>
document.querySelectorAll('[role=listbox][aria-labelledby=lb] [role=option]').forEach(o => o.onclick = () => {
  o.parentElement.querySelectorAll('[role=option]').forEach(x => x.setAttribute('aria-selected', 'false'));
  o.setAttribute('aria-selected', 'true');
});
const box = document.querySelector('[role=combobox]'), pop = document.getElementById('pop');
box.onclick = () => { pop.hidden = !pop.hidden; box.setAttribute('aria-expanded', String(!pop.hidden)); };
pop.querySelectorAll('[role=option]').forEach(o => o.onclick = () => {
  box.textContent = o.textContent; o.setAttribute('aria-selected', 'true');
  pop.hidden = true; box.setAttribute('aria-expanded', 'false');
});
</script>"""


class AriaControls(unittest.TestCase):
    def test_view_shows_groups_and_labelled_combobox(self):
        view = run_page(ARIA, lambda session: session.look())
        kinds = {label: kind for _, kind, label in controls(view)}
        self.assertEqual(kinds["Series"], "radiogroup")
        self.assertEqual(kinds["Mounting"], "listbox")
        self.assertEqual(kinds["Catalogue no."], "combobox")
        self.assertIn('value="Choose"', view)
        self.assertIn("options: Series A | Series B", view)
        fields = {"Mounting": "mounting", "Catalogue no.": "material_number", "Qty": "quantity",
                  "Unit price": "unit_price", "Weeks": "lead_time_weeks"}
        self.assertEqual(mapping_problems(view, fields, set(VALUE_NAMES)), [])

    def test_fill_radio_listbox_combobox_and_number_fields(self):
        async def work(session):
            plan = FillPlan(fields={"Mounting": "mounting", "Catalogue no.": "material_number", "Qty": "quantity",
                                    "Unit price": "unit_price", "Weeks": "lead_time_weeks"})
            entry = {"position": "4.02", "material_number": "M-11", "quantity": 4, "unit_price": D("2.5"),
                     "lead_time_weeks": 3, "mounting": "Clevis"}
            log: list[str] = []
            # a choice no value is mapped to (the series) is made with the model's pick
            error = await fill_position(session, entry, plan, log, {"Series": "Series B"})
            page = session.page
            return error, log, {
                "series": await page.is_checked("input[value=b]"),
                "mounting": await page.get_attribute("text=Clevis", "aria-selected"),
                "catalogue": await page.text_content("[role=combobox]"),
                "qty": await page.input_value("[name=q]"),
                "price": await page.input_value("[name=p]"),
                "weeks": await page.input_value("[name=w]"),
            }

        error, log, got = run_page(ARIA, work)
        self.assertIsNone(error, log)
        self.assertEqual(got, {"series": True, "mounting": "true", "catalogue": "M-11 / 25 bar", "qty": "4",
                               "price": "2.50", "weeks": "3"})

    def test_a_blocked_list_names_the_unmapped_choice(self):
        async def work(session):
            plan = FillPlan(fields={"Mounting": "mounting", "Catalogue no.": "material_number", "Qty": "quantity",
                                    "Unit price": "unit_price", "Weeks": "lead_time_weeks"})
            entry = {"position": "4.02", "material_number": "M-12", "quantity": 4, "unit_price": 2.5,
                     "lead_time_weeks": 3, "mounting": "Clevis", "catalog_group": "B"}
            return await fill_position(session, entry, plan, [])

        error = run_page(ARIA, work)
        self.assertIn("'Series'", error)
        self.assertIn("choices=", error)
        self.assertIn("catalog_group B", error)


class NumberFormat(unittest.TestCase):
    def test_decimal_mark_from_the_brief(self):
        self.assertEqual(decimal_mark("Enter amounts with a decimal point."), ".")
        self.assertEqual(decimal_mark("Betraege mit Dezimalkomma eingeben."), ",")
        self.assertEqual(decimal_mark("Net total e.g. 1234.50, due 10.10.2030"), ".")
        self.assertEqual(decimal_mark("Net total e.g. 1234,50"), ",")
        self.assertEqual(decimal_mark("Fill the form and sign it."), ",")  # nothing said: German default

    def test_values_and_comparison(self):
        self.assertEqual(form_value("unit_price", D("3.1"), "."), "3.10")
        self.assertEqual(form_value("unit_price", D("3.1")), "3,10")
        self.assertTrue(same_number("2.5", "2,50"))
        self.assertTrue(same_number("2,50 EUR", "2.50"))
        self.assertFalse(same_number("2,05", "2,50"))
        self.assertFalse(same_number("", "0,00"))

    def test_fill_in_takes_a_decimal_point_in_number_fields(self):
        from submit import number_field_text

        async def work(session):
            await session.page.set_content('<input type="number" id="n"><input id="t">')
            number = await number_field_text(session.page.locator("#n"), "1234,50")
            text = await number_field_text(session.page.locator("#t"), "1234,50")
            return number, text

        self.assertEqual(run_page("", work), ("1234.50", "1234,50"))


class Attributes(unittest.TestCase):
    def test_catalogue_group_and_missing_attributes_come_from_the_requirement(self):
        from types import SimpleNamespace
        material = SimpleNamespace(bore=D("40"), stroke=None, pressure=D("160"), mounting="Flange", certification="")
        wanted = SimpleNamespace(catalog_group="G-7", bore=D("41"), stroke=D("200"), pressure=D("160"),
                                 mounting="Flange", certification="None")
        self.assertEqual(material_attributes(material, wanted),
                         {"catalog_group": "G-7", "bore": "40", "stroke": "200", "pressure": "160",
                          "mounting": "Flange", "certification": "None"})
        self.assertNotIn("catalog_group", material_attributes(material))


if __name__ == "__main__":
    unittest.main()
