"""The one side effect: the issue_refund Activity and the refund call.

issue_refund sends one refund with an idempotency key derived from the Workflow
ID, through refund_payment: to the offline ledger by default or to Stripe test
mode with --real. Demo 1's agent process calls refund_payment directly. Stripe's
own retries are off, so every retry is a Temporal Activity attempt.

Demo-only pauses and simulated failures are not here. The stage applies them at
the Worker edge with an interceptor (refund_agent/demo_hooks.py).
"""

from __future__ import annotations

import concurrent.futures
import os
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import stripe
from temporalio import activity
from temporalio.exceptions import ApplicationError

from refund_agent.fake_stripe import create_refund, idempotency_key_for, record_refund
from refund_agent.models import RefundDecision, RefundRequest, RefundResult
from refund_agent.settings import validate_stripe_key

# ---------------------------------------------------------------------------
# The one side effect.
# ---------------------------------------------------------------------------

# Stripe's HTTP client waits up to 80 s by default. The refund call instead
# allows 3 s to connect and 10 s of silence while reading Stripe's response,
# given as (connect, read) seconds the way the requests library takes them.
# Those limits apply per socket operation, so they are not a wall-clock bound;
# issue_refund heartbeats while it waits (_call_with_heartbeats), so a slow but
# live call is not mistaken for a lost Worker. A hung call fails on these
# timeouts, and Temporal retries it with the same idempotency key.
STRIPE_TIMEOUT_SECONDS = (3.0, 10.0)

# Like the Stripe SDK, honor a Retry-After of up to 60 s and ignore longer ones.
_STRIPE_MAX_RETRY_AFTER_SECONDS = 60


def _stripe_error_is_retryable(error: stripe.StripeError) -> bool:
    """Return whether a failed refund call deserves another Temporal attempt.

    With max_network_retries = 0 the SDK never retries, so the Activity retry
    policy decides and every attempt shows in Temporal. Stripe's own signals
    come first, as in the SDK's retry logic: a connection error's should_retry
    (true for a timeout or dropped connection, false for an SSL failure), then a
    Stripe-Should-Retry response header. Otherwise, as with the model calls, 429
    and 5xx are retried, and so is 409, which Stripe returns while an earlier
    call with the same idempotency key is still running. Every attempt reuses
    that key, so a retry cannot create a second refund. Other 4xx errors, such
    as a bad PaymentIntent, fail without a retry.
    """

    if isinstance(error, stripe.APIConnectionError):
        return bool(error.should_retry)
    should_retry = (error.headers or {}).get("stripe-should-retry")
    if should_retry in ("true", "false"):
        return should_retry == "true"
    status = error.http_status or 0
    return status in (409, 429) or status >= 500


def _stripe_retry_delay(error: stripe.StripeError) -> timedelta | None:
    """Return the wait Stripe asked for in Retry-After, if it sent one."""

    try:
        seconds = int((error.headers or {}).get("retry-after", ""))
    except (TypeError, ValueError):
        return None
    if 0 < seconds <= _STRIPE_MAX_RETRY_AFTER_SECONDS:
        return timedelta(seconds=seconds)
    return None


def _call_with_heartbeats(call, *args):
    """Run a blocking call on a helper thread, heartbeating each second.

    The heartbeat then means the Worker is alive, however long the call takes.
    The start-to-close timeout still bounds the attempt.
    """

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(call, *args)
        while True:
            try:
                return future.result(timeout=1.0)
            except concurrent.futures.TimeoutError:
                activity.heartbeat("waiting on Stripe")


def _real_stripe_refund(
    request: RefundRequest, workflow_id: str, idempotency_key: str
) -> dict[str, object]:
    secret_key = validate_stripe_key(
        os.getenv("STRIPE_API_KEY"),
        required=True,
    )
    stripe.api_key = secret_key
    stripe.max_network_retries = 0
    stripe.default_http_client = stripe.RequestsClient(timeout=STRIPE_TIMEOUT_SECONDS)
    refund = stripe.Refund.create(
        payment_intent=request.payment_intent_id,
        amount=request.amount_cents,
        reason="requested_by_customer",
        metadata={
            "temporal_workflow_id": workflow_id,
            "temporal_idempotency_key": idempotency_key,
        },
        idempotency_key=idempotency_key,
    )
    return {
        "refund_id": refund.id,
        "status": refund.status,
        "amount_cents": refund.amount,
    }


def _call_directly(call: Callable[..., Any], *args: Any) -> Any:
    return call(*args)


def refund_payment(
    request: RefundRequest,
    *,
    workflow_id: str,
    idempotency_key: str,
    run_blocking: Callable[..., Any] = _call_directly,
) -> tuple[dict[str, object], str]:
    """Send one idempotency-keyed refund and return (refund, mode).

    Both demos refund through this function. dry_run sends it to the offline
    ledger (the Stripe stand-in in fake_stripe.py), which honors the
    idempotency key the same way; otherwise it goes to Stripe test mode.
    run_blocking wraps the Stripe call; the Activity passes
    _call_with_heartbeats. Raises ValueError when the key was reused with
    different parameters, and stripe.StripeError when Stripe refuses.
    """

    if request.dry_run:
        # Offline ledger (demo-only), so the demo runs without a Stripe key. A
        # real app calls Stripe, as below.
        stripe_refund = create_refund(
            workflow_id=workflow_id,
            payment_intent_id=request.payment_intent_id,
            amount_cents=request.amount_cents,
            idempotency_key=idempotency_key,
        )
        return stripe_refund, "dry-run"
    stripe_refund = run_blocking(
        _real_stripe_refund, request, workflow_id, idempotency_key
    )
    # Mirror the real refund locally so the panel can show one refund and its
    # call count. Stripe stays the system of record.
    record_refund(
        workflow_id=workflow_id,
        refund_id=str(stripe_refund["refund_id"]),
        status=str(stripe_refund["status"]),
        amount_cents=int(stripe_refund["amount_cents"]),
        payment_intent_id=request.payment_intent_id,
        idempotency_key=idempotency_key,
    )
    return stripe_refund, "stripe-test"


@activity.defn
def issue_refund(request: RefundRequest, decision: RefundDecision) -> RefundResult:
    """SIDE EFFECT: issue one idempotency-keyed refund."""

    info = activity.info()
    workflow_id = info.workflow_id
    if workflow_id is None:
        raise ApplicationError(
            "issue_refund must run inside a Workflow",
            type="MissingWorkflowIdentity",
            non_retryable=True,
        )

    # IDEMPOTENCY KEY: derived from the Workflow ID, the refund's business
    # identity (refund-demo start builds it from the request ID). Every attempt,
    # every run in a Continue-As-New chain, and a retry of a failed run share
    # the Workflow ID, so they all send the same key and Stripe returns the
    # first refund instead of creating a second one. Never derive it from the
    # Run ID, which changes with each run.
    # Stripe keeps keys for about 24 hours, so a reset after that needs an
    # Stripe check (list the PaymentIntent's refunds) before refunding again.
    idempotency_key = idempotency_key_for(workflow_id)
    activity.logger.info(
        f"EXECUTION STATE | issuing refund: attempt {info.attempt}, decision "
        f"{decision.recommendation}, idempotency key ...{idempotency_key[-8:]}"
    )

    try:
        stripe_refund, mode = refund_payment(
            request,
            workflow_id=workflow_id,
            idempotency_key=idempotency_key,
            # Heartbeat while Stripe answers, so a slow call keeps this attempt
            # alive instead of looking like a lost Worker.
            run_blocking=_call_with_heartbeats,
        )
    except ValueError as error:
        raise ApplicationError(
            str(error),
            type="IdempotencyConflict",
            non_retryable=True,
        ) from error
    except stripe.StripeError as error:
        message = getattr(error, "user_message", None) or str(error)
        if _stripe_error_is_retryable(error):
            # Fail this attempt only. The retry policy runs the next one with
            # the same idempotency key, after Retry-After if Stripe sent one.
            raise ApplicationError(
                f"Stripe refund call failed (retryable): {message}",
                type="StripeRetryableError",
                next_retry_delay=_stripe_retry_delay(error),
            ) from error
        # A bad PaymentIntent or similar cannot be fixed by retrying, so fail
        # fast with a readable message instead of retrying and then surfacing
        # an opaque "Activity task failed".
        raise ApplicationError(
            f"Stripe rejected the refund: {message}",
            type="StripeRefundError",
            non_retryable=True,
        ) from error

    activity.logger.info(
        f"THE SYSTEM | refund accepted at Stripe: {stripe_refund['refund_id']} "
        f"({mode}, attempt {info.attempt})"
    )
    return RefundResult(
        refund_id=str(stripe_refund["refund_id"]),
        status=str(stripe_refund["status"]),
        amount_cents=int(str(stripe_refund["amount_cents"])),
        idempotency_key=idempotency_key,
        activity_attempt=info.attempt,
        mode=mode,
    )
