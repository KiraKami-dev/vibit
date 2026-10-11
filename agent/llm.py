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

LLM_BACKEND=anthropic runs the same agents on a Claude model with the team's own key instead (LLM_MODEL, default
claude-haiku-5-5; ANTHROPIC_API_KEY, a team secret in test and scored runs). Its calls go through the run's metering
proxy too: ANTHROPIC_BASE_URL, which the platform sets to $LLM_PROXY_URL/anthropic. Without a key the run falls back
to the hosted Qwen. Thinking off maps to Anthropic's thinking off (or the closest the model allows), thinking on to
adaptive thinking at LLM_EFFORT (default medium); see `anthropic_thinking`. The per-mode deadlines, the retries and
the call log are the same; the stream watchdog is Qwen-only (Claude calls do not hang the way the hosted model does).

LLM_BACKEND=openrouter runs them on an OpenRouter model with the team's own key (LLM_MODEL, default z-ai/glm-5.3-flash;
OPENROUTER_API_KEY), on the same OpenAI client path as Qwen: deadlines, watchdog and call log included. Calls go to
OPENROUTER_BASE_URL, else the run's proxy ($LLM_PROXY_URL/openrouter/v1), else (locally) OpenRouter itself.
LLM_PROVIDER pins the serving provider (default fireworks, no fallbacks; "any" lets OpenRouter choose). Thinking maps
to OpenRouter's `reasoning`: GLM 5.3 Flash cannot turn reasoning off (400 "Reasoning is mandatory"), so off is
{"effort": LLM_EFFORT_OFF (low), "exclude": true}, which measured 0 reasoning tokens on short answers; on is
{"effort": LLM_EFFORT}.
"""
import asyncio
import contextvars
import dataclasses
import os
import time
from contextlib import AsyncExitStack

import httpx2
from openai import AsyncOpenAI
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, ThinkingPart
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

import localenv  # noqa: F401  (local runs only: loads agent/.env on import; the environment wins)

QWEN_MODEL = "qwen3.8-flash-next"
ANTHROPIC_MODEL = "claude-haiku-5-5"
OPENROUTER_MODEL = "z-ai/glm-5.3-flash"
OPENROUTER_PROVIDER = "fireworks"
USER_AGENT = "error404ai-rfq-agent"
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
        thinking = settings_thinking(settings)
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
    markers = (b'"enable_thinking": true', b'"enable_thinking":true',  # Qwen
               b'"type": "adaptive"', b'"type":"adaptive"')  # Anthropic
    if any(marker in content for marker in markers):
        return "on"
    openrouter = b'"reasoning": {"effort"' in content or b'"reasoning":{"effort"' in content
    excluded = b'"exclude": true' in content or b'"exclude":true' in content
    return "on" if openrouter and not excluded else "off"


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


_WARNED: set[str] = set()


def _warn_once(key: str, text: str) -> None:
    if key not in _WARNED:
        _WARNED.add(key)
        print(text, flush=True)


_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openrouter": "OPENROUTER_API_KEY"}
_ALIASES = {"claude": "anthropic"}


def backend() -> str:
    """Which model backend the run uses: "qwen" (the hosted model, the default), "anthropic" or "openrouter"
    (LLM_BACKEND, with the team's own key). Without its key a backend falls back to Qwen, so a missing secret costs
    speed, not the run."""
    value = os.environ.get("LLM_BACKEND", "").strip().lower()
    value = _ALIASES.get(value, value)
    if value in _KEYS:
        if os.environ.get(_KEYS[value], "").strip():
            return value
        _warn_once("no-key", f"[model] LLM_BACKEND={value} but {_KEYS[value]} is not set: using the hosted Qwen")
    elif value not in ("", "qwen", "hosted"):
        _warn_once("unknown", f"[model] unknown LLM_BACKEND {value!r}: using the hosted Qwen")
    return "qwen"


def model_name() -> str:
    """The model id: LLM_MODEL, else the backend's default (AGENT_MODEL still picks a Qwen-endpoint model)."""
    chosen = os.environ.get("LLM_MODEL", "").strip()
    if backend() == "anthropic":
        return chosen or ANTHROPIC_MODEL
    if backend() == "openrouter":
        return chosen or OPENROUTER_MODEL
    return chosen or os.environ.get("AGENT_MODEL", "").strip() or QWEN_MODEL


def openrouter_base_url() -> str:
    """Where OpenRouter calls go: OPENROUTER_BASE_URL, else the run's proxy (the platform sets no OpenRouter variable,
    so it is built from LLM_PROXY_URL), else (only on a developer's machine) OpenRouter itself."""
    explicit = os.environ.get("OPENROUTER_BASE_URL", "").strip()
    if explicit:
        return explicit
    proxy = os.environ.get("LLM_PROXY_URL", "").strip()
    return proxy.rstrip("/") + "/openrouter/v1" if proxy else "https://openrouter.ai/api/v1"


def openrouter_provider() -> dict | None:
    """OpenRouter's provider routing: LLM_PROVIDER (default fireworks; comma-separated for an order) without
    fallbacks to other providers; "any" leaves the choice to OpenRouter."""
    value = os.environ.get("LLM_PROVIDER", "").strip()
    if value.lower() == "any":
        return None
    order = [name.strip() for name in (value or OPENROUTER_PROVIDER).split(",") if name.strip()]
    return {"order": order, "allow_fallbacks": False}


def openrouter_reasoning(thinking: bool) -> dict:
    """Our thinking on/off as OpenRouter's `reasoning` object. Off keeps the lowest effort the model accepts and
    leaves the reasoning text out of the answer (reasoning cannot be disabled on GLM 5.3 Flash)."""
    return {"effort": effort(True)} if thinking else {"effort": effort(False), "exclude": True}


def settings_thinking(settings) -> bool:
    """Whether model settings ask for thinking, in the Qwen or the OpenRouter shape."""
    body = (settings or {}).get("extra_body") or {}
    if "reasoning" in body:
        reasoning = body["reasoning"]
        return reasoning.get("enabled", True) is not False and not reasoning.get("exclude")
    return bool((body.get("chat_template_kwargs") or {}).get("enable_thinking"))


def anthropic_base_url() -> str | None:
    """Where Anthropic calls go: ANTHROPIC_BASE_URL (the platform sets it to the run's proxy), else the run's proxy
    from LLM_PROXY_URL, else (only on a developer's machine) the SDK's default, the Anthropic API itself."""
    explicit = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    if explicit:
        return explicit
    proxy = os.environ.get("LLM_PROXY_URL", "").strip()
    return proxy.rstrip("/") + "/anthropic" if proxy else None


def model() -> Model:
    """The worker model. The client's own timeout is the thinking-off one; `settings()` overrides it per request,
    so a thinking-on agent gets its longer timeout without a client of its own. With LLM_STREAM_WATCHDOG on, the
    Qwen model streams each call and judges it by progress instead (`_WatchdogModel`)."""
    http_client = _DeadlineClient(event_hooks={"request": [_sent], "response": [_answered]})
    if backend() == "anthropic":
        return _anthropic_model(http_client)
    if backend() == "openrouter":
        base_url, key, headers = openrouter_base_url(), os.environ["OPENROUTER_API_KEY"], {"User-Agent": USER_AGENT}
    else:
        base_url, key, headers = os.environ["QWEN_BASE_URL"], os.environ["QWEN_API_KEY"], None
    client = AsyncOpenAI(base_url=base_url, api_key=key, timeout=timeout_seconds(thinking=False),
                         max_retries=retries(), default_headers=headers, http_client=http_client)
    return _WatchdogModel(model_name(), provider=OpenAIProvider(openai_client=client))


def _anthropic_model(http_client: httpx2.AsyncClient) -> Model:
    from anthropic import AsyncAnthropic
    from pydantic_ai.providers.anthropic import AnthropicProvider

    if watchdog():
        _warn_once("watchdog", "[model] LLM_STREAM_WATCHDOG applies to the hosted Qwen only; Claude calls keep the "
                               "per-mode deadline")
    client = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"], base_url=anthropic_base_url(),
                            timeout=timeout_seconds(thinking=False), max_retries=retries(),
                            default_headers={"User-Agent": USER_AGENT}, http_client=http_client)
    return _anthropic_class()(model_name(), provider=AnthropicProvider(anthropic_client=client))


THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "medium"  # thinking on: Haiku 5.5's own default; Sonnet 5.5 would otherwise think at high
DEFAULT_MAX_TOKENS = 16000  # well above any answer the agents write; keeps calls unstreamed
# Models that cannot turn thinking off at all, and the one that turns it off with `between_tools` instead of
# `disabled` (which it rejects with a 400).
_ALWAYS_THINKING = ("claude-opus-5-5", "claude-fable-5", "claude-mythos-5")
_BETWEEN_TOOLS = ("claude-sonnet-5-5",)


def effort(thinking: bool) -> str:
    """The Anthropic effort for thinking-on calls (LLM_EFFORT, default medium) and for the thinking-off calls of a
    model that always thinks (LLM_EFFORT_OFF, default low)."""
    name, default = ("LLM_EFFORT", DEFAULT_EFFORT) if thinking else ("LLM_EFFORT_OFF", "low")
    value = os.environ.get(name, "").strip().lower()
    return value if value in EFFORTS else default


def anthropic_thinking(name: str, thinking: bool) -> tuple[dict, str | None]:
    """Our thinking on/off as Anthropic's `thinking` object and `effort` (None: the model's default).

    On: adaptive thinking at `effort(True)`. The workers shrink older page views between steps, which changes the
    history a thinking block was made with; `drop_block` makes the API drop such blocks instead of failing the call
    (it needs THINKING_BINDING_BETA). Off: `disabled`; Sonnet 5.5 rejects that and takes `between_tools`; models that
    always think get adaptive thinking at the lowest effort instead.
    """
    adaptive = {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    if thinking:
        return adaptive, effort(True)
    if name.startswith(_BETWEEN_TOOLS):
        return {"type": "between_tools"}, None  # takes no other field; the default effort (high) is allowed
    if name.startswith(_ALWAYS_THINKING):
        return adaptive, effort(False)
    return {"type": "disabled"}, None  # Haiku 5.5's default effort (medium) is allowed with thinking off


def max_tokens() -> int:
    try:
        value = int(os.environ.get("LLM_MAX_TOKENS", "") or DEFAULT_MAX_TOKENS)
    except ValueError:
        return DEFAULT_MAX_TOKENS
    return value if value > 0 else DEFAULT_MAX_TOKENS


def anthropic_settings(name: str, thinking: bool, timeout: float) -> ModelSettings:
    """Model settings for a Claude agent. No temperature: the 5.5 models reject sampling settings. Prompt caching
    marks the instructions, the tools and the conversation (see `_AnthropicModel`)."""
    config, level = anthropic_thinking(name, thinking)
    values: dict = dict(
        parallel_tool_calls=False,  # one tab per worker: actions must run in order
        timeout=timeout,
        max_tokens=max_tokens(),
        extra_headers={"User-Agent": USER_AGENT},  # pydantic-ai would send its own
        anthropic_thinking=config,
        anthropic_cache_instructions=True,
        anthropic_cache_tool_definitions=True,
        anthropic_cache_messages=True,
    )
    if level is not None:
        values["anthropic_effort"] = level
    if "block_binding" in config:
        values["anthropic_betas"] = [THINKING_BINDING_BETA]
    return ModelSettings(**values)  # type: ignore[typeddict-item]


_ANTHROPIC_CLASS = None


def _anthropic_class():
    """The pydantic-ai Anthropic model with two changes for our workers (built on first use, so a Qwen run never
    imports the Anthropic SDK):

    - Cache breakpoints on the last two messages, not just the last: a worker's next step shrinks the page view in
      the last message, so only a breakpoint before it (on the model's last tool call) is still a prefix next time.
    - Without adaptive thinking (thinking off), thinking blocks from earlier steps are left out of the history: the
      API would reject them once the history before them changed, and `drop_block` only works with adaptive thinking.
    """
    global _ANTHROPIC_CLASS
    if _ANTHROPIC_CLASS is not None:
        return _ANTHROPIC_CLASS
    from pydantic_ai.models.anthropic import AnthropicModel

    class _AnthropicModel(AnthropicModel):
        def _apply_message_cache_control(self, anthropic_messages, ttl) -> None:
            if len(anthropic_messages) >= 2:
                self._add_message_cache_control(anthropic_messages[-2], ttl)
            super()._apply_message_cache_control(anthropic_messages, ttl)

        async def request(self, messages: list[ModelMessage], model_settings: ModelSettings | None,
                          model_request_parameters: ModelRequestParameters) -> ModelResponse:
            config = (model_settings or {}).get("anthropic_thinking") or {}
            if config.get("type") != "adaptive":
                messages = without_thinking(messages)
            return await super().request(messages, model_settings, model_request_parameters)

    _ANTHROPIC_CLASS = _AnthropicModel
    return _ANTHROPIC_CLASS


def without_thinking(messages: list[ModelMessage]) -> list[ModelMessage]:
    """The history with the thinking parts of earlier responses left out (other parts unchanged)."""
    result = []
    for message in messages:
        if isinstance(message, ModelResponse) and any(isinstance(p, ThinkingPart) for p in message.parts):
            message = dataclasses.replace(message, parts=[p for p in message.parts if not isinstance(p, ThinkingPart)])
        result.append(message)
    return result


def thinking(phase: str, default: bool) -> bool:
    """Whether `phase` (BRIEF, FORM, REVIEW, ...) thinks: THINKING_<PHASE>=0/1 overrides the default, so
    experiments can switch it per phase and the run records say which way it ran."""
    value = os.environ.get(f"THINKING_{phase.upper()}", "").strip().lower()
    return default if not value else value not in ("0", "false", "no", "off")


def settings(thinking: bool = False, *, timeout: float | None = None) -> ModelSettings:
    """Model settings for one agent: thinking on or off, and the matching per-request timeout (an explicit
    `timeout` in seconds wins, for calls that are known to be slow or quick regardless of mode). The settings
    follow the backend (`anthropic_settings` for Claude)."""
    seconds = timeout if timeout is not None else timeout_seconds(thinking)
    if backend() == "anthropic":
        return anthropic_settings(model_name(), thinking, seconds)
    if backend() == "openrouter":
        body: dict = {"reasoning": openrouter_reasoning(thinking), "usage": {"include": True}}
        if (provider := openrouter_provider()) is not None:
            body["provider"] = provider
        # No temperature: the model's default (GLM 5.3 Flash: 1.0) instead of Qwen's 0.2, under which retries of a
        # call that ran away repeat it. max_tokens bounds a runaway answer: the sandbox showed the same requests
        # hanging until the proxy's 5-minute cut, retry after retry.
        return ModelSettings(parallel_tool_calls=False, timeout=seconds, max_tokens=max_tokens(), extra_body=body)
    return ModelSettings(
        temperature=0.2,
        parallel_tool_calls=False,  # one tab per worker: actions must run in order
        timeout=seconds,
        extra_body={"chat_template_kwargs": {"enable_thinking": thinking}},
    )
