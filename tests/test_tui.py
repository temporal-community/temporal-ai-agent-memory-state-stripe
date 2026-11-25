import asyncio
import io
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("rich")

from rich.console import Console
from rich.text import Text
from temporalio.api.common.v1 import ActivityType, Payloads
from temporalio.api.history.v1 import (
    ActivityTaskCompletedEventAttributes,
    ActivityTaskScheduledEventAttributes,
    History,
    HistoryEvent,
    WorkflowExecutionSignaledEventAttributes,
)
from temporalio.converter import DataConverter

from refund_agent import tui
from refund_agent.models import AgentStep
from refund_agent.naive_refund import _demo_frame, _process_interactive

_CACHED_STEPS = [
    {"kind": "answer", "question_id": "item_opened", "result": "Yes"},
    {"kind": "answer", "question_id": "damage", "result": "Split seam"},
    {"kind": "tool", "label": "Order", "result": "python plushy"},
    {"kind": "tool", "label": "Refund history", "result": "clean"},
    {
        "kind": "tool",
        "tool": "search_memory",
        "label": "Past cases (memory)",
        "result": "none yet",
    },
    {"kind": "ready", "label": "Next action", "result": "issue refund"},
]


def _payloads(*values) -> Payloads:
    converter = DataConverter.default.payload_converter
    return Payloads(payloads=converter.to_payloads(list(values)))


def _recorded_loop(
    *,
    refund_completed: bool = False,
    recommendation: str | None = "approve",
    approved: bool = False,
) -> History:
    """The canned durable loop as Temporal records it, up to the refund."""

    events: list[HistoryEvent] = []

    def activity(name: str, result) -> None:
        scheduled_id = len(events) + 1
        events.append(
            HistoryEvent(
                event_id=scheduled_id,
                activity_task_scheduled_event_attributes=(
                    ActivityTaskScheduledEventAttributes(
                        activity_type=ActivityType(name=name)
                    )
                ),
            )
        )
        events.append(
            HistoryEvent(
                event_id=scheduled_id + 1,
                activity_task_completed_event_attributes=(
                    ActivityTaskCompletedEventAttributes(
                        scheduled_event_id=scheduled_id,
                        result=_payloads(result),
                    )
                ),
            )
        )

    def answer(question_id: str, value: str) -> None:
        events.append(
            HistoryEvent(
                event_id=len(events) + 1,
                workflow_execution_signaled_event_attributes=(
                    WorkflowExecutionSignaledEventAttributes(
                        signal_name="customer_answer",
                        input=_payloads(question_id, value),
                    )
                ),
            )
        )

    def agent_turn(**step) -> None:
        activity("agent_decide_next_step", AgentStep(**step))

    agent_turn(action="ask_customer", question_id="item_opened")
    answer("item_opened", "Yes")
    agent_turn(action="ask_customer", question_id="damage")
    answer("damage", "Split seam")
    agent_turn(action="use_tool", tool="lookup_order")
    activity("lookup_order", {"item": "python plushy"})
    agent_turn(action="use_tool", tool="lookup_customer_history")
    activity("lookup_customer_history", {"prior_refunds": []})
    agent_turn(action="use_tool", tool="search_memory")
    activity("search_memory", {"customer_id": "cus_demo_42", "past_cases": []})
    agent_turn(action="decide", recommendation=recommendation)
    if approved:
        events.append(
            HistoryEvent(
                event_id=len(events) + 1,
                workflow_execution_signaled_event_attributes=(
                    WorkflowExecutionSignaledEventAttributes(
                        signal_name="approve", input=_payloads("approved")
                    )
                ),
            )
        )
    if refund_completed:
        activity("issue_refund", {"status": "succeeded"})
    return History(events=events)


def _fake_client(fetch_history) -> SimpleNamespace:
    description = SimpleNamespace(
        status=SimpleNamespace(name="RUNNING"),
        raw_description=SimpleNamespace(pending_activities=[]),
    )

    async def describe():
        return description

    handle = SimpleNamespace(describe=describe, fetch_history=fetch_history)
    return SimpleNamespace(
        get_workflow_handle=lambda _workflow_id: handle,
        data_converter=DataConverter.default,
    )


def test_agent_panel_starts_with_same_invitation_as_naive_demo(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tui, "_worker_alive", lambda: (True, 123))

    panel = tui._agent_panel("new-refund")

    assert "How can I help you?" in panel.renderable.plain
    assert "no context assembled yet" in panel.renderable.plain


def test_lost_agent_panel_points_to_durable_owners(monkeypatch) -> None:
    monkeypatch.setattr(tui, "_worker_alive", lambda: (False, 123))

    panel = tui._agent_panel("lost-refund")

    assert "LOST" in panel.renderable.plain
    assert "Temporal still owns execution progress" in panel.renderable.plain
    assert "Stripe is still the source of truth" in panel.renderable.plain


def test_stage_agent_returns_without_chat_history(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tui, "_worker_alive", lambda: (True, 123))

    panel = tui._stage_agent_panel("new-refund")

    assert "Welcome back, Nyghtowl" in panel.renderable.plain
    assert "How can I help?" in panel.renderable.plain


def test_reloaded_agent_answers_without_a_new_customer_request(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tui, "_worker_alive", lambda: (True, 456))

    panel = tui._stage_agent_panel(
        "existing-refund",
        recovered=True,
        refund_status="succeeded",
    )

    assert panel.title == "NEW TEMPORAL WORKER"
    assert "NO REPEATED QUESTIONS" in panel.renderable.plain
    assert "NO LOOP RESTART" in panel.renderable.plain
    assert "Same loop, rebuilt from Temporal." in panel.renderable.plain
    assert "reconnected" not in panel.renderable.plain
    assert "Your refund is complete" in panel.renderable.plain


def test_stage_system_view_makes_the_payoff_glanceable() -> None:
    panel = tui._stage_system_view(
        status="COMPLETED",
        refund={"calls": 2, "status": "succeeded"},
        pending_attempt=None,
        refund_step_completed=True,
    )

    assert "Finished by the new Worker." in panel.renderable.plain
    assert "recovery" not in panel.renderable.plain
    assert "2 CALLS  →  1 REFUND" in panel.renderable.plain
    assert "No duplicate" in panel.renderable.plain


def test_saved_loop_is_rebuilt_from_temporal_history() -> None:
    steps = asyncio.run(
        tui._loop_steps_from_history(_recorded_loop(), DataConverter.default)
    )

    assert [step["kind"] for step in steps] == [
        "answer",
        "answer",
        "tool",
        "tool",
        "tool",
        "ready",
    ]
    assert [step["result"] for step in steps[:2]] == ["Yes", "Split seam"]
    # The memory result is decoded from history, so an empty one reads "none yet".
    assert (steps[-2]["label"], steps[-2]["result"]) == (
        "Past cases (memory)",
        "none yet",
    )
    assert steps[-1]["result"] == "issue refund"


def test_history_has_no_next_action_once_the_refund_step_completed() -> None:
    steps = asyncio.run(
        tui._loop_steps_from_history(
            _recorded_loop(refund_completed=True), DataConverter.default
        )
    )

    assert "ready" not in [step["kind"] for step in steps]


@pytest.mark.parametrize(
    ("recommendation", "approved", "ready"),
    [
        ("escalate", False, False),
        ("escalate", True, True),
        # RefundWorkflow.run escalates anything that is not approve or deny.
        ("review", True, True),
        (None, True, True),
        ("deny", True, False),
    ],
)
def test_history_next_action_mirrors_the_workflow_decision(
    recommendation, approved, ready
) -> None:
    steps = asyncio.run(
        tui._loop_steps_from_history(
            _recorded_loop(recommendation=recommendation, approved=approved),
            DataConverter.default,
        )
    )

    assert ("ready" in [step["kind"] for step in steps]) is ready


def test_process_gone_panel_reads_the_loop_from_history_not_the_cache(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))

    async def fetch_history():
        return _recorded_loop()

    panel = asyncio.run(
        tui._stage_system_panel(
            _fake_client(fetch_history),
            "worker-gone",
            loop_steps=[],
            loop_from_history=True,
        )
    )
    text = panel.renderable.plain

    assert "Read from Temporal just now:" in text
    # One label per list: the history label replaces the pre-kill one.
    assert "Saved so far:" not in text
    assert "Agent loop saved." not in text
    assert "Customer answers: 2" in text
    assert "Completed lookups: 3" in text
    assert "Next action: issue refund" in text
    assert "(demo pauses here, before Stripe)" not in text
    assert "earlier reading" not in text


def test_process_gone_panel_labels_the_cached_fallback_when_history_hangs(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tui, "_HISTORY_READ_TIMEOUT_SECONDS", 0.05)

    async def fetch_history():
        await asyncio.sleep(10)

    started = time.monotonic()
    panel = asyncio.run(
        tui._stage_system_panel(
            _fake_client(fetch_history),
            "worker-gone",
            loop_steps=_CACHED_STEPS,
            loop_from_history=True,
        )
    )
    text = panel.renderable.plain

    assert time.monotonic() - started < 2
    assert "Could not re-read Temporal." in text
    assert "Showing the earlier reading:" in text
    assert "just now" not in text
    assert "Saved so far:" not in text
    assert "Customer answers: 2" in text


def test_process_gone_frame_with_history_label_fits_the_stage_width(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tui, "_worker_alive", lambda: (False, 456))
    labels = {
        "history": "Read from Temporal just now:",
        "cached": "Showing the earlier reading:",
    }
    for source, label in labels.items():
        frame = tui._stage_build(
            tui._stage_agent_panel("worker-gone"),
            tui._stage_system_view(
                status="RUNNING",
                refund=None,
                pending_attempt=None,
                refund_step_completed=False,
                loop_steps=_CACHED_STEPS,
                loop_source=source,
            ),
        )
        output = io.StringIO()
        Console(file=output, width=80, height=40).print(frame)
        lines = output.getvalue().splitlines()

        assert "WORKER GONE" in output.getvalue()
        assert "Its in-memory loop is gone." in output.getvalue()
        assert "Temporal still has the saved loop. →" in output.getvalue()
        assert label in output.getvalue()
        assert "Next action: issue refund" in output.getvalue()
        assert len(lines) <= 21
        assert all(len(line) <= 80 for line in lines)


@pytest.mark.parametrize(
    ("loop_source", "pauses"),
    [(None, True), ("history", False), ("cached", False)],
)
def test_demo_pause_is_labeled_only_on_the_pre_kill_frame(loop_source, pauses) -> None:
    panel = tui._stage_system_view(
        status="RUNNING",
        refund=None,
        pending_attempt=None,
        refund_step_completed=False,
        loop_steps=_CACHED_STEPS,
        loop_source=loop_source,
    )
    text = panel.renderable.plain

    assert ("(demo pauses here, before Stripe)" in text) is pauses
    assert ("Saved so far:" in text) is (loop_source is None)


def test_saved_frame_labels_the_pause_and_fits_the_stage_width(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tui, "_worker_alive", lambda: (True, 456))
    frame = tui._stage_build(
        tui._stage_agent_panel("saved", loop_steps=_CACHED_STEPS),
        tui._stage_system_view(
            status="RUNNING",
            refund=None,
            pending_attempt=None,
            refund_step_completed=False,
            loop_steps=_CACHED_STEPS,
        ),
    )
    output = io.StringIO()
    Console(file=output, width=80, height=40).print(frame)
    text = output.getvalue()
    lines = text.splitlines()

    assert "TEMPORAL WORKER" in text
    assert "✓ Package opened: Yes" in text
    assert "Saved so far:" in text
    assert "(demo pauses here, before Stripe)" in text
    assert len(lines) <= 20
    assert all(len(line) <= 80 for line in lines)


def test_reloaded_agent_does_not_call_a_pending_refund_complete(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tui, "_worker_alive", lambda: (True, 456))

    panel = tui._stage_agent_panel(
        "existing-refund",
        recovered=True,
        refund_status="pending",
    )

    assert "Stripe accepted the refund" in panel.renderable.plain
    assert "status is still pending" in panel.renderable.plain
    assert "refund is complete" not in panel.renderable.plain


def test_stage_system_view_reports_pending_stripe_status() -> None:
    panel = tui._stage_system_view(
        status="COMPLETED",
        refund={"calls": 1, "status": "pending"},
        pending_attempt=None,
        refund_step_completed=True,
    )

    assert "Refund: PENDING" in panel.renderable.plain
    assert "Stripe has not confirmed it yet." in panel.renderable.plain
    assert "SUCCEEDED" not in panel.renderable.plain


def test_stage_system_view_explains_a_denied_live_request() -> None:
    panel = tui._stage_system_view(
        status="COMPLETED",
        refund=None,
        pending_attempt=None,
        refund_step_completed=False,
        denied=True,
    )

    assert "This request is complete" in panel.renderable.plain
    assert "No refund step was started" in panel.renderable.plain
    assert "Refund: none" in panel.renderable.plain


def test_naive_restart_returns_to_a_blank_conversation() -> None:
    frame = _demo_frame(
        {"_restarted": True, "_new_process": True},
        [],
        stage_mode=True,
    )
    output = io.StringIO()
    Console(file=output, width=128).print(frame)
    text = output.getvalue()

    assert "Welcome back, Nyghtowl" in text
    assert "NEW AGENT PROCESS" in text
    assert "No answers. No next step." in text
    assert "FRESH START" in text
    assert "It can still read Stripe." in text
    assert "It cannot see the old answers." in text
    assert "WHAT WENT WRONG" not in text
    # The cue moved to the stage's input prompt.
    assert "What happened to my refund?" not in text
    assert "Payment: PAID" in text
    assert "Refund: none" in text


def test_naive_process_gone_is_a_visible_stage_beat() -> None:
    frame = _demo_frame({"_process_gone": True}, [], stage_mode=True)
    output = io.StringIO()
    Console(file=output, width=128).print(frame)
    text = output.getvalue()

    assert "PROCESS GONE" in text
    assert "WORKER GONE" not in text
    assert "It held the answers and next step." in text
    assert "They are gone with it." in text
    assert "WHAT'S LEFT" in text
    assert "Only Stripe's record: PAID, no refund." in text
    assert "That is correct. Stripe was never called." in text
    assert "Payment: PAID" in text
    assert "Refund: none" in text
    assert "Press Enter" not in text


def test_naive_agent_loop_reaches_the_refund_before_stripe_is_called() -> None:
    frame = _demo_frame(
        {
            "user_message": "Please refund my python plushy",
            "context": {"order": "1234", "amount": 8000, "customer": "42"},
            "memory": {"tenure_days": 824, "prior_refunds": 1},
            "_loop_steps": [
                {"kind": "answer", "question_id": "item_opened", "result": "Yes"},
                {"kind": "answer", "question_id": "damage", "result": "Split seam"},
                {"kind": "tool", "label": "Order", "result": "python plushy"},
                {"kind": "ready", "result": "issue refund"},
            ],
        },
        [],
        stage_mode=True,
    )
    output = io.StringIO()
    Console(file=output, width=128).print(frame)
    text = output.getvalue()

    assert "Please refund my python plushy" in text
    assert "AGENT LOOP" in text
    assert "✓ Damage: Split seam" in text
    assert "✓ Order: python plushy" in text
    assert "python plushy (memory)" not in text
    assert "→ Next: issue refund" in text
    assert "WORK NOT SAVED" in text
    assert "Next step: submit the refund." in text
    assert "The answers and next step exist only inside this process." in text
    assert "Press Enter" not in text
    assert "crash" not in text.lower()


def test_naive_status_check_cannot_recover_working_memory_from_stripe() -> None:
    frame = _demo_frame(
        {
            "user_message": "Did I get my refund?",
            "context": {"order": "1234", "amount": 8000, "customer": "42"},
            "memory": {"tenure_days": 824, "prior_refunds": 1},
            "_status_checked": True,
            "_refund_missing": True,
            "_new_process": True,
        },
        [],
        stage_mode=True,
    )
    output = io.StringIO()
    Console(file=output, width=128).print(frame)
    text = output.getvalue()

    assert "Let me check Stripe" in text
    assert "I lost your return answers" in text
    assert "No refund request reached Stripe" in text
    assert "Please start the return again" in text
    assert "THE CUSTOMER STARTS OVER" in text
    assert "Stripe's record is right: paid, no refund." in text
    assert "But Stripe never had the answers or the next step." in text
    # The reply belongs to the one AGENT speaker; no second heading.
    assert "ANSWER" not in text
    assert "DUPLICATE REFUND" not in text
    assert "NEW AGENT PROCESS" in text


def test_naive_status_check_does_not_invent_a_refund() -> None:
    agent: dict = {}
    ledger: list = []
    _process_interactive(agent, ledger, "Please refund my python plushy")

    replacement_agent: dict = {"_restarted": True}
    _process_interactive(replacement_agent, ledger, "Did I get my refund?")

    assert ledger == []
    assert replacement_agent["_status_checked"] is True
    assert replacement_agent["_refund_missing"] is True


def test_naive_stage_frame_does_not_stretch_to_terminal_height() -> None:
    frame = _demo_frame(
        {"_restarted": True},
        [],
        stage_mode=True,
    )
    output = io.StringIO()
    Console(file=output, width=80, height=40).print(frame)

    assert len(output.getvalue().splitlines()) <= 24


def test_durable_stage_frame_does_not_stretch_to_terminal_height(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tui, "_worker_alive", lambda: (True, 456))
    frame = tui._stage_build(
        tui._stage_agent_panel(
            "existing-refund",
            recovered=True,
            refund_status="succeeded",
        ),
        tui._stage_system_view(
            status="COMPLETED",
            refund={"calls": 2, "status": "succeeded"},
            pending_attempt=None,
            refund_step_completed=True,
        ),
    )
    output = io.StringIO()
    Console(file=output, width=80, height=40).print(frame)

    assert len(output.getvalue().splitlines()) <= 20


@pytest.mark.parametrize("width", [80, 100, 128])
def test_recovered_frame_keeps_the_rebuilt_line_whole_at_stage_width(
    tmp_path, monkeypatch, width
) -> None:
    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tui, "_worker_alive", lambda: (True, 456))
    frame = tui._stage_build(
        tui._stage_agent_panel(
            "existing-refund",
            recovered=True,
            refund_status="succeeded",
        ),
        tui._stage_system_view(
            status="COMPLETED",
            refund={"calls": 1, "status": "succeeded"},
            pending_attempt=None,
            refund_step_completed=True,
        ),
    )
    output = io.StringIO()
    Console(file=output, width=width, height=40).print(frame)
    text = output.getvalue()

    # 100 columns is the recording width; 80 is the smallest stage terminal.
    assert "Same loop, rebuilt from Temporal." in text
    assert "NEW TEMPORAL WORKER" in text
    assert "Refund: SUCCEEDED" in text
    # Call counts appear only when a retry made more than one call.
    assert "1 call" not in text
    assert "It succeeded before the Worker reported back." not in text
    assert all(len(line) <= width for line in text.splitlines())


_NAIVE_STAGE_AGENTS = {
    "start": {},
    "question": {
        "context": {"order": "1234", "amount": 8000, "customer": "Nyghtowl"},
        "user_message": "Please refund order 1234.",
        "_loop_steps": _CACHED_STEPS[:1],
        "_pending_question": {"question": "What was damaged?"},
    },
    "ready": {
        "context": {"order": "1234", "amount": 8000, "customer": "Nyghtowl"},
        "user_message": "Please refund order 1234.",
        "_loop_steps": _CACHED_STEPS,
    },
    "gone": {"_process_gone": True},
    "new process": {"_restarted": True, "_new_process": True},
    "starts over": {
        "context": {"order": "1234", "amount": 8000, "customer": "Nyghtowl"},
        "user_message": "What happened to my refund?",
        "_status_checked": True,
        "_refund_missing": True,
        "_new_process": True,
    },
}


@pytest.mark.parametrize("name", list(_NAIVE_STAGE_AGENTS))
def test_naive_stage_frames_never_call_the_agent_process_a_worker(name) -> None:
    frame = _demo_frame(_NAIVE_STAGE_AGENTS[name], [], stage_mode=True)
    output = io.StringIO()
    Console(file=output, width=80, height=40).print(frame)
    text = output.getvalue()
    lines = text.splitlines()

    assert "worker" not in text.lower()
    assert "WHAT SURVIVES" in text
    assert "ORDER + STRIPE" not in text
    assert len(lines) <= 24
    assert all(len(line) <= 80 for line in lines)


def test_naive_stage_frame_has_no_footer_cue() -> None:
    staged = _demo_frame({}, [], stage_mode=True)
    standalone = _demo_frame({}, [])

    # Header, panes, and explanation. The stage's input prompt is the only cue.
    assert len(staged.renderables) == 3
    assert len(standalone.renderables) == 4
    assert "restart / deploy / OOM" in str(standalone.renderables[-1].renderable)


@pytest.mark.parametrize(
    ("setup", "naive_line", "durable_line", "heading"),
    [
        (
            tui.DemoSetup(),
            "OFFLINE: fixed policy, no model calls · offline ledger (no Stripe)",
            "OFFLINE: fixed policy, no model calls · offline ledger (no Stripe)",
            "OFFLINE LEDGER (Stripe stand-in)",
        ),
        (
            tui.DemoSetup(real_stripe=True),
            "Scripted steps · Stripe test mode",
            "Fixed policy (no LLM) · sample lookups · Stripe test mode",
            "STRIPE (test mode)",
        ),
        (
            tui.DemoSetup(real_stripe=True, model_provider="anthropic"),
            "Live model (anthropic) · sample lookups · Stripe test mode",
            "Live model (anthropic) · sample lookups · Stripe test mode",
            "STRIPE (test mode)",
        ),
        (
            tui.DemoSetup(model_provider="openai"),
            "Live model (openai) · sample lookups · offline ledger (no Stripe)",
            "Live model (openai) · sample lookups · offline ledger (no Stripe)",
            "OFFLINE LEDGER (Stripe stand-in)",
        ),
    ],
)
def test_stage_headers_disclose_what_is_scripted(
    tmp_path, monkeypatch, setup, naive_line, durable_line, heading
) -> None:
    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tui, "_worker_alive", lambda: (True, 456))
    naive = io.StringIO()
    Console(file=naive, width=80, height=40).print(
        _demo_frame({}, [], stage_mode=True, setup=setup)
    )
    durable = io.StringIO()
    Console(file=durable, width=80, height=40).print(
        tui._stage_build(
            tui._stage_agent_panel("setup"),
            tui._stage_system_view(
                status=None,
                refund=None,
                pending_attempt=None,
                refund_step_completed=False,
                setup=setup,
            ),
            setup=setup,
        )
    )

    assert "Demo 1: Without Temporal, the agent loses its place" in naive.getvalue()
    assert naive_line in naive.getvalue()
    assert heading in naive.getvalue()
    assert "Demo 2: With Temporal, the agent keeps its place" in durable.getvalue()
    assert durable_line in durable.getvalue()
    assert heading in durable.getvalue()
    # The offline ledger is never labeled as plain Stripe.
    if not setup.real_stripe:
        assert "STRIPE" not in naive.getvalue() + durable.getvalue()
    for output in (naive, durable):
        assert all(len(line) <= 80 for line in output.getvalue().splitlines())


def test_offline_header_is_bold_not_dim() -> None:
    header = tui._demo_header("Demo", "subtitle", tui.DemoSetup().durable_line)
    live = tui._demo_header(
        "Demo", "subtitle", tui.DemoSetup(model_provider="openai").durable_line
    )

    [offline_span] = [span for span in header.spans if "yellow" in str(span.style)]
    assert header.plain[offline_span.start : offline_span.end].startswith(
        "OFFLINE: fixed policy, no model calls"
    )
    assert not [span for span in live.spans if "yellow" in str(span.style)]


def test_agent_view_comes_from_the_progress_query_without_noise_rows() -> None:
    progress = {
        "phase": "issuing_refund",
        "request": {"request_id": "r", "order_id": "order-1234", "amount_cents": 8000},
        "working_memory": [
            {"tool": "agent_step", "action": "ask_customer", "question_id": "damage"},
            {"tool": "customer_answer", "result": {"question_id": "damage"}},
            {"tool": "agent_step", "action": "use_tool", "tool_chosen": "lookup_order"},
            {"tool": "lookup_order", "result": {"item": "python plushy"}},
        ],
        "decision": {"recommendation": "approve", "source": "canned"},
    }

    view = tui.view_from_progress(progress)

    assert view["context"]["order_id"] == "order-1234"
    assert [obs["tool"] for obs in view["observations"]] == ["lookup_order"]
    assert view["decision"]["recommendation"] == "approve"
    assert tui.view_from_progress(None) == {}


def test_denied_agent_panel_explains_why_from_the_query_view(monkeypatch) -> None:
    monkeypatch.setattr(tui, "_worker_alive", lambda: (True, 456))
    view = {
        "context": {"order_id": "order-1234", "amount_cents": 8000, "reason": "x"},
        "observations": [{"tool": "lookup_order"}],
        "decision": {"recommendation": "deny", "rationale": "Not eligible."},
    }

    text = tui._stage_agent_panel("denied", view=view).renderable.plain

    assert "Order details" in text
    assert "Do not issue the refund" in text
    assert "Not eligible." in text


@pytest.mark.parametrize(("attempt", "shown"), [(None, False), (1, False), (2, True)])
def test_only_a_retry_attempt_is_shown(attempt, shown) -> None:
    panel = tui._stage_system_view(
        status="RUNNING",
        refund=None,
        pending_attempt=attempt,
        refund_step_completed=False,
        loop_steps=_CACHED_STEPS,
    )

    assert ("Current attempt" in panel.renderable.plain) is shown


@pytest.mark.parametrize(
    ("refund_id", "heading"),
    [
        (None, "STRIPE (source of truth)"),
        ("re_dry_abc", "OFFLINE LEDGER (Stripe stand-in, source of truth)"),
    ],
)
def test_watch_view_names_the_refund_owner_as_the_source_of_truth(
    tmp_path, monkeypatch, refund_id, heading
) -> None:
    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))
    if refund_id is not None:
        monkeypatch.setattr(
            tui,
            "find_refund",
            lambda _workflow_id: {"refund_id": refund_id, "status": "succeeded"},
        )

    async def fetch_history():
        return _recorded_loop()

    panel = asyncio.run(tui._system_panel(_fake_client(fetch_history), "watch"))
    text = panel.renderable.plain

    assert "EXECUTION STATE" in text
    assert heading in text
    assert "EFFECT STATE" not in text


def test_only_past_cases_row_is_labeled_memory() -> None:
    body = Text()
    tui._append_loop_steps(body, _CACHED_STEPS)

    assert "  ✓ Past cases (memory): none yet\n" in body.plain
    # Record lookups are not memory, so they carry no memory tag.
    assert "  ✓ Refund history: clean\n" in body.plain
    assert body.plain.count("(memory)") == 1


def test_case_note_lines_fit_a_narrow_pane() -> None:
    from refund_agent.activities.tools import case_note_lines

    note = (
        "2026-11-23: refund succeeded for order-1234, $80.00. "
        "Clean history and a damaged item."
    )
    assert case_note_lines(note) == [
        "2026-11-23  refund succeeded",
        "order 1234 · $80.00",
    ]
    # An unexpected format still shows, wrapped and capped.
    assert all(len(line) <= 34 for line in case_note_lines("x " * 80))


def test_final_frame_shows_the_case_note_the_new_worker_saved() -> None:
    panel = tui._stage_system_view(
        status="COMPLETED",
        refund={"calls": 1, "status": "succeeded"},
        pending_attempt=None,
        refund_step_completed=True,
        case_note="2026-11-23: refund succeeded for order-1234, $80.00. Clean.",
    )
    text = panel.renderable.plain

    assert "LONG-TERM MEMORY" in text
    assert "Case note saved after the refund:" in text
    assert "order 1234 · $80.00" in text
    # Each line fits the right pane at the smallest stage width.
    for width in (80, 100):
        output = io.StringIO()
        Console(file=output, width=width // 2).print(panel)
        rendered = output.getvalue()
        assert "order 1234 · $80.00" in rendered
        assert "Case note saved after the refund:" in rendered


def test_final_frame_has_no_memory_section_without_a_note() -> None:
    panel = tui._stage_system_view(
        status="COMPLETED",
        refund={"calls": 1, "status": "succeeded"},
        pending_attempt=None,
        refund_step_completed=True,
    )

    assert "LONG-TERM MEMORY" not in panel.renderable.plain


def test_recovered_agent_reports_back_without_being_asked(monkeypatch) -> None:
    monkeypatch.setattr(tui, "_worker_alive", lambda: (True, 1))

    panel = tui._stage_agent_panel("done", recovered=True, refund_status="succeeded")

    assert "(the customer didn't ask)" in panel.renderable.plain
    assert "Your refund is complete." in panel.renderable.plain


def test_naive_follow_up_finds_no_memory_because_nothing_was_saved() -> None:
    frame = _demo_frame(
        {
            "context": {"order": "1234", "amount": 8000},
            "_status_checked": True,
            "_refund_missing": True,
            "_past_cases": 0,
            "user_message": "What happened to my refund?",
        },
        [],
        stage_mode=True,
    )
    output = io.StringIO()
    Console(file=output, width=100).print(frame)
    text = output.getvalue()

    assert "✓ Past cases (memory): none" in text
    assert "nothing saved; the process died first" in text


def test_get_case_note_reads_one_case(tmp_path, monkeypatch) -> None:
    from refund_agent import store

    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))
    assert store.get_case_note("wf-1") is None
    store.add_case_note("cus_demo_42", "wf-1", "a note")
    assert store.get_case_note("wf-1") == "a note"
