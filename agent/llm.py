"""
The model behind the workers: the hosted Qwen through the run's metering proxy by default.

AGENT_MODEL picks another model on the same OpenAI-compatible endpoint (QWEN_BASE_URL). Thinking is off for
navigation steps, which makes a step about three times faster, and on where a worker has to weigh evidence.

Timeouts are per mode, because the hosted model now and then hangs a call for minutes until the proxy drops it: a
thinking-off step answers in 2 to 5 s, so it gets a short timeout and a quick retry (LLM_TIMEOUT, 45 s, which
leaves room for the 30 to 36 s calls a longer answer takes); a thinking-on call legitimately takes 25 to 45 s, so it
gets a long one (LLM_TIMEOUT_THINKING, 90 s). Time grows with the answer: copying 80 table rows took 85 s even
without thinking. `settings(thinking)` carries the right timeout in the ModelSettings; pydantic-ai passes it to
the OpenAI client per request, so one client serves both modes. A call known to run longer (a reader writing a long
typed answer) passes `settings(thinking, timeout=seconds)`; that per-request value beats any timeout set on the
client. LLM_RETRIES attempts follow each failure with a short backoff (0.5 s doubling, capped at 8 s).

The timeout bounds the whole call, not each read: while the model works, the proxy sends the headers and then a
newline every few seconds, so a plain read timeout never fires and a hung call would run until the proxy gives up.
"""
import asyncio
import os
import time

import httpx2
from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

QWEN_MODEL = "qwen3.8-flash-next"
DEFAULT_TIMEOUT = 45.0  # thinking off: steps answer in seconds, so a hang shows early
DEFAULT_TIMEOUT_THINKING = 90.0  # thinking on: a call reading a whole page may take 25 to 45 s
DEFAULT_RETRIES = 6
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")


def env_seconds(name: str, default: float) -> float:
    """A positive number from the environment, or `default` when unset, empty or unusable."""
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def timeout_seconds(thinking: bool = False) -> float:
    """Seconds one model call may take before it is given up and retried: LLM_TIMEOUT_THINKING for thinking-on
    calls (falling back to LLM_TIMEOUT when it is larger), LLM_TIMEOUT otherwise."""
    short = env_seconds("LLM_TIMEOUT", DEFAULT_TIMEOUT)
    if not thinking:
        return short
    return max(short, env_seconds("LLM_TIMEOUT_THINKING", DEFAULT_TIMEOUT_THINKING))


def retries() -> int:
    """How many times a failed or timed-out call is retried (LLM_RETRIES, default 6; 0 turns retries off)."""
    try:
        value = int(os.environ.get("LLM_RETRIES", "") or DEFAULT_RETRIES)
    except ValueError:
        return DEFAULT_RETRIES
    return value if value >= 0 else DEFAULT_RETRIES


def _thinking(request: httpx2.Request) -> str:
    content = request.content or b""
    return "on" if b'"enable_thinking": true' in content or b'"enable_thinking":true' in content else "off"


async def _sent(request: httpx2.Request) -> None:
    """Note when a call left; a retry (the OpenAI client counts them in a header) is logged, with the timeout the
    attempt runs under, so a hanging model shows in the run logs before the worker times out."""
    request.extensions["sent_at"] = time.monotonic()
    attempt = int(request.headers.get("x-stainless-retry-count", "0") or 0)
    if attempt:
        print(f"[model] retry {attempt}/{retries()} thinking {_thinking(request)} "
              f"timeout {request.headers.get('x-stainless-read-timeout', '?')}s", flush=True)


class _TimedBody(httpx2.AsyncByteStream):
    """A response body that logs the call once it has fully arrived: the proxy sends the headers early and a
    thinking-on body can follow much later, so logging at the headers would hide the real duration."""

    def __init__(self, response: httpx2.Response) -> None:
        self._response = response
        self._stream = response.stream

    @property
    def elapsed(self):
        return getattr(self._stream, "elapsed", None)

    async def __aiter__(self):
        complete = False
        try:
            async for chunk in self._stream:  # type: ignore[union-attr]
                yield chunk
            complete = True
        finally:
            _log(self._response, complete)

    async def aclose(self) -> None:
        await self._stream.aclose()  # type: ignore[union-attr]


def _log(response: httpx2.Response, complete: bool) -> None:
    """One line per model call: status, seconds until the body was in (and until the headers came), request
    size, thinking mode, and which retry it was."""
    request = response.request
    now = time.monotonic()
    sent, headers = request.extensions.get("sent_at", now), request.extensions.get("headers_at", now)
    attempt = request.headers.get("x-stainless-retry-count", "0")
    print(f"[model] {response.status_code} {now - sent:.1f}s (headers {headers - sent:.1f}s) "
          f"{len(request.content or b'')} bytes thinking {_thinking(request)}"
          + (f" retry {attempt}" if attempt not in ("", "0") else "") + ("" if complete else " incomplete"),
          flush=True)


async def _answered(response: httpx2.Response) -> None:
    """Called when the headers are in: remember when, and log the call once the body has been read."""
    response.request.extensions["headers_at"] = time.monotonic()
    response.stream = _TimedBody(response)


class _DeadlineClient(httpx2.AsyncClient):
    """An HTTP client whose read timeout is a deadline for the whole call (headers and body), so the proxy's
    keep-alive newlines cannot hold a call open past it. Expiry raises ReadTimeout, which the OpenAI client retries."""

    async def send(self, request: httpx2.Request, *, stream: bool = False, **kwargs) -> httpx2.Response:
        seconds = (request.extensions.get("timeout") or {}).get("read")
        if not seconds:
            return await super().send(request, stream=stream, **kwargs)
        deadline = time.monotonic() + seconds
        try:
            response = await asyncio.wait_for(super().send(request, stream=True, **kwargs), seconds)
        except TimeoutError:
            raise httpx2.ReadTimeout(f"no answer within {seconds:g}s", request=request) from None
        if stream:
            return response
        try:
            await asyncio.wait_for(response.aread(), max(deadline - time.monotonic(), 0.001))
        except TimeoutError:
            await response.aclose()
            raise httpx2.ReadTimeout(f"no complete answer within {seconds:g}s", request=request) from None
        except BaseException:
            await response.aclose()
            raise
        return response


def model() -> OpenAIChatModel:
    """The worker model. The client's own timeout is the thinking-off one; `settings()` overrides it per request,
    so a thinking-on agent gets its longer timeout without a client of its own."""
    client = AsyncOpenAI(base_url=os.environ["QWEN_BASE_URL"], api_key=os.environ["QWEN_API_KEY"],
                         timeout=timeout_seconds(thinking=False), max_retries=retries(),
                         http_client=_DeadlineClient(event_hooks={"request": [_sent], "response": [_answered]}))
    return OpenAIChatModel(os.environ.get("AGENT_MODEL", QWEN_MODEL), provider=OpenAIProvider(openai_client=client))


def thinking(phase: str, default: bool) -> bool:
    """Whether `phase` (BRIEF, FORM, REVIEW, ...) thinks: THINKING_<PHASE>=0/1 overrides the default, so
    experiments can switch it per phase and the run records say which way it ran."""
    value = os.environ.get(f"THINKING_{phase.upper()}", "").strip().lower()
    return default if not value else value not in ("0", "false", "no", "off")


def settings(thinking: bool = False, *, timeout: float | None = None) -> ModelSettings:
    """Model settings for one agent: thinking on or off, and the matching per-request timeout (an explicit
    `timeout` in seconds wins, for calls that are known to be slow or quick regardless of mode)."""
    return ModelSettings(
        temperature=0.2,
        parallel_tool_calls=False,  # one tab per worker: actions must run in order
        timeout=timeout if timeout is not None else timeout_seconds(thinking),
        extra_body={"chat_template_kwargs": {"enable_thinking": thinking}},
    )
