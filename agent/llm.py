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
                         timeout=float(os.environ.get("LLM_TIMEOUT", "30")), max_retries=6,
                         http_client=httpx2.AsyncClient(event_hooks={"request": [_sent], "response": [_answered]}))
    return OpenAIChatModel(os.environ.get("AGENT_MODEL", QWEN_MODEL), provider=OpenAIProvider(openai_client=client))


def thinking(phase: str, default: bool) -> bool:
    """Whether `phase` (BRIEF, FORM, REVIEW, ...) thinks: THINKING_<PHASE>=0/1 overrides the default, so
    experiments can switch it per phase and the run records say which way it ran."""
    value = os.environ.get(f"THINKING_{phase.upper()}", "").strip().lower()
    return default if not value else value not in ("0", "false", "no", "off")


def settings(thinking: bool = False) -> ModelSettings:
    return ModelSettings(
        temperature=0.2,
        parallel_tool_calls=False,  # one tab per worker: actions must run in order
        extra_body={"chat_template_kwargs": {"enable_thinking": thinking}},
    )
