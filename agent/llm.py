"""
The model behind the workers: the hosted Qwen through the run's metering proxy by default.

AGENT_MODEL picks another model on the same OpenAI-compatible endpoint (QWEN_BASE_URL). Thinking is off for
navigation steps, which makes a step about three times faster, and on where a worker has to weigh evidence.
"""
import os
import time

import httpx2
from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

QWEN_MODEL = "qwen3.8-flash-next"
# Output tokens one call may produce. The model writes about 40 tokens a second (measured), thinking included, so
# the cap bounds a call to roughly 40 s without thinking and 80 s with it; a cut answer fails validation and the
# step is retried. STREAM: answers are read as a stream, so the client's timeout is the longest silence it accepts
# (a hanging call) rather than a limit on a long answer.
MAX_TOKENS = 1500
MAX_TOKENS_THINKING = 3000
STREAM = os.environ.get("LLM_STREAM", "1") != "0"
# Seconds of silence after which a call counts as hanging and is sent again (LLM_TIMEOUT overrides). A streamed
# answer starts within 3 s and pauses up to 11 s (measured); a call read in one piece needs the whole answer in time.
SILENCE_SECONDS = 20 if STREAM else 30
SILENCE_SECONDS_PLAIN = 12  # without thinking a streamed answer never paused longer than 0.5 s (measured)
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")


async def _sent(request: httpx2.Request) -> None:
    request.extensions["sent_at"] = time.monotonic()


async def _answered(response: httpx2.Response) -> None:
    """Log each model call's status and seconds, so slow or hanging calls show in the run logs."""
    seconds = time.monotonic() - response.request.extensions.get("sent_at", time.monotonic())
    print(f"[model] {response.status_code} {seconds:.1f}s {len(response.request.content or b'')} bytes", flush=True)


def model() -> OpenAIChatModel:
    # The run waits for the hosted models to warm up, so a step answers in seconds; now and then a call hangs
    # until the proxy drops it minutes later, so give up early and retry instead.
    client = AsyncOpenAI(base_url=os.environ["QWEN_BASE_URL"], api_key=os.environ["QWEN_API_KEY"],
                         timeout=float(os.environ.get("LLM_TIMEOUT", SILENCE_SECONDS)), max_retries=6,
                         http_client=httpx2.AsyncClient(event_hooks={"request": [_sent], "response": [_answered]}))
    return OpenAIChatModel(os.environ.get("AGENT_MODEL", QWEN_MODEL), provider=OpenAIProvider(openai_client=client))


def max_tokens(thinking: bool = False) -> int:
    """The output-token cap of one call; LLM_MAX_TOKENS and LLM_MAX_TOKENS_THINKING override the defaults."""
    name, default = ("LLM_MAX_TOKENS_THINKING", MAX_TOKENS_THINKING) if thinking else ("LLM_MAX_TOKENS", MAX_TOKENS)
    try:
        return max(256, int(os.environ.get(name, default)))
    except ValueError:
        return default


def settings(thinking: bool = False) -> ModelSettings:
    silence = float(os.environ.get("LLM_TIMEOUT", SILENCE_SECONDS))
    if STREAM and not thinking and "LLM_TIMEOUT" not in os.environ:
        silence = SILENCE_SECONDS_PLAIN
    return ModelSettings(
        max_tokens=max_tokens(thinking),
        timeout=silence,
        temperature=0.2,
        parallel_tool_calls=False,  # one tab per worker: actions must run in order
        extra_body={"chat_template_kwargs": {"enable_thinking": thinking}},
    )
