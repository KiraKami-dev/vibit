"""Per-mode model timeouts: which timeout a thinking-on or thinking-off call gets, how the environment overrides
them, that the settings carry the timeout pydantic-ai forwards per request, and what the call log says."""
import asyncio
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx2  # noqa: E402
from openai._models import FinalRequestOptions  # noqa: E402

import llm  # noqa: E402

TIMEOUT_VARS = ("LLM_TIMEOUT", "LLM_TIMEOUT_THINKING", "LLM_RETRIES", "LLM_BACKEND", "LLM_MODEL")


def env(**values: str):
    """The timeout variables set exactly as given, every other one of them unset."""
    cleared = {name: "" for name in TIMEOUT_VARS}
    return patch.dict(os.environ, {**cleared, **values})


class TimeoutSelection(unittest.TestCase):
    def test_defaults_per_mode(self):
        with env():
            self.assertEqual(llm.timeout_seconds(thinking=False), llm.DEFAULT_TIMEOUT)
            self.assertEqual(llm.timeout_seconds(thinking=True), llm.DEFAULT_TIMEOUT_THINKING)
            self.assertEqual(llm.retries(), llm.DEFAULT_RETRIES)
        self.assertLess(llm.DEFAULT_TIMEOUT, llm.DEFAULT_TIMEOUT_THINKING)

    def test_environment_overrides_each_mode(self):
        with env(LLM_TIMEOUT="20", LLM_TIMEOUT_THINKING="75", LLM_RETRIES="3"):
            self.assertEqual(llm.timeout_seconds(False), 20.0)
            self.assertEqual(llm.timeout_seconds(True), 75.0)
            self.assertEqual(llm.retries(), 3)

    def test_thinking_timeout_never_below_short_one(self):
        with env(LLM_TIMEOUT="120", LLM_TIMEOUT_THINKING="60"):
            self.assertEqual(llm.timeout_seconds(True), 120.0)

    def test_unusable_values_fall_back(self):
        with env(LLM_TIMEOUT="soon", LLM_TIMEOUT_THINKING="-5", LLM_RETRIES="-1"):
            self.assertEqual(llm.timeout_seconds(False), llm.DEFAULT_TIMEOUT)
            self.assertEqual(llm.timeout_seconds(True), llm.DEFAULT_TIMEOUT_THINKING)
            self.assertEqual(llm.retries(), llm.DEFAULT_RETRIES)


class Settings(unittest.TestCase):
    def test_settings_carry_the_mode_timeout(self):
        with env(LLM_TIMEOUT="25", LLM_TIMEOUT_THINKING="80"):
            off, on = llm.settings(), llm.settings(thinking=True)
        self.assertEqual(off["timeout"], 25.0)
        self.assertEqual(on["timeout"], 80.0)
        self.assertFalse(off["extra_body"]["chat_template_kwargs"]["enable_thinking"])
        self.assertTrue(on["extra_body"]["chat_template_kwargs"]["enable_thinking"])
        self.assertFalse(on["parallel_tool_calls"])

    def test_explicit_timeout_wins(self):
        with env(LLM_TIMEOUT_THINKING="80"):
            self.assertEqual(llm.settings(thinking=True, timeout=7)["timeout"], 7)


class Client(unittest.TestCase):
    def test_client_uses_short_timeout_and_retries(self):
        with env(LLM_TIMEOUT="22", LLM_RETRIES="4"), patch.dict(
                os.environ, {"QWEN_BASE_URL": "http://models.example/v1", "QWEN_API_KEY": "k", "AGENT_MODEL": ""}):
            client = llm.model().client
        self.assertEqual(client.max_retries, 4)
        self.assertEqual(client.timeout, 22.0)
        self.assertEqual(str(client.base_url), "http://models.example/v1/")

    def test_per_request_timeout_reaches_the_request(self):
        """The timeout in the settings is what pydantic-ai hands the OpenAI client for each attempt; the client
        writes it into a request header, which is also what the call log reports."""
        with env(LLM_TIMEOUT="22"), patch.dict(
                os.environ, {"QWEN_BASE_URL": "http://models.example/v1", "QWEN_API_KEY": "k"}):
            client = llm.model().client
        request = client._build_request(FinalRequestOptions(method="post", url="/chat/completions",
                                                            timeout=llm.settings(thinking=True)["timeout"]))
        self.assertEqual(request.headers["x-stainless-read-timeout"], str(float(llm.DEFAULT_TIMEOUT_THINKING)))
        plain = client._build_request(FinalRequestOptions(method="post", url="/chat/completions"))
        self.assertEqual(plain.headers["x-stainless-read-timeout"], "22.0")


class Deadline(unittest.TestCase):
    """The proxy answers a slow call with early headers and then a newline every few seconds; the timeout must
    still end the call, so the OpenAI client can retry it."""

    @staticmethod
    def call(headers_after: float, newlines: int, every: float, timeout: float) -> httpx2.Response:
        class Trickle(httpx2.AsyncByteStream):
            async def __aiter__(self):
                for _ in range(newlines):
                    await asyncio.sleep(every)
                    yield b"\n"
                yield b"{}"

        async def handler(request: httpx2.Request) -> httpx2.Response:
            await asyncio.sleep(headers_after)
            return httpx2.Response(200, stream=Trickle())

        async def run() -> httpx2.Response:
            async with llm._DeadlineClient(transport=httpx2.MockTransport(handler), timeout=timeout) as client:
                return await client.post("http://models.example/v1/chat/completions", content=b"{}")

        with redirect_stdout(io.StringIO()):
            return asyncio.run(run())

    def test_trickling_body_times_out_at_the_deadline(self):
        with self.assertRaises(httpx2.ReadTimeout):
            self.call(headers_after=0.05, newlines=20, every=0.05, timeout=0.3)

    def test_late_headers_time_out(self):
        with self.assertRaises(httpx2.ReadTimeout):
            self.call(headers_after=1.0, newlines=0, every=0, timeout=0.2)

    def test_call_within_deadline_is_read_whole(self):
        response = self.call(headers_after=0.05, newlines=3, every=0.02, timeout=2)
        self.assertEqual(response.content, b"\n\n\n{}")


class CallLog(unittest.TestCase):
    @staticmethod
    def request(retry: str | None, thinking: bool) -> httpx2.Request:
        headers = {"x-stainless-read-timeout": "90.0"}
        if retry is not None:
            headers["x-stainless-retry-count"] = retry
        body = b'{"chat_template_kwargs": {"enable_thinking": %s}}' % (b"true" if thinking else b"false")
        return httpx2.Request("POST", "http://models.example/v1/chat/completions", headers=headers, content=body)

    def log(self, request: httpx2.Request, status: int | None = None) -> str:
        out = io.StringIO()
        with env(LLM_RETRIES="6"), redirect_stdout(out):
            asyncio.run(llm._sent(request))
            if status is not None:
                asyncio.run(self.answer(request, status))
        return out.getvalue()

    @staticmethod
    async def answer(request: httpx2.Request, status: int) -> None:
        class Body(httpx2.AsyncByteStream):
            async def __aiter__(self):
                yield b"{}"

        response = httpx2.Response(status, request=request, stream=Body())
        await llm._answered(response)  # what the client does: headers first, then the body is read
        await response.aread()

    def test_first_attempt_is_silent_until_the_body_is_in(self):
        self.assertEqual(self.log(self.request("0", thinking=False)), "")
        line = self.log(self.request("0", thinking=False), status=200)
        self.assertRegex(line, r"^\[model\] 200 \d+\.\ds \(headers \d+\.\ds\) \d+ bytes thinking off\n$")

    def test_retry_is_logged_with_its_timeout(self):
        line = self.log(self.request("2", thinking=True))
        self.assertEqual(line, "[model] retry 2/6 thinking on timeout 90.0s\n")
        line = self.log(self.request("2", thinking=True), status=200)
        self.assertIn(" thinking on retry 2\n", line)


if __name__ == "__main__":
    unittest.main()
