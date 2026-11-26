"""Small serializable values that cross Temporal history boundaries."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RefundRequest:
    """CONTEXT: the request carried through one agent run."""

    request_id: str
    order_id: str
    customer_id: str
    payment_intent_id: str
    amount_cents: int
    reason: str
    # Offline ledger switch (demo-only). True sends the refund to the local Stripe
    # stand-in in fake_stripe.py, so the demo runs without a Stripe key. A real
    # app always calls its payment provider.
    dry_run: bool
    # Intake answers. A request can carry them up front; when one is None, the
    # agent may ask the customer for it with ask_customer during the loop.
    item_opened: str | None = "Yes"
    damage: str | None = "Split seam"
    refund_destination: str = "Original card"
    # Which agent decides, recorded in Workflow input so a new Worker cannot
    # switch providers based on whichever keys happen to be present:
    # "anthropic" or "openai" for a live model, OFFLINE_POLICY for the fixed
    # offline policy (no model calls), or None to pick from the configured keys.
    model_provider: str | None = None
    # How long an escalated refund waits for a person's approval before the
    # request expires with no refund (the Workflow's APPROVAL DEADLINE).
    approval_timeout_seconds: int = 24 * 60 * 60


# The model_provider value that selects the fixed offline policy instead of a
# model. Offline is always an explicit choice, never a fallback for missing keys.
OFFLINE_POLICY = "offline"


@dataclass(frozen=True)
class OrderDetails:
    """Domain facts that are copied into working memory by a tool."""

    order_id: str
    item: str
    amount_cents: int
    status: str
    purchased_at: str


@dataclass(frozen=True)
class CustomerHistory:
    """Domain facts that are copied into working memory by a tool."""

    customer_id: str
    account_tenure_days: int
    purchases: list[str]
    prior_refunds: list[str]


@dataclass(frozen=True)
class CaseMemory:
    """LONG-TERM MEMORY: what earlier runs noted about this customer.

    Read by the search_memory tool, apart from the customer records. Notes
    are newest first; every stage run starts with none.
    """

    customer_id: str
    past_cases: list[str]


@dataclass(frozen=True)
class ReturnStatus:
    """A record a tool reads; the result goes into working memory."""

    order_id: str
    eligible_for_refund: bool
    return_required: bool
    returned: bool
    received_back: bool
    note: str


@dataclass(frozen=True)
class AgentStep:
    """One turn: ask the customer, use a tool, or decide."""

    action: str  # ask_customer | use_tool | decide
    tool: str | None = None
    question_id: str | None = None
    question: str | None = None
    suggested_answer: str | None = None
    recommendation: str | None = None  # approve | escalate | deny
    rationale: str | None = None
    source: str = "canned"


@dataclass(frozen=True)
class RefundDecision:
    """The decision the agent reached, recorded as an Activity result."""

    recommendation: str
    rationale: str
    source: str


@dataclass(frozen=True)
class RefundResult:
    """The known result after the refund Activity completes."""

    refund_id: str
    status: str
    amount_cents: int
    idempotency_key: str
    activity_attempt: int
    mode: str
