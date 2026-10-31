"""A customer's change to an order the restaurant already accepted (Task E).

Before acceptance a change edits the order in place (orders.amend). After it, the bot
builds a proposal - the target the order would become - and the customer confirms it:

- AUTO: while ACCEPTED, only adds products the menu marks add_after_accept, with no
  modifiers or note and the same delivery. Applied on the customer's «sí».
- APPROVAL: anything else. A request on the card that staff accept or reject.

One open request per order (a unique index). `revision` goes up every time its target
changes, and staff act only on the revision they saw. Every write here runs in the
caller's transaction, together with the order change and its queued notice."""

import json

from . import cart, db, orders
from .catalog import CATALOG
from .money import fmt

OPEN = ("PROPOSED", "PENDING")

MODIFIED = "El cliente modificó la solicitud. Revísela otra vez."
RESOLVED = "Esta solicitud ya fue resuelta."
BASE_CHANGED = ("El pedido cambió después de la solicitud. Recházela y pídale al cliente que "
                "la repita.")
STATE_CHANGED = "El pedido o su pago cambió mientras lo revisaba. Revíselo otra vez."
DRIVER_REQUIRED = ("Marque que ya coordinó con el domiciliario la dirección, los productos y "
                   "el cobro.")
REASON_REQUIRED = "Escriba el motivo para avisarle al cliente."
# Customer messages (code-built, FIXED notices).
REJECTED_TEMPLATE = "El local no pudo hacer el cambio que pidió. Motivo: {reason}. Su pedido sigue igual."
NOT_DONE_DELIVERED = "El cambio que pidió no se alcanzó a hacer: su pedido ya fue entregado."


class Conflict(Exception):
    """HTTP 409: what staff saw is no longer what is there."""


class Invalid(Exception):
    """HTTP 400."""


def _row(raw) -> dict | None:
    if raw is None:
        return None
    row = dict(raw)
    row["target"] = db.jloads(row["target"], {})
    row["diff"] = db.jloads(row["diff"], [])
    return row


def get(change_id: int) -> dict | None:
    return _row(db.q1("SELECT * FROM change_requests WHERE id=%s", (change_id,)))


def open_for(order_id: int) -> dict | None:
    return _row(db.q1(
        "SELECT * FROM change_requests WHERE order_id=%s AND status IN ('PROPOSED','PENDING')",
        (order_id,),
    ))


def last_resolved(order_id: int) -> dict | None:
    """The newest request the restaurant decided (applied or rejected)."""
    return _row(db.q1(
        "SELECT * FROM change_requests WHERE order_id=%s AND route='APPROVAL'"
        " AND status IN ('APPLIED','REJECTED') ORDER BY resolved_at DESC, id DESC LIMIT 1",
        (order_id,),
    ))


def board(order_id: int) -> dict:
    """What the card shows: the open request, and automatic additions nobody saw yet."""
    request = open_for(order_id)
    added = [_row(r) for r in db.q(
        "SELECT * FROM change_requests WHERE order_id=%s AND route='AUTO' AND status='APPLIED'"
        " AND seen_at IS NULL ORDER BY id",
        (order_id,),
    )]
    public = ("id", "status", "revision", "route", "diff", "current_total", "proposed_total",
              "created_at", "updated_at", "confirmed_at", "resolved_at")
    show = request is not None and (
        request["status"] == "PENDING" or request["confirmed_at"] is not None
    )
    return {
        "request": {k: request[k] for k in public} if show else None,
        "added": [{k: row[k] for k in public} for row in added],
    }


# ---- what a change is -----------------------------------------------------------

def _label(key: tuple, note: str | None) -> str:
    item_id, modifier_ids, _ = key
    item = CATALOG.item(item_id) or {}
    names = [CATALOG.modifier(m)["name"] for m in modifier_ids if CATALOG.modifier(m)]
    extras = f" ({', '.join(names)})" if names else ""
    return f"{item.get('name', item_id)}{extras}" + (f" — {note}" if note else "")


def _notes(lines: list[dict]) -> dict:
    return {orders._key(line): line.get("note") for line in lines}


def _delivery_changed(order: dict, target: dict) -> bool:
    return not orders.same_target({**target, "lines": []}, {**orders.target_of(order), "lines": []})


def describe(order: dict, target: dict) -> list[str]:
    """The change in words, for the card and the bot: code-built, no prices."""
    before, after = orders.line_counts(orders.order_lines(order)), orders.line_counts(target["lines"])
    notes = {**_notes(orders.order_lines(order)), **_notes(target["lines"])}
    out = []
    for key, qty in after.items():
        if key not in before:
            out.append(f"Agregar {qty}× {_label(key, notes.get(key))}")
    for key, qty in before.items():
        if key not in after:
            out.append(f"Quitar {qty}× {_label(key, notes.get(key))}")
        elif after[key] != qty:
            out.append(f"{_label(key, notes.get(key))}: de {qty} a {after[key]}")
    if _delivery_changed(order, target):
        if target["fulfilment"] == "DELIVERY":
            place = target.get("address") or "la dirección registrada"
            zone = f" ({target['zone']})" if target.get("zone") else ""
            out.append(f"Entrega: a domicilio en {place}{zone}")
        else:
            out.append("Entrega: recoger en el local")
    return out


def added_lines(order: dict, target: dict) -> list[tuple[int, str, int]]:
    """(qty, product, price of those units) for what an AUTO change adds."""
    before, after = orders.line_counts(orders.order_lines(order)), orders.line_counts(target["lines"])
    out = []
    for key, qty in after.items():
        extra = qty - before.get(key, 0)
        if extra > 0:
            out.append((extra, _label(key, None), CATALOG.price_of(key[0]) * extra))
    return out


def route_for(order: dict, target: dict) -> str:
    """AUTO only while ACCEPTED, for additions of authorized products that are
    available, with no modifiers or note, and the same delivery. Anything else, or a
    mix, is one request for approval."""
    if order["state"] != "ACCEPTED" or _delivery_changed(order, target):
        return "APPROVAL"
    before, after = orders.line_counts(orders.order_lines(order)), orders.line_counts(target["lines"])
    if any(after.get(key, 0) < qty for key, qty in before.items()):
        return "APPROVAL"
    for key, qty in after.items():
        if qty > before.get(key, 0):
            item_id, modifier_ids, note = key
            if (modifier_ids or note or not CATALOG.can_add_after_accept(item_id)
                    or not CATALOG.is_available(item_id)):
                return "APPROVAL"
    return "AUTO"


# ---- the bot's side ---------------------------------------------------------------

def propose(order: dict, phone: str, mutate=None, fulfilment=None, address=None, zone=None) -> dict:
    """The customer's change on top of the open request (else the order), staged in the
    customer's cart slot (window 3 has no open cart, like amend) and then set aside as
    PROPOSAL. Stores or updates the single open request as PROPOSED. Returns
    {"kind": "same" | "AUTO" | "APPROVAL", "row", "view", "target"}."""
    if cart.peek_open_cart(phone) is not None:
        raise ValueError("an open cart exists; window 1 wins")
    current = open_for(order["id"])
    base = current["target"] if current else orders.target_of(order)
    staging = cart.open_cart(phone)
    cart.fill_lines(staging, base["lines"])
    if mutate is not None:
        mutate()
    fulfilment = fulfilment or base["fulfilment"]
    if fulfilment == "DELIVERY":
        address, zone = address or base.get("address"), zone or base.get("zone")
    else:
        address = zone = None
    orders.refuse_pickup_on_the_way(order, fulfilment)
    view = cart.summary(phone, zone=zone, fulfilment=fulfilment)
    orders._check_view(view)
    target = {"lines": cart.cart_lines(staging), "fulfilment": fulfilment,
              "address": address, "zone": view["zone"]}
    db.run("UPDATE carts SET state='PROPOSAL', updated_at=%s WHERE id=%s", (db.now(), staging))

    if orders.same_target(target, orders.target_of(order)):
        if current:
            discard(current, "customer")
        return {"kind": "same", "row": None, "view": view, "target": target}
    route = route_for(order, target)
    fields = (route, json.dumps(target, ensure_ascii=False),
              json.dumps(describe(order, target), ensure_ascii=False),
              orders.state_token(order, with_payment=False), order["total"], view["total"], db.now())
    if current:
        db.run(
            "UPDATE change_requests SET status='PROPOSED', revision=revision+1, route=%s, target=%s,"
            " diff=%s, base_token=%s, current_total=%s, proposed_total=%s, updated_at=%s"
            " WHERE id=%s",
            (*fields, current["id"]),
        )
    else:
        db.run(
            "INSERT INTO change_requests(order_id, status, route, target, diff, base_token,"
            " current_total, proposed_total, updated_at, created_at)"
            " VALUES(%s,'PROPOSED',%s,%s,%s,%s,%s,%s,%s,%s)",
            (order["id"], *fields, db.now()),
        )
    return {"kind": route, "row": open_for(order["id"]), "view": view, "target": target}


def auto_is_stale(order: dict, row: dict) -> bool:
    """An AUTO proposal the customer is confirming no longer fits: the order changed,
    moved past ACCEPTED, or a product is no longer authorized or available."""
    return (row["base_token"] != orders.state_token(order, with_payment=False)
            or order["state"] != "ACCEPTED" or route_for(order, row["target"]) != "AUTO")


def apply_auto(order: dict, row: dict) -> tuple[dict, dict]:
    applied, change = orders.apply_target(order["id"], row["target"], ("ACCEPTED",), "customer",
                                          "PROPOSAL")
    _resolve(row, "PROPOSED", "APPLIED", "customer")
    return applied, change


def send_to_restaurant(row: dict) -> None:
    cursor = db.run(
        "UPDATE change_requests SET status='PENDING', confirmed_at=%s, updated_at=%s"
        " WHERE id=%s AND status='PROPOSED' AND revision=%s",
        (db.now(), db.now(), row["id"], row["revision"]),
    )
    if cursor.rowcount == 0:
        raise Conflict(MODIFIED)


def discard(row: dict, by: str, reason: str | None = None) -> None:
    db.run(
        "UPDATE change_requests SET status='DISCARDED', resolved_at=%s, resolved_by=%s, reason=%s"
        " WHERE id=%s AND status IN ('PROPOSED','PENDING')",
        (db.now(), by, reason, row["id"]),
    )


def _resolve(row: dict, from_status: str, to_status: str, by: str, reason: str | None = None) -> None:
    """Guarded on the status and revision the caller saw: a double click, a second tab
    or a request the customer changed meanwhile never resolves twice."""
    cursor = db.run(
        "UPDATE change_requests SET status=%s, resolved_at=%s, resolved_by=%s, reason=%s"
        " WHERE id=%s AND status=%s AND revision=%s",
        (to_status, db.now(), by, reason, row["id"], from_status, row["revision"]),
    )
    if cursor.rowcount == 0:
        raise Conflict(MODIFIED)


# ---- the restaurant's side -------------------------------------------------------

def _pending(order_id: int, change_id: int, revision) -> dict:
    row = get(change_id)
    if row is None or row["order_id"] != order_id:
        raise Conflict(RESOLVED)
    if row["status"] != "PENDING" or row["revision"] != revision:
        raise Conflict(MODIFIED if row["status"] in OPEN else RESOLVED)
    return row


def accept(order_id: int, change_id: int, revision, token: str | None = None,
           driver_ok: bool = False) -> tuple[dict, dict, str]:
    """Staff accept the request they saw (`revision`). Preview passes no `token`;
    a save passes the one its preview returned. Returns (order, change, the full token
    of the order before the change)."""
    row = _pending(order_id, change_id, revision)
    order = orders.get(order_id)
    if order["state"] not in orders.ON_BOARD:
        raise Conflict(f"Este pedido ya no se puede cambiar: está {orders.STATE_WORDS[order['state']]}.")
    if orders.state_token(order, with_payment=False) != row["base_token"]:
        raise Conflict(BASE_CHANGED)
    before = orders.state_token(order)
    if token is not None:
        if token != before:
            raise Conflict(STATE_CHANGED)
        if order["state"] == "OUT_FOR_DELIVERY" and not driver_ok:
            raise Invalid(DRIVER_REQUIRED)
    orders.refuse_pickup_on_the_way(order, row["target"]["fulfilment"])
    applied, change = orders.apply_target(order_id, row["target"], orders.ON_BOARD, "staff",
                                          "PROPOSAL")
    _resolve(row, "PENDING", "APPLIED", "staff")
    return applied, change, before


def reject(order_id: int, change_id: int, revision, reason: str | None) -> dict:
    reason = (reason or "").strip()
    if not reason:
        raise Invalid(REASON_REQUIRED)
    row = _pending(order_id, change_id, revision)
    _resolve(row, "PENDING", "REJECTED", "staff", reason)
    return get(change_id)


def seen(order_id: int, change_id: int) -> None:
    db.run(
        "UPDATE change_requests SET seen_at=%s WHERE id=%s AND order_id=%s AND route='AUTO'"
        " AND status='APPLIED'",
        (db.now(), change_id, order_id),
    )


def close_for_order(order_id: int, by: str, reason: str) -> list[dict]:
    """The order ended, or staff changed it with Editar: its open request closes without
    being applied. Returns the closed rows as they were (their status before)."""
    rows = [_row(r) for r in db.q(
        "SELECT * FROM change_requests WHERE order_id=%s AND status IN ('PROPOSED','PENDING')",
        (order_id,),
    )]
    for row in rows:
        db.run(
            "UPDATE change_requests SET status='CLOSED', resolved_at=%s, resolved_by=%s, reason=%s"
            " WHERE id=%s",
            (db.now(), by, reason, row["id"]),
        )
    return rows


def money_line(row: dict) -> str:
    return f"Total: {fmt(row['current_total'])} → {fmt(row['proposed_total'])}"
