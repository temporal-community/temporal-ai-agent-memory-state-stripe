"""Deterministic orchestration for the refund agent loop.

The loop is the agent: each turn the model (agent_decide_next_step) looks at
what it knows, plans, and either asks the customer, calls a tool, or decides.
The loop itself lives in agent_loop.py and is shared with Demo 1. Here each
model turn and lookup is an Activity and each customer answer is a Signal, so
Temporal records every completed step in Event History and a new Worker
resumes the loop exactly where the old one stopped.
"""

import asyncio
from dataclasses import asdict
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from refund_agent.activities import (
        agent_decide_next_step,
        check_refund_policy,
        issue_refund,
        lookup_customer_history,
        lookup_order,
        remember_case,
        search_memory,
    )
    from refund_agent.agent_loop import run_agent_loop
    from refund_agent.models import (
        AgentStep,
        CaseMemory,
        CustomerHistory,
        OrderDetails,
        RefundDecision,
        RefundRequest,
        RefundResult,
        ReturnStatus,
    )

_MODEL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=20),
    maximum_attempts=5,
)

# issue_refund timeouts.
# Heartbeat timeout is the Worker-loss detection window: issue_refund heartbeats
# every second while it waits on Stripe, so five seconds of silence means the
# Worker is gone and Temporal starts the next attempt on another Worker.
REFUND_HEARTBEAT_TIMEOUT = timedelta(seconds=5)
# Start-to-close bounds one attempt. It stays well above the Stripe client
# timeout (STRIPE_TIMEOUT_SECONDS), so Stripe's own timeout reports a hung call
# first; a lost Worker is caught much sooner by the heartbeat timeout.
REFUND_START_TO_CLOSE_TIMEOUT = timedelta(minutes=5)
# Schedule-to-close is the total recovery window: how long the refund may keep
# retrying, Worker restarts included, before Temporal fails the run.
REFUND_SCHEDULE_TO_CLOSE_TIMEOUT = timedelta(minutes=30)


@workflow.defn(name="RefundApprovalAgent")
class RefundWorkflow:
    """The refund agent loop expressed as durable orchestration.

    The Workflow Type shown in the Temporal UI is RefundApprovalAgent; the
    Python class keeps its name so the rest of the code is unchanged.
    """

    def __init__(self) -> None:
        # EXECUTION STATE: Temporal restores all of this by replay after a restart.
        self.approved = False
        self.approval_note = ""
        self.working_memory: list[dict] = []
        self.customer_answers: dict[str, str] = {}
        self._phase = "starting"
        self._pending_question: dict[str, str] | None = None
        self._request: RefundRequest | None = None
        self._decision: RefundDecision | None = None
        self._turn = 0

    @workflow.run
    async def run(self, request: RefundRequest) -> RefundResult:
        self._request = request
        self._phase = "deciding"
        workflow.logger.info("EXECUTION STATE | phase=agent_loop_start")

        # CANCELLATION before the refund: nothing has reached Stripe yet, so
        # there is nothing to undo. Record it and let the Workflow close as
        # Cancelled; Temporal cancels any model call or wait in flight.
        try:
            # OBSERVE, REASON, ACT: the shared loop runs until the agent decides.
            decision = await run_agent_loop(
                request,
                self.working_memory,
                decide=self._decide,
                use_tool=self._use_tool,
                ask_customer=self._ask_customer,
            )
            self._decision = decision
            workflow.logger.info(
                "EXECUTION STATE | phase=decided "
                f"recommendation={decision.recommendation}"
            )

            if decision.recommendation == "deny":
                self._phase = "denied"
                return RefundResult(
                    refund_id="none",
                    status="denied",
                    amount_cents=request.amount_cents,
                    idempotency_key="none",
                    activity_attempt=0,
                    mode="denied",
                )

            if not decision.recommendation == "approve":
                # Escalate. Treat anything that is not an explicit approve as needing
                # a human, so the irreversible refund never runs without a gate.
                workflow.logger.info(
                    "EXECUTION STATE | phase=waiting_for_approval | DURABLE WAIT"
                )
                self._phase = "waiting_for_approval"
                # DURABLE WAIT: no Worker process has to remain alive here.
                await workflow.wait_condition(lambda: self.approved)
                workflow.logger.info(
                    f"EXECUTION STATE | phase=approved note={self.approval_note}"
                )
        except asyncio.CancelledError:
            self._phase = "cancelled"
            workflow.logger.info("EXECUTION STATE | phase=cancelled before the refund")
            raise

        # SIDE EFFECT: retries reuse one Stripe idempotency key.
        self._phase = "issuing_refund"
        workflow.logger.info("EXECUTION STATE | phase=issuing_refund")
        refund_step = workflow.start_activity(
            issue_refund,
            args=[request, decision],
            start_to_close_timeout=REFUND_START_TO_CLOSE_TIMEOUT,
            schedule_to_close_timeout=REFUND_SCHEDULE_TO_CLOSE_TIMEOUT,
            heartbeat_timeout=REFUND_HEARTBEAT_TIMEOUT,
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(seconds=3),
                maximum_attempts=10,
            ),
            summary="Issue the refund in Stripe",
        )
        try:
            # CANCELLATION during the refund: the call may already have reached
            # Stripe, so a cancel request must not abandon it halfway.
            # asyncio.shield keeps the Activity running, and the Workflow waits
            # for Stripe's answer, then records it and saves the case note.
            result = await asyncio.shield(refund_step)
        except asyncio.CancelledError:
            workflow.logger.info(
                "EXECUTION STATE | cancel requested during the refund; "
                "finishing the refund first"
            )
            result = await refund_step

        # LONG-TERM MEMORY: save a short case note in the memory store, so later
        # runs for this customer can find it with search_memory. The Workflow
        # ID is the case ID, so a retried write saves the note once.
        note = (
            f"{workflow.now().date().isoformat()}: refund {result.status} for "
            f"{request.order_id}, ${request.amount_cents / 100:.2f}. "
            f"{decision.rationale}"
        ).strip()
        await workflow.execute_activity(
            remember_case,
            args=[request.customer_id, workflow.info().workflow_id, note],
            start_to_close_timeout=timedelta(seconds=10),
            summary="Save a case note to long-term memory",
        )

        # The refund step is recorded and nothing is left to wait on. The phase
        # is Query state, not a command, so setting it is replay-safe.
        self._phase = "completed"
        return result

    async def _decide(
        self, request: RefundRequest, working_memory: list[dict]
    ) -> AgentStep:
        # MODEL REASONING: one turn, recorded as an Activity result, so replay
        # reuses it instead of calling the model again.
        self._turn += 1
        return await workflow.execute_activity(
            agent_decide_next_step,
            args=[request, working_memory],
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_MODEL_RETRY,
            summary=f"Agent turn {self._turn}: decide the next step",
        )

    async def _ask_customer(self, step: AgentStep) -> str:
        # The answer arrives as a customer_answer Signal, which Temporal records
        # in Event History. No Worker has to stay alive while the customer types.
        question_id = step.question_id or ""
        self._pending_question = {
            "question_id": question_id,
            "question": step.question or question_id,
            "suggested_answer": step.suggested_answer or "",
        }
        self._phase = "waiting_for_answer"
        workflow.logger.info(
            f"EXECUTION STATE | phase=waiting_for_answer id={question_id}"
        )
        await workflow.wait_condition(lambda: question_id in self.customer_answers)
        self._pending_question = None
        self._phase = "deciding"
        workflow.logger.info(f"EXECUTION STATE | phase=observed answer={question_id}")
        return self.customer_answers[question_id]

    async def _use_tool(self, tool: str) -> dict:
        # The model picks the tool; the IDs come from this run's request. The
        # tool schemas take no IDs, so the model can't point a lookup at another
        # customer's records: a common pattern for binding tools to the session.
        # Each summary names the step in plain words in Event History.
        request = self._request
        assert request is not None
        result: OrderDetails | CustomerHistory | ReturnStatus | CaseMemory
        if tool == "lookup_order":
            result = await workflow.execute_activity(
                lookup_order,
                request.order_id,
                start_to_close_timeout=timedelta(seconds=10),
                # "order-1234" reads as order 1234, as on the stage screen.
                summary=f"Look up order {request.order_id.removeprefix('order-')}",
            )
        elif tool == "lookup_customer_history":
            result = await workflow.execute_activity(
                lookup_customer_history,
                request.customer_id,
                start_to_close_timeout=timedelta(seconds=10),
                summary="Look up the customer's refund history",
            )
        elif tool == "check_refund_policy":
            result = await workflow.execute_activity(
                check_refund_policy,
                request.order_id,
                start_to_close_timeout=timedelta(seconds=10),
                summary="Check the refund policy",
            )
        elif tool == "search_memory":
            # LONG-TERM MEMORY: past case notes come from the memory store, not
            # from a customer-record lookup. Recorded like any other tool result.
            result = await workflow.execute_activity(
                search_memory,
                request.customer_id,
                start_to_close_timeout=timedelta(seconds=10),
                summary="Recall past cases from long-term memory",
            )
        else:
            raise ApplicationError(
                f"agent asked for an unknown tool: {tool}",
                type="UnknownTool",
                non_retryable=True,
            )
        workflow.logger.info(f"EXECUTION STATE | phase=observed tool={tool}")
        return asdict(result)

    @workflow.signal
    def approve(self, note: str = "") -> None:
        # EXECUTION STATE: the approval Signal is persisted in Event History.
        self.approval_note = note
        self.approved = True

    @workflow.signal
    def customer_answer(self, question_id: str, answer: str) -> None:
        """Record one customer answer as durable loop input."""

        self.customer_answers[question_id] = answer

    @workflow.query
    def progress(self) -> dict:
        """Where the loop stands: phase, open question, working memory, decision.

        A running Worker answers a Query from the Workflow's current state.
        Phases: deciding, waiting_for_answer, waiting_for_approval,
        issuing_refund, denied, completed.
        """

        return {
            "phase": self._phase,
            "pending_question": self._pending_question,
            "working_memory": self.working_memory,
            "decision": asdict(self._decision) if self._decision else None,
            "request": asdict(self._request) if self._request else None,
        }
