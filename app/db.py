"""SQLite for the demo. Same shape as the real Postgres schema, minus the parts
only AWS needs. The UNIQUE constraints are real — duplicates are rejected by the
database, not by Python."""

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("DEMO_DB", Path(__file__).resolve().parent.parent / "demo.sqlite3"))

_local = threading.local()

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS conversations (
  phone        TEXT PRIMARY KEY,
  display_name TEXT,
  created_at   TEXT NOT NULL
);

-- R3: idempotency lives in the database.
CREATE TABLE IF NOT EXISTS inbound_messages (
  wa_message_id TEXT PRIMARY KEY,
  phone         TEXT NOT NULL,
  body          TEXT,
  kind          TEXT NOT NULL,
  received_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS carts (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  phone            TEXT NOT NULL,
  state            TEXT NOT NULL DEFAULT 'OPEN',
  -- Set when this cart was opened as a SEPARATE order while an earlier order
  -- was already locked (order-modification.md, window 3).
  related_order_id INTEGER,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cart_items (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  cart_id     INTEGER NOT NULL REFERENCES carts(id),
  item_id     TEXT NOT NULL,
  name        TEXT NOT NULL,
  qty         INTEGER NOT NULL,
  unit_price  INTEGER NOT NULL,
  modifiers   TEXT NOT NULL DEFAULT '[]',
  note        TEXT,
  line_total  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  code             TEXT NOT NULL UNIQUE,
  phone            TEXT NOT NULL,
  cart_id          INTEGER NOT NULL REFERENCES carts(id),
  state            TEXT NOT NULL,
  fulfilment_type  TEXT NOT NULL,
  address          TEXT,
  zone             TEXT,
  subtotal         INTEGER NOT NULL,
  delivery_fee     INTEGER NOT NULL DEFAULT 0,
  total            INTEGER NOT NULL,
  -- Informational only. No rule and no transition reads it
  -- (order-modification.md).
  related_order_id INTEGER,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  edited_at        TEXT
);

CREATE TABLE IF NOT EXISTS order_items (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id   INTEGER NOT NULL REFERENCES orders(id),
  item_id    TEXT NOT NULL,
  name       TEXT NOT NULL,
  qty        INTEGER NOT NULL,
  unit_price INTEGER NOT NULL,
  modifiers  TEXT NOT NULL DEFAULT '[]',
  note       TEXT,
  line_total INTEGER NOT NULL
);

-- Append-only. Nothing ever updates or deletes a row here.
CREATE TABLE IF NOT EXISTS order_transitions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id    INTEGER NOT NULL REFERENCES orders(id),
  from_state  TEXT,
  to_state    TEXT NOT NULL,
  actor       TEXT NOT NULL,
  reason      TEXT,
  at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id           INTEGER NOT NULL UNIQUE REFERENCES orders(id),
  method             TEXT NOT NULL,
  transfer_amount    INTEGER NOT NULL DEFAULT 0,
  cash_amount        INTEGER NOT NULL DEFAULT 0,
  cash_received      INTEGER NOT NULL DEFAULT 0,
  -- Before Task C. Read once by the receipt migration in init(); nothing reads or
  -- writes them after that: overpayment_amount, status, flags, notes,
  -- unreviewed_proofs, proof_reference, proof_image and extracted.
  overpayment_amount INTEGER NOT NULL DEFAULT 0,
  status             TEXT NOT NULL DEFAULT 'PENDING',
  flags              TEXT NOT NULL DEFAULT '[]',
  notes              TEXT NOT NULL DEFAULT '[]',
  unreviewed_proofs  INTEGER NOT NULL DEFAULT 0,
  proof_reference    TEXT,
  proof_image        TEXT,
  extracted          TEXT,
  updated_at         TEXT NOT NULL
);

-- One row per payment receipt (Task C). A repeated reference is rejected by the
-- database (payments.md §3); the code tells a resend from a reuse afterwards.
-- A discarded receipt keeps its reference, so it still blocks reuse.
CREATE TABLE IF NOT EXISTS payment_receipts (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id         INTEGER NOT NULL REFERENCES orders(id),
  status           TEXT NOT NULL,          -- PROOF_RECEIVED | FLAGGED | CONFIRMED | DISCARDED
  confirmed_amount INTEGER,                -- only a person sets it (confirm_receipt)
  flags            TEXT NOT NULL DEFAULT '[]',
  notes            TEXT NOT NULL DEFAULT '[]',
  reference        TEXT,
  extracted        TEXT,                   -- the model's raw OCR JSON
  received_at      TEXT NOT NULL,          -- when the message arrived
  confirmed_at     TEXT,
  discarded_at     TEXT,
  discard_reason   TEXT                    -- staff's words; the customer is told them
);

CREATE UNIQUE INDEX IF NOT EXISTS receipts_reference_unique
  ON payment_receipts(reference) WHERE reference IS NOT NULL;

-- The photo of each receipt. Kept apart so no query that lists receipts ever
-- loads image bytes.
CREATE TABLE IF NOT EXISTS receipt_photos (
  receipt_id INTEGER PRIMARY KEY REFERENCES payment_receipts(id),
  mime       TEXT NOT NULL,
  data       BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS outbound_messages (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  phone    TEXT NOT NULL,
  body     TEXT NOT NULL,
  kind     TEXT NOT NULL DEFAULT 'text',
  status   TEXT NOT NULL,
  error    TEXT,
  sent_at  TEXT NOT NULL
);

-- A person takes over (Task B: an alert on the card; Task D: Chatwoot).
CREATE TABLE IF NOT EXISTS handoff_requests (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  phone       TEXT NOT NULL,
  order_id    INTEGER NOT NULL REFERENCES orders(id),
  summary     TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  resolved_at TEXT
);
"""


def now() -> str:
    """Microseconds, not seconds. Two messages can land in the same second, and
    then the simulator cannot tell which came first when it rebuilds a thread
    from two different tables. Still sorts correctly as a string against a
    seconds-precision bound."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def conn() -> sqlite3.Connection:
    if getattr(_local, "conn", None) is None:
        connection = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        _local.conn = connection
    return _local.conn


MIGRATIONS = [
    ("carts", "related_order_id", "ALTER TABLE carts ADD COLUMN related_order_id INTEGER"),
    ("orders", "related_order_id", "ALTER TABLE orders ADD COLUMN related_order_id INTEGER"),
    ("payments", "cash_received",
     "ALTER TABLE payments ADD COLUMN cash_received INTEGER NOT NULL DEFAULT 0"),
    ("payments", "overpayment_amount",
     "ALTER TABLE payments ADD COLUMN overpayment_amount INTEGER NOT NULL DEFAULT 0"),
    ("payments", "notes", "ALTER TABLE payments ADD COLUMN notes TEXT NOT NULL DEFAULT '[]'"),
    ("payments", "unreviewed_proofs",
     "ALTER TABLE payments ADD COLUMN unreviewed_proofs INTEGER NOT NULL DEFAULT 0"),
    ("orders", "edited_at", "ALTER TABLE orders ADD COLUMN edited_at TEXT"),
    ("payment_receipts", "discard_reason",
     "ALTER TABLE payment_receipts ADD COLUMN discard_reason TEXT"),
]


def init() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    database = conn()
    database.executescript(SCHEMA)
    # A database created before these columns existed must not need deleting.
    for table, column, statement in MIGRATIONS:
        existing = {row["name"] for row in database.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            database.execute(statement)
    # Task B removed the preparation step. A row an older run left there would sit
    # in no column with no button, so it goes back to Recibidos. order_transitions
    # is append-only and keeps its history untouched.
    database.execute("UPDATE orders SET state='ACCEPTED' WHERE state='IN_PREPARATION'")
    # Task C: a payment stored before per-receipt rows becomes its order's first
    # receipt. It has no photo: nothing stored receipt images before Task C.
    #
    # confirmed_amount, for a legacy CONFIRMED payment:
    #  - cash_received = 0: no cash was ever registered from the panel (the panel
    #    only offers it while something is owed, and the amount is required), so
    #    the legacy overpayment_amount is all transfer surplus and belongs to the
    #    receipt: transfer_amount + overpayment_amount.
    #  - otherwise the legacy surplus can't be told apart from cash, so it is
    #    dropped, and confirmed_amount never includes cash. A legacy transfer
    #    surplus on such an order is dropped with it.
    # Idempotent: the NOT EXISTS guard, and new code never writes payments.status,
    # so a payment created after Task C stays PENDING and is never picked up.
    database.execute("""
        INSERT INTO payment_receipts(order_id, status, confirmed_amount, flags, notes,
                                     reference, extracted, received_at, confirmed_at)
        SELECT p.order_id, p.status,
               CASE WHEN p.status = 'CONFIRMED' THEN
                    CASE WHEN p.cash_received = 0 THEN p.transfer_amount + p.overpayment_amount
                         ELSE p.transfer_amount END
               END,
               p.flags, p.notes, p.proof_reference, p.extracted, p.updated_at,
               CASE WHEN p.status = 'CONFIRMED' THEN p.updated_at END
        FROM payments p
        WHERE p.status IN ('PROOF_RECEIVED', 'FLAGGED', 'CONFIRMED')
          AND NOT EXISTS (SELECT 1 FROM payment_receipts r WHERE r.order_id = p.order_id)
    """)
    database.execute("DROP INDEX IF EXISTS payments_reference_unique")
    database.commit()


def reset() -> None:
    """Wipe everything. Used before a demo run so reports start clean."""
    database = conn()
    for table in (
        "outbound_messages", "handoff_requests", "receipt_photos", "payment_receipts",
        "payments", "order_transitions",
        "order_items", "orders", "cart_items", "carts", "inbound_messages", "conversations",
    ):
        database.execute(f"DELETE FROM {table}")
    database.commit()


# ---- small helpers --------------------------------------------------

def q(sql: str, args=()) -> list[sqlite3.Row]:
    return conn().execute(sql, args).fetchall()


def q1(sql: str, args=()):
    return conn().execute(sql, args).fetchone()


def run(sql: str, args=()):
    """Always leaves the connection clean. A rejected INSERT (the UNIQUE
    constraints are load-bearing here) must roll back, or this thread keeps the
    write lock and every later query fails with 'database is locked'."""
    database = conn()
    try:
        cursor = database.execute(sql, args)
    except Exception:
        database.rollback()
        raise
    if not getattr(_local, "in_tx", False):
        database.commit()
    return cursor


@contextmanager
def transaction():
    """Several writes that land together or not at all. Inside the block run()
    does not commit; the block commits once at the end, or rolls back on any
    exception - so a failure half-way leaves nothing behind and no write lock.
    Code inside must not catch a failed run() and carry on: run() has already
    rolled the whole transaction back by then."""
    if getattr(_local, "in_tx", False):     # already inside one: join it
        yield
        return
    database = conn()
    _local.in_tx = True
    try:
        yield
    except BaseException:
        database.rollback()
        raise
    else:
        database.commit()
    finally:
        _local.in_tx = False


def jloads(value, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default
