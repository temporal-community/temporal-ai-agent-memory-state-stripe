"""The model turn: the agent_decide_next_step Activity and the steps behind it.

model_next_step asks a live OpenAI or Anthropic model for one turn's next step,
with tool calling. fixed_policy_step is the offline stand-in: a deterministic
policy that makes no model calls. Each caller (the Activity for Demo 2, the
agent process for Demo 1) picks one explicitly from the request's
model_provider. Usage logging for `refund-demo usage` lives here too.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import anthropic
import openai
from anthropic import Anthropic
from anthropic.types import ToolParam
from openai import OpenAI
from openai.types.responses import FunctionToolParam
from temporalio import activity
from temporalio.exceptions import ApplicationError

from refund_agent.activities.tools import (
    TOOL_HISTORY,
    TOOL_MEMORY,
    TOOL_ORDER,
    TOOL_POLICY,
)
from refund_agent.models import OFFLINE_POLICY, AgentStep, RefundRequest
from refund_agent.settings import model_usage_path

# Refunds at or below this clear on their own; larger ones escalate to a human.
APPROVE_THRESHOLD_CENTS = 10000


# ---------------------------------------------------------------------------
# The fixed offline policy: deterministic, no model calls.
# ---------------------------------------------------------------------------


def _observation(working_memory: list[dict], tool: str) -> dict | None:
    for obs in working_memory:
        if obs.get("tool") == tool:
            result = obs.get("result")
            return result if isinstance(result, dict) else None
    return None


def _customer_answer(working_memory: list[dict], question_id: str) -> str | None:
    for observation in working_memory:
        if observation.get("tool") != "customer_answer":
            continue
        result = observation.get("result") or {}
        if result.get("question_id") == question_id:
            return str(result.get("answer") or "")
    return None


def _missing_question_step(
    request: RefundRequest, working_memory: list[dict]
) -> AgentStep | None:
    # Ask only for intake answers the request did not already carry.
    if (
        request.item_opened is None
        and _customer_answer(working_memory, "item_opened") is None
    ):
        return AgentStep(
            action="ask_customer",
            question_id="item_opened",
            question="Was the package opened?",
            suggested_answer="Yes",
        )
    if request.damage is None and _customer_answer(working_memory, "damage") is None:
        return AgentStep(
            action="ask_customer",
            question_id="damage",
            question="What was damaged?",
            suggested_answer="Split seam",
        )
    return None


def fixed_policy_step(request: RefundRequest, working_memory: list[dict]) -> AgentStep:
    """One turn of the fixed offline policy, the demo's stand-in for a model.

    It asks for any missing intake answer (item_opened, then damage), then
    looks up the order and the customer's history, searches past cases in
    long-term memory, and decides. The path still varies with the request: a
    clean, low-value refund clears there; a larger one digs into the refund
    policy and escalates.
    """

    question = _missing_question_step(request, working_memory)
    if question is not None:
        return question

    # Tool results are keyed by tool name; the agent's own turns ("agent_step")
    # and customer answers never match a lookup.
    done = {obs.get("tool") for obs in working_memory}
    if TOOL_ORDER not in done:
        return AgentStep(action="use_tool", tool=TOOL_ORDER)
    if TOOL_HISTORY not in done:
        return AgentStep(action="use_tool", tool=TOOL_HISTORY)
    if TOOL_MEMORY not in done:
        return AgentStep(action="use_tool", tool=TOOL_MEMORY)
    history = _observation(working_memory, TOOL_HISTORY) or {}
    prior = len(history.get("prior_refunds") or [])
    clean = request.amount_cents <= APPROVE_THRESHOLD_CENTS and prior <= 1
    if clean:
        return AgentStep(
            action="decide",
            recommendation="approve",
            rationale=(
                "Amount is within the auto-approve threshold and the customer "
                "history is clean."
            ),
        )
    if TOOL_POLICY not in done:
        return AgentStep(action="use_tool", tool=TOOL_POLICY)
    return AgentStep(
        action="decide",
        recommendation="escalate",
        rationale=(
            "Amount is above the auto-approve threshold, so a human should confirm."
        ),
    )


# ---------------------------------------------------------------------------
# The live model: OpenAI or Anthropic with tool calling.
# ---------------------------------------------------------------------------

# The lookup tools take no arguments: the Workflow passes this run's order and
# customer IDs, so the model chooses which tool to call but not whose records it
# reads.
_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "ask_customer",
        "description": "Ask for one missing return detail before deciding.",
        "parameters": {
            "type": "object",
            "properties": {
                "question_id": {
                    "type": "string",
                    "enum": ["item_opened", "damage"],
                },
                "question": {"type": "string"},
                "suggested_answer": {"type": "string"},
            },
            "required": ["question_id", "question", "suggested_answer"],
        },
    },
    {
        "type": "function",
        "name": TOOL_ORDER,
        "description": "Look up the order being refunded.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": TOOL_HISTORY,
        "description": "Look up the customer's tenure, purchases, and prior refunds.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": TOOL_MEMORY,
        "description": (
            "Recall notes from this customer's past cases (your long-term "
            "memory). Notes say what an earlier run concluded, not what the "
            "records say."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": TOOL_POLICY,
        "description": (
            "Check authoritative refund eligibility and whether a return is required."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "submit_decision",
        "description": "Submit the final decision once you have enough information.",
        "parameters": {
            "type": "object",
            "properties": {
                "recommendation": {
                    "type": "string",
                    "enum": ["approve", "escalate", "deny"],
                },
                "rationale": {"type": "string"},
            },
            "required": ["recommendation", "rationale"],
        },
    },
]

_AGENT_INSTRUCTIONS = (
    "You are a refund agent. Decide whether to approve, escalate, or deny a "
    "refund. If the request leaves item_opened or damage empty and no "
    "customer_answer observation has it, use ask_customer to collect it, one "
    "question at a time, when it would help your decision. Use the other tools "
    "to gather what you need, then call submit_decision. Before deciding, use "
    "search_memory to check past cases for this customer. Observations with "
    "tool agent_step are your own earlier turns. The lookup tools act on this "
    "request's order and customer, so they take no arguments. "
    "Treat tool results as authoritative. Approve clear, low-value refunds with "
    "a clean customer history. When the policy tool says eligible, approve; do "
    "not require a physical return when return_required is false. Escalate to a "
    "human when the amount is large or the history looks risky. Deny only when "
    "the request clearly conflicts with the order or policy says it is ineligible."
)

# Added to the Anthropic system prompt, since that call can't force a tool.
_ANTHROPIC_ONE_TOOL = (
    " Respond to every turn by calling exactly one tool, with no text reply."
)

_MODEL_PROVIDERS = {"anthropic", "openai"}


def _selected_model_provider(request: RefundRequest) -> str | None:
    configured = request.model_provider or os.getenv("AGENT_MODEL_PROVIDER")
    if configured:
        provider = configured.strip().lower()
        if provider not in _MODEL_PROVIDERS:
            raise ApplicationError(
                "AGENT_MODEL_PROVIDER must be 'anthropic' or 'openai'",
                type="ModelProviderInvalid",
                non_retryable=True,
            )
        return provider
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    return None


def _dollars(cents: int) -> str:
    return f"${cents / 100:.2f}"


def _model_view(value: object) -> object:
    """Copy a request or observation for the model, with money in dollars.

    Every amount_cents field becomes amount, rendered as "$80.00", so the model
    never reasons about cents. The stored request and working memory keep cents.
    """

    if isinstance(value, dict):
        view: dict[str, object] = {}
        for key, item in value.items():
            if key == "amount_cents" and isinstance(item, int):
                view["amount"] = _dollars(item)
            else:
                view[key] = _model_view(item)
        return view
    if isinstance(value, list):
        return [_model_view(item) for item in value]
    return value


# The request fields the model reasons about. Routing and replay-only fields,
# such as dry_run and model_provider, stay out of the prompt.
_MODEL_REQUEST_FIELDS = (
    "request_id",
    "order_id",
    "customer_id",
    "amount_cents",
    "reason",
    "item_opened",
    "damage",
    "refund_destination",
)


def _model_payload(request: RefundRequest, working_memory: list[dict]) -> str:
    """The prompt body both providers get: the request and every observation.

    The observations include the agent's own earlier turns (agent_step), its
    customer answers, and its tool results, in order.
    """

    fields = asdict(request)
    return json.dumps(
        {
            "request": _model_view({key: fields[key] for key in _MODEL_REQUEST_FIELDS}),
            "observations": _model_view(working_memory),
        },
        sort_keys=True,
    )


# COST: which agent made a logged model call, so `refund-demo usage` can report
# Demo 1 (the naive agent process) and Demo 2 (the Temporal Workflow) apart.
USAGE_AGENT_TEMPORAL = "temporal"
USAGE_AGENT_NAIVE = "naive"


def _usage_count(source: object, name: str) -> int:
    return int(getattr(source, name, None) or 0)


def _record_usage(
    path: Path | None,
    provider: str,
    model: str,
    usage: object | None,
    *,
    agent: str = USAGE_AGENT_TEMPORAL,
) -> None:
    """COST: append one model call's token counts when LOG_MODEL_USAGE is on.

    Only counts are written, never prompt text or keys. input_tokens counts every
    prompt token, cached or not. output_tokens already includes reasoning.
    """

    if path is None or usage is None:
        return
    if provider == "openai":
        # OpenAI input_tokens already includes cached and cache-write tokens.
        input_details = getattr(usage, "input_tokens_details", None)
        cached = _usage_count(input_details, "cached_tokens")
        cache_write = _usage_count(input_details, "cache_write_tokens")
        input_tokens = _usage_count(usage, "input_tokens")
        output_details = getattr(usage, "output_tokens_details", None)
        reasoning = _usage_count(output_details, "reasoning_tokens")
    else:
        # Anthropic input_tokens excludes cache reads and writes, so add them.
        cached = _usage_count(usage, "cache_read_input_tokens")
        cache_write = _usage_count(usage, "cache_creation_input_tokens")
        input_tokens = _usage_count(usage, "input_tokens") + cached + cache_write
        output_details = getattr(usage, "output_tokens_details", None)
        reasoning = _usage_count(output_details, "thinking_tokens")
    output_tokens = _usage_count(usage, "output_tokens")
    record: dict[str, object] = {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "agent": agent,
        "provider": provider,
        "model": model,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_tokens": cache_write,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning,
    }
    if activity.in_activity():
        info = activity.info()
        record["workflow_id"] = info.workflow_id
        record["activity_attempt"] = info.attempt
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as log:
            log.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as error:
        # The call is already billed; a failed log write must not retry it.
        activity.logger.warning(f"MODEL USAGE | could not write {path}: {error}")
        return
    activity.logger.info(
        f"MODEL USAGE | {provider}:{model} input {input_tokens}, output {output_tokens}"
    )


def _openai_step(
    request: RefundRequest,
    working_memory: list[dict],
    api_key: str,
    *,
    agent: str = USAGE_AGENT_TEMPORAL,
) -> AgentStep:
    model = os.getenv("OPENAI_MODEL")
    if not model:
        raise ApplicationError(
            "OPENAI_MODEL is required for a real agent run. Set it to a model "
            "your account can access, or choose the offline policy with --offline.",
            type="OpenAIModelMissing",
            non_retryable=True,
        )
    # Read before the paid call, so an invalid LOG_MODEL_USAGE fails first.
    usage_path = model_usage_path()
    # Client retries are disabled so Temporal owns every retry decision.
    client = OpenAI(api_key=api_key, max_retries=0, timeout=45.0)
    payload = _model_payload(request, working_memory)
    try:
        response = client.responses.create(
            model=model,
            instructions=_AGENT_INSTRUCTIONS,
            input=payload,
            # The SDK type requires a "strict" key. The schemas leave it out so
            # the API default applies; the cast changes nothing at runtime.
            tools=cast(list[FunctionToolParam], _TOOL_SCHEMAS),
            tool_choice="required",
            store=False,
        )
    except openai.APIStatusError as error:
        status = error.status_code
        # Transient conditions (rate limit and server errors) are retryable.
        if status == 429 or status >= 500:
            raise
        # Permanent client errors cannot be fixed by retrying.
        raise ApplicationError(
            f"OpenAI returned a permanent error (HTTP {status}): {error}",
            type="OpenAIPermanentError",
            non_retryable=True,
        ) from error
    # Connection and timeout errors are not APIStatusError, so they propagate as
    # ordinary failures that Temporal retries under the RetryPolicy.
    # Log before parsing, so a billed response that fails to parse still counts.
    _record_usage(
        usage_path, "openai", model, getattr(response, "usage", None), agent=agent
    )

    call = None
    for item in response.output:
        if item.type == "function_call":
            call = item
            break
    if call is None:
        raise ApplicationError(
            "OpenAI returned no tool call for this turn",
            type="OpenAIOutputError",
            non_retryable=True,
        )
    args = json.loads(call.arguments) if call.arguments else {}
    source = f"openai:{model}"
    if call.name == "submit_decision":
        return AgentStep(
            action="decide",
            recommendation=str(args.get("recommendation", "escalate")),
            rationale=str(args.get("rationale", "")),
            source=source,
        )
    if call.name == "ask_customer":
        return AgentStep(
            action="ask_customer",
            question_id=str(args.get("question_id", "")),
            question=str(args.get("question", "")),
            suggested_answer=str(args.get("suggested_answer", "")),
            source=source,
        )
    # The lookup tools take no arguments, so nothing else is read from args.
    return AgentStep(action="use_tool", tool=call.name, source=source)


def _anthropic_step(
    request: RefundRequest,
    working_memory: list[dict],
    api_key: str,
    *,
    agent: str = USAGE_AGENT_TEMPORAL,
) -> AgentStep:
    model = os.getenv("ANTHROPIC_MODEL")
    if not model:
        raise ApplicationError(
            "ANTHROPIC_MODEL is required for a Claude agent run.",
            type="AnthropicModelMissing",
            non_retryable=True,
        )
    # Read before the paid call, so an invalid LOG_MODEL_USAGE fails first.
    usage_path = model_usage_path()
    # strict keeps every tool call's arguments valid against its schema, which
    # strict mode requires to forbid extra properties.
    tools: list[ToolParam] = [
        {
            "name": tool["name"],
            "description": tool["description"],
            "input_schema": {**tool["parameters"], "additionalProperties": False},
            "strict": True,
        }
        for tool in _TOOL_SCHEMAS
    ]
    payload = _model_payload(request, working_memory)
    # Client retries are disabled so Temporal owns every retry decision.
    client = Anthropic(api_key=api_key, max_retries=0, timeout=45.0)
    try:
        response = client.messages.create(
            model=model,
            # Room for the model's thinking before its one tool call. Current
            # Claude models think by default, and thinking counts toward this.
            max_tokens=4096,
            # Low effort: each turn is one small choice, so deeper thinking
            # only adds tokens.
            output_config={"effort": "low"},
            system=_AGENT_INSTRUCTIONS + _ANTHROPIC_ONE_TOOL,
            messages=[{"role": "user", "content": payload}],
            tools=tools,
            # Current Claude models reject a forced tool choice ("any" or
            # "tool") with HTTP 400, so the choice is "auto" and the
            # instruction above asks for exactly one tool call.
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
        )
    except anthropic.APIStatusError as error:
        status = error.status_code
        if status == 429 or status >= 500:
            raise
        raise ApplicationError(
            f"Anthropic returned a permanent error (HTTP {status}): {error}",
            type="AnthropicPermanentError",
            non_retryable=True,
        ) from error
    # Log before parsing, so a billed response that fails to parse still counts.
    _record_usage(
        usage_path, "anthropic", model, getattr(response, "usage", None), agent=agent
    )

    call = next(
        (block for block in response.content if block.type == "tool_use"),
        None,
    )
    if call is None:
        if getattr(response, "stop_reason", None) == "refusal":
            raise ApplicationError(
                "Anthropic declined this turn (stop_reason refusal)",
                type="AnthropicRefusal",
                non_retryable=True,
            )
        # With tool_choice "auto" the model can answer in text instead of a
        # tool call. That is usually a one-off, so Temporal retries the turn
        # (up to the model retry policy's attempts in workflow.py).
        raise ApplicationError(
            "Anthropic returned no tool call for this turn",
            type="AnthropicNoToolCall",
        )
    args = dict(call.input)
    source = f"anthropic:{model}"
    if call.name == "submit_decision":
        return AgentStep(
            action="decide",
            recommendation=str(args.get("recommendation", "escalate")),
            rationale=str(args.get("rationale", "")),
            source=source,
        )
    if call.name == "ask_customer":
        return AgentStep(
            action="ask_customer",
            question_id=str(args.get("question_id", "")),
            question=str(args.get("question", "")),
            suggested_answer=str(args.get("suggested_answer", "")),
            source=source,
        )
    # The lookup tools take no arguments, so nothing else is read from args.
    return AgentStep(action="use_tool", tool=call.name, source=source)


def model_next_step(
    request: RefundRequest,
    working_memory: list[dict],
    *,
    agent: str = USAGE_AGENT_TEMPORAL,
) -> AgentStep:
    """Ask the live model for one turn's next step. It never falls back offline.

    The agent_decide_next_step Activity calls this for Demo 2. Demo 1's naive
    agent process calls it directly, with no Temporal, so both demos run the
    same model step. `agent` only tags the usage log.
    """

    provider = _selected_model_provider(request)
    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ApplicationError(
                "OPENAI_API_KEY is required for the OpenAI provider.",
                type="OpenAIKeyMissing",
                non_retryable=True,
            )
        return _openai_step(request, working_memory, api_key, agent=agent)
    if provider == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ApplicationError(
                "ANTHROPIC_API_KEY is required for the Anthropic provider.",
                type="AnthropicKeyMissing",
                non_retryable=True,
            )
        return _anthropic_step(request, working_memory, api_key, agent=agent)
    raise ApplicationError(
        "A live model key is required. Configure Anthropic or OpenAI, or choose "
        "the offline policy explicitly (--offline).",
        type="ModelKeyMissing",
        non_retryable=True,
    )


@activity.defn
def agent_decide_next_step(
    request: RefundRequest, working_memory: list[dict]
) -> AgentStep:
    """MODEL REASONING: one turn of the agent loop."""

    if request.model_provider == OFFLINE_POLICY:
        # Demo-only: the fixed policy stands in for the model, chosen explicitly
        # in the Workflow input, so an offline run makes no model calls and
        # plays the same way every time. A real app calls its model.
        step = fixed_policy_step(request, working_memory)
    else:
        step = model_next_step(request, working_memory)
    activity.logger.info(
        f"MODEL REASONING | {step.action} "
        f"{step.recommendation or step.question_id or step.tool or ''}".rstrip()
    )
    return step
