import pytest

from refund_agent.fake_stripe import (
    create_refund,
    find_refund,
    idempotency_key_for,
    record_refund,
)


def test_idempotency_key_is_stable_and_workflow_scoped() -> None:
    first = idempotency_key_for("refund-one")
    again = idempotency_key_for("refund-one")
    second = idempotency_key_for("refund-two")

    assert first == again
    assert not first == second
    assert first.startswith("durable-refund-")


def test_dry_run_refund_is_deduplicated(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))
    key = idempotency_key_for("refund-test")
    arguments = {
        "workflow_id": "refund-test",
        "payment_intent_id": "pi_test",
        "amount_cents": 4200,
        "idempotency_key": key,
    }

    first = create_refund(**arguments)
    second = create_refund(**arguments)

    assert first["refund_id"] == second["refund_id"]
    assert first["calls"] == 1
    assert second["calls"] == 2


def test_find_refund_matches_by_workflow_id(tmp_path, monkeypatch) -> None:
    # The panels look a refund up by Workflow ID, through the stored field.
    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))
    key = idempotency_key_for("wf-1")
    create_refund(
        workflow_id="wf-1",
        payment_intent_id="pi_test",
        amount_cents=8000,
        idempotency_key=key,
    )

    found = find_refund("wf-1")

    assert found is not None
    assert found["workflow_id"] == "wf-1"
    assert found["idempotency_key"] == key
    assert find_refund("wf-missing") is None


def test_real_refund_mirror_rejects_conflicting_idempotency_key(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))
    arguments = {
        "workflow_id": "wf-1",
        "refund_id": "re_1",
        "status": "pending",
        "amount_cents": 8000,
        "payment_intent_id": "pi_1",
        "idempotency_key": "stable-key",
    }

    first = record_refund(**arguments)
    second = record_refund(**{**arguments, "status": "succeeded"})

    assert first["calls"] == 1
    assert second["calls"] == 2
    assert second["status"] == "succeeded"
    with pytest.raises(ValueError, match="different parameters"):
        record_refund(**{**arguments, "amount_cents": 9000})
