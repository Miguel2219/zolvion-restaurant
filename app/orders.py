"""The order state machine, trimmed to what the demo shows but with the real
transition table. Every illegal transition raises."""

import hashlib
import json
import math
import random
import string
from datetime import datetime, timedelta, timezone

from . import db, handoff, payments
from .cart import (
    _norm_note, fill_from_order, fill_lines, open_cart, order_lines, peek_open_cart,
    related_order_of, staging_cart, summary, summary_of,
)
from .catalog import _fold
from .money import total_of

# (from_state, event) -> (to_state, actor)
TRANSITIONS = {
    ("PENDING_CONFIRMATION", "customer_confirms"): ("CONFIRMED", "customer"),
    ("PENDING_CONFIRMATION", "customer_declines"): ("CANCELLED", "customer"),
    ("PENDING_CONFIRMATION", "expire"): ("EXPIRED", "system"),
    ("CONFIRMED", "staff_accepts"): ("ACCEPTED", "staff"),
    ("CONFIRMED", "staff_rejects"): ("REJECTED", "staff"),
    ("CONFIRMED", "customer_cancels"): ("CANCELLED", "customer"),
    ("ACCEPTED", "food_ready"): ("READY", "staff"),
    ("ACCEPTED", "staff_cancels"): ("CANCELLED", "staff"),
    ("READY", "mark_in_error"): ("ACCEPTED", "staff"),
    ("READY", "hand_over"): ("COMPLETED", "staff"),
    ("READY", "driver_leaves"): ("OUT_FOR_DELIVERY", "staff"),
    ("OUT_FOR_DELIVERY", "delivered"): ("COMPLETED", "staff"),
}

TERMINAL = {"COMPLETED", "CANCELLED", "REJECTED", "EXPIRED"}

# A state as staff read it in an error message (apply_event). Not what the customer reads:
# brain.STATE_LABELS is for that.
STATE_WORDS = {
    "PENDING_CONFIRMATION": "esperando la confirmación del cliente",
    "CONFIRMED": "confirmado por el cliente y sin aceptar",
    "ACCEPTED": "aceptado por el local",
    "READY": "listo",
    "OUT_FOR_DELIVERY": "en camino",
    "COMPLETED": "entregado",
    "CANCELLED": "cancelado",
    "REJECTED": "rechazado",
    "EXPIRED": "vencido",
}

# order-modification.md
EDITABLE = ("PENDING_CONFIRMATION", "CONFIRMED")          # window 2 - orders.amend, in place
LOCKED = ("ACCEPTED", "READY", "OUT_FOR_DELIVERY")        # window 3 - _handoff, to a person

# The states that have a card on the kitchen panel: the columns in static/kitchen.html
# COLS. PENDING_CONFIRMATION has no column, so a handoff box on it would be invisible.
ON_BOARD = ("CONFIRMED",) + LOCKED


class IllegalTransition(Exception):
    pass


class NotEditable(Exception):
    """Staff accepted the order between the check and the write."""


class EmptyEdit(Exception):
    """The change would leave the order with no items."""


class ChangeRefused(Exception):
    """A change that cannot be made as asked (Task E). The text is Spanish, for staff or
    for the bot to say."""


ON_THE_WAY_PICKUP = "El pedido ya va en camino: no se puede cambiar a recoger en el local."


def _code() -> str:
    return "P-" + "".join(random.choices(string.digits, k=4))


def modification_window(phone: str) -> tuple[int, dict | None]:
    """Which window a change request falls into (order-modification.md).

    1 — an OPEN cart exists; it always wins, even next to a live order.
    2 — no open cart, order in PENDING_CONFIRMATION or CONFIRMED: amend() changes
        it in place, same row, same code.
    3 — no open cart, order ACCEPTED..OUT_FOR_DELIVERY: locked, hand off to a
        person via _handoff (brain.py) instead of changing it.
    0 — nothing live. Terminal orders are not window 3.
    """
    if peek_open_cart(phone) is not None:
        return 1, None
    editable = latest_for(phone, list(EDITABLE))
    if editable:
        return 2, editable
    locked = latest_for(phone, list(LOCKED))
    if locked:
        return 3, locked
    return 0, None


def create_from_cart(phone: str, fulfilment: str, address=None, zone=None) -> dict:
    """Freeze the cart into an order. Totals recomputed here, from the catalog.
    The order, its lines, its first transition, the cart and the payment land
    together or not at all."""
    if fulfilment not in ("PICKUP", "DELIVERY"):
        raise ValueError("fulfilment must be PICKUP or DELIVERY")
    with db.transaction():
        view = summary(phone, zone=zone, fulfilment=fulfilment)
        if view["empty"]:
            raise ValueError("cannot create an order from an empty cart")
        # Invariant 6, checked BEFORE anything is written. An explicit raise,
        # not assert: python -O must not strip a guard that protects a write.
        if total_of([line["line_total"] for line in view["lines"]], view["delivery_fee"]) != view["total"]:
            raise ValueError("total does not match its line items")

        cart_id = view["cart_id"]
        related = related_order_of(cart_id)
        code = _code()
        while db.q1("SELECT 1 FROM orders WHERE code=%s", (code,)):
            code = _code()

        # The Chatwoot conversation it is made in (none outside Chatwoot mode): the
        # panel's "ver conversación" link. The pin the customer shared while building
        # this cart goes with it (attach_location), and only with it.
        order_id = db.insert(
            """INSERT INTO orders(code, phone, cart_id, state, fulfilment_type, address, zone,
                                  subtotal, delivery_fee, total, related_order_id,
                                  created_at, updated_at, chatwoot_conversation_id, location)
               VALUES(%s,%s,%s,'PENDING_CONFIRMATION',%s,%s,%s,%s,%s,%s,%s,%s,%s,
                      (SELECT chatwoot_conversation_id FROM conversations WHERE phone=%s),
                      (SELECT location FROM carts WHERE id=%s))
               RETURNING id""",
            (
                code, phone, cart_id, fulfilment, address, view["zone"],
                view["subtotal"], view["delivery_fee"], view["total"], related,
                db.now(), db.now(), phone, cart_id,
            ),
        )

        _write_items(order_id, view["lines"])
        _log(order_id, None, "PENDING_CONFIRMATION", "system", "order created")
        db.run("UPDATE carts SET state='CONVERTED', updated_at=%s WHERE id=%s", (db.now(), cart_id))
        db.run(
            "INSERT INTO payments(order_id, method, transfer_amount, cash_amount, updated_at)"
            " VALUES(%s,'UNSPECIFIED',0,0,%s)",
            (order_id, db.now()),
        )
    return get(order_id)


def _write_items(order_id: int, lines: list[dict]) -> None:
    for line in lines:
        db.run(
            """INSERT INTO order_items(order_id, item_id, name, qty, unit_price, modifiers, note, line_total)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                order_id, line["item_id"], line["name"], line["qty"],
                line["unit_price"], json.dumps(line["modifiers"]),
                line["note"], line["line_total"],
            ),
        )


def amend(
    order_id: int, phone: str, mutate=None, fulfilment=None, address=None, zone=None
) -> tuple[dict, dict]:
    """Change a not-yet-accepted order IN PLACE: same row, same code, same card.

    The order's lines go into a staging cart, `mutate` (a cart function) runs on
    it, and cart.summary() - still the only producer of totals - is written back.
    Each edit starts from the order itself, so the cart can never drift from it.
    All of it is one transaction: if anything fails, nothing is saved - not the
    staging cart, not the order, not its items, not the payment.
    Returns (order, payment change from payments.follow_total)."""
    with db.transaction():
        if peek_open_cart(phone) is not None:
            raise ValueError("an open cart exists; window 1 wins")
        order = get(order_id)
        if order["state"] not in EDITABLE:
            raise NotEditable(order["state"])
        # fill_from_order re-prices lines from the current catalog, which only
        # matters if prices change mid-order.
        staging = fill_from_order(phone, order)
        if mutate is not None:
            mutate()
        fulfilment = fulfilment or order["fulfilment_type"]
        if fulfilment == "DELIVERY":
            address, zone = address or order["address"], zone or order["zone"]
        else:
            address = zone = None
        view = summary(phone, zone=zone, fulfilment=fulfilment)
        _check_view(view)
        _write_back(order_id, staging, fulfilment, address, view, EDITABLE, "customer")
        # The "accepted meanwhile" race cannot happen today: bot turns run in the
        # worker thread and staff buttons on the server's loop, but each tool call and
        # each panel write holds db.write_lock, so they still write one at a time. The
        # guarded UPDATE ... WHERE state IN (...) stays as a defense in case that
        # changes, and the transaction makes its outcome clean either way.
        #
        # follow_total sits INSIDE the transaction on purpose: an order total that
        # moved without its payment split would be the half-saved state this
        # transaction rules out. It only uses db.run/db.q1 and plain raises, with
        # no catch-and-continue, so it is safe inside the block.
        change = payments.follow_total(order_id)
    return get(order_id), change


def _check_view(view: dict) -> None:
    if view["empty"]:
        raise EmptyEdit()
    # Invariant 6, checked BEFORE anything is written. An explicit raise,
    # not assert: python -O must not strip a guard that protects a write.
    if total_of([line["line_total"] for line in view["lines"]], view["delivery_fee"]) != view["total"]:
        raise ValueError("total does not match its line items")


def _write_back(order_id: int, staging: int, fulfilment: str, address, view: dict,
                states, edited_by: str) -> None:
    """The staged cart becomes the order's lines and totals: same row, same code, same
    card. Guarded by the states the change is allowed in: zero rows means the order
    moved meanwhile, and the caller's transaction rolls everything back."""
    marks = ",".join(["%s"] * len(states))
    cursor = db.run(
        f"UPDATE orders SET cart_id=%s, fulfilment_type=%s, address=%s, zone=%s, subtotal=%s,"
        f" delivery_fee=%s, total=%s, edited_at=%s, edited_by=%s, updated_at=%s"
        f" WHERE id=%s AND state IN ({marks})",
        (staging, fulfilment, address, view["zone"], view["subtotal"], view["delivery_fee"],
         view["total"], db.now(), edited_by, db.now(), order_id, *states),
    )
    if cursor.rowcount == 0:
        raise NotEditable("accepted meanwhile")
    db.run("DELETE FROM order_items WHERE order_id=%s", (order_id,))
    _write_items(order_id, view["lines"])
    db.run("UPDATE carts SET state='CONVERTED', updated_at=%s WHERE id=%s", (db.now(), staging))


# ---- Task E: changes after acceptance, the staff's Editar --------------------

def target_of(order: dict) -> dict:
    """What an order is, in the shape a change is written in: lines by item and
    modifier ids, and the delivery."""
    return {
        "lines": order_lines(order), "fulfilment": order["fulfilment_type"],
        "address": order.get("address"), "zone": order.get("zone"),
    }


def _key(line: dict) -> tuple:
    return (line["item_id"], tuple(sorted(line.get("modifier_ids") or [])), _norm_note(line.get("note")))


def line_counts(lines: list[dict]) -> dict:
    """{(item, modifiers, note): qty}: identical lines added up, as the cart merges them."""
    counts: dict = {}
    for line in lines:
        counts[_key(line)] = counts.get(_key(line), 0) + line["qty"]
    return counts


def same_target(a: dict, b: dict) -> bool:
    if line_counts(a["lines"]) != line_counts(b["lines"]) or a["fulfilment"] != b["fulfilment"]:
        return False
    if a["fulfilment"] != "DELIVERY":
        return True
    return (" ".join((a.get("address") or "").split()) == " ".join((b.get("address") or "").split())
            and _fold(a.get("zone") or "") == _fold(b.get("zone") or ""))


def staged_view(phone: str, target: dict, staging_state: str) -> tuple[int, dict, str, str | None]:
    """The target in a staging cart (never the customer's own): its code-made totals.
    (staging cart id, cart.summary_of view, fulfilment, address)."""
    staging = staging_cart(phone, staging_state)
    fill_lines(staging, target["lines"])
    fulfilment = target["fulfilment"]
    if fulfilment == "DELIVERY":
        address, zone = (target.get("address") or "").strip() or None, target.get("zone") or None
    else:
        address = zone = None
    view = summary_of(staging, zone=zone, fulfilment=fulfilment)
    _check_view(view)
    return staging, view, fulfilment, address


def apply_target(order_id: int, target: dict, states, edited_by: str,
                 staging_state: str = "STAFF_EDIT") -> tuple[dict, dict]:
    """Writes `target` onto the order, re-priced from the catalog, and moves the payment
    split with it (payments.follow_total). Runs in the caller's transaction: the
    order, its lines and its payment land together with whatever else the caller
    writes (the request's status, the queued notice). Returns (order, change)."""
    order = get(order_id)
    staging, view, fulfilment, address = staged_view(order["phone"], target, staging_state)
    _write_back(order_id, staging, fulfilment, address, view, states, edited_by)
    change = payments.follow_total(order_id)
    return get(order_id), change


def refuse_pickup_on_the_way(order: dict, fulfilment: str) -> None:
    if order["state"] == "OUT_FOR_DELIVERY" and fulfilment == "PICKUP":
        raise ChangeRefused(ON_THE_WAY_PICKUP)


def state_token(order: dict, with_payment: bool = True) -> str:
    """A fingerprint of what the order is (lines, delivery, totals) and, with_payment,
    of its payment (method, split, every receipt and cash row). A preview carries it;
    the save is refused when it changed, so staff never approve one state and write
    another (Task E review, gap 2). State moves are left out on purpose: the kitchen
    moving a card along does not change what staff reviewed."""
    content = {
        "items": [[i["item_id"], i["qty"], i["unit_price"], list(i.get("modifiers") or []),
                   i.get("note"), i["line_total"]] for i in order["items"]],
        "delivery": [order["fulfilment_type"], order.get("address"), order.get("zone"),
                     order["delivery_fee"], order["total"]],
    }
    if with_payment and order.get("payment"):
        pay = order["payment"]
        content["payment"] = {
            "split": [pay["method"], pay["transfer_amount"], pay["cash_amount"]],
            "receipts": [[r["id"], r["status"], r["confirmed_amount"]] for r in pay["receipts"]],
            "cash": [[c["id"], c["status"], c["amount"]] for c in pay["cash"]],
        }
    raw = json.dumps(content, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def clean_pin(pin) -> dict | None:
    """A shared WhatsApp location as code keeps it: finite coordinates in range, and a
    short name. Anything else is no pin at all."""
    if not isinstance(pin, dict):
        return None
    try:
        latitude, longitude = float(pin.get("latitude")), float(pin.get("longitude"))
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(latitude) and math.isfinite(longitude)
            and -90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    name = str(pin.get("name") or "").strip()[:120] or None
    return {"latitude": latitude, "longitude": longitude, "name": name}


def attach_location(phone: str, pin) -> str | None:
    """Task E: where a pin the customer shared belongs, decided when it arrives (whoever
    answers the chat): the open cart (it goes on the order made from it), else the
    newest live order (the newest pin wins), else a new open cart. Never searched for
    later, so it can never pass to a later order. Returns "cart", "order" or None."""
    clean = clean_pin(pin)
    if clean is None:
        return None
    data = json.dumps(clean, ensure_ascii=False)
    cart_id = peek_open_cart(phone)
    if cart_id is None:
        marks = ",".join(["%s"] * len(EDITABLE + LOCKED))
        live = db.q1(
            f"SELECT id FROM orders WHERE phone=%s AND state IN ({marks}) ORDER BY id DESC LIMIT 1",
            (phone, *EDITABLE, *LOCKED),
        )
        if live is not None:
            db.run("UPDATE orders SET location=%s WHERE id=%s", (data, live["id"]))
            return "order"
        cart_id = open_cart(phone)
    db.run("UPDATE carts SET location=%s, updated_at=%s WHERE id=%s", (data, db.now(), cart_id))
    return "cart"


def clear_location(order_id: int) -> None:
    db.run("UPDATE orders SET location=NULL WHERE id=%s", (order_id,))


def mark_edited(order_id: int) -> None:
    db.run("UPDATE orders SET edited_at=%s, edited_by='customer' WHERE id=%s", (db.now(), order_id))


def _log(order_id: int, from_state, to_state: str, actor: str, reason=None) -> None:
    db.run(
        "INSERT INTO order_transitions(order_id, from_state, to_state, actor, reason, at)"
        " VALUES(%s,%s,%s,%s,%s,%s)",
        (order_id, from_state, to_state, actor, reason, db.now()),
    )


def apply_event(order_id: int, event: str, reason=None) -> dict:
    row = db.q1("SELECT * FROM orders WHERE id=%s", (order_id,))
    if row is None:
        raise IllegalTransition("Ese pedido no existe.")
    current = row["state"]
    words = STATE_WORDS.get(current, current)
    if current in TERMINAL:
        raise IllegalTransition(f"El pedido ya está {words}; no se puede cambiar.")
    target = TRANSITIONS.get((current, event))
    if target is None:
        raise IllegalTransition(f"Esa acción ya no aplica: el pedido está {words}.")
    to_state, actor = target
    if to_state == "OUT_FOR_DELIVERY" and row["fulfilment_type"] != "DELIVERY":
        raise IllegalTransition("Solo un pedido a domicilio puede salir a domicilio.")
    db.run("UPDATE orders SET state=%s, updated_at=%s WHERE id=%s", (to_state, db.now(), order_id))
    _log(order_id, current, to_state, actor, reason)
    return get(order_id)


# ---- the time promised to the customer ---------------------------------

class BadMinutes(Exception):
    """Staff sent no usable number of minutes."""


BAD_MINUTES = "Elija en cuántos minutos estará listo el pedido."


def check_minutes(value) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 240:
        raise BadMinutes(BAD_MINUTES)
    return value


def set_ready_by(order_id: int, minutes: int) -> None:
    """The food will be ready `minutes` from now (staff's estimate, from the panel)."""
    when = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    db.run("UPDATE orders SET ready_by=%s WHERE id=%s", (when.isoformat(), order_id))


def ready_at(order: dict) -> datetime | None:
    if not order.get("ready_by"):
        return None
    return datetime.fromisoformat(order["ready_by"])


def minutes_text(minutes: int) -> str:
    """«aproximadamente 20 minutos»: the customer is told a duration, never a clock time.
    Code writes it; the bot only copies it (rule 1)."""
    return f"aproximadamente {minutes} {'minuto' if minutes == 1 else 'minutos'}"


def minutes_left(order: dict) -> int | None:
    """What is left of the staff's estimate, rounded up to 5 (never «faltan 13»); 0 once
    it has passed; None without an estimate."""
    when = ready_at(order)
    if when is None:
        return None
    seconds = (when - datetime.now(timezone.utc)).total_seconds()
    return 0 if seconds <= 0 else 5 * math.ceil(seconds / 300)


def get(order_id: int) -> dict:
    row = db.q1("SELECT * FROM orders WHERE id=%s", (order_id,))
    if row is None:
        raise KeyError(order_id)
    order = dict(row)
    order["location"] = db.jloads(order.get("location"), None)
    order["items"] = [
        {**dict(item), "modifiers": db.jloads(item["modifiers"], [])}
        for item in db.q("SELECT * FROM order_items WHERE order_id=%s ORDER BY id", (order_id,))
    ]
    if db.q1("SELECT 1 FROM payments WHERE order_id=%s", (order_id,)):
        order["payment"] = payments.current(order_id)
    order["transitions"] = [dict(t) for t in db.q(
        "SELECT * FROM order_transitions WHERE order_id=%s ORDER BY id", (order_id,)
    )]
    order["handoffs"] = handoff.open_for(order_id)
    return order


def latest_for(phone: str, states=None) -> dict | None:
    if states:
        marks = ",".join(["%s"] * len(states))
        row = db.q1(
            f"SELECT id FROM orders WHERE phone=%s AND state IN ({marks}) ORDER BY id DESC LIMIT 1",
            (phone, *states),
        )
    else:
        row = db.q1("SELECT id FROM orders WHERE phone=%s ORDER BY id DESC LIMIT 1", (phone,))
    return get(row["id"]) if row else None


def live_states(phone: str) -> list[str]:
    """The states of this phone's non-terminal orders, newest first - the
    per-phone version of board()."""
    marks = ",".join(["%s"] * len(TERMINAL))
    rows = db.q(
        f"SELECT state FROM orders WHERE phone=%s AND state NOT IN ({marks}) ORDER BY id DESC",
        (phone, *sorted(TERMINAL)),
    )
    return [row["state"] for row in rows]


def board() -> list[dict]:
    """Everything the kitchen screen shows: live orders newest first."""
    rows = db.q(
        "SELECT id FROM orders WHERE state NOT IN ('COMPLETED','CANCELLED','REJECTED','EXPIRED')"
        " ORDER BY id DESC"
    )
    return [get(row["id"]) for row in rows]


def reopen_cart_after_edit(phone: str) -> None:
    """cancel_order just cancelled the order; give the customer a fresh OPEN
    cart so a follow-up message starts a normal new order."""
    open_cart(phone)


def last_actor(order_id: int) -> str | None:
    """Who caused the current state. notifications-and-followups.md §1 reads
    this to decide whether a CANCELLED is announced — a customer who cancelled
    already knows."""
    row = db.q1(
        "SELECT actor FROM order_transitions WHERE order_id=%s ORDER BY id DESC LIMIT 1",
        (order_id,),
    )
    return row["actor"] if row else None


def last_reason(order_id: int) -> str | None:
    row = db.q1(
        "SELECT reason FROM order_transitions WHERE order_id=%s ORDER BY id DESC LIMIT 1",
        (order_id,),
    )
    return row["reason"] if row else None
