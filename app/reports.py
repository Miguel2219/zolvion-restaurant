"""The owner's screen. Every figure is a SQL aggregate over real rows."""

from datetime import datetime, timedelta, timezone

from . import db
from .catalog import CATALOG


def _local_zone():
    """The restaurant's own timezone (Catalog.timezone() — one copy of the
    zoneinfo-may-be-missing fallback)."""
    return CATALOG.timezone()


def _since(days: int) -> str:
    """'Hoy' means today's calendar day where the restaurant is, not the last
    24 hours. At 8pm a rolling window was still counting last night's orders."""
    zone = _local_zone()
    start_local = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    start_local -= timedelta(days=max(days, 1) - 1)
    return start_local.astimezone(timezone.utc).isoformat(timespec="seconds")


COUNTED = ("CONFIRMED", "ACCEPTED", "READY", "OUT_FOR_DELIVERY", "COMPLETED")


def snapshot(days: int = 1) -> dict:
    since = _since(days)
    marks = ",".join("?" * len(COUNTED))

    totals = db.q1(
        f"""SELECT COUNT(*) AS orders, COALESCE(SUM(total),0) AS revenue,
                   COALESCE(SUM(delivery_fee),0) AS delivery_fees
            FROM orders WHERE created_at >= ? AND state IN ({marks})""",
        (since, *COUNTED),
    )
    orders_count = totals["orders"] or 0
    revenue = totals["revenue"] or 0

    by_state = {
        row["state"]: row["n"]
        for row in db.q(
            "SELECT state, COUNT(*) AS n FROM orders WHERE created_at >= ? GROUP BY state",
            (since,),
        )
    }

    top_items = [
        dict(row)
        for row in db.q(
            f"""SELECT oi.name AS name, SUM(oi.qty) AS units, SUM(oi.line_total) AS revenue
                FROM order_items oi JOIN orders o ON o.id = oi.order_id
                WHERE o.created_at >= ? AND o.state IN ({marks})
                GROUP BY oi.name ORDER BY units DESC, revenue DESC LIMIT 5""",
            (since, *COUNTED),
        )
    ]

    by_method = [
        dict(row)
        for row in db.q(
            f"""SELECT p.method AS method, COUNT(*) AS n,
                       COALESCE(SUM(p.transfer_amount),0) AS transfer,
                       COALESCE(SUM(p.cash_amount),0) AS cash
                FROM payments p JOIN orders o ON o.id = p.order_id
                WHERE o.created_at >= ? AND o.state IN ({marks})
                GROUP BY p.method""",
            (since, *COUNTED),
        )
    ]

    fulfilment = [
        dict(row)
        for row in db.q(
            f"""SELECT fulfilment_type AS type, COUNT(*) AS n, COALESCE(SUM(total),0) AS revenue
                FROM orders WHERE created_at >= ? AND state IN ({marks})
                GROUP BY fulfilment_type""",
            (since, *COUNTED),
        )
    ]

    # Receipts, not orders, and all-time (no date filter). A discarded receipt
    # drops out through the status filter.
    flagged = db.q1(
        "SELECT COUNT(*) AS n FROM payment_receipts WHERE status='FLAGGED'"
    )["n"]
    awaiting = db.q1(
        "SELECT COUNT(*) AS n FROM payment_receipts WHERE status='PROOF_RECEIVED'"
    )["n"]

    lost = sum(by_state.get(s, 0) for s in ("CANCELLED", "REJECTED", "EXPIRED"))
    # Money that is one "sí" away. It was invisible before, which made the
    # revenue figure look wrong when orders were sitting unconfirmed.
    pending = db.q1(
        "SELECT COUNT(*) AS n, COALESCE(SUM(total),0) AS amount FROM orders"
        " WHERE created_at >= ? AND state='PENDING_CONFIRMATION'",
        (since,),
    )

    return {
        "window_days": days,
        "since": since,
        "pending_orders": pending["n"] or 0,
        "pending_amount": pending["amount"] or 0,
        "orders": orders_count,
        "revenue": revenue,
        "average_ticket": (revenue // orders_count) if orders_count else 0,
        "delivery_fees": totals["delivery_fees"] or 0,
        "by_state": by_state,
        "top_items": top_items,
        "by_method": by_method,
        "fulfilment": fulfilment,
        "receipts_flagged": flagged,
        "receipts_awaiting_confirmation": awaiting,
        "lost_orders": lost,
    }
