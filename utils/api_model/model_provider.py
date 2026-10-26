import asyncio
import re
import os
import json
import warnings
from contextlib import contextmanager
from agents import (
    ModelProvider,
    OpenAIChatCompletionsModel,
    OpenAIResponsesModel,
    Model,
    set_tracing_disabled,
    _debug
)
from openai import AsyncOpenAI
from openai.types.responses import ResponseOutputMessage, ResponseOutputText, ResponseReasoningItem, ResponseStreamEvent
from openai.types.chat import ChatCompletion, ChatCompletionChunk, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice

from configs.global_configs import global_configs
from addict import Dict

from agents.models.openai_chatcompletions import *
from agents.models.openai_responses import Converter as OpenAIResponsesConverter
from agents.model_settings import ModelSettings

from pydantic import BaseModel

from agents.models.chatcmpl_converter import *

from typing import Any, List, Union
from typing_extensions import Literal, Annotated, TypeAlias
from openai._utils import PropertyInfo

from utils.api_model.usage_stats import add_cached_token_details

from agents.items import ItemHelpers
from agents.version import __version__

_USER_AGENT = f"Agents/Python {__version__}"
_HEADERS = {"User-Agent": _USER_AGENT}

# Timeouts for the OpenAI-compatible HTTP clients. Without an explicit timeout the
# SDK default lets a half-open gateway connection hang forever, which is what makes
# a task sit idle for hours instead of failing over to the retry path.
# TOOLATHLON_LLM_TIMEOUT: total request budget (connect + read + write), seconds.
# TOOLATHLON_LLM_CONNECT_TIMEOUT: TCP/TLS connect budget, seconds.
LLM_TIMEOUT = float(os.getenv("TOOLATHLON_LLM_TIMEOUT", "1800"))
LLM_CONNECT_TIMEOUT = float(os.getenv("TOOLATHLON_LLM_CONNECT_TIMEOUT", "240"))


def _build_llm_timeout() -> "httpx.Timeout":
    """Build an httpx.Timeout: short connect budget, generous read budget.

    The read budget must be large enough for a full streaming completion, but
    finite so a stalled stream raises instead of blocking the agent loop forever.
    """
    import httpx
    return httpx.Timeout(LLM_TIMEOUT, connect=LLM_CONNECT_TIMEOUT)

_RESERVED_EXTRA_REQUEST_PARAM_KEYS = {
    "model",
    "messages",
    "input",
    "instructions",
    "previous_response_id",
    "include",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "stream_options",
}


def _get_extra_request_params(*sources) -> dict:
    merged_params = {}
    for source in sources:
        extra_request_params = getattr(source, "extra_request_params", None) or {}
        if not isinstance(extra_request_params, dict):
            raise ValueError("extra_request_params must be a dictionary")
        merged_params.update(extra_request_params)

    filtered_params = {}
    for key, value in merged_params.items():
        if key in _RESERVED_EXTRA_REQUEST_PARAM_KEYS:
            print(f"[Warning] Ignoring generation.extra_request_params['{key}']; this request field is managed by the runner.")
            continue
        filtered_params[key] = value
    return filtered_params


class ResponseOutputReasoningContent(BaseModel):
    reasoning_content: str | None
    pure_thinking_str: str | None
    extra_contents_in_tool_calls: str | None
    thinking_blocks: Any | None = None
    redacted_thinking: Any | None = None
    """The reasoning content from the model."""
    type: Literal["reasoning_content"] = "reasoning_content"

ExtendedContent: TypeAlias = Annotated[Union[ResponseOutputText, ResponseOutputRefusal, ResponseOutputReasoningContent], PropertyInfo(discriminator="type")]


class ExtendedResponseOutputMessage(ResponseOutputMessage):
    content: List[ExtendedContent]
    """The content of the output message."""


_ANTHROPIC_THINKING_BLOCK_FIELDS = ("thinking_blocks", "redacted_thinking")


@contextmanager
def _suppress_pydantic_serializer_warnings():
    """Silence pydantic's serializer warnings while dumping extended messages.

    `ExtendedResponseOutputMessage` carries `ResponseOutputReasoningContent`,
    which is not a member of the `Response.output` union that pydantic builds
    from the openai types. Dumping such a message inside a `Response` therefore
    trips `PydanticSerializationUnexpectedValue` — a warning, not an error: the
    payload is correct and round-trips fine.

    This cannot be fixed by annotating the subclass (tried: `List[Union[...]]`,
    `List[Any]`, `SerializeAsAny`, `__get_pydantic_core_schema__`) because the
    union's serializer is built once at import time and matches on the concrete
    type. So suppress it narrowly here, at the dump sites only.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Pydantic serializer warnings")
        warnings.filterwarnings("ignore", message="PydanticSerializationUnexpectedValue")
        yield


def _dump_model(obj: Any) -> dict:
    """`obj.model_dump()` without the extended-message serializer noise."""
    with _suppress_pydantic_serializer_warnings():
        return obj.model_dump()


def _get_message_extra_field(message: Any, field_name: str) -> Any | None:
    if hasattr(message, field_name):
        return getattr(message, field_name)

    model_extra = getattr(message, "model_extra", None)
    if isinstance(model_extra, dict) and field_name in model_extra:
        return model_extra[field_name]

    return None


_REASONING_FIELD_NAMES = ("reasoning_content", "reasoning", "reasoning_details")


def _extract_message_reasoning(message: Any) -> tuple[str | None, Any | None]:
    """Return ``(field_name, value)`` for the reasoning payload on a chat message.

    Providers disagree on where the chain of thought lives:

    - ``reasoning_content``  (deepseek-reasoner, kimi, pangu non-streaming)
    - ``reasoning``          (some self-deployed / unified-compatible models)
    - ``reasoning_details``  (a list of typed blocks, e.g. openrouter)

    They are tried in that order so ``reasoning_content`` wins when a provider
    echoes the same text under both it and ``reasoning``. Returns ``(None, None)``
    when the message carries no reasoning.
    """
    for field_name in _REASONING_FIELD_NAMES:
        value = _get_message_extra_field(message, field_name)
        if value is not None:
            return field_name, value
    return None, None


def _json_loads_nullable(value: Any) -> Any | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _has_anthropic_thinking_blocks(content: Any) -> bool:
    return any(bool(getattr(content, field_name, None)) for field_name in _ANTHROPIC_THINKING_BLOCK_FIELDS)


def _print_implicit_thinking() -> None:
    print("\033[90mTHINKING: (Thinking implicitly ...) \033[0m")


class ConverterWithExplicitReasoningContent(Converter):
    @classmethod
    def message_to_output_items(cls, message: ChatCompletionMessage) -> list[TResponseOutputItem]:
        items: list[TResponseOutputItem] = []

        message_item = ExtendedResponseOutputMessage(
            id=FAKE_RESPONSES_ID,
            content=[],
            role="assistant",
            type="message",
            status="completed",
        )
        if message.content:
            message_item.content.append(
                ResponseOutputText(text=message.content, type="output_text", annotations=[])
            )
        if message.refusal:
            message_item.content.append(
                ResponseOutputRefusal(refusal=message.refusal, type="refusal")
            )
        
        reasoning_content = json.dumps(None)
        pure_thinking_str = None
        has_reasoning_payload = False

        # The reasoning field differs by provider: `reasoning_content`,
        # `reasoning`, or `reasoning_details` (a list of typed blocks). Try them
        # in that order so `reasoning_content` wins when a provider echoes the
        # same text under both it and `reasoning`.
        reasoning_field_name, reasoning_value = _extract_message_reasoning(message)
        if reasoning_field_name is not None:
            if reasoning_field_name == "reasoning_details":
                pure_thinking_str = None
                assert isinstance(reasoning_value, list), "REASONING_DETAILS SHOULD BE A LIST IN OUR DESIGN"
                for detail in reasoning_value:
                    if detail.get('type') == 'reasoning.text':
                        pure_thinking_str = detail.get('text')
                        break
                reasoning_content = json.dumps({"field_name": "reasoning_details", "value": reasoning_value})
            else:
                pure_thinking_str = reasoning_value
                reasoning_content = json.dumps({"field_name": reasoning_field_name, "value": reasoning_value})
            has_reasoning_payload = True

        thinking_blocks = _get_message_extra_field(message, "thinking_blocks")
        redacted_thinking = _get_message_extra_field(message, "redacted_thinking")

        # for gemini, the reasoning is in the **first** tool call, so we need to find it
        extra_contents_in_tool_calls = None
        has_tool_calls = hasattr(message, "tool_calls") and message.tool_calls
        if has_tool_calls:
            extra_contents_in_tool_calls = []
            for tool_call in message.tool_calls:
                if hasattr(tool_call, "extra_content") and tool_call.extra_content:
                    extra_contents_in_tool_calls.append(tool_call.extra_content)
                else:
                    extra_contents_in_tool_calls.append(None)

        if (
            has_reasoning_payload
            or thinking_blocks is not None
            or redacted_thinking is not None
            or has_tool_calls
        ):
            message_item.content.append(
                ResponseOutputReasoningContent(
                    reasoning_content=reasoning_content,
                    pure_thinking_str=pure_thinking_str,
                    extra_contents_in_tool_calls=json.dumps(extra_contents_in_tool_calls),
                    thinking_blocks=thinking_blocks,
                    redacted_thinking=redacted_thinking,
                    type="reasoning_content",
                )
            )
        
        if message.audio:
            raise AgentsException("Audio is not currently supported")

        if message_item.content:
            items.append(message_item)

        if message.tool_calls:
            for tool_call in message.tool_calls:
                arguments = tool_call.function.arguments if tool_call.function.arguments else "{}"
                items.append(
                    ResponseFunctionToolCall(
                        id=FAKE_RESPONSES_ID,
                        call_id=tool_call.id,
                        arguments=arguments,
                        name=tool_call.function.name,
                        type="function_call",
                    )
                )

        return items


    @classmethod
    def items_to_messages(
        cls,
        items: str | Iterable[TResponseInputItem],
    ) -> list[ChatCompletionMessageParam]:
        """
        Convert a sequence of 'Item' objects into a list of ChatCompletionMessageParam.

        Rules:
        - EasyInputMessage or InputMessage (role=user) => ChatCompletionUserMessageParam
        - EasyInputMessage or InputMessage (role=system) => ChatCompletionSystemMessageParam
        - EasyInputMessage or InputMessage (role=developer) => ChatCompletionDeveloperMessageParam
        - InputMessage (role=assistant) => Start or flush a ChatCompletionAssistantMessageParam
        - response_output_message => Also produces/flushes a ChatCompletionAssistantMessageParam
        - tool calls get attached to the *current* assistant message, or create one if none.
        - tool outputs => ChatCompletionToolMessageParam
        """

        if isinstance(items, str):
            return [
                ChatCompletionUserMessageParam(
                    role="user",
                    content=items,
                )
            ]

        result: list[ChatCompletionMessageParam] = []
        current_assistant_msg: ChatCompletionAssistantMessageParam | None = None

        def flush_assistant_message() -> None:
            nonlocal current_assistant_msg
            if current_assistant_msg is not None:
                # The API doesn't support empty arrays for tool_calls
                if not current_assistant_msg.get("tool_calls"):
                    del current_assistant_msg["tool_calls"]
                if "extra_contents_in_tool_calls" in current_assistant_msg:
                    del current_assistant_msg["extra_contents_in_tool_calls"]
                result.append(current_assistant_msg)
                current_assistant_msg = None

        def ensure_assistant_message() -> ChatCompletionAssistantMessageParam:
            nonlocal current_assistant_msg
            if current_assistant_msg is None:
                current_assistant_msg = ChatCompletionAssistantMessageParam(role="assistant")
                current_assistant_msg["tool_calls"] = []
            return current_assistant_msg

        for item in items:
            # 1) Check easy input message
            if easy_msg := cls.maybe_easy_input_message(item):
                role = easy_msg["role"]
                content = easy_msg["content"]

                if role == "user":
                    flush_assistant_message()
                    msg_user: ChatCompletionUserMessageParam = {
                        "role": "user",
                        "content": cls.extract_all_content(content),
                    }
                    result.append(msg_user)
                elif role == "system":
                    flush_assistant_message()
                    msg_system: ChatCompletionSystemMessageParam = {
                        "role": "system",
                        "content": cls.extract_text_content(content),
                    }
                    result.append(msg_system)
                elif role == "developer":
                    flush_assistant_message()
                    msg_developer: ChatCompletionDeveloperMessageParam = {
                        "role": "developer",
                        "content": cls.extract_text_content(content),
                    }
                    result.append(msg_developer)
                elif role == "assistant":
                    flush_assistant_message()
                    msg_assistant: ChatCompletionAssistantMessageParam = {
                        "role": "assistant",
                        "content": cls.extract_text_content(content),
                    }
                    result.append(msg_assistant)
                else:
                    raise UserError(f"Unexpected role in easy_input_message: {role}")

            # 2) Check input message
            elif in_msg := cls.maybe_input_message(item):
                role = in_msg["role"]
                content = in_msg["content"]
                flush_assistant_message()

                if role == "user":
                    msg_user = {
                        "role": "user",
                        "content": cls.extract_all_content(content),
                    }
                    result.append(msg_user)
                elif role == "system":
                    msg_system = {
                        "role": "system",
                        "content": cls.extract_text_content(content),
                    }
                    result.append(msg_system)
                elif role == "developer":
                    msg_developer = {
                        "role": "developer",
                        "content": cls.extract_text_content(content),
                    }
                    result.append(msg_developer)
                else:
                    raise UserError(f"Unexpected role in input_message: {role}")

            # 3) response output message => assistant
            elif resp_msg := cls.maybe_response_output_message(item):
                flush_assistant_message()
                new_asst = ChatCompletionAssistantMessageParam(role="assistant")
                contents = resp_msg["content"]
                text_segments = []
                for c in contents:
                    if c["type"] == "output_text":
                        text_segments.append(c["text"])
                    elif c["type"] == "refusal":
                        new_asst["refusal"] = c["refusal"]
                    elif c["type"] == "output_audio":
                        # Can't handle this, b/c chat completions expects an ID which we dont have
                        raise UserError(
                            f"Only audio IDs are supported for chat completions, but got: {c}"
                        )
                    elif c["type"] == "reasoning_content":
                        reasoning_content_tmp = _json_loads_nullable(c.get("reasoning_content"))
                        if isinstance(reasoning_content_tmp, dict):
                            field_name, value = reasoning_content_tmp.get("field_name"), reasoning_content_tmp.get("value")
                            new_asst[field_name] = value
                        # deserialize the extra contents in the tool calls, they will be used later to fill back the extra content in the tool calls
                        new_asst["extra_contents_in_tool_calls"] = _json_loads_nullable(c.get("extra_contents_in_tool_calls"))
                        for thinking_field in _ANTHROPIC_THINKING_BLOCK_FIELDS:
                            thinking_value = c.get(thinking_field)
                            if thinking_value is not None:
                                new_asst[thinking_field] = _json_loads_nullable(thinking_value)
                    else:
                        raise UserError(f"Unknown content type in ExtendedResponseOutputMessage: {c}")

                if text_segments:
                    combined = "\n".join(text_segments)
                    new_asst["content"] = combined

                new_asst["tool_calls"] = []
                current_assistant_msg = new_asst

            # 4) function/file-search calls => attach to assistant
            elif file_search := cls.maybe_file_search_call(item):
                asst = ensure_assistant_message()
                tool_calls = list(asst.get("tool_calls", []))
                new_tool_call = ChatCompletionMessageToolCallParam(
                    id=file_search["id"],
                    type="function",
                    function={
                        "name": "file_search_call",
                        "arguments": json.dumps(
                            {
                                "queries": file_search.get("queries", []),
                                "status": file_search.get("status"),
                            }
                        ),
                    },
                )
                tool_calls.append(new_tool_call)
                asst["tool_calls"] = tool_calls

            elif func_call := cls.maybe_function_tool_call(item):
                asst = ensure_assistant_message()
                # each time we enter this, we will pop one extra content in the tool calls
                extra_contents_in_tool_calls = asst.get("extra_contents_in_tool_calls", [])
                tool_calls = list(asst.get("tool_calls", []))
                arguments = func_call["arguments"] if func_call["arguments"] else "{}"
                new_tool_call = ChatCompletionMessageToolCallParam(
                    id=func_call["call_id"],
                    type="function",
                    function={
                        "name": func_call["name"],
                        "arguments": arguments,
                    },
                )
                if extra_contents_in_tool_calls is not None and len(extra_contents_in_tool_calls) > 0:
                    # pop the first extra content in the tool calls
                    # put them back to the tool_call
                    extra_content = extra_contents_in_tool_calls.pop(0)
                    if extra_content is not None:
                        new_tool_call["extra_content"] = extra_content
                tool_calls.append(new_tool_call)
                asst["tool_calls"] = tool_calls
            # 5) function call output => tool message
            elif func_output := cls.maybe_function_tool_call_output(item):
                flush_assistant_message()
                msg: ChatCompletionToolMessageParam = {
                    "role": "tool",
                    "tool_call_id": func_output["call_id"],
                    "content": func_output["output"],
                }
                result.append(msg)

            # 6) item reference => handle or raise
            elif item_ref := cls.maybe_item_reference(item):
                raise UserError(
                    f"Encountered an item_reference, which is not supported: {item_ref}"
                )

            # 7) If we haven't recognized it => fail or ignore
            else:
                raise UserError(f"Unhandled item type or structure: {item}")

        flush_assistant_message()
        return result


def _extract_chunk_reasoning(delta: Any) -> str:
    """Pull the reasoning text out of a streaming delta.

    Providers disagree on the field name: the pangu endpoint used here emits the
    same text under both `reasoning` and `reasoning_content`, while the
    non-streaming payload only carries `reasoning_content`. Prefer
    `reasoning_content` so both paths normalize to the same field. The order
    matches `_extract_message_reasoning` so streaming and non-streaming turns
    round-trip under the same key.
    """
    for field_name in _REASONING_FIELD_NAMES:
        value = getattr(delta, field_name, None)
        if value is None:
            model_extra = getattr(delta, "model_extra", None)
            if isinstance(model_extra, dict):
                value = model_extra.get(field_name)
        if value:
            return value if isinstance(value, str) else str(value)
    return ""


_STREAM_DIAG_PREFIX = "[STREAM_DIAG]"

# `finish_reason` values that mean the provider finished the turn on purpose.
# Anything else (`length`, `content_filter`, a provider-specific string) means
# the turn was cut short, which downstream is indistinguishable from a
# deliberate stop — the failure this instrumentation exists to catch.
_CLEAN_FINISH_REASONS = {"stop", "tool_calls", "function_call"}

# A turn whose visible text dwarfs its reasoning. Healthy turns here run a few
# hundred characters of text against thousands of characters of reasoning
# (measured ~94:1 the other way); the degraded turns that burn through
# `max_tokens` instead dump a runaway monologue into the *text* channel. The
# threshold sits far above any healthy turn observed and far below the
# pathological ones (257K–611K chars), so it does not fire on normal traffic.
_TEXT_FLOOD_CHARS = 20_000

# How much of the run-on text to keep when a turn looks degraded. Enough to see
# where the model got stuck without dumping hundreds of KB into the log.
_TEXT_TAIL_CHARS = 2000

# Opt-in: set TOOLATHLON_STREAM_DIAG=all to log every streamed turn (for
# capturing a healthy baseline). Unset, only anomalous turns are logged.
_STREAM_DIAG_ALL = os.environ.get("TOOLATHLON_STREAM_DIAG", "").strip().lower() == "all"


def _stream_diag(message: str) -> None:
    """Emit one line about a streamed turn that did not end cleanly.

    A truncated turn and a deliberate stop reach `response.completed` looking
    the same downstream, and the agent loop treats both as "the model chose to
    finish" — which silently ends the task with the work undone. By the time
    that shows up in the conversation history, the facts that would explain it
    (which `finish_reason` arrived, how much each channel produced, what the
    model was mid-sentence saying) are gone. So they are captured here, at the
    only point where they still exist.

    Only anomalous turns are logged; healthy ones stay silent so a full run's
    log remains readable and any line that *does* appear is worth reading.
    """
    try:
        print(f"{_STREAM_DIAG_PREFIX} {message}", flush=True)
    except Exception:
        # Never let a diagnostic break the stream it is describing.
        pass


def _classify_stream_end(
    finish_reasons: list[str],
    content_chars: int,
    reasoning_chars: int,
    has_payload: bool,
) -> str | None:
    """Return why this stream's ending is worth logging, or None if it is normal.

    Kept separate from `handle_stream` so the conditions can be read (and
    adjusted) in one place.
    """
    if not finish_reasons:
        # No terminal chunk at all: the stream was severed mid-generation.
        return "STREAM_SEVERED"
    unexpected = [r for r in finish_reasons if r not in _CLEAN_FINISH_REASONS]
    if unexpected:
        # The provider said it stopped early ("length" on a max_tokens hit,
        # "content_filter", or anything else non-terminal). The value itself is
        # reported in the `finish_reason=` field of the log line.
        return "TRUNCATED"
    if not has_payload:
        # Nothing usable was produced, so this turn reads as a deliberate stop.
        return "EMPTY_TURN"
    if content_chars > _TEXT_FLOOD_CHARS:
        # Runaway monologue landing in the visible-text channel instead of the
        # reasoning channel — the precursor to burning through max_tokens.
        return f"TEXT_FLOOD content_chars={content_chars} reasoning_chars={reasoning_chars}"
    return None


class TruncatedStreamError(Exception):
    """The SSE stream ended without the provider ever signalling a clean stop.

    Raised so the retry loop in `get_response` re-issues the request instead of
    handing back a half-finished turn. See `ChatCmplStreamHandlerWithReasoning`.
    """


class ChatCmplStreamHandlerWithReasoning(ChatCmplStreamHandler):
    """`ChatCmplStreamHandler` that keeps `reasoning_content` instead of dropping it.

    The upstream handler only accumulates `delta.content` and `delta.tool_calls`.
    For reasoning models that stream their chain of thought on a separate delta
    field this silently discards it, so every subsequent turn is sent back
    without the model's own earlier thinking. The model then re-derives it from
    scratch and, on this endpoint, degenerates into repeating itself until it
    burns through `max_tokens`. The non-streaming path
    (`ConverterWithExplicitReasoningContent.message_to_output_items`) already
    preserves reasoning; this restores parity for the streaming path.

    It additionally tracks how the stream ended. The upstream handler never looks
    at `finish_reason`, so a stream that is severed mid-generation (connection
    reset, gateway timeout, or a run that only ever emitted reasoning) still
    yields a `response.completed` event indistinguishable from a real finish.
    Downstream that became a *silent* task death: a turn with no text and no
    tool call reads as "the model chose to stop", so the agent loop exits and
    the task is recorded as `success` without having done the work.
    """

    @classmethod
    async def handle_stream(
        cls,
        response: Response,
        stream: AsyncStream[ChatCompletionChunk],
    ) -> AsyncIterator[TResponseStreamEvent]:
        reasoning_parts: list[str] = []
        saw_terminal_finish_reason = False
        # Diagnostic counters (see `_stream_diag`).
        finish_reasons: list[str] = []
        n_content_chunks = 0
        n_reasoning_chunks = 0
        n_tool_call_chunks = 0
        content_chars = 0
        # Bounded tail of the visible text. If the turn turns out to be degraded,
        # where the model stopped mid-sentence is the single most useful thing to
        # see; keeping only the tail means a 600K-character runaway costs a 2K
        # budget instead of the whole turn.
        text_tail = ""

        # Tee the raw chunks first: `super().handle_stream` consumes the stream
        # itself, so reasoning (and the finish reason) has to be harvested on the
        # way past.
        async def tee() -> AsyncIterator[ChatCompletionChunk]:
            nonlocal saw_terminal_finish_reason, n_content_chunks, n_reasoning_chunks
            nonlocal n_tool_call_chunks, content_chars, text_tail
            async for chunk in stream:
                try:
                    choices = getattr(chunk, "choices", None)
                    if choices:
                        delta = choices[0].delta
                        if delta:
                            reasoning = _extract_chunk_reasoning(delta)
                            if reasoning:
                                reasoning_parts.append(reasoning)
                                n_reasoning_chunks += 1
                            # Track the visible-content channel separately: the
                            # two differ in which provider field the model's
                            # long monologue lands in, and that is what decides
                            # whether the max_tokens cap is ever reached.
                            delta_content = getattr(delta, "content", None)
                            if delta_content:
                                n_content_chunks += 1
                                content_chars += len(delta_content)
                                text_tail = (text_tail + delta_content)[-_TEXT_TAIL_CHARS:]
                            if getattr(delta, "tool_calls", None):
                                n_tool_call_chunks += 1
                        # A provider that completes normally sends at least one
                        # chunk carrying a non-null finish_reason. Only the clean
                        # values in `_CLEAN_FINISH_REASONS` mean the turn ended on
                        # purpose; `length` (max_tokens hit) and `content_filter`
                        # are the provider cutting the turn short, and must not
                        # count as a terminal stop — see the completed-event
                        # handler below.
                        chunk_finish_reason = getattr(choices[0], "finish_reason", None)
                        if chunk_finish_reason:
                            if str(chunk_finish_reason) in _CLEAN_FINISH_REASONS:
                                saw_terminal_finish_reason = True
                            finish_reasons.append(str(chunk_finish_reason))
                except Exception:
                    # Never let bookkeeping break the actual stream.
                    pass
                yield chunk

        async for event in super().handle_stream(response, tee()):
            if event.type == "response.completed":
                # The stream is exhausted and the real assembled response exists
                # on this event, so both the payload check and the ending
                # classification can be made accurately here. (The `response`
                # passed into `handle_stream` is only an empty template — reading
                # it for either purpose would always report "no payload".)
                has_payload = _response_has_payload(event.response)
                verdict = _classify_stream_end(
                    finish_reasons,
                    content_chars,
                    sum(len(p) for p in reasoning_parts),
                    has_payload,
                )
                if verdict or _STREAM_DIAG_ALL:
                    _stream_diag(
                        "model=%s verdict=%s finish_reason=%s content_chunks=%d "
                        "content_chars=%d reasoning_chunks=%d reasoning_chars=%d "
                        "tool_call_chunks=%d payload=%s text_tail=%r"
                        % (
                            getattr(cls, "_diag_model_name", "?"),
                            verdict or "CLEAN",
                            finish_reasons or "NONE",
                            n_content_chunks,
                            content_chars,
                            n_reasoning_chunks,
                            sum(len(p) for p in reasoning_parts),
                            n_tool_call_chunks,
                            has_payload,
                            text_tail[-_TEXT_TAIL_CHARS:],
                        )
                    )
                if not saw_terminal_finish_reason:
                    # The stream never carried a clean finish_reason, so the turn
                    # did not end on purpose. Two shapes reach here:
                    #   - severed with nothing usable (record 15 in the proxy
                    #     log: a single `role` chunk, no [DONE]) — the original
                    #     case this guard was written for;
                    #   - severed or `length`-truncated *with* text: the model
                    #     was mid-monologue when the provider cut it, and the
                    #     half-written text must not be mistaken for a deliberate
                    #     stop. That second shape is bug 2 — `has_payload` is True
                    #     for truncated text, so the payload check alone cannot
                    #     catch it.
                    # In both cases raising here makes `get_response` re-issue the
                    # request instead of returning a half-finished turn that the
                    # agent loop would read as "the model chose to stop", ending
                    # the task silently with the work undone.
                    raise TruncatedStreamError(
                        "stream ended without a clean finish_reason "
                        f"(finish_reasons: {finish_reasons or 'NONE'}, "
                        f"payload: {has_payload}, reasoning chars: "
                        f"{sum(len(p) for p in reasoning_parts)})"
                    )
                try:
                    event = cls._attach_reasoning(event, "".join(reasoning_parts))
                except Exception as e:
                    # Preserving reasoning must never cost us the turn itself: on
                    # any failure fall back to the plain completed event.
                    logger.warning(f"Failed to attach streamed reasoning content: {e}")
            yield event

    @classmethod
    def _attach_reasoning(cls, event: ResponseStreamEvent, reasoning_text: str) -> ResponseStreamEvent:
        if not reasoning_text:
            return event

        final_response = event.response
        reasoning_content = json.dumps({"field_name": "reasoning_content", "value": reasoning_text})

        outputs: list[Any] = list(final_response.output)
        message = next(
            (item for item in outputs if getattr(item, "type", None) == "message"),
            None,
        )

        if message is None:
            # Reasoning-only turn: the upstream handler emits no message item, so
            # create one in the same shape the non-streaming path produces.
            message = ExtendedResponseOutputMessage(
                id=FAKE_RESPONSES_ID,
                content=[],
                role="assistant",
                type="message",
                status="completed",
            )
            outputs.append(message)
        elif not isinstance(message, ExtendedResponseOutputMessage):
            # The upstream handler builds a plain ResponseOutputMessage whenever the
            # turn carries text. Its `content` field is typed as the base union
            # (output_text | refusal), so appending a reasoning block to it makes
            # pydantic emit a serializer warning on every later dump. Promote it to
            # the extended class, whose content field accepts the reasoning block.
            # This is the root cause of the warnings seen in run3 — suppressing
            # them at the dump sites was treating the symptom.
            outputs[outputs.index(message)] = message = ExtendedResponseOutputMessage(
                **message.model_dump()
            )

        message.content = list(message.content) + [
            ResponseOutputReasoningContent(
                reasoning_content=reasoning_content,
                pure_thinking_str=reasoning_text,
                extra_contents_in_tool_calls=json.dumps(None),
                type="reasoning_content",
            )
        ]

        patched = final_response.model_copy(update={"output": outputs})
        return event.model_copy(update={"response": patched})


def _response_has_payload(response: Response) -> bool:
    """True if the response carries a real assistant output.

    A "payload" is visible text or a tool call — the two things the agent loop
    acts on. A message that holds only a reasoning block is *not* a payload:
    the model was still thinking when the turn ended, and the caller must not
    mistake that for a deliberate finish.
    """
    for item in getattr(response, "output", None) or []:
        item_type = getattr(item, "type", None)
        if item_type == "function_call":
            return True
        if item_type == "message":
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", None) in ("output_text", "refusal"):
                    return True
    return False


def _model_response_has_payload(model_response: Any) -> bool:
    """True if a `ModelResponse` carries text or a tool call.

    Companion to `_response_has_payload` for the already-converted items the
    retry loop sees. Reasoning-only items do not count — see that function.
    """
    for item in getattr(model_response, "output", None) or []:
        item_type = getattr(item, "type", None)
        if item_type == "function_call":
            return True
        if item_type == "message":
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", None) in ("output_text", "refusal"):
                    return True
    return False


class ContextTooLongError(Exception):
    """Context length exceeded error"""
    def __init__(self, message, token_count=None, max_tokens=None):
        super().__init__(message)
        self.token_count = token_count
        self.max_tokens = max_tokens

class OpenAIChatCompletionsModelWithRetry(OpenAIChatCompletionsModel):
    def __init__(self, model: str, 
                 openai_client: AsyncOpenAI, 
                 retry_times: int = 10, # FIXME: hardcoded now, should be dynamic
                 retry_delay: float = 10.0,
                 debug: bool = True,
                 short_model_name: str | None = None): # FIXME: hardcoded now, should be dynamic
        super().__init__(model=model, openai_client=openai_client)
        self.retry_times = retry_times
        self.retry_delay = retry_delay
        self.debug = debug
        self.short_model_name = short_model_name

    def _add_cache_control_to_messages(self, messages: list, min_cache_tokens: int = 2048) -> list:
        """
        Add cache_control breakpoints to messages every 20 blocks from the front.
        No more than 4 breakpoints total. If message count is < 20, add at the last position only.
        Additional logic is removed, only every 20th (and last if needed) gets cache_control.
        """
        if not messages:
            return messages

        total = len(messages)
        indices = []

        # Compute target indices for cache_control
        if total <= 20:
            indices = [total - 1]
        else:
            segs = [i for i in range(19, total, 20)]
            indices.extend(segs)
            # Always add the last message if it's not already in segs and we haven't hit four
            if (total - 1) not in indices and len(indices) < 4:
                indices.append(total - 1)
            # Cap to at most 4
            indices = indices[:4]

        modified_messages = []
        for i, message in enumerate(messages):
            new_message = message.copy()
            if i in indices and isinstance(message.get('content'), str):
                new_message['content'] = [
                    {
                        'type': 'text',
                        'text': message['content'],
                        'cache_control': {
                            'type': 'ephemeral'
                        }
                    }
                ]
                # if self.debug:
                    # print(f"🔄 PROMPT CACHING: Added cache_control to message index {i}")
            modified_messages.append(new_message)
        return modified_messages

    def _get_model_specific_config(self):
        """Get model-specific configuration parameters"""
        if 'gpt-5' in self.model:
            basic = {
                'use_max_completion_tokens': True,
                'use_parallel_tool_calls': True
            }
            if "low" in self.short_model_name:
                basic['reasoning_effort'] = "low"
            elif "medium" in self.short_model_name:
                basic['reasoning_effort'] = "medium"
            elif "high" in self.short_model_name:
                basic['reasoning_effort'] = "high"
            return basic
        elif 'o4' in self.model or 'o3' in self.model or 'gemini' in self.model: # for gemini it will raise an error stating it does not support parallel tool calls, but if I remove this parameter, it can still do so, not sure if it has a different parameter name ...
            return {
                'use_max_completion_tokens': True,
                'use_parallel_tool_calls': False
            }
        elif 'claude' in self.model.lower():
            return {
                'use_max_completion_tokens': False,
                'use_parallel_tool_calls': True,
                'supports_prompt_caching': True,
            }
        else:
            return {
                'use_max_completion_tokens': False,
                'use_parallel_tool_calls': True
            }

    @staticmethod
    def _usage_from_chat_completion(response: ChatCompletion) -> Usage:
        if response.usage:
            usage = Usage(
                requests=1,
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
            )
            return add_cached_token_details(usage, response.usage)
        return Usage()

    @staticmethod
    def _usage_from_response(response: Response) -> Usage:
        if response.usage:
            usage = Usage(
                requests=1,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                total_tokens=response.usage.total_tokens,
            )
            return add_cached_token_details(usage, response.usage)
        return Usage()

    async def _collect_streamed_response(
        self,
        response: Response,
        stream: AsyncStream[ChatCompletionChunk],
    ) -> Response:
        final_response: Response | None = None
        async for event in ChatCmplStreamHandlerWithReasoning.handle_stream(response, stream):
            if event.type == "response.completed":
                final_response = event.response

        return final_response or response

    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        span: Span[GenerationSpanData],
        tracing: ModelTracing,
        stream: bool = False,
    ) -> ChatCompletion | tuple[Response, AsyncStream[ChatCompletionChunk]]:
        converted_messages = ConverterWithExplicitReasoningContent.items_to_messages(input)

        if system_instructions:
            converted_messages.insert(
                0,
                {
                    "content": system_instructions,
                    "role": "system",
                },
            )
        
        # Add prompt caching for Claude models
        model_config = self._get_model_specific_config()
        if model_config.get('supports_prompt_caching', False):
            # if self.debug:
            #     print(f"🔄 PROMPT CACHING: Enabled for Claude model: {self.model}")
            converted_messages = self._add_cache_control_to_messages(converted_messages)
        if tracing.include_data():
            span.span_data.input = converted_messages

        parallel_tool_calls = (
            True
            if model_settings.parallel_tool_calls and tools and len(tools) > 0
            else False
            if model_settings.parallel_tool_calls is False
            else NOT_GIVEN
        )
        tool_choice = ConverterWithExplicitReasoningContent.convert_tool_choice(model_settings.tool_choice)
        response_format = ConverterWithExplicitReasoningContent.convert_response_format(output_schema)

        converted_tools = [ConverterWithExplicitReasoningContent.tool_to_openai(tool) for tool in tools] if tools else []

        for handoff in handoffs:
            converted_tools.append(ConverterWithExplicitReasoningContent.convert_handoff_tool(handoff))

        if _debug.DONT_LOG_MODEL_DATA:
            logger.debug("Calling LLM")
        else:
            logger.debug(
                f"{json.dumps(converted_messages, indent=2)}\n"
                f"Tools:\n{json.dumps(converted_tools, indent=2)}\n"
                f"Stream: {stream}\n"
                f"Tool choice: {tool_choice}\n"
                f"Response format: {response_format}\n"
            )

        reasoning_effort = model_settings.reasoning.get("effort") if isinstance(model_settings.reasoning, Dict) else (model_settings.reasoning.get("effort") if model_settings.reasoning else None)
        store = ChatCmplHelpers.get_store_param(self._get_client(), model_settings)

        stream_options = ChatCmplHelpers.get_stream_options_param(
            self._get_client(), model_settings, stream=stream
        )


        # Load user-specified model parameters if provided
        user_model_params = None
        model_params_file = os.environ.get('TOOLATHLON_MODEL_PARAMS_FILE')
        if model_params_file and os.path.exists(model_params_file):
            try:
                with open(model_params_file, 'r') as f:
                    user_model_params = json.load(f)
                logger.info(f"[ModelProvider] Using custom model parameters from: {model_params_file}")
                logger.info(f"[ModelProvider] Custom parameters: {json.dumps(user_model_params)}")
            except Exception as e:
                logger.warning(f"[ModelProvider] Failed to load model parameters file: {e}")

        # Build base parameters
        extra_request_params = _get_extra_request_params(self, model_settings)
        if user_model_params:
            # User specified parameters: only use required params + user params
            logger.info("[ModelProvider] Using USER-SPECIFIED parameters mode")
            base_params = {
                'model': self.model,
                'messages': converted_messages,
                'tools': converted_tools or NOT_GIVEN,
                'tool_choice': tool_choice,
                'stream': extra_request_params.get('stream', stream),
                **extra_request_params,
                **user_model_params  # User's custom parameters
            }
        else:
            # Default mode: use all parameters from model_settings
            logger.info("[ModelProvider] Using DEFAULT parameters mode")
            base_params = {
                'model': self.model,
                'messages': converted_messages,
                'tools': converted_tools or NOT_GIVEN,

                'temperature': self._non_null_or_not_given(model_settings.temperature),
                'top_p': self._non_null_or_not_given(model_settings.top_p),
                'frequency_penalty': self._non_null_or_not_given(model_settings.frequency_penalty),
                'presence_penalty': self._non_null_or_not_given(model_settings.presence_penalty),
                'tool_choice': tool_choice,
                'response_format': response_format,
                'stream': extra_request_params.get('stream', stream),
                'stream_options': self._non_null_or_not_given(stream_options),
                'store': self._non_null_or_not_given(store),
                'reasoning_effort': self._non_null_or_not_given(reasoning_effort),
                'extra_headers': { **HEADERS, **(model_settings.extra_headers or {}) },
                'extra_query': model_settings.extra_query,
                'extra_body': model_settings.extra_body,
                'metadata': self._non_null_or_not_given(model_settings.metadata),
            }

            # Add model-specific parameters
            if model_config['use_max_completion_tokens']:
                base_params['max_completion_tokens'] = self._non_null_or_not_given(model_settings.max_tokens)
            else:
                base_params['max_tokens'] = self._non_null_or_not_given(model_settings.max_tokens)

            if model_config['use_parallel_tool_calls']:
                base_params['parallel_tool_calls'] = parallel_tool_calls

            # override reasoning_effort
            if model_config.get('reasoning_effort') is not None:
                base_params['reasoning_effort'] = model_config['reasoning_effort']

            # for claude-4.5-sonnet, top_p and temperament cannot be set simultaneously
            if "claude" in self.model and any(version in self.model for version in ["4.5", "4-5"]):
                base_params.pop('top_p')

            base_params.update(extra_request_params)
        
        ret = await self._get_client().chat.completions.create(**base_params)

        # Diagnostic only: `handle_stream` is a classmethod with no handle on the
        # instance, so stash the model name for `_stream_diag` to report.
        ChatCmplStreamHandlerWithReasoning._diag_model_name = str(self.model)

        if isinstance(ret, ChatCompletion):
            return ret

        response = Response(
            id=FAKE_RESPONSES_ID,
            created_at=time.time(),
            model=self.model,
            object="response",
            output=[],
            tool_choice=cast(Literal["auto", "required", "none"], tool_choice)
            if tool_choice != NOT_GIVEN
            else "auto",
            top_p=model_settings.top_p,
            temperature=model_settings.temperature,
            tools=[],
            parallel_tool_calls=parallel_tool_calls or False,
            # No `reasoning=` here on purpose. This object is only a template for
            # the streaming path (it is never sent to the API and nothing reads
            # this field back — `raw_get_response` only consumes `output`), but
            # `Response` validates it against the client-side
            # `ReasoningEffort = Literal["low", "medium", "high"]`. Server-side
            # extensions such as `"max"` therefore raise a pydantic
            # ValidationError and kill the whole turn. The real effort value
            # still travels to the server via `base_params["reasoning_effort"]`.
        )
        return response, ret

    async def raw_get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        previous_response_id: str | None,
    ) -> ModelResponse:
        with generation_span(
            model=str(self.model),
            model_config=model_settings.to_json_dict() | {"base_url": str(self._client.base_url)},
            disabled=tracing.is_disabled(),
        ) as span_generation:
            response = await self._fetch_response(
                system_instructions,
                input,
                model_settings,
                tools,
                output_schema,
                handoffs,
                span_generation,
                tracing,
                stream=False,
            )

            message: ChatCompletionMessage | None = None
            first_choice: Choice | None = None

            if isinstance(response, tuple):
                initial_response, stream = response
                final_response = await self._collect_streamed_response(initial_response, stream)
                usage = self._usage_from_response(final_response)
                items = final_response.output

                if _debug.DONT_LOG_MODEL_DATA:
                    logger.debug("Received model response")
                else:
                    logger.debug(
                        "LLM resp:\n%s\n",
                        json.dumps(_dump_model(final_response), indent=2, ensure_ascii=False),
                    )

                if tracing.include_data():
                    span_generation.span_data.output = [_dump_model(final_response)]
            else:
                if response.choices and len(response.choices) > 0:
                    first_choice = response.choices[0]
                    message = first_choice.message

                if _debug.DONT_LOG_MODEL_DATA:
                    logger.debug("Received model response")
                else:
                    if message is not None:
                        logger.debug(
                            "LLM resp:\n%s\n",
                            json.dumps(message.model_dump(), indent=2, ensure_ascii=False),
                        )
                    else:
                        finish_reason = first_choice.finish_reason if first_choice else "-"
                        logger.debug(f"LLM resp had no message. finish_reason: {finish_reason}")

                usage = self._usage_from_chat_completion(response)
                if tracing.include_data():
                    span_generation.span_data.output = (
                        [message.model_dump()] if message is not None else []
                    )
                items = (
                    ConverterWithExplicitReasoningContent.message_to_output_items(message)
                    if message is not None
                    else []
                )

            span_generation.span_data.usage = {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            }

            return ModelResponse(
                output=items,
                usage=usage,
                response_id=None,
            )

    async def get_response(self, *args, **kwargs):
        for i in range(self.retry_times):
            try:
                model_response = await self.raw_get_response(*args, **kwargs)
                output_items = model_response.output
                if self.debug:
                    for item in output_items:
                        if isinstance(item, ExtendedResponseOutputMessage):
                            pure_thinking_str = None
                            for content in item.content:
                                if isinstance(content, ResponseOutputReasoningContent):
                                    pure_thinking_str = content.pure_thinking_str
                                    break
                            if pure_thinking_str:
                                print("\033[90mTHINKING: ", pure_thinking_str.strip(), "\033[0m")
                            # a little bit ugly ...
                            elif isinstance(content, ResponseOutputReasoningContent):
                                if _has_anthropic_thinking_blocks(content):
                                    _print_implicit_thinking()
                                else:
                                    extra_contents = _json_loads_nullable(content.extra_contents_in_tool_calls)
                                    if isinstance(extra_contents, list) and any(extra_contents):
                                        _print_implicit_thinking()
                            # find text content in the output items
                            text_content = None
                            for content in item.content:
                                if isinstance(content, ResponseOutputText):
                                    text_content = content.text
                                    break
                            if text_content:
                                match = re.search(r'<think>(.*?)</think>', text_content, re.DOTALL)
                                display_text = text_content
                                if match:
                                    thinking = match.group(1).strip()
                                    print("\033[90mTHINKING: ", thinking, "\033[0m")
                                    # Remove only the first <think>...</think> for the assistant answer
                                    display_text = text_content[:match.start()] + text_content[match.end():]
                                stripped_text = display_text.strip()
                                if stripped_text:
                                    print("ASSISTANT: ", stripped_text)

                # A turn with neither text nor a tool call is not a legitimate
                # end-of-task signal — the model was still thinking when the
                # stream stopped. Returning it would make the agent loop exit
                # and mark the task `success` with the work undone, so treat it
                # as a failed attempt and let the retry loop re-issue.
                # if not _model_response_has_payload(model_response):
                #     raise TruncatedStreamError(
                #         "model returned a turn with no text and no tool call "
                #         f"(items: {len(output_items)})"
                #     )

                return model_response
            except Exception as e:
                error_str = str(e)                
                # Detect various forms of context too long errors
                context_too_long = False
                current_tokens, max_tokens = None, None
                
                # 1. Check if error code is 400 (usually means bad request)
                if "Error code: 400" in error_str:
                    # Directly search for keywords in error string
                    lower_error = error_str.lower()
                    if any(pattern in lower_error for pattern in [
                        'token count exceeds',
                        'exceeds the maximum',
                        'string too long',
                        'too long',
                        'context_length_exceeded',
                        'maximum context length',
                        'token limit exceeded',
                        'content too long',
                        'message too long',
                        'prompt is too long',
                        'maximum number of tokens',
                        'maximum prompt length is', # for xAI model
                        'request exceeded model token limit', # for kimi
                        'exceed max message tokens', # for seed
                        "exceeds the model's context window",
                        'is longer than'
                    ]):
                        context_too_long = True
                        
                        # Try to extract token numbers from message
                        # Pattern 1: "input token count exceeds the maximum number of tokens allowed (1048576)"
                        match = re.search(r'maximum number of tokens allowed \((\d+)\)', error_str)
                        if match:
                            max_tokens = int(match.group(1))
                        
                        # Pattern 2: "123456 tokens > 100000 maximum"
                        match = re.search(r'(\d+) tokens > (\d+) maximum', error_str)
                        if match:
                            current_tokens, max_tokens = int(match.group(1)), int(match.group(2))
                        
                        # Pattern 3: "maximum length 10485760, but got a string with length 30893644"
                        match = re.search(r'maximum length (\d+).*length (\d+)', error_str)
                        if match:
                            max_tokens, current_tokens = int(match.group(1)), int(match.group(2))
                        
                        # Pattern 4: xAI
                        match = re.search(r'maximum prompt length is (\d+).*request contains (\d+)', error_str)
                        if match:
                            max_tokens, current_tokens = int(match.group(1)), int(match.group(2))
                        
                        # Pattern 5: kimi
                        match = re.search(r'request exceeded model token limit: (\d+)', error_str)
                        if match:
                            max_tokens = int(match.group(1))

                        # Pattern 6: "Prompt length plus max_tokens exceeds the
                        # model's context window: 266350 prompt tokens + 256000
                        # max_tokens > 262144."
                        match = re.search(
                            r"(\d+) prompt tokens \+ (\d+) max_tokens > (\d+)",
                            error_str,
                            re.IGNORECASE,
                        )
                        if match:
                            prompt_tokens, requested_output_tokens, max_tokens = map(int, match.groups())
                            current_tokens = prompt_tokens + requested_output_tokens
                
                # 2. Try parsing structured error (OpenAI API error object)
                if hasattr(e, 'response') and hasattr(e.response, 'json'):
                    try:
                        error_data = e.response.json()
                        error_msg = error_data.get('error', {}).get('message', '').lower()
                        error_code = error_data.get('error', {}).get('code', '')
                        error_type = error_data.get('error', {}).get('type', '')
                        
                        if any(pattern in error_msg for pattern in [
                            'token count exceeds',
                            'exceeds the maximum',
                            'string too long',
                            'too long',
                            'context_length_exceeded',
                            'maximum context length',
                            'token limit exceeded',
                            'content too long',
                            'message too long',
                            'prompt is too long',
                            'maximum number of tokens',
                            'maximum prompt length is', # for xAI model
                            'request exceeded model token limit', # for kimi
                            'exceed max message tokens', # for seed
                            "exceeds the model's context window",
                            'is longer than'
                        ]) or error_code in ['string_above_max_length', 'context_length_exceeded', 'messages_too_long']:
                            context_too_long = True
                    except:
                        pass
                
                # 3. Extra safety: check for any error containing a certain keyword
                if not context_too_long:
                    lower_error = error_str.lower()
                    if any(pattern in lower_error for pattern in [
                        'token count exceeds',
                        'exceeds the maximum',
                        'string too long',
                        'too long',
                        'context_length_exceeded',
                        'maximum context length',
                        'token limit exceeded',
                        'content too long',
                        'message too long',
                        'prompt is too long',
                        'maximum number of tokens',
                        'maximum prompt length is', # for xAI model
                        'request exceeded model token limit', # for kimi
                        'exceed max message tokens', # for seed
                        "exceeds the model's context window",
                        'is longer than'
                    ]):
                        context_too_long = True
                
                # If context too long detected, do not retry, raise
                if context_too_long:
                    if self.debug:
                        print(f"Context too long detected: {error_str}")
                    
                    # Create more detailed error message
                    error_msg = f"Context too long: {error_str}"
                    if current_tokens and max_tokens:
                        error_msg = f"Context too long: current={current_tokens} tokens, max={max_tokens} tokens. Original error: {error_str}"
                    elif max_tokens:
                        error_msg = f"Context too long: exceeds maximum of {max_tokens} tokens. Original error: {error_str}"
                    
                    raise ContextTooLongError(
                        error_msg,
                        token_count=current_tokens,
                        max_tokens=max_tokens
                    )
                
                # For other errors: continue retry logic
                if self.debug:
                    # import traceback
                    # traceback.print_exc()
                    print(f"Error in get_response: {e}, retry {i+1}/{self.retry_times}, waiting {self.retry_delay} seconds...")
                
                # Raise if it's the last try
                if i == self.retry_times - 1:
                    raise Exception(f"Failed to get response after {self.retry_times} retries, error: {e}")
                
                await asyncio.sleep(self.retry_delay)


class OpenAIResponsesModelWithRetry(OpenAIResponsesModel):
    def __init__(self, model: str, 
                 openai_client: AsyncOpenAI, 
                 retry_times: int = 10, # FIXME: hardcoded now, should be dynamic
                 retry_delay: float = 10.0,
                 debug: bool = True,
                 short_model_name: str | None = None): # FIXME: hardcoded now, should be dynamic
        super().__init__(model=model, openai_client=openai_client)
        self.retry_times = retry_times
        self.retry_delay = retry_delay
        self.debug = debug
        self.short_model_name = short_model_name

    async def get_response(self, *args, **kwargs):
        for i in range(self.retry_times):
            try:
                model_response = await super().get_response(*args, **kwargs)
                output_items = model_response.output
                if self.debug:
                    for item in output_items:
                        if isinstance(item, ResponseReasoningItem):
                            if item.summary and len(item.summary) > 0:
                                summary_text = "\n----------------------\n".join([s.text.strip() for s in item.summary])
                                print("\033[90mTHINKING SUMMARY: ", summary_text, "\033[0m")
                            else:
                                print("\033[90mTHINKING: (Thinking implicitly ...) \033[0m")
                        elif isinstance(item, ResponseOutputMessage):
                            if item.content and len(item.content) > 0:
                                print("ASSISTANT: ", item.content[0].text.strip())
                return model_response
            except Exception as e:
                error_str = str(e)       
                # Detect various forms of context too long errors
                context_too_long = False
                current_tokens, max_tokens = None, None
                
                # 1. Check if error code is 400 (usually means bad request)
                if "Error code: 400" in error_str:
                    # Directly search for keywords in error string
                    lower_error = error_str.lower()
                    if any(pattern in lower_error for pattern in [
                        'maximum context length is',
                        'exceeds the context window',
                        "exceeds the model's context window",
                        # Some OpenAI-Responses-compatible endpoints do not emit an
                        # explicit context-length message. When the (server-side,
                        # stateful) input exceeds the model's context window they
                        # instead return only a generic invalid_request_error 400.
                        # Treat that generic message as a context-too-long signal so
                        # the context-reset recovery path can trigger instead of
                        # burning through retries and hard-failing. This retry class
                        # is only used by the stateful-responses provider, so the
                        # blast radius of this heuristic is limited.
                        'the request contains invalid parameters',
                    ]):
                        context_too_long = True
                        
                        # Try to extract token numbers from message
                        # Pattern 1: "maximum context length is 400000 tokens. However, you requested about 482984 tokens" -- for openrouter responses, thought we should abort it
                        match = re.search(r'maximum context length is (\d+) tokens. however, you requested about (\d+) tokens', error_str)
                        if match:
                            max_tokens, current_tokens = int(match.group(1)), int(match.group(2))

                        match = re.search(
                            r"(\d+) prompt tokens \+ (\d+) max_tokens > (\d+)",
                            error_str,
                            re.IGNORECASE,
                        )
                        if match:
                            prompt_tokens, requested_output_tokens, max_tokens = map(int, match.groups())
                            current_tokens = prompt_tokens + requested_output_tokens
                
                # If context too long detected, do not retry, raise
                if context_too_long:
                    if self.debug:
                        print(f"Context too long detected: {error_str}")
                    
                    # Create more detailed error message
                    error_msg = f"Context too long: {error_str}"

                    if current_tokens and max_tokens:
                        error_msg = f"Context too long: current={current_tokens} tokens, max={max_tokens} tokens. Original error: {error_str}"
                    elif max_tokens:
                        error_msg = f"Context too long: exceeds maximum of {max_tokens} tokens. Original error: {error_str}"
                    
                    raise ContextTooLongError(
                        error_msg,
                        token_count=current_tokens,
                        max_tokens=max_tokens
                    )
                
                # For other errors: continue retry logic
                if self.debug:
                    # import traceback
                    # traceback.print_exc()
                    print(f"Error in get_response: {e}, retry {i+1}/{self.retry_times}, waiting {self.retry_delay} seconds...")
                
                # Raise if it's the last try
                if i == self.retry_times - 1:
                    raise Exception(f"Failed to get response after {self.retry_times} retries, error: {e}")
                
                await asyncio.sleep(self.retry_delay)

    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        previous_response_id: str | None,
        stream: Literal[True] | Literal[False] = False,
    ) -> Response | AsyncStream[ResponseStreamEvent]:
        list_input = ItemHelpers.input_to_new_input_list(input)

        parallel_tool_calls = (
            True
            if model_settings.parallel_tool_calls and tools and len(tools) > 0
            else False
            if model_settings.parallel_tool_calls is False
            else NOT_GIVEN
        )

        tool_choice = OpenAIResponsesConverter.convert_tool_choice(model_settings.tool_choice)
        converted_tools = OpenAIResponsesConverter.convert_tools(tools, handoffs)
        response_format = OpenAIResponsesConverter.get_response_format(output_schema)

        if _debug.DONT_LOG_MODEL_DATA:
            logger.debug("Calling LLM")
        else:
            logger.debug(
                f"Calling LLM {self.model} with input:\n"
                f"{json.dumps(list_input, indent=2)}\n"
                f"Tools:\n{json.dumps(converted_tools.tools, indent=2)}\n"
                f"Stream: {stream}\n"
                f"Tool choice: {tool_choice}\n"
                f"Response format: {response_format}\n"
                f"Previous response id: {previous_response_id}\n"
            )

        # Load user-specified model parameters if provided
        user_model_params = None
        model_params_file = os.environ.get('TOOLATHLON_MODEL_PARAMS_FILE')
        if model_params_file and os.path.exists(model_params_file):
            try:
                with open(model_params_file, 'r') as f:
                    user_model_params = json.load(f)
                logger.info(f"[ModelProvider] Using custom model parameters from: {model_params_file}")
                logger.info(f"[ModelProvider] Custom parameters: {json.dumps(user_model_params)}")
            except Exception as e:
                logger.warning(f"[ModelProvider] Failed to load model parameters file: {e}")

        # Build base parameters
        extra_request_params = _get_extra_request_params(self, model_settings)
        if user_model_params:
            # User specified parameters: only use required params + user params
            logger.info("[ModelProvider] Using USER-SPECIFIED parameters mode")
            base_params = {
                "previous_response_id": self._non_null_or_not_given(previous_response_id),
                "instructions": self._non_null_or_not_given(system_instructions),
                "model": self.model,
                "input": list_input,
                "include": converted_tools.includes,
                "tools": converted_tools.tools,
                "tool_choice": tool_choice,
                "parallel_tool_calls": parallel_tool_calls,
                "stream": stream,
                **extra_request_params,
                **user_model_params  # User's custom parameters
            }
        else:
            base_params = {
                "previous_response_id": self._non_null_or_not_given(previous_response_id),
                "instructions": self._non_null_or_not_given(system_instructions),
                "model": self.model,
                "input": list_input,
                "include": converted_tools.includes,
                "tools": converted_tools.tools,
                "temperature": self._non_null_or_not_given(model_settings.temperature),
                "top_p": self._non_null_or_not_given(model_settings.top_p),
                "truncation": self._non_null_or_not_given(model_settings.truncation),
                "max_output_tokens": self._non_null_or_not_given(model_settings.max_tokens),
                "tool_choice": tool_choice,
                "parallel_tool_calls": parallel_tool_calls,
                "stream": stream,
                "extra_headers": {**_HEADERS, **(model_settings.extra_headers or {})},
                "extra_query": model_settings.extra_query,
                "extra_body": model_settings.extra_body,
                "text": response_format,
                "store": self._non_null_or_not_given(model_settings.store),
                "reasoning": self._non_null_or_not_given(model_settings.reasoning),
                "metadata": self._non_null_or_not_given(model_settings.metadata),
            }
            base_params.update(extra_request_params)

        return await self._client.responses.create(**base_params)


class CustomModelProviderAiHubMix(ModelProvider):
    def get_model(self, model_name: str | None, debug: bool = True, short_model_name: str | None = None) -> Model:
        client = AsyncOpenAI(
            api_key=global_configs.aihubmix_key,
            base_url="https://aihubmix.com/v1",
        )
        return OpenAIChatCompletionsModelWithRetry(model=model_name, 
                                                   openai_client=client,
                                                   debug=debug,
                                                   short_model_name=short_model_name)

class CustomModelProviderAnthropic(ModelProvider):
    def get_model(self, model_name: str | None, debug: bool = True, short_model_name: str | None = None) -> Model:
        client = AsyncOpenAI(
            api_key=global_configs.anthropic_official_key,
            base_url="https://api.anthropic.com/v1/",
        )
        return OpenAIChatCompletionsModelWithRetry(model=model_name, 
                                                   openai_client=client,
                                                   debug=debug,
                                                   short_model_name=short_model_name)

class CustomModelProviderGoogle(ModelProvider):
    def get_model(self, model_name: str | None, debug: bool = True, short_model_name: str | None = None) -> Model:
        client = AsyncOpenAI(
            api_key=global_configs.google_official_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
        return OpenAIChatCompletionsModelWithRetry(model=model_name, 
                                                   openai_client=client,
                                                   debug=debug,
                                                   short_model_name=short_model_name)

class CustomModelProviderLocalVLLM(ModelProvider):
    def get_model(self, model_name: str | None, debug: bool = True, short_model_name: str | None = None) -> Model:
        import os
        vllm_base_url = os.getenv('VLLM_BASE_URL', 'http://localhost:8000/v1')
        client = AsyncOpenAI(
            api_key="fake-key",  # VLLM doesn't require a real API key
            base_url=vllm_base_url,
        )
        return OpenAIChatCompletionsModelWithRetry(model=model_name, 
                                                   openai_client=client,
                                                   debug=debug,
                                                   short_model_name=short_model_name)

class CustomModelProviderOpenRouter(ModelProvider):
    def get_model(self, model_name: str | None, debug: bool = True, short_model_name: str | None = None) -> Model:
        client = AsyncOpenAI(
            api_key=global_configs.openrouter_key,
            base_url="https://openrouter.ai/api/v1",
        )
        return OpenAIChatCompletionsModelWithRetry(model=model_name, 
                                                   openai_client=client,
                                                   debug=debug,
                                                   short_model_name=short_model_name)

class CustomModelProviderQwenOfficial(ModelProvider):
    def get_model(self, model_name: str | None, debug: bool = True, short_model_name: str | None = None) -> Model:
        client = AsyncOpenAI(
            api_key=global_configs.qwen_official_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        return OpenAIChatCompletionsModelWithRetry(model=model_name, 
                                                   openai_client=client,
                                                   debug=debug,
                                                   short_model_name=short_model_name)

class CustomModelProviderKimiOfficial(ModelProvider):
    def get_model(self, model_name: str | None, debug: bool = True, short_model_name: str | None = None) -> Model:
        client = AsyncOpenAI(
            api_key=global_configs.kimi_official_key,
            base_url="https://api.moonshot.cn/v1",
        )
        return OpenAIChatCompletionsModelWithRetry(model=model_name, 
                                                   openai_client=client,
                                                   debug=debug,
                                                   short_model_name=short_model_name)

class CustomModelProviderDeepSeekOfficial(ModelProvider):
    def get_model(self, model_name: str | None, debug: bool = True, short_model_name: str | None = None) -> Model:
        client = AsyncOpenAI(
            api_key=global_configs.deepseek_official_key,
            base_url="https://api.deepseek.com/v1",
        )
        return OpenAIChatCompletionsModelWithRetry(model=model_name, 
                                                   openai_client=client,
                                                   debug=debug,
                                                   short_model_name=short_model_name)

class CustomModelProviderXAI(ModelProvider):
    def get_model(self, model_name: str | None, debug: bool = True, short_model_name: str | None = None) -> Model:
        client = AsyncOpenAI(
            api_key=global_configs.xai_official_key,
            base_url="https://api.x.ai/v1",
        )
        return OpenAIChatCompletionsModelWithRetry(model=model_name, 
                                                   openai_client=client,
                                                   debug=debug,
                                                   short_model_name=short_model_name)

class CustomModelProviderZAI(ModelProvider):
    def get_model(self, model_name: str | None, debug: bool = True, short_model_name: str | None = None) -> Model:
        client = AsyncOpenAI(
            api_key=global_configs.zai_official_key,
            base_url="https://open.bigmodel.cn/api/paas/v4/",
        )
        return OpenAIChatCompletionsModelWithRetry(model=model_name, 
                                                   openai_client=client,
                                                   debug=debug,
                                                   short_model_name=short_model_name)
                                                   
class CustomModelProviderUnified(ModelProvider):
    def get_model(self, model_name: str | None, debug: bool = True, short_model_name: str | None = None) -> Model:
        import os
        base_url = os.getenv('TOOLATHLON_OPENAI_BASE_URL', None)
        if base_url is None:
            raise ValueError("TOOLATHLON_OPENAI_BASE_URL is not set! You must set it in the environment variables when using unified model provider!")
        api_key = os.getenv('TOOLATHLON_OPENAI_API_KEY', "fake-key")
        if api_key == "fake-key":
            print("[Warning] TOOLATHLON_OPENAI_API_KEY is not set! Usually this is only expected when you are running some self-deployed models like via vllm or sglang!")
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=_build_llm_timeout(),
            max_retries=0,
        )
        return OpenAIChatCompletionsModelWithRetry(model=model_name,
                                                   openai_client=client,
                                                   debug=debug,
                                                   short_model_name=short_model_name)

class CustomModelProviderOpenAIStatefulResponses(ModelProvider):
    def get_model(self, model_name: str | None, debug: bool = True, short_model_name: str | None = None) -> Model:
        import os
        base_url = os.getenv('TOOLATHLON_OPENAI_BASE_URL', None)
        if base_url is None:
            raise ValueError("TOOLATHLON_OPENAI_BASE_URL is not set! You must set it in the environment variables when using unified model provider!")
        api_key = os.getenv('TOOLATHLON_OPENAI_API_KEY', "fake-key")
        if api_key == "fake-key":
            print("[Warning] TOOLATHLON_OPENAI_API_KEY is not set! Usually this is only expected when you are running some self-deployed models like via vllm or sglang!")
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=_build_llm_timeout(),
            max_retries=0,
        )
        return OpenAIResponsesModelWithRetry(model=model_name, openai_client=client,
                                             debug=debug, short_model_name=short_model_name)

model_provider_mapping = {
    "aihubmix": CustomModelProviderAiHubMix,
    "anthropic": CustomModelProviderAnthropic,
    "local_vllm": CustomModelProviderLocalVLLM,
    "openrouter": CustomModelProviderOpenRouter,
    "qwen_official": CustomModelProviderQwenOfficial,
    "kimi_official": CustomModelProviderKimiOfficial,
    "deepseek_official": CustomModelProviderDeepSeekOfficial,
    "google": CustomModelProviderGoogle,
    "xai": CustomModelProviderXAI,
    "zai_official": CustomModelProviderZAI,
    "unified": CustomModelProviderUnified,
    "openai_stateful_responses": CustomModelProviderOpenAIStatefulResponses,
}

API_MAPPINGS = {
    'deepseek-v3.2-exp': Dict(
        api_model={"deepseek_official": "deepseek-chat"}, # 2025/9/29, this model may become other model later
        price=[0.28/1000, 0.42/1000], # an estimated price, no cache considered
        concurrency=32,
        context_window=128000,
    ),
    'gpt-5': Dict(
        api_model={"aihubmix": "gpt-5",
                   "openrouter": "openai/gpt-5"},
        price=[1.25/1000, 10/1000.0],
        concurrency=32,
        context_window=400000,
        openrouter_config={"provider": {"only": ["openai/default"]}}
    ),
    'gpt-5-low': Dict(
        api_model={"openrouter": "openai/gpt-5"},
        price=[1.25/1000, 10/1000.0],
        concurrency=32,
        context_window=400000,
        openrouter_config={"provider": {"only": ["openai/default"]}}
    ),
    'gpt-5-medium': Dict(
        api_model={"openrouter": "openai/gpt-5"},
        price=[1.25/1000, 10/1000.0],
        concurrency=32,
        context_window=400000,
        openrouter_config={"provider": {"only": ["openai/default"]}}
    ),
    'gpt-5-high': Dict(
        api_model={"openrouter": "openai/gpt-5"},
        price=[1.25/1000, 10/1000.0],
        concurrency=32,
        context_window=400000,
        openrouter_config={"provider": {"only": ["openai/default"]}}
    ),
    'gpt-5-mini': Dict(
        api_model={"aihubmix": "gpt-5-mini",
                   "openrouter": "openai/gpt-5-mini"},
        price=[0.25/1000,2/1000.0],
        concurrency=32,
        context_window=400000,
        openrouter_config={"provider": {"only": ["openai"]}}
    ),
    'o4-mini': Dict(
        api_model={"aihubmix": "o4-mini",
                   "openrouter": "openai/o4-mini"},
        price=[0.0011, 0.0044],
        concurrency=32,
        context_window=200000,
        openrouter_config={"provider": {"only": ["openai"]}}
    ),
    'o3': Dict(
        api_model={"aihubmix": "o3",
                   "openrouter": "openai/o3"},
        price=[0.010, 0.040],
        concurrency=32,
        context_window=200000,
        openrouter_config={"provider": {"only": ["openai"]}}
    ),
    'o3-pro': Dict(
        api_model={"aihubmix": "o3-pro"}, # no o3-pro in aihubmix
        price=[0.022, 0.088],
        concurrency=32,
        context_window=200000,
        openrouter_config={"provider": {"only": ["openai"]}}
    ),
    'claude-4.5-sonnet-0929': Dict(
        api_model={"aihubmix": "claude-sonnet-4-5-20250929",
                   "anthropic": "claude-sonnet-4-5-20250929",
                   "openrouter": "anthropic/claude-sonnet-4.5"},
        price=[0.003, 0.015],
        concurrency=32,
        context_window=1000000,
        openrouter_config={"provider": {"only": ["anthropic"]}}
    ),
    'claude-4.5-opus': Dict(
        api_model={"openrouter": "anthropic/claude-opus-4.5"},
        price=[0.005, 0.025],
        concurrency=32,
        context_window=1000000,
        openrouter_config={"provider": {"only": ["anthropic"]}}
    ),
    'claude-4.5-haiku-1001': Dict(
        api_model={"anthropic": "claude-haiku-4-5-20251001",
                   "openrouter": "anthropic/claude-haiku-4.5"},
        price=[0.001, 0.005],
        concurrency=32,
        context_window=1000000,
        openrouter_config={"provider": {"only": ["anthropic"]}}
    ),
    'claude-4-sonnet-0514': Dict(
        api_model={"aihubmix": "claude-sonnet-4-20250514",
                   "anthropic": "claude-sonnet-4-20250514",
                   "openrouter": "anthropic/claude-sonnet-4"},
        price=[0.003, 0.015],
        concurrency=32,
        context_window=1000000,
        openrouter_config={"provider": {"only": ["anthropic"]}}
    ),
    'claude-4.6-sonnet': Dict(
        api_model={"openrouter": "anthropic/claude-sonnet-4.6"},
        price=[0.003, 0.015],
        concurrency=32,
        context_window=1000000,
        openrouter_config={"provider": {"only": ["anthropic"]}}
    ),
    'claude-4.1-opus-0805': Dict(
        api_model={"aihubmix": "claude-opus-4-1-20250805",
                   "openrouter": "anthropic/claude-opus-4.1",
                   "anthropic": "claude-opus-4-1-20250805"},
        price=[16.5/1000, 82.5/1000],
        concurrency=32,
        context_window=200000,
        openrouter_config={"provider": {"only": ["anthropic"]}}
    ),
    'claude-4.5-opus': Dict(
        api_model={"openrouter": "anthropic/claude-opus-4.5"},
        price=[5/1000, 25/1000],
        concurrency=32,
        context_window=200000,
        openrouter_config={"provider": {"only": ["anthropic"]}}
    ),
    'gemini-3-pro-preview': Dict(
        api_model={"openrouter": "google/gemini-3-pro-preview",
                   },
        price=[0.002, 0.012],
        concurrency=32,
        context_window=1000000,
        openrouter_config={"provider": {"only": ["google-vertex"]}}
    ),
    'gemini-3.1-pro-preview': Dict(
        api_model={"openrouter": "google/gemini-3.1-pro-preview",
                   },
        price=[0.002, 0.012],
        concurrency=32,
        context_window=1000000,
        openrouter_config={"provider": {"only": ["google-vertex"]}}
    ),
    'gemini-2.5-pro': Dict(
        api_model={"aihubmix": "gemini-2.5-pro",
                   "openrouter": "google/gemini-2.5-pro",
                   "google": "gemini-2.5-pro"},
        price=[0.00125, 0.010],
        concurrency=32,
        context_window=1000000,
        openrouter_config={"provider": {"only": ["google-vertex"]}}
    ),
    'gemini-2.5-flash': Dict(
        api_model={"aihubmix": "gemini-2.5-flash",
                   "openrouter": "google/gemini-2.5-flash",
                   "google": "gemini-2.5-flash"},
        price=[0.00015, 0.0035],
        concurrency=32,
        context_window=1000000,
        openrouter_config={"provider": {"only": ["google-vertex"]}}
    ),
    'grok-4': Dict(
        api_model={"openrouter": "x-ai/grok-4",
                    "xai":"grok-4-0709"},
        price=[3/1000, 15/1000],
        concurrency=32,
        context_window=256000,
        openrouter_config={"provider": {"only": ["xai"]}}
    ),
    'grok-code-fast-1': Dict(
        api_model={"aihubmix": "grok-code-fast-1",
                   "openrouter": "x-ai/grok-code-fast-1",
                   "xai":"grok-code-fast-1"},
        price=[0.2/1000, 1.5/1000],
        concurrency=32,
        context_window=256000,
        openrouter_config={"provider": {"only": ["xai"]}}
    ),    
    'grok-4-fast': Dict(
        api_model={"openrouter": "x-ai/grok-4-fast",
                   "xai":"grok-4-fast"},
        price=[0.2/1000, 0.5/1000],
        concurrency=32,
        context_window=2000000,
        openrouter_config={"provider": {"only": ["xai"]}}
    ),
    'kimi-k2-0905': Dict(
        api_model={"aihubmix": "Kimi-K2-0905",
                   "openrouter": "moonshotai/kimi-k2-0905",
                   "kimi_official": "kimi-k2-0905-preview"},
        price=[0.548/1000, 2.192/1000],
        concurrency=32,
        context_window=256000,
        openrouter_config={"provider": {"only": ["moonshotai"]}}
    ),
    'kimi-k2.5': Dict(
        api_model={"kimi_official": "kimi-k2.5"},
        price=[0.548/1000, 3.05/1000],
        concurrency=32,
        context_window=256000,
        openrouter_config={"provider": {"only": ["moonshotai"]}}
    ),
    'glm-4.6': Dict(
        api_model={"openrouter": "z-ai/glm-4.6",
                   "zai_official": "glm-4.6"},
        price=[0.6/1000, 2.2/1000],
        concurrency=32,
        context_window=128000,
        openrouter_config={"provider": {"only": ["z-ai"]}}
    ),
    'glm-4.7': Dict(
        api_model={"openrouter": "z-ai/glm-4.7",
                    "zai_official": "glm-4.7"},
        price=[0.6/1000, 2.2/1000],
        concurrency=32,
        context_window=200000,
        openrouter_config={"provider": {"only": ["z-ai"]}}
    ),
    "qwen-3-coder-0722": Dict(
        api_model={"qwen_official": "qwen3-coder-plus-2025-07-22"},
        price=[0.54/1000, 2.16/1000],
        concurrency=32,
        context_window=256000,
    ),
    "qwen-3-coder-0923": Dict(
        api_model={"qwen_official": "qwen3-coder-plus-2025-09-23"},
        price=[0.54/1000, 2.16/1000],
        concurrency=32,
        context_window=256000,
    ),
    "qwen-3-coder": Dict(
        api_model={"qwen_official": "qwen3-coder-plus"},
        price=[0.54/1000, 2.16/1000],
        concurrency=32,
        context_window=256000,
    ),
    "qwen-3-max": Dict(
        api_model={
            "qwen_official": "qwen3-max-2025-09-23",
            "openrouter": "qwen/qwen3-max"},
        price=[1.2/1000, 6/1000],
        concurrency=32,
        context_window=256000,
        openrouter_config={"provider": {"only": ["alibaba"]}}
    ),
    "minimax-m2": Dict(
        api_model={
            "openrouter": "minimax/minimax-m2:free"},
        price=[0.3/1000, 1.2/1000],
        concurrency=32,
        context_window=200000,
        openrouter_config={"provider": {"only": ["minimax"]}}
    ),
    "minimax-m2.1": Dict(
        api_model={
            "openrouter": "minimax/minimax-m2.1"},
        price=[0.3/1000, 1.2/1000],
        concurrency=32,
        context_window=204800,
        openrouter_config={"provider": {"only": ["minimax"]}}
    ),
    # "gpt-oss-120b": Dict(
    #     api_model={"openrouter": "openai/gpt-oss-120b"},
    #     price=[0.00125, 0.010],
    #     concurrency=32,
    #     context_window=256000,
    #     openrouter_config={"provider": {"only": ["fireworks"]}}
    # ),
}

set_tracing_disabled(disabled=True)

def calculate_cost(model_name, input_tokens, output_tokens):
    # For local VLLM models, cost is 0
    if model_name not in API_MAPPINGS:
        return 0.0, 0.0, 0.0
    
    prices = API_MAPPINGS[model_name]['price']
    input_price_per_1k = prices[0] / 1000
    output_price_per_1k = prices[1] / 1000
    
    input_cost = input_tokens * input_price_per_1k
    output_cost = output_tokens * output_price_per_1k
    total_cost = input_cost + output_cost
    
    return input_cost, output_cost, total_cost

def get_context_window(model_name, context_window = None):
    # For local VLLM models, assume a reasonable default context window
    if context_window is not None:
        # if this model has a specific context window, return it
        return context_window
    
    if model_name not in API_MAPPINGS:
        return 1000000  # Default context window for local models
    
    return API_MAPPINGS[model_name]['context_window']
