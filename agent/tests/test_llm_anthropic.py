"""The optional Anthropic backend, without a model: which backend and model the environment selects, how thinking
on/off maps to Anthropic's thinking and effort, the settings and client it builds, and the request a typed agent
sends (checked against a fake Messages API)."""
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
from anthropic import AsyncAnthropic  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ThinkingPart  # noqa: E402
from pydantic_ai.models.anthropic import AnthropicModel  # noqa: E402
from pydantic_ai.providers.anthropic import AnthropicProvider  # noqa: E402

import llm  # noqa: E402

VARS = ("LLM_BACKEND", "LLM_MODEL", "AGENT_MODEL", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "LLM_PROXY_URL",
        "LLM_EFFORT", "LLM_EFFORT_OFF", "LLM_MAX_TOKENS", "LLM_TIMEOUT", "LLM_TIMEOUT_THINKING", "LLM_RETRIES",
        "LLM_STREAM_WATCHDOG")


def env(**values: str):
    """The backend variables set exactly as given, every other one of them unset."""
    cleared = {name: "" for name in VARS}
    patcher = patch.dict(os.environ, {**cleared, **values})

    class Scope:
        def __enter__(self):
            patcher.start()
            for name in VARS:  # unset rather than empty, as on a fresh machine
                if name not in values:
                    os.environ.pop(name, None)
            llm._WARNED.clear()

        def __exit__(self, *exc):
            patcher.stop()

    return Scope()


class Backend(unittest.TestCase):
    def test_default_is_qwen(self):
        with env():
            self.assertEqual(llm.backend(), "qwen")
            self.assertEqual(llm.model_name(), llm.QWEN_MODEL)

    def test_anthropic_with_key(self):
        with env(LLM_BACKEND="anthropic", ANTHROPIC_API_KEY="sk-ant-test"):
            self.assertEqual(llm.backend(), "anthropic")
            self.assertEqual(llm.model_name(), "claude-haiku-5-5")
        with env(LLM_BACKEND="Claude", ANTHROPIC_API_KEY="sk-ant-test", LLM_MODEL="claude-sonnet-5-5"):
            self.assertEqual(llm.backend(), "anthropic")
            self.assertEqual(llm.model_name(), "claude-sonnet-5-5")

    def test_anthropic_without_key_falls_back_to_qwen(self):
        out = io.StringIO()
        with env(LLM_BACKEND="anthropic"), redirect_stdout(out):
            self.assertEqual(llm.backend(), "qwen")
            self.assertEqual(llm.backend(), "qwen")
            self.assertEqual(llm.model_name(), llm.QWEN_MODEL)
        self.assertEqual(out.getvalue().count("ANTHROPIC_API_KEY is not set"), 1)

    def test_unknown_backend_falls_back_to_qwen(self):
        with env(LLM_BACKEND="other", ANTHROPIC_API_KEY="sk-ant-test"), redirect_stdout(io.StringIO()):
            self.assertEqual(llm.backend(), "qwen")

    def test_qwen_model_overrides(self):
        with env(AGENT_MODEL="qwen-other"):
            self.assertEqual(llm.model_name(), "qwen-other")
        with env(AGENT_MODEL="qwen-other", LLM_MODEL="qwen-third"):
            self.assertEqual(llm.model_name(), "qwen-third")

    def test_base_url(self):
        with env():
            self.assertIsNone(llm.anthropic_base_url())
        with env(LLM_PROXY_URL="https://api.example.com/llm/run/token/"):
            self.assertEqual(llm.anthropic_base_url(), "https://api.example.com/llm/run/token/anthropic")
        with env(LLM_PROXY_URL="https://api.example.com/llm/run/token",
                 ANTHROPIC_BASE_URL="https://proxy.example.com/anthropic"):
            self.assertEqual(llm.anthropic_base_url(), "https://proxy.example.com/anthropic")


class Thinking(unittest.TestCase):
    def test_on_is_adaptive_with_effort_and_drop_block(self):
        with env():
            config, effort = llm.anthropic_thinking("claude-haiku-5-5", True)
        self.assertEqual(config, {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}})
        self.assertEqual(effort, "medium")
        with env(LLM_EFFORT="low"):
            self.assertEqual(llm.anthropic_thinking("claude-sonnet-5-5", True)[1], "low")
        with env(LLM_EFFORT="loud"):
            self.assertEqual(llm.anthropic_thinking("claude-haiku-5-5", True)[1], "medium")

    def test_off_per_model(self):
        with env():
            self.assertEqual(llm.anthropic_thinking("claude-haiku-5-5", False), ({"type": "disabled"}, None))
            self.assertEqual(llm.anthropic_thinking("claude-sonnet-5-5", False), ({"type": "between_tools"}, None))
            config, effort = llm.anthropic_thinking("claude-opus-5-5", False)
        self.assertEqual(config["type"], "adaptive")
        self.assertEqual(effort, "low")


class Settings(unittest.TestCase):
    def test_qwen_settings_unchanged(self):
        with env():
            settings = llm.settings(thinking=True)
        self.assertTrue(settings["extra_body"]["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual(settings["temperature"], 0.2)

    def test_anthropic_settings(self):
        with env(LLM_BACKEND="anthropic", ANTHROPIC_API_KEY="sk-ant-test", LLM_TIMEOUT="25",
                 LLM_TIMEOUT_THINKING="80"):
            off, on, fixed = llm.settings(), llm.settings(thinking=True), llm.settings(True, timeout=7)
        for settings in (off, on):
            self.assertNotIn("temperature", settings)
            self.assertNotIn("extra_body", settings)
            self.assertFalse(settings["parallel_tool_calls"])
            self.assertEqual(settings["max_tokens"], llm.DEFAULT_MAX_TOKENS)
            self.assertTrue(settings["anthropic_cache_messages"])
        self.assertEqual(off["timeout"], 25.0)
        self.assertEqual(off["anthropic_thinking"], {"type": "disabled"})
        self.assertNotIn("anthropic_effort", off)
        self.assertNotIn("anthropic_betas", off)
        self.assertEqual(on["timeout"], 80.0)
        self.assertEqual(on["anthropic_thinking"]["type"], "adaptive")
        self.assertEqual(on["anthropic_effort"], "medium")
        self.assertEqual(on["anthropic_betas"], [llm.THINKING_BINDING_BETA])
        self.assertEqual(fixed["timeout"], 7)


class Client(unittest.TestCase):
    def test_model_and_client(self):
        with env(LLM_BACKEND="anthropic", ANTHROPIC_API_KEY="sk-ant-test", LLM_TIMEOUT="22", LLM_RETRIES="4",
                 LLM_PROXY_URL="https://api.example.com/llm/run/token"):
            model = llm.model()
        self.assertIsInstance(model, AnthropicModel)
        self.assertEqual(model.model_name, "claude-haiku-5-5")
        client = model.client
        self.assertEqual(client.max_retries, 4)
        self.assertEqual(client.timeout, 22.0)
        self.assertEqual(str(client.base_url), "https://api.example.com/llm/run/token/anthropic/")
        self.assertEqual(client.default_headers["User-Agent"], llm.USER_AGENT)
        self.assertIsInstance(client._client, llm._DeadlineClient)

    def test_qwen_model_stays_openai(self):
        with env(QWEN_BASE_URL="http://models.example/v1", QWEN_API_KEY="k"):
            self.assertIsInstance(llm.model(), llm._WatchdogModel)


class History(unittest.TestCase):
    def test_without_thinking_drops_only_thinking_parts(self):
        messages = [ModelRequest.user_text_prompt("hi"),
                    ModelResponse(parts=[ThinkingPart(content="", signature="sig"), TextPart(content="ok")])]
        stripped = llm.without_thinking(messages)
        self.assertIs(stripped[0], messages[0])
        self.assertEqual([type(p) for p in stripped[1].parts], [TextPart])
        self.assertEqual(len(messages[1].parts), 2)  # the original history is untouched

    def test_cache_breakpoints_on_the_last_two_messages(self):
        model = llm._anthropic_class()("claude-haiku-5-5", provider=AnthropicProvider(
            anthropic_client=AsyncAnthropic(api_key="sk-ant-test", base_url="http://models.example")))
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "goal"}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "look", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "page"}]},
        ]
        model._apply_message_cache_control(messages, "5m")
        marked = ["cache_control" in message["content"][-1] for message in messages]
        self.assertEqual(marked, [False, True, True])


class Answer(BaseModel):
    city: str
    count: int


class Request(unittest.TestCase):
    """A typed agent on the Anthropic backend against a fake Messages API: the request it sends, and that the
    structured answer comes back."""

    def run_agent(self, thinking: bool) -> tuple[Answer, dict, httpx2.Headers]:
        seen: dict = {}

        def handler(request: httpx2.Request) -> httpx2.Response:
            body = json.loads(request.content)
            seen["body"], seen["headers"] = body, request.headers
            tool = next((t["name"] for t in body.get("tools", [])), None)
            if tool:
                content = [{"type": "tool_use", "id": "toolu_1", "name": tool, "input": {"city": "Example",
                                                                                        "count": 3}}]
            else:
                content = [{"type": "text", "text": json.dumps({"city": "Example", "count": 3})}]
            return httpx2.Response(200, json={
                "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"], "content": content,
                "stop_reason": "tool_use" if tool else "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0,
                          "cache_creation_input_tokens": 0}})

        with env(LLM_BACKEND="anthropic", ANTHROPIC_API_KEY="sk-ant-test",
                 ANTHROPIC_BASE_URL="http://models.example/anthropic"):
            client = AsyncAnthropic(api_key="sk-ant-test", base_url=llm.anthropic_base_url(), max_retries=0,
                                    default_headers={"User-Agent": llm.USER_AGENT},
                                    http_client=llm._DeadlineClient(transport=httpx2.MockTransport(handler)))
            model = llm._anthropic_class()(llm.model_name(), provider=AnthropicProvider(anthropic_client=client))
            agent = Agent(model, output_type=Answer, instructions="Answer with the city.",
                          model_settings=llm.settings(thinking))
            with redirect_stdout(io.StringIO()):
                result = asyncio.run(agent.run("Which city?"))
        return result.output, seen["body"], seen["headers"]

    def test_thinking_off(self):
        output, body, headers = self.run_agent(thinking=False)
        self.assertEqual(output, Answer(city="Example", count=3))
        self.assertEqual(body["model"], "claude-haiku-5-5")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertNotIn("temperature", body)
        self.assertEqual(body["max_tokens"], llm.DEFAULT_MAX_TOKENS)
        self.assertTrue(body["tool_choice"].get("disable_parallel_tool_use"))
        self.assertEqual(headers["user-agent"], llm.USER_AGENT)
        self.assertEqual(headers["x-api-key"], "sk-ant-test")
        self.assertIn("cache_control", json.dumps(body["system"]))

    def test_thinking_on(self):
        output, body, headers = self.run_agent(thinking=True)
        self.assertEqual(output, Answer(city="Example", count=3))
        self.assertEqual(body["thinking"]["type"], "adaptive")
        self.assertEqual(body["thinking"]["block_binding"], {"prefix_mismatch_behavior": "drop_block"})
        self.assertEqual(body["output_config"]["effort"], "medium")
        self.assertIn(llm.THINKING_BINDING_BETA, headers["anthropic-beta"])


if __name__ == "__main__":
    unittest.main()
