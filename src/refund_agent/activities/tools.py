"""The agent's three lookup tools and its memory write, each an Activity.

The lookups read the demo's small SQLite store (refund_agent.store), which
stands in for the order database, the customer records, and the refund policy.
Each prints what it pulled, and its result becomes part of the agent's working
memory. remember_case writes a short note to the same store after a refund, so
later runs can read it back: a light version of long-term memory.
"""

from __future__ import annotations

from temporalio import activity
from temporalio.exceptions import ApplicationError

from refund_agent import store
from refund_agent.activities.view import _line
from refund_agent.models import CustomerHistory, OrderDetails, ReturnStatus

# Tool names, shared between the loop dispatch and the model.
TOOL_ORDER = "lookup_order"
TOOL_HISTORY = "lookup_customer_history"
TOOL_POLICY = "check_refund_policy"


def _not_found(kind: str, key: str) -> ApplicationError:
    # A missing record will not appear on retry, so the run fails right away.
    return ApplicationError(
        f"no {kind} {key} in the demo store", type="NotFound", non_retryable=True
    )


# ---------------------------------------------------------------------------
# Retrieval tools. Each reads the store and prints what it pulled.
# ---------------------------------------------------------------------------


@activity.defn
def lookup_order(order_id: str) -> OrderDetails:
    """Retrieve domain state and return a copy for working memory."""

    row = store.get_order(order_id)
    if row is None:
        raise _not_found("order", order_id)
    order = OrderDetails(
        order_id=order_id,
        item=row["item"],
        amount_cents=row["amount_cents"],
        status=row["status"],
        purchased_at=row["purchased_at"],
    )
    _line(
        "MEMORY COPY",
        f"lookup_order: {order.item}, ${order.amount_cents / 100:.2f}, {order.status}",
    )
    return order


@activity.defn
def lookup_customer_history(customer_id: str) -> CustomerHistory:
    """Retrieve domain facts and past case notes for working memory."""

    record = store.get_customer(customer_id)
    if record is None:
        raise _not_found("customer", customer_id)
    history = CustomerHistory(customer_id=customer_id, **record)
    summary = (
        f"lookup_customer_history: {history.account_tenure_days} days, "
        f"{len(history.prior_refunds)} prior"
    )
    if history.past_cases:
        summary += f", {len(history.past_cases)} past case notes"
    _line("MEMORY COPY", summary)
    return history


@activity.defn
def check_refund_policy(order_id: str) -> ReturnStatus:
    """Retrieve domain state and return a copy for working memory."""

    row = store.get_order(order_id)
    if row is None:
        raise _not_found("order", order_id)
    status = ReturnStatus(
        order_id=order_id,
        eligible_for_refund=bool(row["eligible_for_refund"]),
        return_required=bool(row["return_required"]),
        returned=bool(row["returned"]),
        received_back=bool(row["received_back"]),
        note=row["policy_note"],
    )
    _line("MEMORY COPY", f"check_refund_policy: {status.note}")
    return status


# ---------------------------------------------------------------------------
# Memory write. Runs after the refund, so later runs can read the case back.
# ---------------------------------------------------------------------------


@activity.defn
def remember_case(customer_id: str, case_id: str, note: str) -> bool:
    """LONG-TERM MEMORY: save one case note for this customer.

    case_id is the Workflow ID, so a retried write saves the note once.
    Returns False when the note was already saved.
    """

    saved = store.add_case_note(customer_id, case_id, note)
    _line("LONG-TERM MEMORY", f"remember_case: {note}" if saved else "already saved")
    return saved
