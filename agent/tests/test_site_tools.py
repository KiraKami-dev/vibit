"""The list, search and normalising helpers of site_tools, on invented pages in a local Chromium."""
import asyncio
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from browser_tools import SiteSession  # noqa: E402
from evidence import ParseError  # noqa: E402
from site_tools import (  # noqa: E402
    Extraction,
    Found,
    ground,
    paginate,
    price_unit,
    read_date,
    read_number,
    readings,
    run_search,
    unit_price,
)
from tracing import Trace  # noqa: E402

# A record list that appends three rows per "load more" click and repeats itself past the end.
LOAD_MORE = """
<nav><a href="/x">Home</a><a href="/y">Next steps</a></nav>
<h1>Records</h1>
<table id="hits"><thead><tr><th>Number</th><th>Bore</th></tr></thead><tbody id="rows">
<tr><td><a href="/rec/1">M-1001</a></td><td>40 mm</td><td><span role="tooltip">status A</span></td></tr>
<tr><td><a href="/rec/2">M-1002</a></td><td>50 mm</td></tr>
<tr><td><a href="/rec/3">M-1003</a></td><td>63 mm</td></tr>
</tbody></table>
<button id="more" onclick="more()">Weitere laden</button>
<script>
let page = 1;
function more() {
  if (page >= 3) return;
  page += 1;
  for (let i = 1; i <= 3; i++) {
    const n = (page - 1) * 3 + i;
    document.getElementById('rows').insertAdjacentHTML('beforeend',
      `<tr><td><a href="/rec/${n}">M-${1000 + n}</a></td><td>${n * 10} mm</td></tr>`);
  }
}
</script>
"""

# A paged list whose "next" replaces the rows; past the last page it shows the last page again.
NEXT_PAGE = """
<h1>Messages</h1>
<ul class="list">
<li class="row"><a href="/msg/1">Subject one</a> <span>01/25</span></li>
<li class="row"><a href="/msg/2">Subject two</a> <span>01/26</span></li>
</ul>
<p>Page <span id="p">1</span></p>
<a href="#" id="next" onclick="next(); return false;">Nächste Seite ›</a>
<script>
let page = 1;
function next() {
  page = Math.min(page + 1, 3);
  document.getElementById('p').textContent = page;
  const ul = document.querySelector('ul.list');
  ul.innerHTML = '';
  for (let i = 1; i <= 2; i++) {
    const n = (page - 1) * 2 + i;
    ul.insertAdjacentHTML('beforeend',
      `<li class="row"><a href="/msg/${n}">Subject ${n}</a> <span>02/0${n}</span></li>`);
  }
}
</script>
"""

# A register with one record per page behind "next page" links, and an imitation toolbar with repeated children.
ONE_PER_PAGE = """
<div class="bar"><a href="/a">←</a><span>Address</span><button>↻</button></div>
<div class="bar"><a href="/b">Open</a><span>file.html</span><button>×</button></div>
<h1>Register</h1>
<table><thead><tr><th>Number</th><th>Title</th></tr></thead><tbody id="rows">
<tr><td><canvas role="img" aria-label="DOC-001"></canvas></td><td>Guide <button>Actions</button></td></tr>
</tbody></table>
<p>Page <span id="p">1</span> of 3</p>
<a href="#" id="next" onclick="next(); return false;">Nächste Seite</a>
<script>
let page = 1;
function next() {
  page = Math.min(page + 1, 3);
  document.getElementById('p').textContent = page;
  document.getElementById('rows').innerHTML =
    `<tr><td>DOC-00${page}</td><td>Record ${page} <button>Actions</button></td></tr>`;
}
</script>
"""

# A search form: "many" is refused as too broad, "none" finds nothing, anything else lists two hits.
SEARCH = """
<form onsubmit="go(); return false;">
<label>Term <input id="q" name="q"></label> <button type="submit">Suchen</button>
</form>
<div id="out"></div>
<script>
function go() {
  const q = document.getElementById('q').value;
  const out = document.getElementById('out');
  if (q === 'many') out.innerHTML = '<p>Zu viele Treffer (mehr als 20). Bitte Suche einschränken.</p>';
  else if (q === 'none') out.innerHTML = '<p>Keine Treffer für diese Suche.</p>';
  else out.innerHTML = '<div class="hit"><a href="/h/1">Hit A ' + q + '</a> 1.234,50 (de-DE)</div>'
                     + '<div class="hit"><a href="/h/2">Hit B ' + q + '</a> 2,345.60 (en-US)</div>';
}
</script>
"""


class _Browser:
    """One headless Chromium for the list tests; each test gets a fresh tab with its own page content."""

    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.trace_dir = tempfile.mkdtemp()

    async def __aenter__(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context()
        return self

    async def session(self, html: str) -> SiteSession:
        site = {"siteId": "test-site", "url": "http://localhost/", "credentials": {}}
        session = SiteSession(site, self.context, Trace(self.trace_dir))
        page = await self.context.new_page()
        session._watch(page)
        await page.set_content(html)
        await page.wait_for_timeout(100)
        return session

    async def __aexit__(self, *exc):
        await self.browser.close()
        await self.playwright.stop()


def run(coro):
    return asyncio.run(coro)


class PaginateTest(unittest.TestCase):
    def test_load_more_list_is_read_to_the_end_once(self):
        async def go():
            async with _Browser() as b:
                session = await b.session(LOAD_MORE)
                return await paginate(session), session
        listing, session = run(go())
        texts = [r.text for r in listing.rows]
        self.assertEqual(len(listing.rows), 9, texts)
        self.assertTrue(texts[0].startswith("M-1001 | 40 mm"), texts[0])
        self.assertIn("(tooltip: status A)", texts[0])
        self.assertEqual(listing.pages, 4)  # three pages of rows, one click that showed nothing new
        self.assertTrue(all(r.current for r in listing.rows))
        self.assertTrue(all(r.links and r.links[0]["ref"].startswith("e") for r in listing.rows))
        self.assertEqual(listing.rows[8].links[0]["href"], "/rec/9")
        self.assertIn("M-1009", session.seen)  # every page went through look(), so grounding has seen it

    def test_next_page_list_dedupes_and_keeps_targets(self):
        async def go():
            async with _Browser() as b:
                session = await b.session(NEXT_PAGE)
                return await paginate(session)
        listing = run(go())
        self.assertEqual(len(listing.rows), 6, [r.text for r in listing.rows])
        self.assertEqual(listing.rows[0].links[0]["href"], "/msg/1")
        self.assertFalse(listing.rows[0].current)
        self.assertTrue(listing.rows[5].current)
        self.assertFalse(listing.capped)

    def test_a_ref_inside_a_row_selects_that_list(self):
        async def go():
            async with _Browser() as b:
                other = '<table id="other"><tr><td>a</td></tr><tr><td>b</td></tr></table>'
                session = await b.session(NEXT_PAGE + other)
                view = await session.look()
                import re
                ref = re.search(r'\[(e\d+) link "Subject two"', view).group(1)
                return await paginate(session, ref, max_pages=1)
        listing = run(go())
        self.assertEqual([r.text.split(" | ")[0][:11] for r in listing.rows], ["Subject one", "Subject two"])
        self.assertEqual(listing.how, "list around ref")

    def test_one_record_per_page_table_is_followed(self):
        async def go():
            async with _Browser() as b:
                session = await b.session(ONE_PER_PAGE)
                return await paginate(session)
        listing = run(go())
        self.assertEqual([r.text for r in listing.rows],
                         ["[img: DOC-001] | Guide Actions", "DOC-002 | Record 2 Actions", "DOC-003 | Record 3 Actions"])
        self.assertEqual(listing.how, "main list of the page")

    def test_page_without_a_list_says_so(self):
        async def go():
            async with _Browser() as b:
                session = await b.session("<h1>Only a heading</h1><p>and text</p>")
                return await paginate(session)
        listing = run(go())
        self.assertEqual(listing.rows, [])
        self.assertIn("no list", listing.notes[0])


class SearchTest(unittest.TestCase):
    def _search(self, query: str) -> str:
        async def go():
            async with _Browser() as b:
                session = await b.session(SEARCH)
                import re
                view = await session.look()
                ref = re.search(r'\[(e\d+) textbox "Term"', view).group(1)
                return await run_search(session, ref, query)
        return run(go())

    def test_hits_come_as_numbered_rows_with_links(self):
        out = self._search("abc")
        self.assertIn("2 rows", out)
        self.assertRegex(out, r'1\. Hit A abc 1\.234,50 \(de-DE\)  links: \[e\d+ "Hit A abc" -> /h/1\]')
        self.assertIn("2. Hit B abc 2,345.60 (en-US)", out)

    def test_too_many_hits_is_reported(self):
        out = self._search("many")
        self.assertIn("too many hits", out)
        self.assertIn("(no rows)", out)

    def test_no_hits_is_reported(self):
        out = self._search("none")
        self.assertIn("no hits", out)


class GroundTest(unittest.TestCase):
    VIEW = "Material M-1001\nStatus: 02 (tooltip: released)\nFree stock: 1.250 (de-DE) ST\n[e3 link \"Back\" -> /]"

    def test_only_quoted_values_survive(self):
        out = Extraction(fields=[
            Found(name="stock", value="1.250 (de-DE)", quote="Free stock: 1.250 (de-DE) ST"),
            Found(name="status", value="02", quote="Status: 02 (tooltip: released)"),
            Found(name="invented", value="99", quote="Blocked: 99"),
            Found(name="unquoted", value="M-1001", quote="Material number M-1001 shown"),
            Found(name="absent", value="", quote=""),
        ])
        kept, dropped = ground(out, self.VIEW)
        self.assertEqual(set(kept), {"stock", "status", "unquoted"})
        self.assertEqual(kept["stock"]["value"], "1.250 (de-DE)")
        self.assertIn("note", kept["unquoted"])
        self.assertEqual(dropped, ["invented (its quote is not on the page)", "absent (not shown)"])

    def test_readings_annotate_numbers_and_dates(self):
        self.assertEqual(readings("1.250 (de-DE)"), "number: 1250 (de)")
        self.assertIn("1.234 or 1234 (separator ambiguous)", readings("1.234"))
        self.assertEqual(readings("03/04/2027"), "date: 2027-04-03 or 2027-03-04 (day/month order ambiguous)")
        self.assertEqual(readings("25.01.2027"), "date: 2027-01-25")
        self.assertEqual(readings("42"), "")


class NumberTest(unittest.TestCase):
    def test_tagged_numbers_are_settled(self):
        self.assertEqual(read_number("1.234,56 (de-DE)").value, Decimal("1234.56"))
        self.assertEqual(read_number("1,234.56 (en-US)").value, Decimal("1234.56"))
        self.assertEqual(read_number("1,234 (en-US)").value, Decimal("1234"))
        self.assertEqual(read_number("1.234 (de-DE)").value, Decimal("1234"))
        self.assertFalse(read_number("1.234 (de-DE)").ambiguous)

    def test_hint_settles_untagged_numbers(self):
        self.assertEqual(read_number("1,234", "en-US").value, Decimal("1234"))
        self.assertEqual(read_number("1,234", "de").value, Decimal("1.234"))

    def test_two_separators_are_settled_by_the_last(self):
        self.assertEqual(read_number("12.345,6").value, Decimal("12345.6"))
        self.assertEqual(read_number("12,345.6").value, Decimal("12345.6"))

    def test_one_separator_before_three_digits_is_ambiguous(self):
        reading = read_number("1,234")
        self.assertTrue(reading.ambiguous)
        self.assertEqual(reading.readings, (Decimal("1.234"), Decimal("1234")))
        self.assertFalse(read_number("1,23").ambiguous)
        self.assertFalse(read_number("1.234.567").ambiguous)

    def test_prices_per_unit(self):
        self.assertEqual(price_unit("100 ST"), 100)
        self.assertEqual(price_unit("pro 100 Stück"), 100)
        self.assertEqual(price_unit("1"), 1)
        self.assertEqual(price_unit(""), 1)
        self.assertEqual(unit_price("1.234,50 (de-DE)", "100 ST"), Decimal("12.345"))
        self.assertEqual(unit_price("12,50 (de-DE) pro 100 Stück"), Decimal("0.125"))
        self.assertEqual(unit_price(Decimal("7.5"), 1), Decimal("7.5"))

    def test_no_number(self):
        with self.assertRaises(ParseError):
            read_number("none")


class DateTest(unittest.TestCase):
    def test_settled_orders(self):
        self.assertEqual(read_date("2027-01-25").readings, (date(2027, 1, 25),))
        self.assertEqual(read_date("25.01.2027").readings, (date(2027, 1, 25),))
        self.assertEqual(read_date("01/25/2027").readings, (date(2027, 1, 25),))  # 25 cannot be a month
        self.assertEqual(read_date("03/04/2027 (MM/TT)").readings, (date(2027, 3, 4),))
        self.assertEqual(read_date("03/04 (TT/MM) 2027").readings, (date(2027, 4, 3),))
        self.assertEqual(read_date("03/04/2027", order="MDY").readings, (date(2027, 3, 4),))

    def test_ambiguous_order_returns_both(self):
        reading = read_date("03/04/2027")
        self.assertTrue(reading.ambiguous)
        self.assertEqual(reading.readings, (date(2027, 4, 3), date(2027, 3, 4)))
        self.assertEqual(reading.order, "ambiguous")

    def test_without_a_year(self):
        reading = read_date("01/25")
        self.assertEqual(reading.partial, ((1, 25),))
        self.assertFalse(reading.ambiguous)
        self.assertEqual(read_date("02/03").partial, ((3, 2), (2, 3)))
        self.assertEqual(read_date("02/03 (MM/TT)").partial, ((2, 3),))

    def test_no_date(self):
        with self.assertRaises(ParseError):
            read_date("13/13/2027")


if __name__ == "__main__":
    unittest.main()
