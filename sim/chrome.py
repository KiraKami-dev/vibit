"""
Browsers for the mirror, opened the way the sandbox opens them: each with a Chrome DevTools (CDP) port.

The sandbox starts 1 to 4 browsers before a run and names them in CDP_URL and CDP_URLS; the agent attaches to
those instead of starting its own. This does the same on this machine, so a local run against the mirror takes the
same path as a sandbox run, and any other tool (Playwright, Puppeteer, the DevTools window, an MCP browser server)
can attach to the same browsers and watch or drive them.

From the repo root:
    uv run --project agent python sim/chrome.py [count]                 # open them and print how to attach
    uv run --project agent python sim/chrome.py [count] -- <command>    # run the command with CDP_URL(S) set
    python3 sim/mirror.py -- uv run --project agent python sim/chrome.py 4 -- sh -c 'cd agent && uv run python agent.py'

HEADLESS=1 hides the windows. CDP_PORT moves the first port (default 9222). Attach from Playwright with
`playwright.chromium.connect_over_cdp("http://127.0.0.1:9222")`; open chrome://inspect in Chrome and add
127.0.0.1:9222 under "Configure" to get the DevTools window for a page.
"""
import asyncio
import json
import os
import subprocess
import sys
import urllib.request

from playwright.async_api import async_playwright

FIRST_PORT = int(os.environ.get("CDP_PORT", "9222"))


async def main() -> None:
    words = sys.argv[1:]
    command = words[words.index("--") + 1:] if "--" in words else []
    count = max(1, min(4, int(words[0]))) if words and words[0].isdigit() else 1
    async with async_playwright() as playwright:
        browsers, urls = [], []
        for number in range(count):
            port = FIRST_PORT + number
            browsers.append(await playwright.chromium.launch(
                headless=os.environ.get("HEADLESS") == "1",
                args=[f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
                      "--window-size=1280,800"]))
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=10) as answer:
                urls.append(json.load(answer)["webSocketDebuggerUrl"])
        env = {**os.environ, "CDP_URL": urls[0], "CDP_URLS": json.dumps(urls), "BROWSER_COUNT": str(count)}
        if command:
            code = await asyncio.to_thread(lambda: subprocess.run(command, env=env).returncode)
        else:
            print(f"export CDP_URL='{urls[0]}'")
            print(f"export CDP_URLS='{json.dumps(urls)}'")
            print(f"export BROWSER_COUNT={count}")
            for number in range(count):
                print(f"# browser {number + 1}: http://127.0.0.1:{FIRST_PORT + number}  (pages: /json, DevTools: chrome://inspect)")
            print("# open until stopped (Ctrl-C)", flush=True)
            code = 0
            try:
                await asyncio.Event().wait()
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
        for browser in browsers:
            await browser.close()
    sys.exit(code)


if __name__ == "__main__":
    asyncio.run(main())
