r"""Replay a recorded stage run to guard the Workflow's determinism.

What this guards: Temporal rebuilds a Workflow's state by running its code
again against the recorded Event History. If a code change makes the Workflow
issue different commands for the same history (for example, a new Activity
before the first agent turn, or Activities in a different order), every run
that is already in flight fails with a nondeterminism error when a Worker picks
it up. This test replays a real recorded history against the current
RefundWorkflow code, offline and without a Temporal server, so that kind of
change fails here instead of on stage.

The fixture, tests/histories/stage_offline_worker_hooks.json, is the complete
Event History of one offline guided stage run (`refund-demo stage --offline`):
the fixed policy, the offline Stripe ledger, and no keys. The agent asks two
questions, looks up the order and the customer's history, searches past cases
with search_memory, and approves. The stage's Worker hook (demo_hooks.py)
holds issue_refund attempt 1 before Stripe; the stage kills that Worker, and
a new Worker runs attempt 2, then remember_case.

This is a demo, so the repository carries no compatibility code for runs
recorded by older code: finish or terminate running demo Workflows before
upgrading (AGENTS.md, "Changing the Workflow safely"). When a change alters
the Workflow's commands on purpose, re-record the fixture as below so this
test replays a history from the current code. If the test fails after a
change you did not mean to make to the commands, the change would break
Workflows that are already running; fix the change, not the fixture.

How to regenerate the fixture, from the repository root:

1. Install the extras the stage needs: `uv sync --extra dev --extra tui`.
2. In one terminal, start a throwaway dev server on spare ports, so that no
   other server is touched:

       mkdir -p /tmp/replay-fixture
       temporal server start-dev --ip 127.0.0.1 --port 7263 --ui-port 8263 \
           --http-port 7264 --metrics-port 7265 --headless \
           --db-filename /tmp/replay-fixture/temporal.db

3. In a second terminal, run the offline stage unattended. Pressing Enter at
   every prompt accepts each default answer. --offline is required, because
   without a terminal a run with missing keys fails instead of going offline.
   DOTENV_PATH=/dev/null keeps values from .env (such as a Temporal Cloud
   namespace) out of the run. Unsetting
   TEMPORAL_IDENTITY and setting TEMPORAL_NAMESPACE keep values exported in
   your shell out of it too. DEMO_STATE_DIR keeps the stage files out of the
   repository:

       yes '' | env -u TEMPORAL_IDENTITY DOTENV_PATH=/dev/null \
           TEMPORAL_ADDRESS=127.0.0.1:7263 TEMPORAL_NAMESPACE=default \
           DEMO_STATE_DIR=/tmp/replay-fixture/state \
           uv run refund-demo stage --offline \
           --workflow-id replay-fixture-offline-hooks

4. Export the completed history:

       temporal workflow show --workflow-id replay-fixture-offline-hooks \
           --address 127.0.0.1:7263 --namespace default --output json \
           > tests/histories/stage_offline_worker_hooks.json

5. Stop the dev server with Ctrl+C and delete /tmp/replay-fixture.
6. Before committing, search the new file for hostnames, usernames, file paths,
   and keys. The payloads are base64-encoded JSON, so decode them to search
   them. By default every identity is `<pid>@refund-demo`, which names no
   machine.
"""

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio import workflow
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer
from temporalio.workflow import NondeterminismError

from refund_agent.activities import lookup_order
from refund_agent.models import RefundRequest
from refund_agent.workflow import RefundWorkflow

HISTORIES = Path(__file__).parent / "histories"
RECORDED_RUN = HISTORIES / "stage_offline_worker_hooks.json"
RECORDED_WORKFLOW_ID = "replay-fixture-offline-hooks"


def _recorded_history() -> WorkflowHistory:
    return WorkflowHistory.from_json(
        RECORDED_WORKFLOW_ID, RECORDED_RUN.read_text(encoding="utf-8")
    )


# A deliberately changed copy of the Workflow, registered under the same
# Workflow type name. It looks up the order before the first agent turn, so its
# first command does not match the recorded history. It exists only to prove
# that the replay test can fail. It runs outside the sandbox because the sandbox
# would otherwise re-import this test module.
@workflow.defn(name="RefundApprovalAgent", sandboxed=False)
class ReorderedRefundWorkflow:
    @workflow.run
    async def run(self, request: RefundRequest) -> None:
        await workflow.execute_activity(
            lookup_order,
            request.order_id,
            start_to_close_timeout=timedelta(seconds=10),
        )


def test_current_workflow_replays_recorded_stage_run() -> None:
    history = _recorded_history()
    result = asyncio.run(Replayer(workflows=[RefundWorkflow]).replay_workflow(history))
    assert result.replay_failure is None


def test_changed_workflow_fails_replay_with_nondeterminism() -> None:
    history = _recorded_history()
    # The error names both Activity types: the recorded agent_decide_next_step
    # and the lookup_order command that the changed code issued instead.
    with pytest.raises(NondeterminismError, match="lookup_order"):
        asyncio.run(
            Replayer(workflows=[ReorderedRefundWorkflow]).replay_workflow(history)
        )
