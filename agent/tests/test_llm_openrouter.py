"""The optional OpenRouter backend, without a model: selection, base URL, provider pinning, the thinking mapping
onto OpenRouter's `reasoning`, and the request a typed agent sends (checked against a fake chat completions API)."""
import asyncio
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx2  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.providers.openai import OpenAIProvider  # noqa: E402

import llm  # noqa: E402

VARS = ("LLM_BACKEND", "LLM_MODEL", "AGENT_MODEL", "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "LLM_PROXY_URL",
        "LLM_PROVIDER", "LLM_EFFORT", "LLM_EFFORT_OFF", "LLM_TIMEOUT", "LLM_TIMEOUT_THINKING", "LLM_RETRIES",
        "LLM_STREAM_WATCHDOG")


class env:
    """The backend variables set exactly as given, every other one of them unset."""

    def __init__(self, **values: str):
        self.values = values
        self.patcher = patch.dict(os.environ, {})

    def __enter__(self):
        self.patcher.start()
        for name in VARS:
            os.environ.pop(name, None)
        os.environ.update(self.values)
        llm._WARNED.clear()

    def __exit__(self, *exc):
        self.patcher.stop()


KEYED = {"LLM_BACKEND": "openrouter", "OPENROUTER_API_KEY": "sk-or-test"}


class Selection(unittest.TestCase):
    def test_openrouter_with_key(self):
        with env(**KEYED):
            self.assertEqual(llm.backend(), "openrouter")
            self.assertEqual(llm.model_name(), "z-ai/glm-5.3-flash")
        with env(**KEYED, LLM_MODEL="z-ai/other"):
            self.assertEqual(llm.model_name(), "z-ai/other")

    def test_without_key_falls_back_to_qwen(self):
        out = io.StringIO()
        with env(LLM_BACKEND="openrouter"), redirect_stdout(out):
            self.assertEqual(llm.backend(), "qwen")
        self.assertIn("OPENROUTER_API_KEY is not set", out.getvalue())

    def test_base_url(self):
        with env():
            self.assertEqual(llm.openrouter_base_url(), "https://openrouter.ai/api/v1")
        with env(LLM_PROXY_URL="https://api.example.com/llm/run/token/"):
            self.assertEqual(llm.openrouter_base_url(), "https://api.example.com/llm/run/token/openrouter/v1")
        with env(LLM_PROXY_URL="https://api.example.com/llm/run/token",
                 OPENROUTER_BASE_URL="https://proxy.example.com/v1"):
            self.assertEqual(llm.openrouter_base_url(), "https://proxy.example.com/v1")

    def test_provider_pinning(self):
        with env():
            self.assertEqual(llm.openrouter_provider(), {"order": ["fireworks"], "allow_fallbacks": False})
        with env(LLM_PROVIDER="together, fireworks"):
            self.assertEqual(llm.openrouter_provider()["order"], ["together", "fireworks"])
        with env(LLM_PROVIDER="any"):
            self.assertIsNone(llm.openrouter_provider())

    def test_client(self):
        with env(**KEYED, LLM_TIMEOUT="22", LLM_RETRIES="4", LLM_PROXY_URL="https://api.example.com/llm/run/t"):
            model = llm.model()
        self.assertIsInstance(model, llm._WatchdogModel)
        self.assertEqual(model.model_name, "z-ai/glm-5.3-flash")
        self.assertEqual(model.client.max_retries, 4)
        self.assertEqual(model.client.timeout, 22.0)
        self.assertEqual(str(model.client.base_url), "https://api.example.com/llm/run/t/openrouter/v1/")


class Thinking(unittest.TestCase):
    def test_settings(self):
        with env(**KEYED, LLM_TIMEOUT="25", LLM_TIMEOUT_THINKING="80"):
            off, on = llm.settings(), llm.settings(thinking=True)
        self.assertEqual(off["extra_body"]["reasoning"], {"effort": "low", "exclude": True})
        self.assertEqual(on["extra_body"]["reasoning"], {"effort": "medium"})
        for settings in (off, on):
            self.assertEqual(settings["extra_body"]["usage"], {"include": True})
            self.assertEqual(settings["extra_body"]["provider"], {"order": ["fireworks"], "allow_fallbacks": False})
            self.assertNotIn("chat_template_kwargs", settings["extra_body"])
            self.assertFalse(settings["parallel_tool_calls"])
            self.assertNotIn("temperature", settings)
            self.assertEqual(settings["max_tokens"], llm.DEFAULT_MAX_TOKENS)
        self.assertEqual((off["timeout"], on["timeout"]), (25.0, 80.0))
        self.assertFalse(llm.settings_thinking(off))
        self.assertTrue(llm.settings_thinking(on))

    def test_any_provider_sends_no_routing(self):
        with env(**KEYED, LLM_PROVIDER="any"):
            self.assertNotIn("provider", llm.settings()["extra_body"])

    def test_call_log_reads_the_mode(self):
        def mode(reasoning: dict) -> str:
            body = json.dumps({"reasoning": reasoning}).encode()
            return llm._thinking(httpx2.Request("POST", "http://models.example/v1", content=body))

        self.assertEqual(mode({"effort": "medium"}), "on")
        self.assertEqual(mode({"effort": "low", "exclude": True}), "off")


class Answer(BaseModel):
    city: str
    count: int


class Request(unittest.TestCase):
    """A typed agent on the OpenRouter backend against a fake chat completions API."""

    def run_agent(self, thinking: bool) -> tuple[Answer, dict, httpx2.Headers, str]:
        seen: dict = {}

        def handler(request: httpx2.Request) -> httpx2.Response:
            body = json.loads(request.content)
            seen["body"], seen["headers"], seen["url"] = body, request.headers, str(request.url)
            tool = body["tools"][0]["function"]["name"]
            return httpx2.Response(200, json={
                "id": "gen-1", "object": "chat.completion", "created": 1, "model": body["model"],
                "provider": "Fireworks",
                "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": None, "tool_calls": [{
                        "id": "call_1", "type": "function",
                        "function": {"name": tool, "arguments": json.dumps({"city": "Example", "count": 3})}}]}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.00001}})

        with env(**KEYED, LLM_PROXY_URL="http://api.example.com/llm/run/t"):
            client = AsyncOpenAI(base_url=llm.openrouter_base_url(), api_key="sk-or-test", max_retries=0,
                                 http_client=llm._DeadlineClient(transport=httpx2.MockTransport(handler)))
            model = llm._WatchdogModel(llm.model_name(), provider=OpenAIProvider(openai_client=client))
            agent = Agent(model, output_type=Answer, instructions="Answer with the city.",
                          model_settings=llm.settings(thinking))
            with redirect_stdout(io.StringIO()):
                result = asyncio.run(agent.run("Which city?"))
        return result.output, seen["body"], seen["headers"], seen["url"]

    def test_request_shape(self):
        for thinking, reasoning in ((False, {"effort": "low", "exclude": True}), (True, {"effort": "medium"})):
            output, body, headers, url = self.run_agent(thinking)
            self.assertEqual(output, Answer(city="Example", count=3))
            self.assertEqual(url, "http://api.example.com/llm/run/t/openrouter/v1/chat/completions")
            self.assertEqual(headers["authorization"], "Bearer sk-or-test")
            self.assertEqual(body["model"], "z-ai/glm-5.3-flash")
            self.assertEqual(body["reasoning"], reasoning)
            self.assertEqual(body["provider"], {"order": ["fireworks"], "allow_fallbacks": False})
            self.assertEqual(body["usage"], {"include": True})
            self.assertFalse(body["parallel_tool_calls"])
            self.assertNotIn("temperature", body)
            self.assertEqual(body.get("max_completion_tokens", body.get("max_tokens")), llm.DEFAULT_MAX_TOKENS)
            self.assertTrue(body["tools"])


if __name__ == "__main__":
    unittest.main()
