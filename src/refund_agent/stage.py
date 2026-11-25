"""A guided, single-terminal version of the complete meetup demo.

The stage runner owns the disposable processes it needs. It uses a private task
queue and state directory, so it can stop and replace its Worker without
touching another Worker the presenter may already have running.

It follows Demo 2 through the Workflow's `progress` Query and Event History,
and it applies the demo's pause and failure drills at the Worker edge with the
interceptor in demo_hooks.py, so the Workflow and Activities stay free of demo
machinery.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import IO

import stripe
from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text
from temporalio.client import Client, WorkflowHandle

from refund_agent.activities.tools import (
    MEMORY_LABEL,
    TOOL_MEMORY,
    past_cases_summary,
)
from refund_agent.fake_stripe import find_refund
from refund_agent.models import OFFLINE_POLICY, RefundRequest
from refund_agent.settings import (
    refund_hold_marker,
    state_dir,
    temporal_address,
    temporal_identity,
    temporal_namespace,
    validate_stripe_key,
    worker_pid_file,
)
from refund_agent.tui import DemoSetup
from refund_agent.workflow import RefundWorkflow

_SERVER_START_TIMEOUT_SECONDS = 20
_WORKER_START_TIMEOUT_SECONDS = 15
_DEMO_TIMEOUT_SECONDS = 90
_DEFAULT_REFUND_REQUEST = "Please refund order 1234."
# The customer's side of the story: the plushy was opened and arrived with a
# split seam. Pressing Enter at a question gives these answers, whichever way
# the model words the question, so a live model's suggested answer can't
# change the scenario. A question outside the script falls back to it.
_CUSTOMER_ANSWERS = {"item_opened": "Yes", "damage": "Split seam"}


def _scripted_answer(question_id: str, suggested: str) -> str:
    return _CUSTOMER_ANSWERS.get(question_id, suggested)


_SIMULATED_RETRY_WINDOW_SECONDS = 30
# Longer than the refund's 5 s heartbeat timeout (workflow.py), so Temporal has
# already timed out the killed attempt when the next frame draws.
_SIMULATED_RETRY_DETECTION_SECONDS = 6


def _roles() -> Panel:
    body = Text()
    body.append("This demo covers four of them:\n\n", style="dim")
    body.append("CONTEXT           ", style="bold yellow")
    body.append("what the model sees this turn\n")
    body.append("WORKING MEMORY    ", style="bold cyan")
    body.append("this run's answers and results; part of execution state\n")
    body.append("LONG-TERM MEMORY  ", style="bold blue")
    body.append("case notes kept across runs, in their own store\n")
    body.append("STATE             ", style="bold green")
    body.append("where the work stands:\n")
    body.append("  execution state ", style="green")
    body.append("where the run is and what's next (Temporal)\n")
    body.append("  Stripe          ", style="green")
    body.append("source of truth for whether the refund happened")
    return Panel(
        body,
        title="An agent works with different kinds of information",
        border_style="white",
    )


def _intro() -> Group:
    thesis = Text()
    thesis.append(
        "An agent can know exactly what to do and still lose the work a "
        "customer submitted.\n\n"
    )
    thesis.append(
        "We'll stop the agent right before it calls Stripe: first without "
        "Temporal, then with it."
    )
    return Group(
        Panel(thesis, title="Agent memory and state", border_style="cyan"),
        _roles(),
    )


def _closing(refund_status: str = "succeeded") -> Group:
    result = Text()
    result.append(
        "Without Temporal: the customer had to start over.\n",
        style="bold red",
    )
    if refund_status.lower() == "succeeded":
        result.append(
            "With Temporal: a new Worker picked up at the saved next action.\n",
            style="bold green",
        )
        # The case note is written after the refund, so only a finished run
        # has one: Demo 1's process died before it could save anything.
        result.append(
            "The agent reported back and saved a case note to long-term memory.",
            style="green",
        )
    else:
        result.append(
            "With Temporal: a new Worker picked up the same request.\n",
            style="bold yellow",
        )
        result.append(
            f"Stripe status: {refund_status.upper()} — not reported as complete.",
            style="yellow",
        )
    return Group(
        Panel(result, title="The difference", border_style="green"),
        Panel(
            "Memory helps reasoning continue.\n"
            "Temporal helps the operation continue.\n"
            "Stripe knows whether money moved.",
            border_style="white",
        ),
    )


def _display_path(path: Path) -> str:
    """Show a path relative to the working directory when it is inside it.

    An absolute path would put the presenter's username on screen. The relative
    form still works as `refund-demo usage --state-dir` from the same directory.
    """

    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _tail(path: Path, line_count: int = 12) -> str:
    if not path.exists():
        return "(no log output)"
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-line_count:])


def _refund_is_held() -> bool:
    """Whether the stage's Worker hook is holding the refund (demo_hooks.py)."""

    return refund_hold_marker().exists()


async def _wait_for_refund_hold(*, timeout: float) -> None:
    """Wait until the Worker hook holds the refund, so a kill lands before Stripe."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _refund_is_held():
            return
        await asyncio.sleep(0.1)
    raise RuntimeError("timed out waiting for the Worker to hold the refund")


def _stop_process(process: subprocess.Popen[str] | None, *, hard: bool = False) -> None:
    if process is None or process.poll() is not None:
        return
    if hard:
        process.kill()
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class _Services:
    def __init__(
        self,
        *,
        base_state: Path,
        stage_state: Path,
        task_queue: str,
    ) -> None:
        self.base_state = base_state
        self.stage_state = stage_state
        self.task_queue = task_queue
        self.server: subprocess.Popen[str] | None = None
        self.worker: subprocess.Popen[str] | None = None
        self.server_log_path = stage_state / "temporal.log"
        self.worker_log_path = stage_state / "worker.log"
        self._server_log: IO[str] | None = None
        self._worker_log: IO[str] | None = None

    async def connect_or_start_server(self) -> Client:
        try:
            return await asyncio.wait_for(self._connect(), timeout=2)
        except Exception as first_error:
            # Do not silently replace an explicitly configured remote service
            # with localhost. That would make the demo appear to use the wrong
            # system of record.
            address = temporal_address()
            host = address.rsplit(":", 1)[0]
            if host not in {"localhost", "127.0.0.1", "::1"}:
                raise RuntimeError(
                    f"could not connect to configured Temporal service {address}"
                ) from first_error

        executable = shutil.which("temporal")
        if executable is None:
            raise RuntimeError(
                "Temporal is not reachable and the `temporal` CLI is not on PATH"
            )
        self.base_state.mkdir(parents=True, exist_ok=True)
        self._server_log = self.server_log_path.open("a", encoding="utf-8")
        self.server = subprocess.Popen(
            [
                executable,
                "server",
                "start-dev",
                "--db-filename",
                str(self.base_state / "temporal.db"),
            ],
            stdout=self._server_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + _SERVER_START_TIMEOUT_SECONDS
        last_error: BaseException | None = None
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                raise RuntimeError(
                    "Temporal dev server exited while starting:\n"
                    + _tail(self.server_log_path)
                )
            try:
                return await asyncio.wait_for(self._connect(), timeout=1)
            except Exception as error:
                last_error = error
                await asyncio.sleep(0.25)
        raise RuntimeError("Temporal dev server did not become ready") from last_error

    async def _connect(self) -> Client:
        return await Client.connect(
            temporal_address(),
            namespace=temporal_namespace(),
            identity=temporal_identity(),
        )

    async def start_worker(
        self,
        *,
        hold_refund: bool = False,
        simulate_stripe_timeout: bool = False,
        restart_window_seconds: float = 0,
    ) -> None:
        """Start a Worker, with the demo-only hooks this beat needs.

        The hooks are an Activity interceptor (demo_hooks.py) that only this
        runner switches on: hold_refund holds the refund before Stripe until
        the Worker is killed, simulate_stripe_timeout does the same as a Stripe
        API that never answers, and restart_window_seconds keeps the refund
        open after Stripe accepts it. A Worker started with none of them is a
        plain Worker.
        """

        if self.worker is not None and self.worker.poll() is None:
            return
        if self._worker_log is None:
            self._worker_log = self.worker_log_path.open("a", encoding="utf-8")
        refund_hold_marker().unlink(missing_ok=True)
        environment = os.environ.copy()
        environment["DEMO_STATE_DIR"] = str(self.stage_state)
        environment["TEMPORAL_TASK_QUEUE"] = self.task_queue
        environment["STAGE_HOLD_REFUND"] = "1" if hold_refund else "0"
        environment["STAGE_SIMULATE_STRIPE_TIMEOUT"] = (
            "1" if simulate_stripe_timeout else "0"
        )
        environment["REFUND_RESTART_WINDOW_SECONDS"] = str(restart_window_seconds)
        self.worker = subprocess.Popen(
            [sys.executable, "-m", "refund_agent.worker"],
            env=environment,
            stdout=self._worker_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + _WORKER_START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self.worker.poll() is not None:
                raise RuntimeError(
                    "Worker exited while starting:\n" + _tail(self.worker_log_path)
                )
            path = worker_pid_file()
            if path.exists():
                try:
                    if int(path.read_text(encoding="utf-8").strip()) == self.worker.pid:
                        await asyncio.sleep(0.5)
                        return
                except ValueError:
                    pass
            await asyncio.sleep(0.1)
        raise RuntimeError("Worker did not become ready")

    def kill_worker(self) -> None:
        # A real SIGKILL, like a crash or an out-of-memory kill: the Worker gets
        # no chance to shut down cleanly.
        _stop_process(self.worker, hard=True)

    def close(self) -> None:
        _stop_process(self.worker)
        _stop_process(self.server)
        pid_path = self.stage_state / "worker.pid"
        if self.worker is not None and pid_path.exists():
            try:
                recorded_pid = int(pid_path.read_text(encoding="utf-8").strip())
            except ValueError:
                recorded_pid = None
            if recorded_pid == self.worker.pid:
                pid_path.unlink()
        if self._worker_log is not None:
            self._worker_log.close()
        if self._server_log is not None:
            self._server_log.close()


async def _wait_for_first_refund(
    workflow_id: str,
    *,
    timeout: float,
    on_update: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Wait until Stripe (or the offline ledger) records this Workflow's refund."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if on_update is not None:
            await on_update()
        if find_refund(workflow_id) is not None:
            return
        await asyncio.sleep(0.2)
    raise RuntimeError("timed out waiting for the first refund call")


def _live_model_provider(explicit: str | None) -> str:
    configured = explicit or os.getenv("AGENT_MODEL_PROVIDER")
    if configured:
        provider = configured.strip().lower()
        if provider not in {"anthropic", "openai"}:
            raise RuntimeError("model provider must be 'anthropic' or 'openai'")
    else:
        available = [
            provider
            for provider, key in (
                ("anthropic", os.getenv("ANTHROPIC_API_KEY")),
                ("openai", os.getenv("OPENAI_API_KEY")),
            )
            if key
        ]
        if len(available) > 1:
            raise RuntimeError(
                "both live-model keys are configured; choose --model-provider"
            )
        if not available:
            raise RuntimeError(
                "--real-model requires ANTHROPIC_API_KEY or OPENAI_API_KEY"
            )
        provider = available[0]

    required = {
        "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"),
        "openai": ("OPENAI_API_KEY", "OPENAI_MODEL"),
    }[provider]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"{provider} live model requires {', '.join(missing)}")
    return provider


def _start_naive_at_boundary(
    stage_state: Path,
    *,
    amount_cents: int,
    payment_intent: str = "pi_dry_run_demo",
    reason: str = _DEFAULT_REFUND_REQUEST,
    real: bool = False,
    real_model: bool = False,
    model_provider: str | None = None,
) -> subprocess.Popen[str]:
    """Start Demo 1's agent process with the same request Demo 2 will get."""

    environment = os.environ.copy()
    environment["DEMO_STATE_DIR"] = str(stage_state)
    command = [
        sys.executable,
        "-m",
        "refund_agent.naive_refund",
        "refund",
        "--order",
        "1234",
        "--amount-cents",
        str(amount_cents),
        "--payment-intent",
        payment_intent,
        # One token, so request text that starts with "-" is not read as a flag.
        f"--reason={reason}",
        "--interactive-loop",
        "--hold-before-refund",
    ]
    if real:
        command.append("--real")
    if real_model:
        command.append("--real-model")
        if model_provider:
            command.extend(["--model-provider", model_provider])
    return subprocess.Popen(
        command,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _start_naive_replacement(
    stage_state: Path,
    *,
    amount_cents: int,
    payment_intent: str,
    real: bool,
) -> subprocess.Popen[str]:
    """Start a fresh Demo 1 agent process for the post-loss status question."""

    environment = os.environ.copy()
    environment["DEMO_STATE_DIR"] = str(stage_state)
    command = [
        sys.executable,
        "-m",
        "refund_agent.naive_refund",
        "replacement-status",
        "--order",
        "1234",
        "--amount-cents",
        str(amount_cents),
        "--payment-intent",
        payment_intent,
        "--hold-after-status",
    ]
    if real:
        command.append("--real")
    return subprocess.Popen(
        command,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _stop_naive_process(process: subprocess.Popen[str] | None) -> None:
    _stop_process(process, hard=True)
    if process is not None and process.stdin is not None:
        process.stdin.close()
    if process is not None and process.stdout is not None:
        process.stdout.close()


async def _read_naive_loop_event(
    process: subprocess.Popen[str], *, timeout: float = 60
) -> dict[str, str]:
    # A live model turn can take seconds; its client times out at 45 s.
    if process.stdout is None:
        raise RuntimeError("Demo 1 agent process output is unavailable")
    # A local keeps the None check above in force inside the nested function.
    stdout = process.stdout

    async def read_event() -> dict[str, str]:
        while True:
            line = await asyncio.to_thread(stdout.readline)
            if not line:
                raise RuntimeError("Demo 1 agent process exited during the agent loop")
            label, separator, payload = line.partition("|")
            if not separator:
                continue
            if label.strip() == "AGENT STEP":
                return json.loads(payload)
            if label.strip() == "AGENT ERROR":
                message = json.loads(payload).get("message", "unknown error")
                raise RuntimeError(f"the Demo 1 agent process failed: {message}")

    try:
        return await asyncio.wait_for(read_event(), timeout=timeout)
    except TimeoutError as error:
        raise RuntimeError("timed out waiting for the naive agent loop") from error


async def _drive_naive_replacement(
    process: subprocess.Popen[str],
    *,
    status_question: str,
    timeout: float = 10,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Send one question to the replacement process and read its result."""

    if process.stdin is None or process.stdout is None:
        raise RuntimeError("new Demo 1 agent process pipes are unavailable")
    process.stdin.write(status_question + "\n")
    process.stdin.flush()
    # A local keeps the None check above in force inside the nested function.
    stdout = process.stdout

    async def read_result() -> tuple[dict[str, object], list[dict[str, object]]]:
        while True:
            line = await asyncio.to_thread(stdout.readline)
            if not line:
                raise RuntimeError("new Demo 1 agent process exited before answering")
            label, separator, raw_payload = line.partition("|")
            if not separator:
                continue
            label = label.strip()
            if label not in {"AGENT RESULT", "AGENT ERROR"}:
                continue
            payload = json.loads(raw_payload)
            if label == "AGENT ERROR":
                raise RuntimeError(
                    "Stripe could not verify the naive outcome: "
                    f"{payload.get('message', 'unknown error')}"
                )
            return dict(payload["agent"]), list(payload["refunds"])

    try:
        return await asyncio.wait_for(read_result(), timeout=timeout)
    except TimeoutError as error:
        raise RuntimeError(
            "timed out waiting for the new Demo 1 agent process"
        ) from error


def _seed_test_payment(amount_cents: int) -> str:
    stripe.api_key = validate_stripe_key(os.getenv("STRIPE_API_KEY"), required=True)
    stripe.max_network_retries = 0
    intent = stripe.PaymentIntent.create(
        amount=amount_cents,
        currency="usd",
        payment_method="pm_card_visa",
        payment_method_types=["card"],
        confirm=True,
        description="python plushy (durable refund demo)",
    )
    return str(intent.id)


def _show(console: Console, renderable, cue: str) -> str:
    console.clear()
    console.print(renderable)
    return input(f"\n{cue}  ")


def _ask_for_refund(
    console: Console,
    renderable,
    cue: str,
    *,
    default: str = _DEFAULT_REFUND_REQUEST,
) -> str:
    console.clear()
    console.print(renderable)
    request = input(f"\n{cue}\nyou> ").strip()
    return request or default


async def _drive_naive_loop(
    console: Console,
    process: subprocess.Popen[str],
    *,
    request_text: str,
    amount_cents: int,
    setup: DemoSetup | None = None,
) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Let the real naive subprocess choose and ask each visible question."""

    from refund_agent.naive_refund import _demo_frame

    if process.stdin is None:
        raise RuntimeError("Demo 1 agent process input is unavailable")
    steps: list[dict[str, str]] = []
    answers: dict[str, str] = {}
    base = {
        "context": {
            "order": "1234",
            "amount": amount_cents,
            "customer": "Nyghtowl",
        },
        "user_message": request_text,
    }
    while True:
        # With --real-model the agent spends a few seconds per model turn.
        with console.status("The agent is choosing its next step..."):
            event = await _read_naive_loop_event(process)
        kind = event.get("kind")
        if kind == "question":
            console.clear()
            console.print(
                _demo_frame(
                    {**base, "_loop_steps": steps, "_pending_question": event},
                    [],
                    stage_mode=True,
                    setup=setup,
                )
            )
            default = _scripted_answer(
                str(event.get("question_id") or ""),
                str(event.get("suggested_answer") or ""),
            )
            answer = input(f"\nagent> {event.get('question')} [{default}]  ").strip()
            answer = answer or default
            process.stdin.write(answer + "\n")
            process.stdin.flush()
        elif kind in {"answer", "tool"}:
            steps.append(event)
            if kind == "answer":
                answers[event["question_id"]] = event["result"]
        elif kind == "ready":
            if event.get("recommendation") == "deny":
                # No refund to stop before Stripe, so Demo 1 has no story.
                raise RuntimeError(
                    "the model denied the Demo 1 refund; run the stage again "
                    "(after a --real take, reconcile with: uv run refund-demo cleanup)"
                )
            steps.append(event)
            return steps, answers


def _loop_steps_from_progress(progress: dict) -> list[dict[str, str]]:
    steps: list[dict[str, str]] = []
    for observation in progress.get("working_memory") or []:
        tool = observation.get("tool")
        result = observation.get("result") or {}
        if tool == "customer_answer":
            question_id = str(result.get("question_id") or "")
            steps.append(
                {
                    "kind": "answer",
                    "question_id": question_id,
                    "result": str(result.get("answer") or ""),
                }
            )
        elif tool == "lookup_order":
            steps.append(
                {
                    "kind": "tool",
                    "tool": tool,
                    "label": "Order",
                    "result": str(result.get("item") or "python plushy"),
                }
            )
        elif tool == "lookup_customer_history":
            steps.append(
                {
                    "kind": "tool",
                    "tool": tool,
                    "label": "Refund history",
                    "result": "clean",
                }
            )
        elif tool == "check_refund_policy":
            steps.append(
                {
                    "kind": "tool",
                    "tool": tool,
                    "label": "Refund policy",
                    "result": "eligible"
                    if result.get("eligible_for_refund")
                    else "not eligible",
                }
            )
        elif tool == TOOL_MEMORY:
            steps.append(
                {
                    "kind": "tool",
                    "tool": tool,
                    "label": MEMORY_LABEL,
                    "result": past_cases_summary(result),
                }
            )
    # Once the refund step is recorded (completed) there is no next action, as in
    # tui._loop_steps_from_history.
    if progress.get("phase") == "issuing_refund":
        steps.append(
            {"kind": "ready", "label": "Next action", "result": "issue refund"}
        )
    return steps


async def _drive_temporal_loop(
    handle: WorkflowHandle,
    answers: dict[str, str],
    *,
    wait_for_hold: bool = True,
    timeout: float = _DEMO_TIMEOUT_SECONDS,
) -> tuple[str, list[dict[str, str]]]:
    """Fast-forward the agent's questions and wait for the durable next action.

    Polls the Workflow's `progress` Query. It answers whatever the agent asks,
    in any order and any number of times, with the customer's Demo 1 answer or
    the question's suggested answer, so it never waits for a question that does
    not come. With wait_for_hold, it returns once the refund step has started
    and the stage's Worker hook is holding it before Stripe.
    """

    deadline = time.monotonic() + timeout
    approval_sent = False
    sent_questions: set[str] = set()
    last_progress: dict = {}
    while time.monotonic() < deadline:
        try:
            last_progress = await handle.query(RefundWorkflow.progress)
        except Exception:
            await asyncio.sleep(0.1)
            continue
        phase = last_progress.get("phase")
        pending = last_progress.get("pending_question") or {}
        if pending:
            # Staged: answer each question with the customer's Demo 1 answer,
            # sent as a customer_answer Signal, so nobody types it twice. A
            # question Demo 1's agent never asked gets the scripted answer. A
            # real app sends the customer's own reply as the Signal.
            question_id = str(pending.get("question_id") or "")
            if question_id not in sent_questions:
                answer = answers.get(
                    question_id,
                    _scripted_answer(
                        question_id, str(pending.get("suggested_answer") or "")
                    ),
                )
                await handle.signal(
                    RefundWorkflow.customer_answer,
                    args=[question_id, answer],
                )
                sent_questions.add(question_id)
        elif phase == "waiting_for_approval" and not approval_sent:
            # Staged: the runner approves an escalated refund itself. A real app
            # waits here for a person to send the approve Signal.
            await handle.signal(
                RefundWorkflow.approve,
                "approved by the guided stage runner",
            )
            approval_sent = True
        elif phase == "issuing_refund" and (not wait_for_hold or _refund_is_held()):
            # The refund step has started. On the main path the stage's Worker
            # hook holds it before Stripe (demo_hooks.py), so the kill that
            # follows lands at the same point every run.
            return "ready", _loop_steps_from_progress(last_progress)
        elif phase == "completed":
            # Past the refund already, so stop polling instead of timing out.
            return "completed", _loop_steps_from_progress(last_progress)
        elif phase == "denied":
            return "denied", _loop_steps_from_progress(last_progress)
        await asyncio.sleep(0.1)
    raise RuntimeError("timed out waiting for the durable agent loop")


async def _agent_view(handle: WorkflowHandle) -> dict | None:
    """The agent's live view from the progress Query, or None without a Worker."""

    try:
        progress = await asyncio.wait_for(
            handle.query(RefundWorkflow.progress), timeout=3
        )
    except Exception:
        return None
    from refund_agent import tui

    return tui.view_from_progress(progress)


async def _durable_frame(
    client: Client,
    workflow_id: str,
    *,
    recovered: bool = False,
    refund_status: str | None = None,
    loop_steps: list[dict[str, str]] | None = None,
    pending_question: dict[str, str] | None = None,
    loop_from_history: bool = False,
    setup: DemoSetup | None = None,
    stopped_before_refund: bool = False,
):
    # The talk path uses plain language. `refund-demo watch` keeps the detailed
    # execution vocabulary for technical exploration.
    from refund_agent import tui

    agent = tui._stage_agent_panel(
        workflow_id,
        recovered=recovered,
        refund_status=refund_status,
        loop_steps=loop_steps,
        pending_question=pending_question,
        stopped_before_refund=stopped_before_refund,
    )
    setup = setup or tui.OFFLINE_SETUP
    system = await tui._stage_system_panel(
        client,
        workflow_id,
        loop_steps=loop_steps,
        loop_from_history=loop_from_history,
        setup=setup,
    )
    return tui._stage_build(agent, system, setup=setup)


async def _denied_frame(
    client: Client, workflow_id: str, *, setup: DemoSetup | None = None
):
    from refund_agent import tui

    setup = setup or tui.OFFLINE_SETUP
    view = await _agent_view(client.get_workflow_handle(workflow_id))
    agent = tui._stage_agent_panel(workflow_id, view=view)
    system = tui._stage_system_view(
        status="COMPLETED",
        refund=None,
        pending_attempt=None,
        refund_step_completed=False,
        denied=True,
        setup=setup,
    )
    return tui._stage_build(agent, system, setup=setup)


def _durable_request(
    *,
    workflow_id: str,
    payment_intent: str,
    amount_cents: int,
    reason: str,
    real: bool,
    real_model: bool,
    model_provider: str | None,
) -> RefundRequest:
    """Build the Demo 2 Workflow input for the selected stage path.

    It carries no demo switches: the pause and the failure drills happen at
    the stage's Worker (demo_hooks.py), not in the Workflow.
    """

    return RefundRequest(
        request_id=workflow_id,
        order_id="order-1234",
        customer_id="cus_demo_42",
        payment_intent_id=payment_intent,
        amount_cents=amount_cents,
        reason=reason,
        dry_run=not real,
        # No answers up front, so the agent asks for the ones it needs.
        item_opened=None,
        damage=None,
        refund_destination="Original card",
        # Offline is an explicit choice recorded in the Workflow input.
        model_provider=model_provider if real_model else OFFLINE_POLICY,
    )


async def run(
    *,
    workflow_id: str | None,
    real: bool,
    real_model: bool,
    amount_cents: int,
    model_provider: str | None = None,
    simulate_stripe_retry: bool = False,
    simulate_stripe_timeout: bool = False,
) -> None:
    """Run both demos from one guided terminal."""

    from refund_agent.naive_refund import _demo_frame

    console = Console()
    if model_provider and not real_model:
        raise RuntimeError("--model-provider requires --real-model")
    if simulate_stripe_retry and simulate_stripe_timeout:
        raise RuntimeError(
            "--simulate-stripe-retry and --simulate-stripe-timeout are mutually "
            "exclusive"
        )
    selected_provider = _live_model_provider(model_provider) if real_model else None
    setup = DemoSetup(real_stripe=real, model_provider=selected_provider)
    run_token = uuid.uuid4().hex[:8]
    workflow_id = workflow_id or f"talk-refund-{run_token}"
    base_state = state_dir().resolve()
    stage_state = base_state / f"stage-{run_token}"
    stage_state.mkdir(parents=True, exist_ok=True)
    queue = f"refund-stage-{run_token}"
    services = _Services(
        base_state=base_state,
        stage_state=stage_state,
        task_queue=queue,
    )
    previous_state_dir = os.environ.get("DEMO_STATE_DIR")
    os.environ["DEMO_STATE_DIR"] = str(stage_state)
    client: Client | None = None
    handle: WorkflowHandle | None = None
    naive_process: subprocess.Popen[str] | None = None
    completed = False

    try:
        _show(console, _intro(), "Press Enter to start Demo 1 (without Temporal)")

        payment_intent = "pi_dry_run_demo"
        if real:
            with console.status("Creating Nyghtowl's paid Stripe test order..."):
                try:
                    payment_intent = await asyncio.to_thread(
                        _seed_test_payment, amount_cents
                    )
                except stripe.StripeError as error:
                    raise RuntimeError(
                        f"Stripe could not create the test payment: {error}"
                    ) from error

        naive_request = _ask_for_refund(
            console,
            _demo_frame({}, [], stage_mode=True, setup=setup),
            "Ask for a refund",
        )
        naive_process = _start_naive_at_boundary(
            stage_state,
            amount_cents=amount_cents,
            payment_intent=payment_intent,
            reason=naive_request,
            real=real,
            real_model=real_model,
            model_provider=selected_provider,
        )
        naive_steps, customer_answers = await _drive_naive_loop(
            console,
            naive_process,
            request_text=naive_request,
            amount_cents=amount_cents,
            setup=setup,
        )
        first_attempt = {
            "context": {
                "order": "1234",
                "amount": amount_cents,
                "customer": "Nyghtowl",
            },
            "memory": {"tenure_days": 824, "prior_refunds": 1},
            "user_message": naive_request,
            "_loop_steps": naive_steps,
        }
        _show(
            console,
            _demo_frame(
                first_attempt,
                [],
                stage_mode=True,
                setup=setup,
            ),
            "Press Enter to submit the refund",
        )
        _stop_naive_process(naive_process)
        naive_process = None
        _show(
            console,
            _demo_frame(
                {"_process_gone": True},
                [],
                stage_mode=True,
                setup=setup,
            ),
            "Press Enter to start a new agent process",
        )
        naive_process = _start_naive_replacement(
            stage_state,
            amount_cents=amount_cents,
            payment_intent=payment_intent,
            real=real,
        )
        status_question = _ask_for_refund(
            console,
            _demo_frame(
                {"_restarted": True, "_new_process": True},
                [],
                stage_mode=True,
                setup=setup,
            ),
            "Ask the new process: What happened to my refund?",
            default="What happened to my refund?",
        )
        with console.status("The new agent process is checking Stripe..."):
            recovered_naive, naive_refunds = await _drive_naive_replacement(
                naive_process,
                status_question=status_question,
            )
        _show(
            console,
            _demo_frame(
                recovered_naive,
                naive_refunds,
                stage_mode=True,
                setup=setup,
            ),
            "Press Enter for Demo 2: the same test with Temporal",
        )
        _stop_naive_process(naive_process)
        naive_process = None

        with console.status("Starting Temporal and a Temporal Worker..."):
            client = await services.connect_or_start_server()
            # Demo-only Worker hooks (demo_hooks.py): hold the refund before
            # Stripe so the kill lands at the same point every run, or, on the
            # timeout drill, hang it like a Stripe API that never answers.
            await services.start_worker(
                hold_refund=not simulate_stripe_timeout,
                simulate_stripe_timeout=simulate_stripe_timeout,
            )
        durable_request = _ask_for_refund(
            console,
            await _durable_frame(client, workflow_id, setup=setup),
            "Ask for a refund (order 1234, the python plushy)",
        )
        request = _durable_request(
            workflow_id=workflow_id,
            payment_intent=payment_intent,
            amount_cents=amount_cents,
            reason=durable_request,
            real=real,
            real_model=real_model,
            model_provider=selected_provider,
        )
        handle = await client.start_workflow(
            RefundWorkflow.run,
            request,
            id=workflow_id,
            task_queue=queue,
        )
        # The runner sends the Demo 1 answers in as Signals; say so on screen.
        with console.status(
            "Reusing your Demo 1 answers so you don't type them twice..."
        ):
            outcome, durable_steps = await _drive_temporal_loop(
                handle,
                customer_answers,
            )
        if outcome == "completed":
            raise RuntimeError(
                "the refund finished before the stage could stop the Worker"
            )
        if outcome == "denied":
            await handle.result()
            completed = True
            _show(
                console,
                await _denied_frame(client, workflow_id, setup=setup),
                "The model denied this request. Press Enter to exit",
            )
            if real:
                console.print(
                    "A Stripe test payment was created but not refunded. "
                    "Reconcile it with: uv run refund-demo cleanup",
                    style="bold yellow",
                )
            return
        if simulate_stripe_timeout:
            # Failure drill with no pause: wait until attempt 1 hangs on the
            # simulated Stripe timeout, then kill the Worker mid-Activity.
            with console.status(
                "Attempt 1 is waiting on Stripe; killing the Worker..."
            ):
                await _wait_for_refund_hold(timeout=_DEMO_TIMEOUT_SECONDS)
                services.kill_worker()
                # Wait out the heartbeat timeout, so Temporal has already timed
                # out attempt 1 when the next frame draws.
                await asyncio.sleep(_SIMULATED_RETRY_DETECTION_SECONDS)
            _show(
                console,
                await _durable_frame(
                    client,
                    workflow_id,
                    loop_steps=durable_steps,
                    loop_from_history=True,
                    setup=setup,
                ),
                "Stripe did not respond on attempt 1. Temporal is waiting at "
                "attempt 2. Press Enter to start a new Worker",
            )
            with console.status("New Worker is running attempt 2..."):
                # A plain Worker, with no hooks, picks up attempt 2.
                await services.start_worker()
                result = await asyncio.wait_for(
                    handle.result(), timeout=_DEMO_TIMEOUT_SECONDS
                )
        else:
            _show(
                console,
                await _durable_frame(
                    client,
                    workflow_id,
                    loop_steps=durable_steps,
                    setup=setup,
                ),
                "Press Enter to submit the refund",
            )

            # The Worker hook is holding the refund before Stripe, so this kill
            # lands at the same point on every run: after the agent chose the
            # refund, before the refund reaches Stripe.
            services.kill_worker()
            await asyncio.sleep(0.25)
            # No Worker can answer a Query now, so read the saved loop from
            # Temporal's history instead of reusing the counts from before.
            _show(
                console,
                await _durable_frame(
                    client,
                    workflow_id,
                    loop_steps=durable_steps,
                    loop_from_history=True,
                    setup=setup,
                    stopped_before_refund=True,
                ),
                "Press Enter to start a new Worker",
            )

        if simulate_stripe_retry:
            # Failure drill after the pause: a new Worker runs the next refund
            # attempt, and its restart window (demo_hooks.py) holds that attempt
            # open after Stripe accepts it. The stage kills that Worker before
            # Temporal records the result.
            with console.status(
                "Calling Stripe, then killing the Worker before Temporal records it..."
            ):
                await services.start_worker(
                    restart_window_seconds=_SIMULATED_RETRY_WINDOW_SECONDS
                )
                await _wait_for_first_refund(
                    workflow_id,
                    timeout=_DEMO_TIMEOUT_SECONDS,
                )
                services.kill_worker()
                # Wait out the heartbeat timeout, as in the timeout drill.
                await asyncio.sleep(_SIMULATED_RETRY_DETECTION_SECONDS)
            _show(
                console,
                await _durable_frame(
                    client,
                    workflow_id,
                    loop_steps=durable_steps,
                    setup=setup,
                ),
                "Stripe accepted the refund, but the Worker disappeared before "
                "reporting it. Press Enter to start a new Worker",
            )
            with console.status(
                "New Worker is retrying with the same idempotency key..."
            ):
                # Attempt 2 reuses the idempotency key, so Stripe returns the
                # first refund instead of creating a second one.
                await services.start_worker()
                result = await asyncio.wait_for(
                    handle.result(), timeout=_DEMO_TIMEOUT_SECONDS
                )
        elif not simulate_stripe_timeout:
            with console.status(
                "Starting a new Worker. It picks up from Temporal's history..."
            ):
                # A plain Worker, with no hooks. Temporal times out the killed
                # attempt (its heartbeats stopped) and hands the next attempt to
                # this Worker, which replays history and runs the refund.
                await services.start_worker()
                result = await asyncio.wait_for(
                    handle.result(), timeout=_DEMO_TIMEOUT_SECONDS
                )
        if result.status == "denied":
            completed = True
            _show(
                console,
                await _denied_frame(client, workflow_id, setup=setup),
                "The model denied this request. Press Enter to exit",
            )
            if real:
                console.print(
                    "A Stripe test payment was created but not refunded. "
                    "Reconcile it with: uv run refund-demo cleanup",
                    style="bold yellow",
                )
            return
        completed = True
        _show(
            console,
            await _durable_frame(
                client,
                workflow_id,
                recovered=True,
                refund_status=result.status,
                loop_steps=durable_steps,
                setup=setup,
            ),
            "Press Enter for the takeaway",
        )
        console.clear()
        console.print(_closing(result.status))
        console.print(f"\nStage logs: {_display_path(stage_state)}", style="dim")
    finally:
        _stop_naive_process(naive_process)
        if handle is not None and not completed:
            try:
                await handle.terminate(reason="single-window stage runner closed")
            except Exception:
                pass
        services.close()
        if previous_state_dir is None:
            os.environ.pop("DEMO_STATE_DIR", None)
        else:
            os.environ["DEMO_STATE_DIR"] = previous_state_dir
