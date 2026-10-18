"""A person takes over (Task B: an alert on the kitchen card).

With Chatwoot (Task D) the box also hands the conversation to a person there: a
private note with the summary and the status "open". The bot keeps answering until
Chatwoot says open, so a box starts OPENING and the worker does the Chatwoot part
after the bot's reply went out (worker.sync_chatwoot), retrying every 30 s if
Chatwoot fails. Pending or resolved in Chatwoot closes the box."""

from . import chatwoot, db


def request(phone: str, order_id: int, summary: str) -> None:
    existing = db.q1(
        "SELECT id FROM handoff_requests WHERE order_id=%s AND summary=%s AND resolved_at IS NULL",
        (order_id, summary),
    )
    if existing:
        return
    in_chatwoot = chatwoot.enabled() and chatwoot.conversation_for(phone) is not None
    db.run(
        "INSERT INTO handoff_requests(phone, order_id, summary, created_at, chatwoot_state)"
        " VALUES(%s,%s,%s,%s,%s)",
        (phone, order_id, summary, db.now(), "OPENING" if in_chatwoot else "NONE"),
    )


def resolve(order_id: int) -> int:
    cursor = db.run(
        "UPDATE handoff_requests SET resolved_at=%s WHERE order_id=%s AND resolved_at IS NULL",
        (db.now(), order_id),
    )
    return cursor.rowcount


def close_for_phone(phone: str, states: tuple[str, ...]) -> int:
    """Chatwoot gave the conversation back (Pending) or closed it (resolved)."""
    marks = ",".join(["%s"] * len(states))
    cursor = db.run(
        f"UPDATE handoff_requests SET resolved_at=%s WHERE phone=%s AND resolved_at IS NULL"
        f" AND chatwoot_state IN ({marks})",
        (db.now(), phone, *states),
    )
    return cursor.rowcount


def in_chatwoot(order_id: int) -> bool:
    """An open box that handed (or is handing) the conversation to a person in Chatwoot."""
    return db.q1(
        "SELECT 1 FROM handoff_requests WHERE order_id=%s AND resolved_at IS NULL"
        " AND chatwoot_state <> 'NONE'",
        (order_id,),
    ) is not None


def open_for(order_id: int) -> list[dict]:
    return [
        {
            "id": row["id"], "summary": row["summary"], "created_at": row["created_at"],
            "chatwoot_state": row["chatwoot_state"],
            # The card's «Chatwoot sin actualizar»: the note or the "open" failed.
            "chatwoot_failed": row["chatwoot_state"] == "OPENING" and bool(row["chatwoot_error"]),
        }
        for row in db.q(
            "SELECT id, summary, created_at, chatwoot_state, chatwoot_error FROM handoff_requests"
            " WHERE order_id=%s AND resolved_at IS NULL ORDER BY id",
            (order_id,),
        )
    ]
