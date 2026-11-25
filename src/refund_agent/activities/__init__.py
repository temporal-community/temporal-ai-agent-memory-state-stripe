"""Activities contain every non-deterministic or fallible operation.

The agent loop (refund_agent/agent_loop.py) runs in the Workflow. Each turn it
calls agent_decide_next_step (the model), which asks the customer, asks for a
tool, or reaches a decision. The lookups (lookup_order, lookup_customer_history,
check_refund_policy) retrieve domain records, and search_memory retrieves past
case notes from long-term memory; their results become the agent's working
memory. issue_refund is the one side effect, and remember_case saves a
case note to long-term memory after the refund.

The code is split by role:

- tools.py: the three lookup Activities, search_memory, and remember_case.
  They read and write the demo's SQLite store (refund_agent/store.py), whose
  memory table is kept apart from the customer records.
- model.py: agent_decide_next_step, the fixed offline policy
  (fixed_policy_step), the live model step (model_next_step), and usage
  logging.
- refund.py: issue_refund, the shared refund call (refund_payment), and the
  idempotency key.

Activities log with activity.logger and have no UI side effects. The stage
builds its screens from Workflow Queries and Event History instead.

Every Activity keeps its name, and the names below are re-exported here, so
`from refund_agent.activities import ...` keeps working.
"""

from refund_agent.activities.model import (
    USAGE_AGENT_NAIVE,
    _selected_model_provider,
    agent_decide_next_step,
    fixed_policy_step,
    model_next_step,
)
from refund_agent.activities.refund import (
    STRIPE_TIMEOUT_SECONDS,
    issue_refund,
    refund_payment,
)
from refund_agent.activities.tools import (
    TOOL_HISTORY,
    TOOL_MEMORY,
    TOOL_ORDER,
    TOOL_POLICY,
    check_refund_policy,
    lookup_customer_history,
    lookup_order,
    remember_case,
    search_memory,
)

__all__ = [
    "STRIPE_TIMEOUT_SECONDS",
    "TOOL_HISTORY",
    "TOOL_MEMORY",
    "TOOL_ORDER",
    "TOOL_POLICY",
    "USAGE_AGENT_NAIVE",
    "_selected_model_provider",
    "agent_decide_next_step",
    "check_refund_policy",
    "fixed_policy_step",
    "issue_refund",
    "lookup_customer_history",
    "lookup_order",
    "model_next_step",
    "search_memory",
    "refund_payment",
    "remember_case",
]
