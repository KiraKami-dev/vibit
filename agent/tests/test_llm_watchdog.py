"""
The stream watchdog (LLM_STREAM_WATCHDOG=1) against fake streamed answers: a normal stream, a call that never
starts, a keep-alive trickle without tokens, a slow stream that keeps progressing, a tool call the proxy holds back
until it is complete, and hedging.
"""
import asyncio
import io
import json
import os
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx2  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402
from pydantic_ai.exceptions import ModelAPIError  # noqa: E402
from pydantic_ai.messages import ModelRequest, TextPart, ThinkingPart, ToolCallPart  # noqa: E402
from pydantic_ai.models import ModelRequestParameters  # noqa: E402
from pydantic_ai.providers.openai import OpenAIProvider  # noqa: E402
from pydantic_ai.tools import ToolDefinition  # noqa: E402

import llm  # noqa: E402


def chunk(delta: dict, finish: str | None = None) -> bytes:
    body = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return b"data: " + json.dumps(body).encode() + b"\n\n"


ROLE = chunk({"role": "assistant", "content": ""})
DONE = (chunk({}, "stop")
        + b'data: {"id":"c1","object":"chat.completion.chunk","created":0,"model":"m","choices":[],'
          b'"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}\n\n'
        + b"data: [DONE]\n\n")
KEEPALIVE = b": keepalive\n\n"


def text(*words: str) -> list[tuple[float, bytes]]:
    return [(0, ROLE), *((0, chunk({"content": w})) for w in words), (0, DONE)]


class Script(httpx2.AsyncByteStream):
    """A response body that sends each piece after its delay."""

    def __init__(self, pieces: list[tuple[float, bytes]]) -> None:
        self.pieces = pieces

    async def __aiter__(self):
        for delay, piece in self.pieces:
            await asyncio.sleep(delay)
            yield piece


class Fake:
    """The proxy: answers the n-th call with the n-th script (headers after `headers` seconds; None = never)."""

    def __init__(self, *calls: tuple[float | None, list[tuple[float, bytes]]]) -> None:
        self.calls = list(calls)
        self.count = 0

    async def handler(self, request: httpx2.Request) -> httpx2.Response:
        headers, pieces = self.calls[min(self.count, len(self.calls) - 1)]
        self.count += 1
        if headers is None:
            await asyncio.sleep(3600)
        await asyncio.sleep(headers)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=Script(pieces))


WATCH = {"LLM_STREAM_WATCHDOG": "1", "LLM_FIRST_TOKEN_TIMEOUT": "0.3", "LLM_IDLE_TIMEOUT": "0.2",
         "LLM_MAX_CALL": "5", "LLM_RETRIES": "2", "LLM_HEDGE_AFTER": "", "LLM_TIMEOUT": "0.6",
         "LLM_TIMEOUT_THINKING": "0.6", "LLM_BACKEND": ""}
TOOL = ToolDefinition(name="answer", parameters_json_schema={"type": "object", "properties": {}})


def run(fake: Fake, *, tools: bool = False, **env: str):
    """One model request through the watchdog; returns the response, the seconds it took and the log."""
    async def go():
        http = llm._DeadlineClient(transport=httpx2.MockTransport(fake.handler))
        client = AsyncOpenAI(base_url="http://models.example/v1", api_key="k", max_retries=0, http_client=http)
        model = llm._WatchdogModel("m", provider=OpenAIProvider(openai_client=client))
        params = ModelRequestParameters(output_tools=[TOOL] if tools else [], allow_text_output=not tools)
        return await model.request([ModelRequest.user_text_prompt("hi")], llm.settings(), params)

    out = io.StringIO()
    started = time.monotonic()
    with patch.dict(os.environ, {**WATCH, **env}), redirect_stdout(out):
        response = asyncio.run(go())
    return response, time.monotonic() - started, out.getvalue()


class Watchdog(unittest.TestCase):
    def setUp(self):
        backoff = patch.object(llm, "_backoff", lambda attempt: 0.0)
        backoff.start()
        self.addCleanup(backoff.stop)

    def test_normal_stream_becomes_one_response(self):
        fake = Fake((0, text("Hel", "lo")))
        response, _, log = run(fake)
        self.assertEqual([p.content for p in response.parts if isinstance(p, TextPart)], ["Hello"])
        self.assertEqual(response.usage.output_tokens, 3)
        self.assertEqual(fake.count, 1)
        self.assertNotIn("stalled", log)

    def test_call_that_never_starts_is_retried_after_the_first_token_timeout(self):
        fake = Fake((None, []), (0, text("ok")))
        response, seconds, log = run(fake)
        self.assertEqual(response.parts[0].content, "ok")
        self.assertEqual(fake.count, 2)
        self.assertIn("no first chunk within 0.3s; retry 1/2", log)
        self.assertLess(seconds, 1.5)

    def test_keepalive_trickle_without_tokens_is_retried(self):
        trickle = [(0, ROLE), *((0.05, KEEPALIVE) for _ in range(40)), (0, DONE)]
        fake = Fake((0, trickle), (0, text("ok")))
        response, seconds, log = run(fake)
        self.assertEqual(response.parts[0].content, "ok")
        self.assertEqual(fake.count, 2)
        self.assertIn("no progress for 0.2s", log)
        self.assertLess(seconds, 1.5)  # the trickle alone would take 2 s

    def test_slow_but_progressing_stream_is_never_cut(self):
        """Tokens 0.15 s apart for 1.5 s: longer than the call's timeout (0.6 s), each gap below the idle limit."""
        slow = [(0, ROLE), *((0.15, chunk({"content": "x"})) for _ in range(10)), (0, DONE)]
        fake = Fake((0, slow))
        response, seconds, log = run(fake)
        self.assertEqual(response.parts[0].content, "x" * 10)
        self.assertEqual(fake.count, 1)
        self.assertGreater(seconds, 1.4)
        self.assertNotIn("stalled", log)

    def test_reasoning_counts_as_progress(self):
        thinking = [(0, ROLE), *((0.15, chunk({"reasoning_content": "hm "})) for _ in range(6)),
                    (0.15, chunk({"content": "done"})), (0, DONE)]
        fake = Fake((0, thinking))
        response, _, log = run(fake)
        self.assertEqual(fake.count, 1)
        self.assertTrue(any(isinstance(p, ThinkingPart) for p in response.parts))
        self.assertNotIn("stalled", log)

    def test_held_back_tool_call_gets_the_call_timeout(self):
        """The proxy sends a tool call only once it is complete: silence past the idle limit but within the call's
        timeout (0.6 s) is no stall when the request has tools."""
        call = {"tool_calls": [{"index": 0, "id": "t1", "type": "function",
                                "function": {"name": "answer", "arguments": "{}"}}]}
        held = [(0, ROLE), *((0.1, KEEPALIVE) for _ in range(4)), (0, chunk(call)), (0, DONE)]
        response, _, log = run(Fake((0, held)), tools=True)
        self.assertIsInstance(response.parts[0], ToolCallPart)
        self.assertNotIn("stalled", log)
        fake = Fake((0, [(0, ROLE), *((0.1, KEEPALIVE) for _ in range(20)), (0, DONE)]), (0, held))
        response, _, log = run(fake, tools=True)
        self.assertEqual(fake.count, 2)
        self.assertIn("no progress for 0.6s", log)

    def test_gives_up_after_the_retries(self):
        fake = Fake((None, []))
        with self.assertRaises(ModelAPIError) as caught:
            run(fake)
        self.assertEqual(fake.count, 3)
        self.assertIn("failed 3 times", str(caught.exception))

    def test_hedge_copy_wins_when_the_first_hangs(self):
        fake = Fake((None, []), (0, text("ok")))
        response, seconds, log = run(fake, LLM_HEDGE_AFTER="0.1")
        self.assertEqual(response.parts[0].content, "ok")
        self.assertEqual(fake.count, 2)
        self.assertIn("sending a hedge copy", log)
        self.assertIn("the hedge copy started first", log)
        self.assertNotIn("stalled", log)
        self.assertLess(seconds, 0.3)

    def test_hedging_is_on_by_default_and_capped(self):
        with patch.dict(os.environ, {"LLM_HEDGE_AFTER": ""}):
            self.assertEqual(llm.hedge_seconds(), llm.DEFAULT_HEDGE)
        with patch.dict(os.environ, {"LLM_HEDGE_AFTER": "0"}):
            self.assertEqual(llm.hedge_seconds(), 0.0)
        fake = Fake((None, []), (0, text("ok")))
        with patch.object(llm, "_hedges", llm.MAX_HEDGES):  # every second copy allowed at one time is out
            response, _, log = run(fake, LLM_HEDGE_AFTER="0.1")
        self.assertNotIn("sending a hedge copy", log)
        self.assertIn("stalled", log)  # the call waits for its first copy and is retried as before
        self.assertEqual(response.parts[0].content, "ok")
        run(Fake((None, []), (0, text("ok"))), LLM_HEDGE_AFTER="0.1")
        self.assertEqual(llm._hedges, 0)  # a finished call gives its second copy back

    def test_watchdog_off_keeps_plain_requests(self):
        body = json.dumps({"id": "c1", "object": "chat.completion", "created": 0, "model": "m", "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "plain"}, "finish_reason": "stop"}]}).encode()
        fake = Fake((0, [(0, body)]))
        response, _, _ = run(fake, LLM_STREAM_WATCHDOG="0")
        self.assertEqual(response.parts[0].content, "plain")


if __name__ == "__main__":
    unittest.main()
