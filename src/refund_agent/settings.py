"""Process settings used outside deterministic Workflow code."""

import math
import os
from pathlib import Path


def load_env_file() -> None:
    """Load KEY=VALUE lines from .env into the environment if not already set.

    Kept dependency free on purpose. Values already in the environment win, so an
    explicit export always overrides the file. Call this from process entry
    points (the Worker and the CLI), never from deterministic Workflow code.
    """

    path = Path(os.getenv("DOTENV_PATH", ".env"))
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def temporal_address() -> str:
    return os.getenv("TEMPORAL_ADDRESS", "localhost:7233")


def temporal_namespace() -> str:
    return os.getenv("TEMPORAL_NAMESPACE", "default")


def temporal_identity() -> str:
    """Return the identity every Temporal client in this process reports.

    The SDK default is ``<pid>@<hostname>``, which puts the machine's hostname in
    Temporal Web (the Workers tab and the identity on started and signaled
    events). This keeps the PID, so a new Worker still shows up as a different
    process, and replaces the hostname with a neutral name. A non-empty
    TEMPORAL_IDENTITY is used verbatim instead, so every process that inherits
    it reports the same identity.
    """

    return os.getenv("TEMPORAL_IDENTITY", "").strip() or f"{os.getpid()}@refund-demo"


def task_queue() -> str:
    return os.getenv("TEMPORAL_TASK_QUEUE", "refund-demo")


def state_dir() -> Path:
    return Path(os.getenv("DEMO_STATE_DIR", ".demo-state"))


def worker_pid_file() -> Path:
    return state_dir() / "worker.pid"


def refund_hold_marker() -> Path:
    """Where the stage's Worker hook notes that it is holding the refund.

    Demo-only (demo_hooks.py). The stage reads it from its private state
    directory to know the refund is held before it kills the Worker.
    """

    return state_dir() / "refund-held.json"


MODEL_USAGE_FILE = "model-usage.jsonl"


def model_usage_path() -> Path | None:
    """Return the opt-in token usage log, or None while LOG_MODEL_USAGE is off."""

    raw_value = os.getenv("LOG_MODEL_USAGE", "").strip().lower()
    if raw_value in ("", "0", "false", "no"):
        return None
    if raw_value in ("1", "true", "yes"):
        return state_dir() / MODEL_USAGE_FILE
    raise RuntimeError("LOG_MODEL_USAGE must be 1, true, yes, 0, false, or no")


def refund_restart_window_seconds() -> float:
    raw_value = os.getenv("REFUND_RESTART_WINDOW_SECONDS", "0")
    try:
        value = float(raw_value)
    except ValueError as error:
        raise RuntimeError(
            "REFUND_RESTART_WINDOW_SECONDS must be a finite number"
        ) from error
    if not math.isfinite(value) or value < 0:
        raise RuntimeError(
            "REFUND_RESTART_WINDOW_SECONDS must be a finite number, zero or greater"
        )
    return value


def validate_stripe_key(key: str | None, *, required: bool) -> str | None:
    """Accept test credentials only and reject live credentials loudly."""

    if not key:
        if required:
            raise RuntimeError("STRIPE_API_KEY is required for a real refund")
        return None
    if key.startswith(("sk_live_", "rk_live_")):
        raise RuntimeError("A live Stripe key was detected and has been rejected")
    if not key.startswith(("sk_test_", "rk_test_")):
        raise RuntimeError("STRIPE_API_KEY must be a Stripe test mode key")
    return key
