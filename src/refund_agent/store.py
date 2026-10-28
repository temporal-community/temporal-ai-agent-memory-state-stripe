"""A small SQLite database that stands in for the business systems and memory.

The lookup tools read orders, customer history, and the refund policy from it,
and the remember_case Activity writes a short note after each refund. Later
runs read those notes back with the customer's history: a light version of
long-term memory, kept in a store you own rather than in Temporal.

The file lives in the state directory (.demo-state/store.sqlite by default).
Each guided stage run has its own state directory, so every stage run starts
from the same seed data. Manual runs share .demo-state, so their notes add up.
SQLite ships with Python, so nothing extra is installed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from refund_agent.settings import state_dir

STORE_FILE = "store.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    item TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    status TEXT NOT NULL,
    purchased_at TEXT NOT NULL,
    eligible_for_refund INTEGER NOT NULL,
    return_required INTEGER NOT NULL,
    returned INTEGER NOT NULL,
    received_back INTEGER NOT NULL,
    policy_note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,
    account_tenure_days INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS purchases (
    customer_id TEXT NOT NULL,
    line TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prior_refunds (
    customer_id TEXT NOT NULL,
    line TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS case_notes (
    customer_id TEXT NOT NULL,
    case_id TEXT NOT NULL UNIQUE,
    note TEXT NOT NULL
);
"""

# The demo's one customer and order. Purchases and prior refunds are newest
# first, as the panels show them.
_SEED_ORDERS = [
    (
        "order-1234",
        "cus_demo_42",
        "python plushy",
        8000,
        "delivered",
        "2026-06-03",
        1,
        0,
        0,
        0,
        "eligible for refund; this low-value damaged item does not need to be returned",
    )
]
_SEED_CUSTOMERS = [("cus_demo_42", 824)]
_SEED_PURCHASES = [
    ("cus_demo_42", "2026-06-03, python plushy, $80.00"),
    ("cus_demo_42", "2026-02-14, mechanical keyboard, $89.00"),
    ("cus_demo_42", "2025-11-20, rubber duck, $24.00"),
]
_SEED_PRIOR_REFUNDS = [
    ("cus_demo_42", "2025-08-09, laptop stickers, $18.00, approved"),
]


def store_path() -> Path:
    return state_dir() / STORE_FILE


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Open the store, creating and seeding it on first use."""

    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    try:
        connection.executescript(_SCHEMA)
        if connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
            _seed(connection)
        yield connection
        connection.commit()
    finally:
        connection.close()


def _seed(connection: sqlite3.Connection) -> None:
    connection.executemany(
        "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", _SEED_ORDERS
    )
    connection.executemany("INSERT INTO customers VALUES (?, ?)", _SEED_CUSTOMERS)
    connection.executemany("INSERT INTO purchases VALUES (?, ?)", _SEED_PURCHASES)
    connection.executemany(
        "INSERT INTO prior_refunds VALUES (?, ?)", _SEED_PRIOR_REFUNDS
    )
    connection.commit()


def get_order(order_id: str) -> dict | None:
    with connect() as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM orders WHERE order_id = ?", (order_id,)
        ).fetchone()
    return dict(row) if row else None


def get_customer(customer_id: str) -> dict | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT account_tenure_days FROM customers WHERE customer_id = ?",
            (customer_id,),
        ).fetchone()
        if row is None:
            return None
        purchases = connection.execute(
            "SELECT line FROM purchases WHERE customer_id = ? ORDER BY rowid",
            (customer_id,),
        ).fetchall()
        refunds = connection.execute(
            "SELECT line FROM prior_refunds WHERE customer_id = ? ORDER BY rowid",
            (customer_id,),
        ).fetchall()
        notes = connection.execute(
            "SELECT note FROM case_notes WHERE customer_id = ? ORDER BY rowid DESC",
            (customer_id,),
        ).fetchall()
    return {
        "account_tenure_days": row[0],
        "purchases": [line for (line,) in purchases],
        "prior_refunds": [line for (line,) in refunds],
        "past_cases": [note for (note,) in notes],
    }


def add_case_note(customer_id: str, case_id: str, note: str) -> bool:
    """Save one case note. Returns False when this case is already saved.

    case_id is unique, so a retried write saves the note once.
    """

    with connect() as connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO case_notes VALUES (?, ?, ?)",
            (customer_id, case_id, note),
        )
        return cursor.rowcount == 1
