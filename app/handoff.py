"""A person takes over (Task B: an alert on the kitchen card; Task D: this also
opens the Chatwoot handoff and silences the bot for that customer until the
human closes it)."""

from . import db


def request(phone: str, order_id: int, summary: str) -> None:
    existing = db.q1(
        "SELECT id FROM handoff_requests WHERE order_id=%s AND summary=%s AND resolved_at IS NULL",
        (order_id, summary),
    )
    if existing:
        return
    db.run(
        "INSERT INTO handoff_requests(phone, order_id, summary, created_at) VALUES(%s,%s,%s,%s)",
        (phone, order_id, summary, db.now()),
    )


def resolve(order_id: int) -> int:
    cursor = db.run(
        "UPDATE handoff_requests SET resolved_at=%s WHERE order_id=%s AND resolved_at IS NULL",
        (db.now(), order_id),
    )
    return cursor.rowcount


def open_for(order_id: int) -> list[dict]:
    return [
        {"id": row["id"], "summary": row["summary"], "created_at": row["created_at"]}
        for row in db.q(
            "SELECT id, summary, created_at FROM handoff_requests"
            " WHERE order_id=%s AND resolved_at IS NULL ORDER BY id",
            (order_id,),
        )
    ]
