"""Demo-only hooks at the Worker edge: the stage's pause and failure drills.

The Workflow and Activities are shaped like what a real app ships. To show a
crash at the same point on every run, the stage starts its Workers with this
Activity interceptor, which wraps the issue_refund Activity and nothing else:

- STAGE_HOLD_REFUND=1: hold each issue_refund attempt before it runs,
  heartbeating, until the stage kills this Worker. The refund never reaches
  Stripe from this Worker. A new Worker started without the hold picks up the
  next attempt.
- STAGE_SIMULATE_STRIPE_TIMEOUT=1: the same hold, logged as a Stripe API that
  never answers. If the Worker is not killed within 30 s, the attempt fails
  the way a client timeout would, and Temporal retries it.
- REFUND_RESTART_WINDOW_SECONDS=N: after Stripe accepts the refund, keep the
  attempt open N seconds before reporting it, so a kill lands after Stripe accepts
  it and before Temporal records the result. Used by the stage's retry
  drill and the manual walkthrough in docs/GUIDE.md.

An interceptor runs in the Worker, not in Workflow code, so it never changes
the Workflow's commands or its replay. worker.py registers it only when one of
these settings is on; with none set, `refund-worker` is a plain Worker.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.worker import (
    ActivityInboundInterceptor,
    ActivityOutboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
)

from refund_agent.settings import (
    refund_hold_marker,
    refund_restart_window_seconds,
    state_dir,
)

# Heartbeat details for each hold. Temporal hands the last details to the next
# attempt, so a new Worker can tell that an earlier attempt already reached
# Stripe and must not open the restart window again.
_HELD_BEFORE_REFUND = "held before the refund (demo)"
_REFUND_ACCEPTED = "refund accepted, result not reported"
_SIMULATED_TIMEOUT_SECONDS = 30


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class DemoHooks:
    """Which demo-only hooks this Worker applies to issue_refund."""

    hold_refund: bool = False
    simulate_stripe_timeout: bool = False
    restart_window_seconds: float = 0

    @classmethod
    def from_env(cls) -> DemoHooks:
        return cls(
            hold_refund=_flag("STAGE_HOLD_REFUND"),
            simulate_stripe_timeout=_flag("STAGE_SIMULATE_STRIPE_TIMEOUT"),
            restart_window_seconds=refund_restart_window_seconds(),
        )

    @property
    def enabled(self) -> bool:
        return (
            self.hold_refund
            or self.simulate_stripe_timeout
            or self.restart_window_seconds > 0
        )


def interceptors_from_env() -> list[Interceptor]:
    """The Worker's interceptors: none unless a demo hook is switched on."""

    hooks = DemoHooks.from_env()
    return [DemoHooksInterceptor(hooks)] if hooks.enabled else []


class DemoHooksInterceptor(Interceptor):
    def __init__(self, hooks: DemoHooks) -> None:
        self.hooks = hooks

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        return _RefundHooks(next, self.hooks)


class _RefundHooks(ActivityInboundInterceptor):
    def __init__(self, next: ActivityInboundInterceptor, hooks: DemoHooks) -> None:
        super().__init__(next)
        self._hooks = hooks
        self._outbound: ActivityOutboundInterceptor | None = None

    def init(self, outbound: ActivityOutboundInterceptor) -> None:
        # Keep the outbound side: its heartbeat is safe to call from this
        # coroutine, before and after the synchronous Activity runs.
        self._outbound = outbound
        super().init(outbound)

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        assert self._outbound is not None
        info = self._outbound.info()
        if info.activity_type != "issue_refund":
            return await super().execute_activity(input)

        if self._hooks.simulate_stripe_timeout:
            # Like a call to a Stripe API that never answers: Stripe accepts
            # nothing. The stage kills this Worker during the hold.
            _write_marker(info.workflow_id, info.attempt, "stripe_timeout")
            activity.logger.info(
                "THE SYSTEM | Stripe API is not responding (simulated); "
                "no refund accepted"
            )
            await self._hold(_HELD_BEFORE_REFUND, _SIMULATED_TIMEOUT_SECONDS)
            raise ApplicationError(
                "simulated Stripe API timeout", type="StripeAPITimeout"
            )
        if self._hooks.hold_refund:
            # The stage's pause: the agent has chosen the refund and Temporal
            # has started the refund step, but this Worker never calls Stripe.
            _write_marker(info.workflow_id, info.attempt, "hold")
            activity.logger.info(
                "EXECUTION STATE | refund held before Stripe (demo pause)"
            )
            await self._hold(_HELD_BEFORE_REFUND, None)

        result = await super().execute_activity(input)

        window = self._hooks.restart_window_seconds
        if window > 0 and _REFUND_ACCEPTED not in info.heartbeat_details:
            activity.logger.info(
                f"EXECUTION STATE | restart window open {window:.0f}s; "
                "run: refund-demo kill-worker"
            )
            await self._hold(_REFUND_ACCEPTED, window)
        return result

    async def _hold(self, detail: str, seconds: float | None) -> None:
        # Heartbeats keep the held attempt alive until the Worker is killed;
        # then the heartbeat timeout tells Temporal to run the next attempt.
        assert self._outbound is not None
        deadline = None if seconds is None else time.monotonic() + seconds
        while deadline is None or time.monotonic() < deadline:
            self._outbound.heartbeat(detail)
            await asyncio.sleep(1.0)


def _write_marker(workflow_id: str | None, attempt: int, mode: str) -> None:
    # The stage waits for this file before it kills the Worker, so the kill
    # always lands while the refund is held.
    state_dir().mkdir(parents=True, exist_ok=True)
    path = refund_hold_marker()
    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps({"workflow_id": workflow_id, "attempt": attempt, "mode": mode}),
        encoding="utf-8",
    )
    os.replace(temporary_path, path)
