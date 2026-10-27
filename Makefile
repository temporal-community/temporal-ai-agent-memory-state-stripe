# Shortcuts for the commands in README.md. Each recipe is the raw command the
# README shows beside it; `make -n <target>` prints it without running it.

.PHONY: help setup run run-offline run-real failure reset test lint typecheck usage

help: ## List the targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  make %-11s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## Install the project with the dev and tui extras
	uv sync --extra dev --extra tui

run: ## Live stage run: a model and Stripe test mode (asks for missing keys)
	uv run refund-demo stage

run-offline: ## Offline stage run: no keys, no model calls
	uv run refund-demo stage --offline

run-real: ## Stripe test mode with the fixed policy instead of a model
	uv run refund-demo stage --real

failure: ## Kill the Worker while the refund Activity is in flight (offline)
	uv run refund-demo stage --offline --simulate-stripe-timeout

reset: ## Refund leftover Stripe test charges; print the manual steps
	@echo "Stripe test mode cleanup. Offline, a missing STRIPE_API_KEY is expected."
	uv run refund-demo cleanup
	@echo ""
	@echo "Nothing was deleted or stopped. To reset further, by hand:"
	@echo "  Running Workflow from an aborted take: uv run refund-demo stop <workflow-id>"
	@echo "  Empty Temporal Web list: Ctrl+C your own temporal server start-dev, then"
	@echo "    start it again. Never restart a server someone else is using."
	@echo "  Local files: stop any dev server using .demo-state/temporal.db, then"
	@echo "    run rm -rf .demo-state (logs, usage logs, ledgers, stage server data)."

test: ## Offline test suite
	uv run --extra dev pytest -q

lint: ## Ruff lint and format check
	uv run --extra dev ruff check .
	uv run --extra dev ruff format --check .

typecheck: ## Type-check src with mypy
	uv run --extra dev mypy

usage: ## Sum model tokens and dollars logged with LOG_MODEL_USAGE=1
	uv run refund-demo usage
