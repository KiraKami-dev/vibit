"""A session whose tabs are all gone opens the site again instead of failing (local Chromium)."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from browser_tools import SiteSession  # noqa: E402
from tracing import Trace  # noqa: E402


class ReviveTest(unittest.TestCase):
    def test_look_reopens_a_closed_tab(self):
        async def run():
            page_file = Path(tempfile.mkdtemp()) / "start.html"
            page_file.write_text("<h1>Start page</h1><a href='#x'>Next</a>", encoding="utf-8")
            site = {"siteId": "site-a", "url": page_file.as_uri(), "credentials": {"username": "u", "password": "p"}}
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                context = await browser.new_context()
                session = SiteSession(site, context, Trace(tempfile.mkdtemp()))
                await session.start()
                for page in session.pages:
                    await page.close()
                view = await session.look()
                await browser.close()
                return view
        view = asyncio.run(run())
        self.assertIn("Start page", view)
        self.assertIn("opened the website again", view)


if __name__ == "__main__":
    unittest.main()
