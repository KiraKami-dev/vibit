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

LLM_STREAM_WATCHDOG=1 swaps the whole-call deadline for progress checks (see `_WatchdogModel`): each call is
streamed, a call whose first chunk does not arrive within LLM_FIRST_TOKEN_TIMEOUT (20 s) is retried, and a call
that is answering may run on as long as it keeps making progress, up to LLM_MAX_CALL (300 s). The proxy streams
text and reasoning token by token but holds back tool-call arguments until they are complete, so a request with
tools may stay silent after its last token for as long as its timeout above; one without tools may pause
LLM_IDLE_TIMEOUT (15 s). LLM_HEDGE_AFTER sends a second copy of a call that has not started after that many
seconds and keeps whichever starts first (the losing copy may still be billed).
"""
import asyncio
import contextvars
import os
import time
from contextlib import AsyncExitStack

import httpx2
from openai import AsyncOpenAI
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import ModelRequestParameters
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


DEFAULT_FIRST_TOKEN = 20.0  # measured: the first chunk came within 9.4 s (p90 5.9 s) in 179 calls, either mode
DEFAULT_IDLE = 15.0  # text and reasoning tokens arrive at most ~1.5 s apart while the model answers
DEFAULT_MAX_CALL = 300.0  # a streamed answer is not cut by the proxy: one runaway reasoning ran 814 s


def watchdog() -> bool:
    """Whether calls are streamed and judged by progress (LLM_STREAM_WATCHDOG=1) instead of a whole-call deadline."""
    return os.environ.get("LLM_STREAM_WATCHDOG", "").strip().lower() in ("1", "true", "yes", "on")


def hedge_seconds() -> float:
    """After how many seconds without a first chunk a second copy of the call is sent (LLM_HEDGE_AFTER; 0 = never)."""
    try:
        value = float(os.environ.get("LLM_HEDGE_AFTER", "") or 0)
    except ValueError:
        return 0.0
    return max(value, 0.0)


def _backoff(attempt: int) -> float:
    """Seconds to wait before retry `attempt`: 0.5 s doubling, capped at 8 s (as the OpenAI client does)."""
    return min(0.5 * 2 ** (attempt - 1), 8.0)


class Stalled(Exception):
    """A streamed call stopped making progress (raised by the watchdog, then retried)."""


# The progress limits of the call being made in this task; set by _WatchdogModel, read by the stream wrapper.
_WATCH: contextvars.ContextVar["_Limits | None"] = contextvars.ContextVar("llm_watch", default=None)


class _Limits:
    def __init__(self, *, first: float, idle: float, silence: float, tools: bool) -> None:
        self.first, self.idle, self.silence, self.tools = first, idle, silence, tools

    @property
    def quiet(self) -> float:
        """How long the call may go without a token once it has started: a request with tools may be writing tool
        arguments, which the proxy only sends when they are complete."""
        return max(self.idle, self.silence) if self.tools else self.idle


def _progress(chunk) -> bool:
    """Whether a stream chunk carries model output (text, reasoning or a tool call), not just a role or usage."""
    for choice in getattr(chunk, "choices", None) or ():
        delta = choice.delta
        if delta is None:
            continue
        extra = delta.model_extra or {}
        if delta.content or delta.tool_calls or extra.get("reasoning_content") or extra.get("reasoning"):
            return True
    return False


class _WatchedStream:
    """An OpenAI chunk stream that raises Stalled when the call goes quiet for longer than its limits allow.
    The proxy's keep-alive comments never reach here (the SSE decoder drops them), so they do not count."""

    def __init__(self, stream, limits: _Limits) -> None:
        self._stream, self._limits = stream, limits
        self._chunks = 0
        self._last = time.monotonic()

    def _wait(self) -> float:
        quiet = self._limits.first if not self._chunks else self._limits.quiet
        return max(self._last + quiet - time.monotonic(), 0.0)

    async def __aiter__(self):
        source = self._stream.__aiter__()
        while True:
            try:
                chunk = await asyncio.wait_for(anext(source), self._wait())
            except StopAsyncIteration:
                return
            except TimeoutError:
                if not self._chunks:
                    raise Stalled(f"no first chunk within {self._limits.first:g}s") from None
                raise Stalled(f"no progress for {self._limits.quiet:g}s after {self._chunks} chunks") from None
            if not self._chunks or _progress(chunk):
                self._last = time.monotonic()
            self._chunks += 1
            yield chunk

    async def __aenter__(self):
        await self._stream.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._stream.__aexit__(*exc)

    async def close(self) -> None:
        await self._stream.close()

    def __getattr__(self, name):
        return getattr(self._stream, name)


class _WatchdogModel(OpenAIChatModel):
    """The OpenAI chat model; with LLM_STREAM_WATCHDOG on, a request is streamed under progress limits, retried when
    it stalls, and handed back as one ModelResponse, so agents run unchanged."""

    async def _completions_create(self, messages, stream, model_settings, model_request_parameters):
        response = await super()._completions_create(messages, stream, model_settings, model_request_parameters)
        limits = _WATCH.get()
        if stream and limits is not None and not isinstance(response, ModelResponse):
            return _WatchedStream(response, limits)
        return response

    async def request(self, messages: list[ModelMessage], model_settings: ModelSettings | None,
                      model_request_parameters: ModelRequestParameters) -> ModelResponse:
        if not watchdog():
            return await super().request(messages, model_settings, model_request_parameters)
        settings = ModelSettings(**(model_settings or {}))
        thinking = bool(((settings.get("extra_body") or {}).get("chat_template_kwargs") or {}).get("enable_thinking"))
        silence = float(settings.get("timeout") or timeout_seconds(thinking))  # type: ignore[arg-type]
        cap = env_seconds("LLM_MAX_CALL", DEFAULT_MAX_CALL)
        settings["timeout"] = cap  # the HTTP layer only guards the cap; progress is judged here
        tools = bool(model_request_parameters.function_tools or model_request_parameters.output_tools)
        limits = _Limits(first=env_seconds("LLM_FIRST_TOKEN_TIMEOUT", DEFAULT_FIRST_TOKEN),
                         idle=env_seconds("LLM_IDLE_TIMEOUT", DEFAULT_IDLE), silence=silence, tools=tools)
        attempts = retries() + 1
        for attempt in range(1, attempts + 1):
            token = _WATCH.set(limits)
            try:
                async with asyncio.timeout(cap):
                    return await self._attempt(messages, settings, model_request_parameters, limits)
            except (Stalled, TimeoutError, httpx2.TransportError) as error:
                reason = str(error) or f"no answer within {cap:g}s"
            except ModelAPIError as error:
                if isinstance(error, ModelHTTPError) and error.status_code < 500 and error.status_code != 429:
                    raise
                reason = str(error)
            finally:
                _WATCH.reset(token)
            if attempt == attempts:
                raise ModelAPIError(self.model_name, f"model call failed {attempts} times; last: {reason}")
            print(f"[model] stalled thinking {'on' if thinking else 'off'}: {reason}; retry {attempt}/{attempts - 1}",
                  flush=True)
            await asyncio.sleep(_backoff(attempt))
        raise AssertionError("unreachable")

    async def _attempt(self, messages, settings, params, limits: _Limits) -> ModelResponse:
        """One streamed call (two when hedging): wait for the first chunk, then read the stream to its end."""
        async with AsyncExitStack() as stack:
            streamed = await self._start(stack, messages, settings, params, limits)
            async for _ in streamed:
                pass
            return streamed.get()

    async def _open(self, messages, settings, params):
        stack = AsyncExitStack()
        try:
            streamed = await stack.enter_async_context(self.request_stream(messages, settings, params))
        except BaseException:
            await stack.aclose()
            raise
        return stack, streamed

    async def _start(self, stack: AsyncExitStack, messages, settings, params, limits: _Limits):
        """Open the stream (request_stream returns once the first chunk is in). With LLM_HEDGE_AFTER, a second copy
        goes out when the first has not started by then; the first copy to start wins, the other is dropped."""
        hedge = hedge_seconds()
        tasks = [asyncio.create_task(self._open(messages, settings, params))]
        first_copy = tasks[0]
        deadline = time.monotonic() + limits.first
        hedged = False
        try:
            if 0 < hedge < limits.first:
                done, _ = await asyncio.wait(tasks, timeout=hedge)
                if not done:
                    print(f"[model] no first chunk after {hedge:g}s: sending a hedge copy", flush=True)
                    tasks.append(asyncio.create_task(self._open(messages, settings, params)))
                    deadline = time.monotonic() + limits.first
                    hedged = True
            while tasks:
                done, _ = await asyncio.wait(tasks, timeout=max(deadline - time.monotonic(), 0),
                                             return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    raise Stalled(f"no first chunk within {limits.first:g}s")
                for task in done:
                    tasks.remove(task)
                    if task.exception() is None:
                        inner, streamed = task.result()
                        stack.push_async_callback(inner.aclose)
                        if hedged:
                            print(f"[model] hedge: the {'first' if task is first_copy else 'hedge'} copy "
                                  "started first and is kept", flush=True)
                        return streamed
                    if not tasks:
                        raise task.exception()  # type: ignore[misc]
            raise AssertionError("unreachable")
        finally:
            for task in tasks:  # copies that lost the race or never started
                task.cancel()
            for result in await asyncio.gather(*tasks, return_exceptions=True):
                if isinstance(result, tuple):
                    await result[0].aclose()


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
                complete = complete or b"data: [DONE]" in chunk  # a stream's reader may stop right there
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
    so a thinking-on agent gets its longer timeout without a client of its own. With LLM_STREAM_WATCHDOG on, the
    model streams each call and judges it by progress instead (`_WatchdogModel`)."""
    client = AsyncOpenAI(base_url=os.environ["QWEN_BASE_URL"], api_key=os.environ["QWEN_API_KEY"],
                         timeout=timeout_seconds(thinking=False), max_retries=retries(),
                         http_client=_DeadlineClient(event_hooks={"request": [_sent], "response": [_answered]}))
    return _WatchdogModel(os.environ.get("AGENT_MODEL", QWEN_MODEL), provider=OpenAIProvider(openai_client=client))


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
