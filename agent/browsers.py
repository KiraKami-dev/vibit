"""The run's browsers: attach over CDP and get a page. Driving them is up to you.

In a run the sandbox starts the browsers (agent.json "browsers": 1 to 4, default 1) before the clock starts and sets
CDP_URL (browser 1) and CDP_URLS (all of them, as a JSON array). Use those: the sandbox stops browsers your agent
starts itself. On your own machine there is no CDP_URLS, so `connect` starts a local Chromium (once:
`uv run playwright install chromium`; HEADLESS=1 hides it). Docs: $API/docs/building-an-agent.md
"""
import json
import os


def cdp_urls(env=os.environ) -> list[str]:
    """Every browser of the run, in order; [] on your own machine."""
    try:
        urls = [url for url in json.loads(env.get("CDP_URLS") or "[]") if isinstance(url, str)]
    except ValueError:
        urls = []
    return urls or ([env["CDP_URL"]] if env.get("CDP_URL") else [])


async def connect(playwright, number: int = 0):
    """Browser `number` of the run (0 is browser 1) and a page in it. Locally, a Chromium of its own."""
    urls = cdp_urls()
    if urls:
        browser = await playwright.chromium.connect_over_cdp(urls[number])
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
    else:
        browser = await playwright.chromium.launch(headless=os.environ.get("HEADLESS") == "1")
        context = await browser.new_context()
    page = context.pages[0] if context.pages else await context.new_page()
    return browser, page


async def contexts(playwright, count: int) -> list:
    """`count` browser contexts to work websites in, spread over the run's browsers (round robin). Locally they are
    separate contexts of one Chromium of its own, at the sandbox's 1280×800."""
    urls = cdp_urls()
    if urls:
        browsers = [await playwright.chromium.connect_over_cdp(url) for url in urls[:count]]
        found = []
        for i in range(count):
            browser = browsers[i % len(browsers)]
            found.append(browser.contexts[0] if browser.contexts else await browser.new_context())
        return found
    browser = await playwright.chromium.launch(headless=os.environ.get("HEADLESS") == "1")
    return [await browser.new_context(viewport={"width": 1280, "height": 800}) for _ in range(count)]


async def more_contexts(page, count: int) -> list:
    """Browser contexts for `count` websites, the first being `page`'s. In a run, website n gets browser n where the
    run has one (agent.json "browsers"); with fewer browsers than websites the remaining websites share the
    browsers' contexts in turn (different websites do not get in each other's way in one context). Locally,
    contexts of the kit's own Chromium at the sandbox's 1280×800."""
    urls = cdp_urls()
    found = [page.context]
    for number in range(1, count):
        if number < len(urls):
            browser = await page.context.browser.browser_type.connect_over_cdp(urls[number])
            found.append(browser.contexts[0] if browser.contexts else await browser.new_context())
        elif urls:
            found.append(found[number % len(urls)])
        else:
            found.append(await page.context.browser.new_context(viewport={"width": 1280, "height": 800}))
    return found
