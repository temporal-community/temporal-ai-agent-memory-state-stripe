"""Demo 1: an autonomous loop without durable execution state.

The agent asks questions, observes answers, performs lookups, and chooses its
next action. Its agent process then disappears before calling the refund
system. Its process-local working memory disappears too. Stripe correctly
retains the paid charge with no refund, but it does not own the interrupted
agent loop.

It runs the same agent loop as Demo 2 (agent_loop.py), in this process with no
Temporal: direct calls instead of Activities and stdin instead of Signals.
Each turn uses the fixed offline policy by default, or the same live model step
with --real-model. A refund goes through the same refund_payment call as Demo
2's issue_refund Activity.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import stripe

from refund_agent.activities import (
    TOOL_HISTORY,
    TOOL_MEMORY,
    TOOL_ORDER,
    TOOL_POLICY,
    USAGE_AGENT_NAIVE,
    _selected_model_provider,
    check_refund_policy,
    fixed_policy_step,
    lookup_customer_history,
    lookup_order,
    model_next_step,
    refund_payment,
    search_memory,
)
from refund_agent.activities.tools import MEMORY_LABEL, past_cases_summary
from refund_agent.agent_loop import run_agent_loop
from refund_agent.fake_stripe import (
    forget_refunds,
    idempotency_key_for,
    refunds_for_payment_intent,
)
from refund_agent.models import OFFLINE_POLICY, AgentStep, RefundDecision, RefundRequest
from refund_agent.settings import load_env_file, validate_stripe_key

# Color only when writing to a real terminal, so piped output stays clean.
_COLOR = sys.stdout.isatty()
_CODES = {"bold": "1", "dim": "2", "system": "32", "danger": "31"}


# The demo customer, the same one Demo 2 serves.
_NAIVE_CUSTOMER_ID = "cus_demo_42"


def paint(text: str, name: str) -> str:
    if not _COLOR:
        return text
    return f"\033[{_CODES[name]}m{text}\033[0m"


def _line(label: str, text: str) -> None:
    print(f"{label:<15}| {text}", flush=True)


def _loop_event(payload: dict[str, str]) -> None:
    """Emit a machine-readable step while keeping the subprocess real."""

    _line("AGENT STEP", json.dumps(payload, sort_keys=True))


def _money(cents: int) -> str:
    return f"${cents / 100:.2f}"


def _state_dir() -> Path:
    return Path(os.getenv("DEMO_STATE_DIR", ".demo-state"))


def _done_path(order: str) -> Path:
    return _state_dir() / f"naive-done-{order}.json"


def _read_real_stripe_state(
    payment_intent_id: str,
) -> tuple[str, list[dict[str, object]]]:
    """Read the payment and its refunds from Stripe without creating a refund."""

    stripe.api_key = validate_stripe_key(os.getenv("STRIPE_API_KEY"), required=True)
    stripe.max_network_retries = 0
    intent = stripe.PaymentIntent.retrieve(payment_intent_id)
    refunds = stripe.Refund.list(payment_intent=payment_intent_id, limit=10)
    intent_status = str(intent.status).lower()
    payment_status = "PAID" if intent_status == "succeeded" else intent_status.upper()
    ledger = [
        {
            "refund_id": str(refund.id),
            "order": "1234",
            "amount": int(refund.amount),
            "status": str(refund.status).lower(),
        }
        for refund in refunds.data
    ]
    return payment_status, ledger


def _offline_refunds(payment_intent_id: str) -> list[dict[str, object]]:
    """Refunds the offline ledger (the Stripe stand-in) holds for this payment."""

    return [
        {
            "refund_id": str(refund["refund_id"]),
            "order": "1234",
            "amount": int(refund["amount_cents"]),
            "status": str(refund.get("status", "succeeded")).lower(),
        }
        for refund in refunds_for_payment_intent(payment_intent_id)
    ]


def _naive_request(args: argparse.Namespace) -> RefundRequest:
    """The request Demo 2's Workflow gets, held only in this process."""

    request = RefundRequest(
        request_id=f"naive-{uuid.uuid4().hex[:8]}",
        order_id=f"order-{args.order}",
        customer_id=_NAIVE_CUSTOMER_ID,
        payment_intent_id=args.payment_intent,
        amount_cents=args.amount_cents,
        reason=args.reason,
        dry_run=not args.real,
        # No answers up front, so the agent may ask for them.
        item_opened=None,
        damage=None,
        # Offline is chosen explicitly; --real-model never falls back to it.
        model_provider=args.model_provider if args.real_model else OFFLINE_POLICY,
    )
    if args.real_model and _selected_model_provider(request) is None:
        raise RuntimeError("--real-model requires ANTHROPIC_API_KEY or OPENAI_API_KEY")
    return request


def _lookup(tool: str, request: RefundRequest) -> dict:
    # The same store and dispatch as RefundWorkflow._use_tool, run in this
    # process: no Activity, so nothing is recorded outside it.
    if tool == TOOL_ORDER:
        return asdict(lookup_order(request.order_id))
    if tool == TOOL_HISTORY:
        return asdict(lookup_customer_history(request.customer_id))
    if tool == TOOL_POLICY:
        return asdict(check_refund_policy(request.order_id))
    if tool == TOOL_MEMORY:
        return asdict(search_memory(request.customer_id))
    raise RuntimeError(f"agent asked for an unknown tool: {tool}")


def _tool_event(tool: str, result: dict) -> dict[str, str]:
    # The same labels Demo 2 shows for its recorded lookups.
    if tool == TOOL_ORDER:
        label, shown = "Order", str(result.get("item") or "python plushy")
    elif tool == TOOL_HISTORY:
        label, shown = "Refund history", "clean"
    elif tool == TOOL_MEMORY:
        label, shown = MEMORY_LABEL, past_cases_summary(result)
    else:
        label = "Refund policy"
        shown = "eligible" if result.get("eligible_for_refund") else "not eligible"
    return {"kind": "tool", "tool": tool, "label": label, "result": shown}


def _run_interactive_agent_loop(request: RefundRequest) -> RefundDecision:
    """Run Demo 2's loop in this process until the agent decides.

    The fixed policy or the live model asks the questions it needs, picks the
    lookups, and decides. The answers, observations, and next action live only
    in this process.
    """

    async def decide(request: RefundRequest, working_memory: list[dict]) -> AgentStep:
        # The offline policy is an explicit choice in the request, as in the
        # agent_decide_next_step Activity.
        if request.model_provider == OFFLINE_POLICY:
            return fixed_policy_step(request, working_memory)
        return model_next_step(request, working_memory, agent=USAGE_AGENT_NAIVE)

    async def use_tool(tool: str) -> dict:
        result = _lookup(tool, request)
        _loop_event(_tool_event(tool, result))
        return result

    async def ask_customer(step: AgentStep) -> str:
        question_id = step.question_id or ""
        question = step.question or question_id
        suggested_answer = step.suggested_answer or ""
        _loop_event(
            {
                "kind": "question",
                "question_id": question_id,
                "question": question,
                "suggested_answer": suggested_answer,
            }
        )
        line = sys.stdin.readline()
        if not line:
            raise RuntimeError("customer input closed during the agent loop")
        answer = line.strip() or suggested_answer
        _loop_event(
            {
                "kind": "answer",
                "question_id": question_id,
                "question": question,
                "result": answer,
            }
        )
        return answer

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
    recommendation = decision.recommendation
    _line("MODEL REASONING", f"decide -> {recommendation} ({decision.rationale})")
    # Demo 2's stage runner approves an escalation, so this loop treats one the
    # same way; only a denial leaves no refund to issue.
    _loop_event(
        {
            "kind": "ready",
            "label": "Next action",
            "result": "no refund" if recommendation == "deny" else "issue refund",
            "recommendation": recommendation,
        }
    )
    return decision


def _replacement_status(args: argparse.Namespace) -> None:
    """Run the replacement agent's Stripe check in a new process."""

    _state_dir().mkdir(parents=True, exist_ok=True)
    question = sys.stdin.readline()
    if not question:
        raise RuntimeError("customer input closed before the replacement status check")

    try:
        if args.real:
            payment_status, refunds = _read_real_stripe_state(args.payment_intent)
        else:
            payment_status = "PAID"
            refunds = _offline_refunds(args.payment_intent)
    except stripe.StripeError as error:
        message = getattr(error, "user_message", None) or str(error)
        _line("AGENT ERROR", json.dumps({"message": message}))
        return

    agent = {
        "context": {
            "order": args.order,
            "amount": args.amount_cents,
            "customer": "Nyghtowl",
        },
        "_status_checked": True,
        "_refund_missing": not refunds,
        # The same search_memory read Demo 2's agent uses. The old process
        # died before the refund, so it never saved a case note.
        "_past_cases": len(search_memory(_NAIVE_CUSTOMER_ID).past_cases),
        "_payment_status": payment_status,
        "_new_process": True,
        "_process_pid": os.getpid(),
        "note": "replacement process checked Stripe after the customer asked",
        "user_message": question.strip(),
    }
    _line(
        "AGENT RESULT",
        json.dumps({"agent": agent, "refunds": refunds}, sort_keys=True),
    )

    if args.hold_after_status:
        while True:
            time.sleep(60)


def _refund(args: argparse.Namespace) -> None:
    order = args.order
    amount = args.amount_cents
    _state_dir().mkdir(parents=True, exist_ok=True)

    print(paint("=" * 64, "dim"))
    _line("CONTEXT", f"order {order}, {_money(amount)}, customer {_NAIVE_CUSTOMER_ID}")

    # The completion marker is separate from the refund. It can be durable after
    # it is written and still cannot close the crash gap before that write.
    if _done_path(order).exists():
        _line("PROGRESS RECORD", "completion marker found; already refunded, done")
        return
    _line(
        "PROGRESS RECORD",
        "no marker; cannot infer whether the refund reached Stripe",
    )

    request = _naive_request(args)
    # Manual reconciliation: ask Stripe, the source of truth, whether this
    # payment already has a refund. It is a read, never a refund call.
    if args.real:
        _, existing = _read_real_stripe_state(args.payment_intent)
    else:
        existing = _offline_refunds(args.payment_intent)
    if existing:
        _line(
            "STRIPE CHECK",
            f"refund system confirms {existing[0]['refund_id']} already exists",
        )
        _line(
            "RECOVERY",
            "result reconstructed because this new process checked",
        )
        return

    decision: RefundDecision | None = None
    if args.interactive_loop:
        try:
            decision = _run_interactive_agent_loop(request)
        except Exception as error:
            # One readable line for the stage instead of a traceback it ignores.
            _line("AGENT ERROR", json.dumps({"message": str(error)}))
            sys.stdout.flush()
            sys.exit(1)
    else:
        _line(
            "RETURN FORM",
            f"opened={args.opened}; damage={args.damage}; refund_to={args.refund_to}",
        )
    if decision is not None and decision.recommendation == "deny":
        # Nothing to hold before: a denial issues no refund.
        _line("THE SYSTEM", f"no refund issued for order {order}")
        return
    if args.hold_before_refund:
        # Demo-only pause, at the same point as Demo 2's: hold before calling
        # Stripe so the stage can kill this process there every run.
        _line(
            "REQUEST BUFFER",
            "next action held by this agent process; Stripe not called",
        )
        sys.stdout.flush()
        while True:
            time.sleep(60)

    if decision is None:
        _line("MODEL REASONING", "approve (amount within policy, clean history)")
    # The same refund call as Demo 2's issue_refund Activity. With no Workflow
    # run to derive it from, the idempotency key comes from this process's
    # request id, so a new process would send a new key: the reason for the
    # Stripe check above.
    stripe_refund, _mode = refund_payment(
        request,
        workflow_id=request.request_id,
        idempotency_key=idempotency_key_for(request.request_id),
    )
    refund_id = str(stripe_refund["refund_id"])
    status = str(stripe_refund.get("status") or "unknown").lower()
    _line("THE SYSTEM", f"issued refund {refund_id} for order {order}: {status}")
    # An accepted call is not a completed refund. Only Stripe's `succeeded`
    # counts as done; anything else (pending, failed) is shown as-is and no
    # completion marker is written.
    if status != "succeeded":
        _line(
            "PROGRESS RECORD",
            f"refund {status}, not complete; no completion recorded",
        )
        return

    if args.hold_after_refund:
        _line(
            "PROGRESS RECORD",
            "refund succeeded; waiting before recording completion",
        )
        sys.stdout.flush()
        while True:
            time.sleep(60)

    if args.exit_after_refund:
        _line(
            "PROGRESS RECORD",
            paint("process exits before recording completion", "danger"),
        )
        sys.stdout.flush()
        os._exit(1)

    _done_path(order).write_text(
        json.dumps({"order_id": order, "refund_id": refund_id}) + "\n",
        encoding="utf-8",
    )
    _line("PROGRESS RECORD", "recorded completion in a separate write")


def _ledger(args: argparse.Namespace) -> None:
    # Demo 1's refunds live in the same offline ledger as Demo 2's; its
    # Workflow-ID field holds the naive request id instead.
    refunds = [
        refund
        for refund in refunds_for_payment_intent(args.payment_intent)
        if str(refund.get("workflow_id", "")).startswith("naive-")
    ]
    print(paint(f"offline ledger, Demo 1 refunds: {len(refunds)}", "bold"))
    for refund in refunds:
        detail = f"{refund['refund_id']}  {_money(int(refund['amount_cents']))}"
        _line("THE SYSTEM", detail)


def _reset(_args: argparse.Namespace) -> None:
    removed = forget_refunds("naive-")
    for path in _state_dir().glob("naive-done-*.json"):
        path.unlink(missing_ok=True)
    print(
        paint(
            f"cleared {removed} Demo 1 refund(s) from the offline ledger and the "
            "completion records",
            "dim",
        )
    )


# The answers the interactive view's customer gives when the agent asks.
_INTERACTIVE_ANSWERS = {"item_opened": "Yes", "damage": "Split seam"}


def _offline_loop_steps(order: str, amount_cents: int) -> list[dict[str, str]]:
    """Run the shared loop with the fixed offline policy and return its steps.

    The same loop and lookups as the stage, so this view shows what the agent
    actually asked and looked up, not a fixed list.
    """

    request = RefundRequest(
        request_id=f"naive-{uuid.uuid4().hex[:8]}",
        order_id=f"order-{order}",
        customer_id=_NAIVE_CUSTOMER_ID,
        payment_intent_id="pi_dry_run_demo",
        amount_cents=amount_cents,
        reason=f"Please refund order {order}.",
        dry_run=True,
        item_opened=None,
        damage=None,
        model_provider=OFFLINE_POLICY,
    )
    steps: list[dict[str, str]] = []

    async def decide(request: RefundRequest, working_memory: list[dict]) -> AgentStep:
        return fixed_policy_step(request, working_memory)

    async def use_tool(tool: str) -> dict:
        result = _lookup(tool, request)
        steps.append(_tool_event(tool, result))
        return result

    async def ask_customer(step: AgentStep) -> str:
        question_id = step.question_id or ""
        answer = _INTERACTIVE_ANSWERS.get(question_id, step.suggested_answer or "")
        steps.append({"kind": "answer", "question_id": question_id, "result": answer})
        return answer

    decision = asyncio.run(
        run_agent_loop(
            request, [], decide=decide, use_tool=use_tool, ask_customer=ask_customer
        )
    )
    next_action = "no refund" if decision.recommendation == "deny" else "issue refund"
    steps.append({"kind": "ready", "label": "Next action", "result": next_action})
    return steps


def _process_interactive(
    agent: dict, ledger: list, user_message: str = "Please refund order 1234"
) -> None:
    # This interactive fixture keeps progress in process to make the loss
    # visible. The scripted path above demonstrates the broader crash gap between
    # the refund and a separate completion write.
    order, amount, customer = "1234", 8000, "42"
    agent["user_message"] = user_message
    agent["context"] = {"order": order, "amount": amount, "customer": customer}
    normalized = user_message.lower()
    if any(word in normalized for word in ("did i", "what happened", "status")):
        agent["_status_checked"] = True
        agent["_refund_missing"] = not any(item["order"] == order for item in ledger)
        agent["note"] = "checked Stripe after a new customer message"
        return
    agent["_loop_steps"] = _offline_loop_steps(order, amount)
    agent["note"] = "agent chose the refund; loop position remains in this process"


def _demo_frame(
    agent: dict,
    ledger: list,
    *,
    stage_mode: bool = False,
    setup=None,
):
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    from refund_agent.tui import OFFLINE_SETUP, _compact_columns, _demo_header

    setup = setup or OFFLINE_SETUP
    refund_status = (
        str(ledger[-1].get("status", "succeeded")).lower() if ledger else None
    )

    # General-audience copy calls this side the "agent process", never a Worker:
    # it is not a Temporal Worker.
    left = Text()
    if "context" not in agent:
        if agent.get("_process_gone"):
            left.append("PROCESS GONE\n\n", style="bold red")
            left.append(
                "It held the answers and next step.\n\nThey are gone with it.\n\n",
                style="red",
            )
            # The presenter's cue says "submit", so the screen says who stopped it.
            left.append(
                "The demo stops the process here,\nbefore the refund reaches Stripe.",
                style="dim",
            )
        else:
            left.append("Welcome back, Nyghtowl\n\n", style="bold cyan")
            if agent.get("_restarted"):
                left.append("No answers. No next step.", style="yellow")
            else:
                left.append("How can I help?", style="dim")
    elif agent.get("_status_checked"):
        context = agent.get("context") or {}
        amount = (context.get("amount") or 0) / 100
        left.append("YOU\n", style="bold yellow")
        left.append(f"  {agent.get('user_message', 'What happened to my refund?')}\n\n")
        left.append("AGENT\n", style="bold cyan")
        if "_past_cases" in agent:
            count = int(agent["_past_cases"])
            if count == 0:
                left.append("  ✓ Past cases (memory): none\n", style="green")
                left.append("    nothing saved; the process died first\n", style="dim")
            else:
                left.append(f"  ✓ Past cases (memory): {count}\n", style="green")
        left.append("  Let me check Stripe.\n")
        if agent.get("_refund_missing", not ledger):
            left.append("  No refund request reached Stripe.\n", style="bold")
            left.append("  I lost your return answers.\n", style="yellow")
            left.append("  Please start the return again.\n", style="yellow")
        elif refund_status == "succeeded":
            left.append(f"  Yes — your ${amount:.2f} refund succeeded.\n", style="bold")
        else:
            left.append("  Stripe has a refund record.\n", style="bold")
            left.append(
                f"  Current status: {(refund_status or 'unknown').upper()}.\n",
                style="yellow",
            )
            left.append("  It is not confirmed complete.\n", style="yellow")
    else:
        from refund_agent.tui import _append_loop_steps

        context = agent.get("context") or {}
        amount = (context.get("amount") or 0) / 100
        left.append("YOU\n", style="bold yellow")
        left.append(f"  {agent.get('user_message', 'Please refund order 1234')}\n\n")
        if agent.get("_loop_steps") or agent.get("_pending_question"):
            _append_loop_steps(
                left,
                agent.get("_loop_steps") or [],
                pending_question=agent.get("_pending_question"),
            )
        else:
            left.append(f"  Order {context.get('order')}: ${amount:.2f}\n")

    right = Text()
    right.append("LAST ORDER\n", style="bold")
    right.append("  Python plushy · $80.00\n\n")
    right.append(f"{setup.ledger_heading}\n", style="bold green")
    right.append(f"  Payment: {agent.get('_payment_status', 'PAID')}\n")
    if not ledger:
        right.append("  Refund: none\n", style="dim")
    else:
        right.append(
            f"  Refund: {(refund_status or 'unknown').upper()}\n",
            style="green" if refund_status == "succeeded" else "yellow",
        )
    for number, entry in enumerate(ledger, start=1):
        amount = entry["amount"] / 100
        right.append(f"  Refund {number}: order {entry['order']}, ${amount:.2f}\n")
        status = str(entry.get("status", "succeeded")).lower()
        right.append(f"  Status: {status}\n")

    header = Panel(
        _demo_header(
            "Demo 1: Without Temporal, the agent loses its place",
            "Its answers and next step live only inside this agent process.",
            setup.naive_line,
        ),
        border_style="cyan",
    )
    # The left pane says what is gone; this panel says what is left. Stripe's
    # record is correct, and it never received or lost a refund request here.
    if agent.get("_process_gone"):
        explanation = (
            "Only Stripe's record: PAID, no refund.\n"
            "That is correct. Stripe was never called."
        )
        explanation_title = "WHAT'S LEFT"
    elif agent.get("_status_checked") and not ledger:
        explanation = (
            "Stripe's record is right: paid, no refund.\n"
            "But Stripe never had the answers or the next step."
        )
        explanation_title = "THE CUSTOMER STARTS OVER"
    elif any(step.get("kind") == "ready" for step in agent.get("_loop_steps") or []):
        explanation = (
            "Next step: submit the refund.\n"
            "The answers and next step exist only inside this process."
        )
        explanation_title = "WORK NOT SAVED"
    elif agent.get("_restarted"):
        explanation = "It can still read Stripe.\nIt cannot see the old answers."
        explanation_title = "FRESH START"
    else:
        explanation = "What if this agent process disappears right before the refund?"
        explanation_title = "THE QUESTION"
    why = Panel(
        explanation,
        title=explanation_title,
        border_style="yellow",
    )
    if agent.get("_process_gone"):
        agent_panel_title = "AGENT PROCESS"
        agent_border_style = "red"
    elif agent.get("_new_process") or agent.get("_restarted"):
        agent_panel_title = "NEW AGENT PROCESS"
        agent_border_style = "cyan"
    else:
        agent_panel_title = "AGENT PROCESS"
        agent_border_style = "cyan"
    panes = _compact_columns(
        Panel(
            left,
            title=agent_panel_title,
            border_style=agent_border_style,
        ),
        Panel(
            right,
            title="WHAT SURVIVES",
            border_style="green",
        ),
    )
    if stage_mode:
        # On stage the input prompt is the only cue, so nothing can disagree.
        return Group(header, panes, why)
    footer = Panel(
        "Type a refund request    restart / deploy / OOM    reset    quit",
        border_style="dim",
    )
    return Group(header, panes, why, footer)


def _run_interactive() -> None:
    try:
        from rich.console import Console
    except ModuleNotFoundError:
        print("The interactive view needs rich. Install it with: uv sync --extra tui")
        return
    console = Console()
    agent: dict = {}
    ledger: list = []
    while True:
        console.clear()
        console.print(_demo_frame(agent, ledger))
        try:
            command = input("you>  ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if command in ("quit", "q", "exit"):
            break
        if command in (
            "agent restart",
            "agent deploy",
            "agent oom",
            "restart",
            "deploy",
            "oom",
            "crash",
        ):
            # The agent process goes away: a restart, a deploy, an eviction, an
            # OOM kill. Its in-memory state is gone, including whether it already
            # refunded. A crash is just the blunt version.
            agent.clear()
            agent["_restarted"] = True
        elif command == "reset":
            agent.clear()
            ledger.clear()
        elif command in ("", "go") or "refund" in command:
            _process_interactive(agent, ledger, command or "Please refund order 1234")


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser(
        prog="naive-refund",
        description="A refund agent with no durable execution (Demo 1)",
    )
    commands = parser.add_subparsers(dest="command")

    refund = commands.add_parser("refund", help="process one refund")
    refund.add_argument("--order", default="1234")
    refund.add_argument("--amount-cents", type=int, default=8000)
    refund.add_argument("--opened", default="Yes")
    refund.add_argument("--damage", default="Split seam")
    refund.add_argument("--refund-to", default="Original card")
    refund.add_argument(
        "--interactive-loop",
        action="store_true",
        help="run the agent loop: ask each return question, then let the agent "
        "choose the lookups and the decision",
    )
    refund.add_argument("--reason", default="Please refund order 1234.")
    refund.add_argument("--payment-intent", default="pi_dry_run_demo")
    refund.add_argument(
        "--real",
        action="store_true",
        help="use Stripe test mode, as Demo 2 does; the stage stops this process "
        "before any refund call",
    )
    refund.add_argument(
        "--real-model",
        action="store_true",
        help="decide with the live model instead of the fixed policy",
    )
    refund.add_argument(
        "--model-provider",
        choices=("anthropic", "openai"),
        help="live provider for --real-model",
    )
    boundary = refund.add_mutually_exclusive_group()
    boundary.add_argument(
        "--hold-before-refund",
        action="store_true",
        help="wait after choosing the refund but before calling the refund system",
    )
    boundary.add_argument(
        "--exit-after-refund",
        action="store_true",
        help="exit right after refunding, before recording completion",
    )
    boundary.add_argument(
        "--hold-after-refund",
        action="store_true",
        help="wait after refunding so the process can be replaced at the boundary",
    )

    ledger = commands.add_parser(
        "ledger", help="show Demo 1's refunds in the offline ledger"
    )
    ledger.add_argument("--payment-intent", default="pi_dry_run_demo")

    commands.add_parser(
        "reset", help="clear Demo 1's offline refunds and completion records"
    )

    replacement = commands.add_parser(
        "replacement-status",
        help="check Stripe for the refund from a new Demo 1 agent process",
    )
    replacement.add_argument("--order", default="1234")
    replacement.add_argument("--amount-cents", type=int, default=8000)
    replacement.add_argument("--payment-intent", default="pi_dry_run_demo")
    replacement.add_argument("--real", action="store_true")
    replacement.add_argument("--hold-after-status", action="store_true")

    args = parser.parse_args()
    if args.command == "refund":
        _refund(args)
    elif args.command == "ledger":
        _ledger(args)
    elif args.command == "reset":
        _reset(args)
    elif args.command == "replacement-status":
        _replacement_status(args)
    else:
        # No subcommand: launch the interactive two-pane demo.
        _run_interactive()


if __name__ == "__main__":
    main()
