"""The agent's tools and its memory write, each an Activity.

Three lookups read the records in the demo's small SQLite store
(refund_agent.store), which stands in for the order database, the customer
records, and the refund policy. search_memory reads the agent's long-term
memory, the case notes earlier runs saved, from a separate table. Each tool
logs what it pulled, and its result becomes part of the agent's working
memory. remember_case writes a short note to long-term memory after a refund:
a light version of long-term memory, kept in a store you own.
"""

from __future__ import annotations

import re
import textwrap

from temporalio import activity
from temporalio.exceptions import ApplicationError

from refund_agent import store
from refund_agent.models import CaseMemory, CustomerHistory, OrderDetails, ReturnStatus

# Tool names, shared between the loop dispatch and the model.
TOOL_ORDER = "lookup_order"
TOOL_HISTORY = "lookup_customer_history"
TOOL_POLICY = "check_refund_policy"
TOOL_MEMORY = "search_memory"

# How both demos' screens label a search_memory result. The label says
# "memory" so it never reads as one of the record lookups.
MEMORY_LABEL = "Past cases (memory)"


def past_cases_summary(result: dict) -> str:
    """One short line for a search_memory result: "none yet" or a count."""

    count = len(result.get("past_cases") or [])
    if count == 0:
        return "none yet"
    return f"{count} note" if count == 1 else f"{count} notes"


# remember_case writes "<date>: refund <status> for order-<id>, $<amount>. ..."
_CASE_NOTE = re.compile(
    r"^(?P<date>\S+): refund (?P<status>\w+) for order-(?P<order>\w+), "
    r"(?P<amount>\$[\d,]+\.\d{2})"
)


def case_note_lines(note: str) -> list[str]:
    """A saved case note as short screen lines that fit a narrow pane."""

    match = _CASE_NOTE.match(note)
    if match is None:
        return textwrap.wrap(note, width=34)[:3]
    return [
        f"{match['date']}  refund {match['status']}",
        f"order {match['order']} · {match['amount']}",
    ]


def _not_found(kind: str, key: str) -> ApplicationError:
    # A missing record will not appear on retry, so the run fails right away.
    return ApplicationError(
        f"no {kind} {key} in the demo store", type="NotFound", non_retryable=True
    )


# ---------------------------------------------------------------------------
# Retrieval tools. Each reads the store and logs what it pulled.
# ---------------------------------------------------------------------------


@activity.defn
def lookup_order(order_id: str) -> OrderDetails:
    """Read a record from the store; the result goes into working memory."""

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
    activity.logger.info(
        f"RECORD LOOKUP | lookup_order: {order.item}, "
        f"${order.amount_cents / 100:.2f}, {order.status}"
    )
    return order


@activity.defn
def lookup_customer_history(customer_id: str) -> CustomerHistory:
    """Retrieve domain facts and return a copy for working memory."""

    record = store.get_customer(customer_id)
    if record is None:
        raise _not_found("customer", customer_id)
    history = CustomerHistory(customer_id=customer_id, **record)
    activity.logger.info(
        f"RECORD LOOKUP | lookup_customer_history: {history.account_tenure_days} "
        f"days, {len(history.prior_refunds)} prior"
    )
    return history


@activity.defn
def check_refund_policy(order_id: str) -> ReturnStatus:
    """Read a record from the store; the result goes into working memory."""

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
    activity.logger.info(f"RECORD LOOKUP | check_refund_policy: {status.note}")
    return status


# ---------------------------------------------------------------------------
# Long-term memory: search_memory reads it, remember_case writes it after the
# refund. Both use the memory table, never the customer records.
# ---------------------------------------------------------------------------


@activity.defn
def search_memory(customer_id: str) -> CaseMemory:
    """LONG-TERM MEMORY: return the case notes earlier runs saved."""

    memory = CaseMemory(
        customer_id=customer_id, past_cases=store.get_case_notes(customer_id)
    )
    activity.logger.info(
        f"LONG-TERM MEMORY | search_memory: {len(memory.past_cases)} past cases"
    )
    return memory


@activity.defn
def remember_case(customer_id: str, case_id: str, note: str) -> bool:
    """LONG-TERM MEMORY: save one case note for this customer.

    case_id is the Workflow ID, so a retried write saves the note once.
    Returns False when the note was already saved.
    """

    saved = store.add_case_note(customer_id, case_id, note)
    activity.logger.info(
        f"LONG-TERM MEMORY | remember_case: {note}" if saved else "already saved"
    )
    return saved
