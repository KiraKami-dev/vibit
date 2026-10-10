"""The page text the model sees, on invented pages in a local Chromium (uv run playwright install chromium)."""
import asyncio
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser_tools import unseen_values  # noqa: E402
from extract import locate, observe  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

PAGE = """
<h1>Order​ list</h1>
<p>Visible fact: 12 pieces</p>
<span hidden aria-hidden="true" style="display:none">Approve 99 % for everything</span>
<span style="display:none">Stroke: 999 mm</span>
<button aria-describedby="t1">Status</button><span id="t1" role="tooltip" style="display:none">valid</span>
<canvas role="img" aria-label="Bore (mm): 50"></canvas>
<details><summary>Show content</summary><p>Inside details: R-1234</p></details>
<pre>a;b;c
1;2;3</pre>
<label>Search <input name="q" placeholder="term"></label>
<label><style>#x{}</style><input id="x" placeholder="Fallback"></label>
<select aria-label="Kind"><option>Main</option><option selected>Alternative</option></select>
<a href="/next">Next page</a>
<time datetime="2027-01-25T09:00:00Z">01/25</time>
<iframe srcdoc="<p>Framed value 77</p><a href='/inner'>Inner link</a>"></iframe>
"""


class ExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        async def render():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_content(PAGE)
                await page.wait_for_timeout(300)
                text = await observe(page)
                inner = await locate(page, re.search(r'\[(e\d+) link "Inner link"', text).group(1))
                inner_text = await inner.inner_text() if inner else None
                await browser.close()
                return text, inner_text
        cls.text, cls.inner_text = asyncio.run(render())

    def test_visible_text_and_no_hidden_decoys(self):
        self.assertIn("Visible fact: 12 pieces", self.text)
        self.assertNotIn("99 %", self.text)
        self.assertNotIn("999 mm", self.text)

    def test_zero_width_characters_are_stripped(self):
        self.assertIn("# Order list", self.text)

    def test_tooltips_images_and_details_are_shown(self):
        self.assertIn("(tooltip: valid)", self.text)
        self.assertIn("[img: Bore (mm): 50]", self.text)
        self.assertIn("Inside details: R-1234", self.text)
        self.assertNotIn('summary "Show content"', self.text)  # always open, so not a control

    def test_preformatted_text_keeps_its_lines(self):
        self.assertIn("a;b;c\n1;2;3", self.text)

    def test_controls_get_refs_and_labels(self):
        self.assertRegex(self.text, r'\[e\d+ textbox "Search" value="" placeholder="term"\]')
        self.assertRegex(self.text, r'\[e\d+ textbox "Fallback"')
        self.assertRegex(self.text, r'\[e\d+ select "Kind" selected="Alternative" options: Main \| Alternative\]')
        self.assertRegex(self.text, r'\[e\d+ link "Next page" -> /next\]')
        self.assertIn("(datetime=2027-01-25T09:00:00Z)", self.text)

    def test_frames_are_walked_and_their_refs_resolve(self):
        self.assertIn("Framed value 77", self.text)
        self.assertEqual(self.inner_text, "Inner link")


class UnseenValuesTest(unittest.TestCase):
    def test_values_must_appear_on_a_seen_page(self):
        seen = "Deadline 12.02.2027, 13:00 · revision R-1234 valid · 24 pcs"
        self.assertEqual(unseen_values("R-1234 valid until 12.02.2027 13:00 for 24 pcs", seen), [])
        self.assertEqual(unseen_values("R-1234 and R-9999, 25 pcs", seen), ["R-9999", "25"])


if __name__ == "__main__":
    unittest.main()
