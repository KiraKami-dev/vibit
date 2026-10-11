"""The per-caller usage ledger: each model call is booked under the agent function (or worker) that made it."""
import asyncio
import io
import json
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


class Answer(BaseModel):
    value: str


def _handler(request: httpx2.Request) -> httpx2.Response:
    body = {"id": "c1", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": "t1", "type": "function",
                    "function": {"name": "final_result", "arguments": json.dumps({"value": "x"})}}]}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 7, "total_tokens": 127,
                      "prompt_tokens_details": {"cached_tokens": 100}}}
    return httpx2.Response(200, json=body)


def _model() -> llm._WatchdogModel:
    http = llm._DeadlineClient(transport=httpx2.MockTransport(_handler))
    client = AsyncOpenAI(base_url="http://models.example/v1", api_key="k", max_retries=0, http_client=http)
    return llm._WatchdogModel("m", provider=OpenAIProvider(openai_client=client))


async def reader_function() -> str:
    agent = Agent(_model(), output_type=Answer, model_settings=llm.settings(thinking=True))
    return (await agent.run("hi")).output.value


class Ledger(unittest.TestCase):
    def setUp(self):
        llm.USAGE.clear()

    def test_call_is_booked_under_the_calling_function(self):
        with patch.object(llm, "_PACKAGE", str(Path(__file__).resolve().parent)), redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(reader_function()), "x")
        row = llm.USAGE["test_llm_usage.reader_function"]
        self.assertEqual(row[:4], [1, 120, 100, 7])
        self.assertEqual(row[5], 1)  # thinking on
        self.assertIn("test_llm_usage.reader_function", llm.usage_table())

    def test_worker_label_when_no_agent_frame(self):
        async def run():
            token = llm.LABEL.set("worker site Thing")
            try:
                return await reader_function()
            finally:
                llm.LABEL.reset(token)
        with redirect_stdout(io.StringIO()):  # tests/ is outside the package: no agent frame is found
            asyncio.run(run())
        self.assertIn("worker site Thing", llm.USAGE)


if __name__ == "__main__":
    unittest.main()
