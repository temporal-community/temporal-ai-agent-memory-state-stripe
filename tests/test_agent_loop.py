import asyncio
import json
import shutil
import time
import uuid
from dataclasses import asdict, replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
import stripe
from temporalio import activity
from temporalio.exceptions import ApplicationError

from refund_agent.activities import (
    STRIPE_TIMEOUT_SECONDS,
    TOOL_HISTORY,
    TOOL_MEMORY,
    TOOL_ORDER,
    TOOL_POLICY,
    check_refund_policy,
    fixed_policy_step,
    lookup_customer_history,
    lookup_order,
    search_memory,
)
from refund_agent.agent_loop import run_agent_loop
from refund_agent.models import (
    OFFLINE_POLICY,
    AgentStep,
    CaseMemory,
    CustomerHistory,
    OrderDetails,
    RefundDecision,
    RefundRequest,
    RefundResult,
    ReturnStatus,
)
from refund_agent.workflow import (
    REFUND_HEARTBEAT_TIMEOUT,
    REFUND_START_TO_CLOSE_TIMEOUT,
    RefundWorkflow,
)


@pytest.fixture(autouse=True)
def _demo_store_in_tmp(tmp_path, monkeypatch) -> None:
    # The lookup tools read the SQLite demo store in DEMO_STATE_DIR. Each test
    # gets a fresh one, so nothing is written under .demo-state.
    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))


def _request(amount_cents: int) -> RefundRequest:
    return RefundRequest(
        request_id="r",
        order_id="order-1234",
        customer_id="cus_demo_42",
        payment_intent_id="pi",
        amount_cents=amount_cents,
        reason="x",
        dry_run=True,
    )


def _lookup(request: RefundRequest, tool: str) -> dict:
    if tool == TOOL_ORDER:
        return asdict(lookup_order(request.order_id))
    if tool == TOOL_HISTORY:
        return asdict(lookup_customer_history(request.customer_id))
    if tool == TOOL_POLICY:
        return asdict(check_refund_policy(request.order_id))
    if tool == TOOL_MEMORY:
        return asdict(search_memory(request.customer_id))
    raise AssertionError(f"unknown tool {tool}")


def _run_loop(request: RefundRequest, steps=None, answers=None):
    """Run the shared agent loop with direct calls, as Demo 1 does.

    steps scripts the model's turns; by default the fixed policy decides.
    Returns (decision, working memory, tools used, questions asked).
    """

    scripted = iter(steps or [])
    tools_used: list[str] = []
    asked: list[str] = []

    async def decide(request: RefundRequest, working_memory: list[dict]) -> AgentStep:
        if steps is None:
            return fixed_policy_step(request, working_memory)
        return next(scripted)

    async def use_tool(tool: str) -> dict:
        tools_used.append(tool)
        return _lookup(request, tool)

    async def ask_customer(step: AgentStep) -> str:
        asked.append(str(step.question_id))
        return (answers or {}).get(str(step.question_id), step.suggested_answer or "")

    working_memory: list[dict] = []
    decision = asyncio.run(
        run_agent_loop(
            request,
            working_memory,
            decide=decide,
            use_tool=use_tool,
            ask_customer=ask_customer,
        )
    )
    return decision, working_memory, tools_used, asked


def test_canned_clean_refund_auto_approves() -> None:
    decision, _, tools, asked = _run_loop(_request(8000))
    assert decision.recommendation == "approve"
    assert tools == [TOOL_ORDER, TOOL_HISTORY, TOOL_MEMORY]
    # The request already carried the intake answers, so nothing was asked.
    assert asked == []


def test_fixed_policy_asks_each_missing_question_before_using_tools() -> None:
    request = replace(_request(8000), item_opened=None, damage=None)

    decision, working_memory, tools, asked = _run_loop(
        request, answers={"damage": "torn wing"}
    )

    assert asked == ["item_opened", "damage"]
    assert tools == [TOOL_ORDER, TOOL_HISTORY, TOOL_MEMORY]
    assert decision.recommendation == "approve"
    answers = [obs for obs in working_memory if obs["tool"] == "customer_answer"]
    assert [obs["result"]["answer"] for obs in answers] == ["Yes", "torn wing"]


def test_fixed_policy_asks_only_for_what_the_request_lacks() -> None:
    request = replace(_request(8000), damage=None)

    _, _, _, asked = _run_loop(request)

    assert asked == ["damage"]


def test_canned_large_refund_escalates() -> None:
    decision, _, tools, _ = _run_loop(_request(15000))
    assert decision.recommendation == "escalate"
    assert TOOL_POLICY in tools


def test_history_holds_one_entry_per_model_turn() -> None:
    request = replace(_request(8000), item_opened=None, damage=None)

    _, working_memory, _, _ = _run_loop(request)

    assert [obs["tool"] for obs in working_memory] == [
        "agent_step",
        "customer_answer",
        "agent_step",
        "customer_answer",
        "agent_step",
        TOOL_ORDER,
        "agent_step",
        TOOL_HISTORY,
        "agent_step",
        TOOL_MEMORY,
        "agent_step",
    ]
    turns = [obs for obs in working_memory if obs["tool"] == "agent_step"]
    assert turns[0] == {
        "tool": "agent_step",
        "action": "ask_customer",
        "question_id": "item_opened",
        "rationale": "",
    }
    assert turns[2]["tool_chosen"] == TOOL_ORDER
    assert turns[-1]["action"] == "decide"
    assert turns[-1]["recommendation"] == "approve"
    assert turns[-1]["rationale"]


def _ask(question_id: str) -> AgentStep:
    return AgentStep(
        action="ask_customer",
        question_id=question_id,
        question=f"{question_id}?",
        suggested_answer="suggested",
    )


_DECIDE = AgentStep(action="decide", recommendation="approve", rationale="ok")


@pytest.mark.parametrize(
    "questions",
    [
        [],  # a live model may decide without asking
        ["damage"],
        ["damage", "item_opened"],  # any order
        ["item_opened", "item_opened"],  # a repeat reuses the first answer
    ],
)
def test_loop_accepts_any_number_and_order_of_questions(questions) -> None:
    steps = [_ask(question_id) for question_id in questions] + [_DECIDE]

    decision, working_memory, _, asked = _run_loop(_request(8000), steps=steps)

    assert decision.recommendation == "approve"
    assert asked == list(dict.fromkeys(questions))
    answered = [
        obs["result"]["question_id"]
        for obs in working_memory
        if obs["tool"] == "customer_answer"
    ]
    assert answered == questions


@pytest.mark.parametrize(
    ("step", "error_type"),
    [
        (_ask("refund_destination"), "UnknownCustomerQuestion"),
        (AgentStep(action="use_tool", tool="issue_refund"), "UnknownTool"),
    ],
)
def test_loop_rejects_unknown_questions_and_tools(step, error_type) -> None:
    with pytest.raises(ApplicationError) as error:
        _run_loop(_request(8000), steps=[step])

    assert error.value.type == error_type
    assert error.value.non_retryable


def test_loop_stops_at_the_turn_budget() -> None:
    from refund_agent.agent_loop import MAX_TURNS

    steps = [AgentStep(action="use_tool", tool=TOOL_ORDER)] * MAX_TURNS

    with pytest.raises(ApplicationError) as error:
        _run_loop(_request(8000), steps=steps)

    assert error.value.type == "AgentLoopExhausted"


def test_stage_refund_policy_is_explicitly_eligible_without_a_return() -> None:
    policy = check_refund_policy("order-1234")

    assert policy.eligible_for_refund is True
    assert policy.return_required is False
    assert "eligible for refund" in policy.note


class _FakeCall:
    def __init__(self, name: str, arguments: str) -> None:
        self.type = "function_call"
        self.name = name
        self.arguments = arguments


class _FakeResponse:
    def __init__(self, output: list, usage: object | None = None) -> None:
        self.output = output
        self.usage = usage


class _FakeResponses:
    def __init__(self, output: list, usage: object | None = None) -> None:
        self._output = output
        self._usage = usage

    def create(self, **_kwargs):
        return _FakeResponse(self._output, self._usage)


class _FakeClient:
    def __init__(self, output: list, usage: object | None = None) -> None:
        self.responses = _FakeResponses(output, usage)


def test_openai_step_maps_tool_and_decision(monkeypatch) -> None:
    from refund_agent.activities import model

    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    request = _request(8000)

    monkeypatch.setattr(
        model,
        "OpenAI",
        lambda **_kw: _FakeClient([_FakeCall("lookup_order", '{"order_id": "o1"}')]),
    )
    step = model._openai_step(request, [], "key")
    assert step.action == "use_tool"
    assert step.tool == "lookup_order"
    # Lookups take no arguments: the model's arguments are not copied in.
    assert not hasattr(step, "tool_args")

    monkeypatch.setattr(
        model,
        "OpenAI",
        lambda **_kw: _FakeClient(
            [
                _FakeCall(
                    "submit_decision",
                    '{"recommendation": "approve", "rationale": "clean"}',
                )
            ]
        ),
    )
    step = model._openai_step(request, [], "key")
    assert step.action == "decide"
    assert step.recommendation == "approve"

    monkeypatch.setattr(
        model,
        "OpenAI",
        lambda **_kw: _FakeClient(
            [
                _FakeCall(
                    "ask_customer",
                    '{"question_id":"damage","question":"What was damaged?",'
                    '"suggested_answer":"Split seam"}',
                )
            ]
        ),
    )
    step = model._openai_step(request, [], "key")
    assert step.action == "ask_customer"
    assert step.question_id == "damage"


class _FakeAnthropicBlock:
    type = "tool_use"

    def __init__(self, name: str, arguments: dict) -> None:
        self.name = name
        self.input = arguments


class _FakeAnthropicResponse:
    def __init__(self, content: list, usage: object | None = None) -> None:
        self.content = content
        self.usage = usage


class _FakeMessages:
    def __init__(self, content: list, usage: object | None = None) -> None:
        self._content = content
        self._usage = usage

    def create(self, **_kwargs):
        return _FakeAnthropicResponse(self._content, self._usage)


class _FakeAnthropicClient:
    def __init__(self, content: list, usage: object | None = None) -> None:
        self.messages = _FakeMessages(content, usage)


def test_anthropic_step_maps_tool_and_decision(monkeypatch) -> None:
    from refund_agent.activities import model

    monkeypatch.setenv("ANTHROPIC_MODEL", "test-claude")
    request = _request(8000)

    monkeypatch.setattr(
        model,
        "Anthropic",
        lambda **_kw: _FakeAnthropicClient(
            [_FakeAnthropicBlock("lookup_order", {"order_id": "o1"})]
        ),
    )
    step = model._anthropic_step(request, [], "key")
    assert step.action == "use_tool"
    assert step.tool == "lookup_order"

    monkeypatch.setattr(
        model,
        "Anthropic",
        lambda **_kw: _FakeAnthropicClient(
            [
                _FakeAnthropicBlock(
                    "submit_decision",
                    {"recommendation": "approve", "rationale": "clean"},
                )
            ]
        ),
    )
    step = model._anthropic_step(request, [], "key")
    assert step.action == "decide"
    assert step.recommendation == "approve"

    monkeypatch.setattr(
        model,
        "Anthropic",
        lambda **_kw: _FakeAnthropicClient(
            [
                _FakeAnthropicBlock(
                    "ask_customer",
                    {
                        "question_id": "damage",
                        "question": "What was damaged?",
                        "suggested_answer": "Split seam",
                    },
                )
            ]
        ),
    )
    step = model._anthropic_step(request, [], "key")
    assert step.action == "ask_customer"
    assert step.question_id == "damage"


def test_model_clients_leave_every_retry_to_temporal(monkeypatch) -> None:
    from refund_agent.activities import model

    # Both SDKs retry inside the call by default, where Temporal can't see it.
    # The clients disable that, and their timeout must end before the 60 s
    # agent_decide_next_step start_to_close_timeout in workflow.py so Temporal
    # sees the failure.
    monkeypatch.delenv("LOG_MODEL_USAGE", raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("ANTHROPIC_MODEL", "test-claude")
    built: dict[str, dict] = {}

    def fake_openai(**kwargs):
        built["openai"] = kwargs
        return _FakeClient([_FakeCall("lookup_order", '{"order_id": "o1"}')])

    def fake_anthropic(**kwargs):
        built["anthropic"] = kwargs
        return _FakeAnthropicClient(
            [_FakeAnthropicBlock("lookup_order", {"order_id": "o1"})]
        )

    monkeypatch.setattr(model, "OpenAI", fake_openai)
    monkeypatch.setattr(model, "Anthropic", fake_anthropic)
    model._openai_step(_request(8000), [], "key")
    model._anthropic_step(_request(8000), [], "key")

    assert set(built) == {"openai", "anthropic"}
    for kwargs in built.values():
        assert kwargs["max_retries"] == 0
        assert 0 < kwargs["timeout"] < 60


class _RecordingResponses:
    def __init__(self, sent: list[dict]) -> None:
        self._sent = sent

    def create(self, **kwargs):
        self._sent.append(kwargs)
        return _FakeResponse([_FakeCall("lookup_order", '{"order_id": "o1"}')])


class _RecordingMessages:
    def __init__(self, sent: list[dict]) -> None:
        self._sent = sent

    def create(self, **kwargs):
        self._sent.append(kwargs)
        return _FakeAnthropicResponse(
            [_FakeAnthropicBlock("lookup_order", {"order_id": "o1"})]
        )


def test_model_prompts_show_money_as_dollars_not_cents(monkeypatch) -> None:
    from refund_agent.activities import model

    monkeypatch.delenv("LOG_MODEL_USAGE", raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("ANTHROPIC_MODEL", "test-claude")
    sent: list[dict] = []
    monkeypatch.setattr(
        model,
        "OpenAI",
        lambda **_kw: SimpleNamespace(responses=_RecordingResponses(sent)),
    )
    monkeypatch.setattr(
        model,
        "Anthropic",
        lambda **_kw: SimpleNamespace(messages=_RecordingMessages(sent)),
    )
    request = _request(8000)
    working_memory = [
        {"tool": TOOL_ORDER, "result": asdict(lookup_order(request.order_id))}
    ]

    model._openai_step(request, working_memory, "key")
    model._anthropic_step(request, working_memory, "key")

    openai_prompt = sent[0]["input"]
    anthropic_prompt = sent[1]["messages"][0]["content"]
    for prompt in (openai_prompt, anthropic_prompt):
        payload = json.loads(prompt)
        # The request and the lookup_order observation both show dollars.
        assert payload["request"]["amount"] == "$80.00"
        assert payload["observations"][0]["result"]["amount"] == "$80.00"
        assert "amount_cents" not in prompt
        assert "8000" not in prompt
    # The stored data keeps cents.
    assert request.amount_cents == 8000
    assert working_memory[0]["result"]["amount_cents"] == 8000


def test_the_live_model_chooses_its_own_intake_questions(monkeypatch) -> None:
    from refund_agent.activities import model

    monkeypatch.delenv("LOG_MODEL_USAGE", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    sent: list[dict] = []
    monkeypatch.setattr(
        model,
        "OpenAI",
        lambda **_kw: SimpleNamespace(responses=_RecordingResponses(sent)),
    )
    request = replace(
        _request(8000), item_opened=None, damage=None, model_provider="openai"
    )

    # No scripted question comes first: turn one already goes to the model.
    step = model.model_next_step(request, [])

    assert step.tool == "lookup_order"
    assert len(sent) == 1
    assert "use ask_customer to collect it" in sent[0]["instructions"]
    payload = json.loads(sent[0]["input"])
    assert payload["request"]["item_opened"] is None
    # Routing fields stay out of the prompt.
    assert "model_provider" not in payload["request"]
    assert "dry_run" not in payload["request"]


def test_the_model_step_never_falls_back_to_the_offline_policy(monkeypatch) -> None:
    from refund_agent.activities import model

    for name in ("AGENT_MODEL_PROVIDER", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ApplicationError) as error:
        model.model_next_step(_request(8000), [])

    assert error.value.type == "ModelKeyMissing"
    assert "--offline" in str(error.value)


def test_the_activity_keeps_its_name_and_runs_the_shared_decision(
    tmp_path, monkeypatch
) -> None:
    from temporalio.testing import ActivityEnvironment

    from refund_agent.activities import model

    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))
    request = replace(_request(8000), model_provider=OFFLINE_POLICY)
    working_memory = [
        {"tool": TOOL_ORDER, "result": asdict(lookup_order(request.order_id))},
        {
            "tool": TOOL_HISTORY,
            "result": asdict(lookup_customer_history(request.customer_id)),
        },
        {"tool": TOOL_MEMORY, "result": asdict(search_memory(request.customer_id))},
    ]
    calls: list[tuple] = []

    def recording_model_step(*args, **_kwargs):
        calls.append(args)
        return AgentStep(action="decide", recommendation="deny", source="model")

    monkeypatch.setattr(model, "model_next_step", recording_model_step)

    # Offline is an explicit choice in the request: the fixed policy, no model.
    step = ActivityEnvironment().run(
        model.agent_decide_next_step, request, working_memory
    )

    definition = activity._Definition.must_from_callable(model.agent_decide_next_step)
    assert definition.name == "agent_decide_next_step"
    assert calls == []
    assert step == fixed_policy_step(request, working_memory)
    assert step.recommendation == "approve"

    # Any other provider goes to the shared model step.
    live = replace(request, model_provider="openai")
    step = ActivityEnvironment().run(model.agent_decide_next_step, live, working_memory)
    assert calls == [(live, working_memory)]
    assert step.source == "model"


# ---------------------------------------------------------------------------
# COST: opt-in token usage log and its summary.
# ---------------------------------------------------------------------------

_USAGE_KEYS = {
    "timestamp",
    "agent",
    "provider",
    "model",
    "input_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
}


def _usage_records(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_openai_usage_is_logged_as_counts_only(tmp_path, monkeypatch) -> None:
    from refund_agent.activities import model

    monkeypatch.setenv("LOG_MODEL_USAGE", "1")
    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    usage = SimpleNamespace(
        input_tokens=1200,
        input_tokens_details=SimpleNamespace(cached_tokens=200, cache_write_tokens=0),
        output_tokens=360,
        output_tokens_details=SimpleNamespace(reasoning_tokens=300),
    )
    monkeypatch.setattr(
        model,
        "OpenAI",
        lambda **_kw: _FakeClient(
            [_FakeCall("lookup_order", '{"order_id": "o1"}')], usage
        ),
    )

    model._openai_step(_request(8000), [], "sk-test-not-logged")

    log = tmp_path / "model-usage.jsonl"
    (record,) = _usage_records(log)
    assert set(record) == _USAGE_KEYS
    assert record["agent"] == "temporal"
    assert record["provider"] == "openai"
    assert record["model"] == "test-model"
    assert record["input_tokens"] == 1200
    assert record["cached_input_tokens"] == 200
    assert record["cache_write_tokens"] == 0
    assert record["output_tokens"] == 360
    assert record["reasoning_tokens"] == 300
    # Only counts reach the log: no prompt text and no API key.
    assert "plush" not in log.read_text()
    assert "sk-test-not-logged" not in log.read_text()


def test_anthropic_usage_counts_cache_reads_and_writes_as_input(
    tmp_path, monkeypatch
) -> None:
    from refund_agent.activities import model

    monkeypatch.setenv("LOG_MODEL_USAGE", "yes")
    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_MODEL", "test-claude")
    # Anthropic reports input_tokens without cache reads or cache writes.
    usage = SimpleNamespace(
        input_tokens=900,
        cache_read_input_tokens=100,
        cache_creation_input_tokens=50,
        output_tokens=120,
        output_tokens_details=None,
    )
    monkeypatch.setattr(
        model,
        "Anthropic",
        lambda **_kw: _FakeAnthropicClient(
            [_FakeAnthropicBlock("lookup_order", {"order_id": "o1"})], usage
        ),
    )

    model._anthropic_step(_request(8000), [], "key")

    (record,) = _usage_records(tmp_path / "model-usage.jsonl")
    assert set(record) == _USAGE_KEYS
    assert record["provider"] == "anthropic"
    assert record["input_tokens"] == 1050
    assert record["cached_input_tokens"] == 100
    assert record["cache_write_tokens"] == 50
    assert record["output_tokens"] == 120
    assert record["reasoning_tokens"] == 0


def test_usage_inside_an_activity_names_the_workflow_and_attempt(tmp_path) -> None:
    from temporalio.testing import ActivityEnvironment

    from refund_agent.activities import model

    log = tmp_path / "model-usage.jsonl"
    usage = SimpleNamespace(input_tokens=10, output_tokens=5)

    ActivityEnvironment().run(
        model._record_usage, log, "anthropic", "test-claude", usage
    )

    (record,) = _usage_records(log)
    assert record["workflow_id"] == "test"
    assert record["activity_attempt"] == 1


def test_usage_log_is_off_by_default(tmp_path, monkeypatch) -> None:
    from refund_agent.activities import model

    monkeypatch.delenv("LOG_MODEL_USAGE", raising=False)
    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    usage = SimpleNamespace(input_tokens=1, output_tokens=1)
    monkeypatch.setattr(
        model,
        "OpenAI",
        lambda **_kw: _FakeClient(
            [_FakeCall("lookup_order", '{"order_id": "o1"}')], usage
        ),
    )

    model._openai_step(_request(8000), [], "key")

    assert not (tmp_path / "model-usage.jsonl").exists()


def test_usage_summary_splits_input_and_prices_each_part() -> None:
    from refund_agent.cli import _LIST_PRICES, _usage_dollars, _usage_totals

    records = [
        {
            "provider": "openai",
            "model": "gpt-5.6-luna",
            "input_tokens": 1200,
            "cached_input_tokens": 200,
            "cache_write_tokens": 0,
            "output_tokens": 360,
            "reasoning_tokens": 300,
        },
        {
            "provider": "openai",
            "model": "gpt-5.6-luna",
            "input_tokens": 800,
            "cached_input_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 40,
            "reasoning_tokens": 0,
        },
        {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "input_tokens": 5000,
            "cached_input_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 300,
            "reasoning_tokens": 0,
        },
    ]

    totals = _usage_totals(records)

    luna = totals[("openai", "gpt-5.6-luna")]
    assert luna == {
        "calls": 2,
        "uncached_input": 1800,
        "cached_input": 200,
        "cache_write": 0,
        "output": 400,
        "reasoning": 300,
    }
    # 1,800 x $0.20 + 200 x $0.02 + 400 x $1.20, per 1M tokens. Reasoning is
    # already inside output, so it is not charged twice.
    assert _usage_dollars(luna, _LIST_PRICES["gpt-5.6-luna"]) == pytest.approx(0.000844)
    claude = totals[("anthropic", "claude-sonnet-4-6")]
    assert claude["calls"] == 1
    # 5,000 x $3 + 300 x $15, per 1M tokens.
    assert _usage_dollars(claude, _LIST_PRICES["claude-sonnet-4-6"]) == pytest.approx(
        0.0195
    )


def test_usage_command_prints_tokens_and_dollars(tmp_path, capsys) -> None:
    from refund_agent.cli import _parser, _usage

    record = {
        "provider": "openai",
        "model": "gpt-5.6-luna",
        "input_tokens": 1200,
        "cached_input_tokens": 200,
        "cache_write_tokens": 0,
        "output_tokens": 360,
        "reasoning_tokens": 300,
    }
    (tmp_path / "model-usage.jsonl").write_text(json.dumps(record) + "\n")

    _usage(_parser().parse_args(["usage", "--state-dir", str(tmp_path)]))
    listed = capsys.readouterr().out

    assert "openai:gpt-5.6-luna" in listed
    assert "1,000 tokens" in listed  # uncached input: 1,200 - 200 cached
    # 1,000 x $0.20 + 200 x $0.02 + 360 x $1.20, per 1M tokens.
    assert "1,560 tokens  $0.000636" in listed
    assert "list prices as of 2026-09-29" in listed

    _usage(
        _parser().parse_args(
            [
                "usage",
                "--state-dir",
                str(tmp_path),
                "--input-price",
                "1",
                "--output-price",
                "10",
            ]
        )
    )
    overridden = capsys.readouterr().out

    # 1,200 input tokens x $1 + 360 output tokens x $10, per 1M tokens.
    assert "$0.004800" in overridden
    assert "your --input-price/--output-price" in overridden


def test_usage_command_reports_demo_one_and_demo_two_apart(
    tmp_path, monkeypatch, capsys
) -> None:
    from refund_agent.activities import model
    from refund_agent.cli import _parser, _usage

    monkeypatch.setenv("LOG_MODEL_USAGE", "1")
    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.6-luna")

    def client_with(input_tokens: int):
        usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=100)
        return lambda **_kw: _FakeClient(
            [_FakeCall("lookup_order", '{"order_id": "o1"}')], usage
        )

    # Demo 1's naive process tags its calls; the Activity path keeps the default.
    monkeypatch.setattr(model, "OpenAI", client_with(1000))
    model._openai_step(_request(8000), [], "key", agent="naive")
    model._openai_step(_request(8000), [], "key", agent="naive")
    monkeypatch.setattr(model, "OpenAI", client_with(3000))
    model._openai_step(_request(8000), [], "key")

    records = _usage_records(tmp_path / "model-usage.jsonl")
    assert [record["agent"] for record in records] == ["naive", "naive", "temporal"]

    _usage(_parser().parse_args(["usage", "--state-dir", str(tmp_path)]))
    listed = capsys.readouterr().out
    demo_one, demo_two = listed.split("Demo 2: Temporal Workflow")

    assert "Demo 1: naive agent process (no Temporal)" in demo_one
    assert "model calls             2" in demo_one
    assert "uncached input      2,000 tokens" in demo_one
    assert "model calls             1" in demo_two
    assert "uncached input      3,000 tokens" in demo_two


# ---------------------------------------------------------------------------
# REFUND: issue_refund timeouts, Stripe failures, and the final phase.
# ---------------------------------------------------------------------------

_STRIPE_WORST_CASE = timedelta(seconds=sum(STRIPE_TIMEOUT_SECONDS))


def test_refund_timeouts_detect_a_lost_worker_before_stripe_gives_up() -> None:
    # issue_refund heartbeats every second, so a few missed beats mean the
    # Worker is gone. One attempt outlasts the Stripe client timeout, so
    # Stripe's own timeout reports a hung call before Temporal ends the attempt.
    assert timedelta(seconds=1) < REFUND_HEARTBEAT_TIMEOUT < _STRIPE_WORST_CASE
    assert REFUND_START_TO_CLOSE_TIMEOUT > _STRIPE_WORST_CASE


def test_real_refund_call_is_bounded_and_leaves_retries_to_temporal(
    monkeypatch,
) -> None:
    from refund_agent.activities import refund

    monkeypatch.setenv("STRIPE_API_KEY", "sk_test_example")
    monkeypatch.setattr(stripe, "api_key", None)
    monkeypatch.setattr(stripe, "max_network_retries", 2)
    monkeypatch.setattr(stripe, "default_http_client", None)
    created: dict = {}

    def fake_create(**kwargs):
        created.update(kwargs)
        return SimpleNamespace(id="re_test", status="succeeded", amount=8000)

    monkeypatch.setattr(stripe.Refund, "create", fake_create)

    stripe_refund = refund._real_stripe_refund(_request(8000), "wf", "key-1")

    assert stripe_refund == {
        "refund_id": "re_test",
        "status": "succeeded",
        "amount_cents": 8000,
    }
    assert created["idempotency_key"] == "key-1"
    assert stripe.max_network_retries == 0
    assert isinstance(stripe.default_http_client, stripe.RequestsClient)
    assert stripe.default_http_client._timeout == STRIPE_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (stripe.APIConnectionError("ReadTimeout", should_retry=True), True),
        # The SDK marks an SSL failure as not worth retrying.
        (stripe.APIConnectionError("SSLError", should_retry=False), False),
        (stripe.APIError("idempotency key in use", http_status=409), True),
        (stripe.RateLimitError("slow down", http_status=429), True),
        (stripe.APIError("server error", http_status=500), True),
        # Stripe-Should-Retry overrides the status code either way.
        (
            stripe.APIError(
                "replayed failure",
                http_status=500,
                headers={"stripe-should-retry": "false"},
            ),
            False,
        ),
        (
            stripe.InvalidRequestError(
                "lock timeout",
                None,
                http_status=400,
                headers={"stripe-should-retry": "true"},
            ),
            True,
        ),
        (
            stripe.InvalidRequestError(
                "No such payment_intent", "payment_intent", http_status=404
            ),
            False,
        ),
        (stripe.AuthenticationError("bad key", http_status=401), False),
    ],
)
def test_issue_refund_retries_only_transient_stripe_failures(
    error, retryable, tmp_path, monkeypatch
) -> None:
    from temporalio.testing import ActivityEnvironment

    from refund_agent.activities import refund

    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REFUND_RESTART_WINDOW_SECONDS", raising=False)

    def failing_refund(*_args):
        raise error

    monkeypatch.setattr(refund, "_real_stripe_refund", failing_refund)
    decision = RefundDecision(recommendation="approve", rationale="", source="test")

    with pytest.raises(ApplicationError) as raised:
        ActivityEnvironment().run(
            refund.issue_refund,
            replace(_request(8000), dry_run=False),
            decision,
        )

    assert raised.value.non_retryable is not retryable
    assert raised.value.__cause__ is error


def _run_real_refund(monkeypatch, tmp_path, fake_refund, on_heartbeat=None):
    from temporalio.testing import ActivityEnvironment

    from refund_agent.activities import refund

    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REFUND_RESTART_WINDOW_SECONDS", raising=False)
    monkeypatch.setattr(refund, "_real_stripe_refund", fake_refund)
    environment = ActivityEnvironment()
    if on_heartbeat is not None:
        environment.on_heartbeat = on_heartbeat
    return environment.run(
        refund.issue_refund,
        replace(_request(8000), dry_run=False),
        RefundDecision(recommendation="approve", rationale="", source="test"),
    )


@pytest.mark.parametrize(("retry_after", "delay"), [("30", 30), ("120", None)])
def test_retryable_stripe_error_carries_retry_after(
    retry_after, delay, tmp_path, monkeypatch
) -> None:
    error = stripe.RateLimitError(
        "slow down", http_status=429, headers={"retry-after": retry_after}
    )

    def rate_limited(*_args):
        raise error

    with pytest.raises(ApplicationError) as raised:
        _run_real_refund(monkeypatch, tmp_path, rate_limited)

    assert not raised.value.non_retryable
    expected = timedelta(seconds=delay) if delay else None
    assert raised.value.next_retry_delay == expected


def test_idempotency_key_follows_the_workflow_id_not_the_run(
    tmp_path, monkeypatch
) -> None:
    # One refund request is one Workflow ID. A reset or a new run of that
    # Workflow ID gets a new Run ID but must send Stripe the same key.
    from temporalio.testing import ActivityEnvironment

    from refund_agent.activities import refund
    from refund_agent.fake_stripe import idempotency_key_for

    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))
    keys = []
    for run_id in ("run-a", "run-b"):
        environment = ActivityEnvironment()
        environment.info = replace(
            environment.info, workflow_id="refund-42", workflow_run_id=run_id
        )
        result = environment.run(
            refund.issue_refund,
            _request(8000),
            RefundDecision(recommendation="approve", rationale="", source="test"),
        )
        keys.append(result.idempotency_key)

    assert keys == [idempotency_key_for("refund-42")] * 2


def test_issue_refund_heartbeats_while_stripe_is_slow(tmp_path, monkeypatch) -> None:
    heartbeats: list[tuple] = []

    def slow_refund(*_args):
        time.sleep(1.3)
        return {"refund_id": "re_slow", "status": "succeeded", "amount_cents": 8000}

    result = _run_real_refund(
        monkeypatch,
        tmp_path,
        slow_refund,
        on_heartbeat=lambda *details: heartbeats.append(details),
    )

    assert result.refund_id == "re_slow"
    # A slow call keeps the attempt alive instead of looking like a lost Worker.
    assert heartbeats


async def _wait_for_phase(handle, phase: str) -> dict:
    deadline = time.monotonic() + 15
    seen = None
    while time.monotonic() < deadline:
        progress = await handle.query(RefundWorkflow.progress)
        seen = progress["phase"]
        if seen == phase:
            return progress
        await asyncio.sleep(0.05)
    raise AssertionError(f"phase stayed {seen!r}; expected {phase!r}")


def _fake_activities(refund_may_return: asyncio.Event):
    """Stand-ins for the seven Activities, registered under their real names."""

    @activity.defn(name="agent_decide_next_step")
    async def decide(request: RefundRequest, working_memory: list[dict]) -> AgentStep:
        if request.customer_id == "uses-every-tool":
            done = {observation["tool"] for observation in working_memory}
            for tool in (TOOL_ORDER, TOOL_HISTORY, TOOL_POLICY, TOOL_MEMORY):
                if tool not in done:
                    return AgentStep(action="use_tool", tool=tool)
        return AgentStep(action="decide", recommendation="approve", rationale="")

    @activity.defn(name="lookup_order")
    async def order(order_id: str) -> OrderDetails:
        return OrderDetails(order_id, "python plushy", 8000, "delivered", "2026-06-03")

    @activity.defn(name="lookup_customer_history")
    async def history(customer_id: str) -> CustomerHistory:
        return CustomerHistory(customer_id, 824, [], [])

    @activity.defn(name="check_refund_policy")
    async def policy(order_id: str) -> ReturnStatus:
        return ReturnStatus(order_id, True, False, False, False, "eligible")

    @activity.defn(name="search_memory")
    async def memory(customer_id: str) -> CaseMemory:
        return CaseMemory(customer_id, [])

    @activity.defn(name="remember_case")
    async def remember(customer_id: str, case_id: str, note: str) -> bool:
        return True

    @activity.defn(name="issue_refund")
    async def refund(request: RefundRequest, decision: RefundDecision) -> RefundResult:
        await refund_may_return.wait()
        return RefundResult(
            refund_id="re_test",
            status="succeeded",
            amount_cents=request.amount_cents,
            idempotency_key="key",
            activity_attempt=activity.info().attempt,
            mode="dry-run",
        )

    return [decide, order, history, policy, memory, refund, remember]


async def _check_refund_phases(temporal: str) -> None:
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    refund_may_return = asyncio.Event()
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=temporal
    ) as env:
        queue = f"phase-test-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[RefundWorkflow],
            activities=_fake_activities(refund_may_return),
        ):
            plain = await env.client.start_workflow(
                RefundWorkflow.run,
                _request(8000),
                id=f"plain-{queue}",
                task_queue=queue,
            )
            progress = await _wait_for_phase(plain, "issuing_refund")
            # The progress Query carries what the stage and watch view show.
            assert progress["decision"]["recommendation"] == "approve"
            assert progress["request"]["order_id"] == "order-1234"
            assert progress["working_memory"][-1]["tool"] == "agent_step"
            refund_may_return.set()
            await plain.result()
            await _wait_for_phase(plain, "completed")
            history = await plain.fetch_history()
            [scheduled] = [
                event.activity_task_scheduled_event_attributes
                for event in history.events
                if event.HasField("activity_task_scheduled_event_attributes")
                and event.activity_task_scheduled_event_attributes.activity_type.name
                == "issue_refund"
            ]
            assert scheduled.heartbeat_timeout.ToTimedelta() == REFUND_HEARTBEAT_TIMEOUT
            # The names a viewer reads in Event History match the code on screen.
            [turn] = [
                event
                for event in history.events
                if event.HasField("activity_task_scheduled_event_attributes")
                and event.activity_task_scheduled_event_attributes.activity_type.name
                == "agent_decide_next_step"
            ]
            assert json.loads(turn.user_metadata.summary.data) == (
                "Agent turn 1: decide the next step"
            )

            # Every other Activity names its step in plain words too.
            every_tool = await env.client.start_workflow(
                RefundWorkflow.run,
                replace(
                    _request(8000), order_id="order-1234", customer_id="uses-every-tool"
                ),
                id=f"every-tool-{queue}",
                task_queue=queue,
            )
            await every_tool.result()
            history = await every_tool.fetch_history()
            summaries = {
                event.activity_task_scheduled_event_attributes.activity_type.name: (
                    json.loads(event.user_metadata.summary.data)
                )
                for event in history.events
                if event.HasField("activity_task_scheduled_event_attributes")
            }
            assert summaries == {
                "agent_decide_next_step": "Agent turn 5: decide the next step",
                "lookup_order": "Look up order 1234",
                "lookup_customer_history": "Look up the customer's refund history",
                "check_refund_policy": "Check the refund policy",
                "search_memory": "Recall past cases from long-term memory",
                "issue_refund": "Issue the refund in Stripe",
                "remember_case": "Save a case note to long-term memory",
            }


def test_progress_query_follows_the_run_to_completion() -> None:
    # A real local dev server on a free port, from the temporal CLI on PATH.
    # Nothing is downloaded, and no other Temporal server is touched.
    temporal = shutil.which("temporal")
    if temporal is None:
        pytest.skip("needs the temporal CLI on PATH")
    asyncio.run(_check_refund_phases(temporal))


async def _check_stage_hold_and_new_worker(temporal: str) -> None:
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from refund_agent.demo_hooks import DemoHooks, DemoHooksInterceptor
    from refund_agent.settings import refund_hold_marker

    refund_may_return = asyncio.Event()
    refund_may_return.set()
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=temporal
    ) as env:
        queue = f"hook-test-{uuid.uuid4().hex[:8]}"
        # The stage's first Worker: the hook holds the refund before it runs.
        held_worker = Worker(
            env.client,
            task_queue=queue,
            workflows=[RefundWorkflow],
            activities=_fake_activities(refund_may_return),
            interceptors=[DemoHooksInterceptor(DemoHooks(hold_refund=True))],
        )
        async with held_worker:
            handle = await env.client.start_workflow(
                RefundWorkflow.run,
                _request(8000),
                id=f"hook-{queue}",
                task_queue=queue,
            )
            deadline = time.monotonic() + 15
            while not refund_hold_marker().exists():
                assert time.monotonic() < deadline, "the hook never held the refund"
                await asyncio.sleep(0.05)
            progress = await handle.query(RefundWorkflow.progress)
            assert progress["phase"] == "issuing_refund"
        # The held Worker is gone (shut down here; SIGKILL on stage). A plain
        # Worker picks up the next attempt and finishes the run.
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[RefundWorkflow],
            activities=_fake_activities(refund_may_return),
        ):
            result = await asyncio.wait_for(handle.result(), timeout=30)
        assert result.refund_id == "re_test"
        assert result.activity_attempt == 2


def test_stage_hook_holds_the_refund_until_a_new_worker_takes_over() -> None:
    temporal = shutil.which("temporal")
    if temporal is None:
        pytest.skip("needs the temporal CLI on PATH")
    asyncio.run(_check_stage_hold_and_new_worker(temporal))


async def _check_offline_run_with_real_activities(temporal: str) -> None:
    import concurrent.futures

    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from refund_agent.activities import (
        agent_decide_next_step,
        issue_refund,
        remember_case,
    )
    from refund_agent.fake_stripe import find_refund

    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=temporal
    ) as env:
        queue = f"offline-test-{uuid.uuid4().hex[:8]}"
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            async with Worker(
                env.client,
                task_queue=queue,
                workflows=[RefundWorkflow],
                activities=[
                    agent_decide_next_step,
                    lookup_order,
                    lookup_customer_history,
                    check_refund_policy,
                    search_memory,
                    issue_refund,
                    remember_case,
                ],
                activity_executor=executor,
            ):
                request = replace(
                    _request(8000),
                    item_opened=None,
                    damage=None,
                    model_provider=OFFLINE_POLICY,
                )
                handle = await env.client.start_workflow(
                    RefundWorkflow.run,
                    request,
                    id=f"offline-{queue}",
                    task_queue=queue,
                )
                # Answers can arrive before the questions; the loop uses them.
                await handle.signal(
                    RefundWorkflow.customer_answer, args=["damage", "torn wing"]
                )
                await handle.signal(
                    RefundWorkflow.customer_answer, args=["item_opened", "Yes"]
                )
                result = await asyncio.wait_for(handle.result(), timeout=30)
                progress = await handle.query(RefundWorkflow.progress)

    assert (result.status, result.mode, result.activity_attempt) == (
        "succeeded",
        "dry-run",
        1,
    )
    assert find_refund(f"offline-{queue}")["calls"] == 1
    answers = [
        obs["result"]["answer"]
        for obs in progress["working_memory"]
        if obs["tool"] == "customer_answer"
    ]
    assert answers == ["Yes", "torn wing"]


def test_offline_run_completes_with_the_real_activities() -> None:
    # The real Activities on a real Worker, so their argument types decode.
    temporal = shutil.which("temporal")
    if temporal is None:
        pytest.skip("needs the temporal CLI on PATH")
    asyncio.run(_check_offline_run_with_real_activities(temporal))


def test_lookups_read_the_demo_store() -> None:
    order = lookup_order("order-1234")
    assert (order.item, order.amount_cents, order.status) == (
        "python plushy",
        8000,
        "delivered",
    )
    history = lookup_customer_history("cus_demo_42")
    assert history.account_tenure_days == 824
    assert len(history.prior_refunds) == 1
    assert search_memory("cus_demo_42").past_cases == []
    assert check_refund_policy("order-1234").return_required is False


def test_lookup_of_an_unknown_order_fails_without_retrying() -> None:
    with pytest.raises(ApplicationError) as error:
        lookup_order("order-9999")
    assert error.value.non_retryable


def test_case_notes_are_saved_once_and_recalled_apart_from_the_records() -> None:
    from refund_agent.activities import remember_case

    assert remember_case("cus_demo_42", "wf-1", "refund succeeded") is True
    # A retried write with the same case ID saves nothing new.
    assert remember_case("cus_demo_42", "wf-1", "refund succeeded") is False
    assert remember_case("cus_demo_42", "wf-2", "second case") is True
    assert search_memory("cus_demo_42").past_cases == [
        "second case",
        "refund succeeded",
    ]
    # Memory never comes back through a record lookup, and notes are not refunds.
    history = asdict(lookup_customer_history("cus_demo_42"))
    assert "past_cases" not in history
    assert len(history["prior_refunds"]) == 1


def test_lookup_tools_take_no_ids_from_the_model() -> None:
    from refund_agent.activities.model import _TOOL_SCHEMAS

    lookups = {TOOL_ORDER, TOOL_HISTORY, TOOL_POLICY, TOOL_MEMORY}
    for schema in _TOOL_SCHEMAS:
        if schema["name"] in lookups:
            assert schema["parameters"]["properties"] == {}
