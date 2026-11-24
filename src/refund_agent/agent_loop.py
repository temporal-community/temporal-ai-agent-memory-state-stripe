"""The agent loop both demos run: observe, reason, act, until the agent decides.

Demo 1 (naive_refund.py) runs it in a plain process with direct calls and
stdin. Demo 2 (workflow.py) runs it inside the RefundApprovalAgent Workflow,
where each injected call is an Activity or a Signal wait. The loop itself does
no I/O and reads no clock or randomness, so it is safe in Workflow code and
replays exactly: everything non-deterministic happens behind the callables.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from temporalio.exceptions import ApplicationError

from refund_agent.activities.tools import (
    TOOL_HISTORY,
    TOOL_MEMORY,
    TOOL_ORDER,
    TOOL_POLICY,
)
from refund_agent.models import AgentStep, RefundDecision, RefundRequest

# The loop is bounded so a model that never decides fails loudly instead of
# looping forever.
MAX_TURNS = 10

# The only questions the agent may put to the customer (the ask_customer enum).
QUESTION_IDS = ("item_opened", "damage")
# The tools the agent may call: three record lookups and its long-term memory.
LOOKUP_TOOLS = (TOOL_ORDER, TOOL_HISTORY, TOOL_POLICY, TOOL_MEMORY)

Decide = Callable[[RefundRequest, list[dict]], Awaitable[AgentStep]]
UseTool = Callable[[str], Awaitable[dict]]
AskCustomer = Callable[[AgentStep], Awaitable[str]]


def agent_step_entry(step: AgentStep) -> dict:
    """The working-memory entry for one model turn, so later turns see it."""

    entry: dict = {"tool": "agent_step", "action": step.action}
    if step.action == "use_tool":
        entry["tool_chosen"] = step.tool
    elif step.action == "ask_customer":
        entry["question_id"] = step.question_id
    elif step.action == "decide":
        entry["recommendation"] = step.recommendation
    entry["rationale"] = step.rationale or ""
    return entry


async def run_agent_loop(
    request: RefundRequest,
    working_memory: list[dict],
    *,
    decide: Decide,
    use_tool: UseTool,
    ask_customer: AskCustomer,
) -> RefundDecision:
    """Run turns until the agent decides, appending each turn to working_memory.

    working_memory is updated in place, so the caller can show it while the
    loop runs. It gets one entry per model turn, then the customer's answer or
    the tool result that turn produced.
    """

    answers: dict[str, str] = {}
    for _turn in range(MAX_TURNS):
        step = await decide(request, working_memory)
        working_memory.append(agent_step_entry(step))
        if step.action == "decide":
            return RefundDecision(
                recommendation=step.recommendation or "escalate",
                rationale=step.rationale or "",
                source=step.source,
            )
        if step.action == "ask_customer":
            question_id = step.question_id or ""
            if question_id not in QUESTION_IDS:
                raise ApplicationError(
                    f"agent asked an unsupported question: {question_id}",
                    type="UnknownCustomerQuestion",
                    non_retryable=True,
                )
            # A repeated question reuses the earlier answer, so the customer is
            # never asked the same thing twice.
            if question_id not in answers:
                answers[question_id] = await ask_customer(step)
            # The agent's question goes in with the answer, so the next turn
            # sees what it asked as well as what it heard.
            working_memory.append(
                {
                    "tool": "customer_answer",
                    "question": step.question or question_id,
                    "result": {
                        "question_id": question_id,
                        "answer": answers[question_id],
                    },
                }
            )
            continue
        tool = step.tool or ""
        if tool not in LOOKUP_TOOLS:
            raise ApplicationError(
                f"agent asked for an unknown tool: {step.tool}",
                type="UnknownTool",
                non_retryable=True,
            )
        # A tool the agent chose. Its result becomes part of what it knows.
        working_memory.append({"tool": tool, "result": await use_tool(tool)})
    raise ApplicationError(
        "agent did not reach a decision within the turn budget",
        type="AgentLoopExhausted",
        non_retryable=True,
    )
