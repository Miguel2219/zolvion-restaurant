"""Cart operations. Every price is read from the catalog and every total is
computed here. Nothing that came out of the model is ever used as a number."""

import json

from . import db
from .catalog import CATALOG
from .money import fmt, total_of


class CartError(Exception):
    """Something the customer asked for cannot be done. The text is safe to send."""


def peek_open_cart(phone: str) -> int | None:
    """Is there an OPEN cart? Does NOT create one.

    This is the discriminator for order-modification.md: 'an open cart always
    wins'. open_cart() creates on miss, so it can never answer this."""
    row = db.q1(
        "SELECT id FROM carts WHERE phone=%s AND state='OPEN' ORDER BY id DESC LIMIT 1",
        (phone,),
    )
    return row["id"] if row else None


def open_cart(phone: str, related_order_id: int | None = None) -> int:
    row = db.q1(
        "SELECT id FROM carts WHERE phone=%s AND state='OPEN' ORDER BY id DESC LIMIT 1",
        (phone,),
    )
    if row:
        return row["id"]
    db.run(
        "INSERT INTO conversations(phone, created_at) VALUES(%s,%s) ON CONFLICT DO NOTHING",
        (phone, db.now()),
    )
    return db.insert(
        "INSERT INTO carts(phone, state, related_order_id, created_at, updated_at)"
        " VALUES(%s,'OPEN',%s,%s,%s) RETURNING id",
        (phone, related_order_id, db.now(), db.now()),
    )


def related_order_of(cart_id: int) -> int | None:
    row = db.q1("SELECT related_order_id FROM carts WHERE id=%s", (cart_id,))
    return row["related_order_id"] if row else None


def fill_from_order(phone: str, order: dict) -> int:
    """Window 2: the order is being edited, so its items go back into a fresh
    cart. Without this the customer would have to repeat the whole order."""
    cart_id = open_cart(phone)
    # A stored line keeps modifier names, in order. A modifier added twice is stored
    # twice ("Queso", "Queso") and must stay twice: walking the catalog instead would
    # keep it once and silently make the line cheaper.
    by_name = {m["name"]: m["id"] for m in CATALOG.raw["menu"].get("modifiers", [])}
    for item in order["items"]:
        modifier_ids = [by_name[n] for n in (item.get("modifiers") or []) if n in by_name]
        unit_price = CATALOG.price_of(item["item_id"]) + sum(
            CATALOG.modifier_price(m) for m in modifier_ids
        )
        db.run(
            """INSERT INTO cart_items(cart_id, item_id, name, qty, unit_price, modifiers, note, line_total)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                cart_id, item["item_id"], item["name"], item["qty"], unit_price,
                json.dumps(modifier_ids), item.get("note"),
                _line_total(item["item_id"], item["qty"], modifier_ids),
            ),
        )
    _touch(cart_id)
    return cart_id


def _touch(cart_id: int) -> None:
    db.run("UPDATE carts SET updated_at=%s WHERE id=%s", (db.now(), cart_id))


def _line_total(item_id: str, qty: int, modifier_ids: list[str]) -> int:
    unit = CATALOG.price_of(item_id)
    for modifier_id in modifier_ids:
        unit += CATALOG.modifier_price(modifier_id)
    return unit * qty


def _norm_note(note) -> str:
    """Notes are typed by the model: case and extra spaces must not split a line."""
    return " ".join((note or "").split()).casefold()


def _line_key(modifier_ids, note) -> tuple:
    # sorted, not set: a modifier added twice costs twice, so it is a different line
    return (tuple(sorted(modifier_ids or [])), _norm_note(note))


def _row_key(row) -> tuple:
    return _line_key(db.jloads(row["modifiers"], []), row["note"])


def _row_label(row) -> str:
    ids = sorted(db.jloads(row["modifiers"], []))
    return (f"{row['qty']}x {row['name']} (modifier_ids: {json.dumps(ids)}, "
            f"note: {json.dumps(row['note'] or '', ensure_ascii=False)})")


class LineChoiceNeeded(Exception):
    """The item sits on several different lines and the call did not say which."""
    def __init__(self, item_name: str, lines: list[str], matched_nothing: bool = False):
        super().__init__(item_name)
        self.item_name, self.lines, self.matched_nothing = item_name, lines, matched_nothing


def add_item(phone: str, item_id: str, qty: int = 1, modifier_ids=None, note=None) -> dict:
    modifier_ids = list(modifier_ids or [])
    item = CATALOG.item(item_id)
    if item is None:
        raise CartError(f"'{item_id}' no está en la carta.")
    if not CATALOG.is_available(item_id):
        raise CartError(f"{item['name']} está agotado hoy.")
    if not isinstance(qty, int) or qty < 1 or qty > 50:
        raise CartError("La cantidad debe estar entre 1 y 50.")
    for modifier_id in modifier_ids:
        modifier = CATALOG.modifier(modifier_id)
        if modifier is None:
            raise CartError(f"La adición '{modifier_id}' no existe.")
        if not CATALOG.modifier_allowed(modifier_id, item_id):
            raise CartError(f"{modifier['name']} no aplica para {item['name']}.")

    cart_id = open_cart(phone)
    rows = db.q(
        "SELECT * FROM cart_items WHERE cart_id=%s AND item_id=%s ORDER BY id",
        (cart_id, item_id),
    )
    key = _line_key(modifier_ids, note)
    match = next((row for row in rows if _row_key(row) == key), None)
    if match is not None:
        new_qty = match["qty"] + qty
        if new_qty > 50:
            raise CartError("La cantidad debe estar entre 1 y 50.")
        db.run(
            "UPDATE cart_items SET qty=%s, line_total=%s WHERE id=%s",
            (new_qty, _line_total(item_id, new_qty, modifier_ids), match["id"]),
        )
        _touch(cart_id)
        return summary(phone)

    unit_price = CATALOG.price_of(item_id) + sum(
        CATALOG.modifier_price(m) for m in modifier_ids
    )
    db.run(
        """INSERT INTO cart_items(cart_id, item_id, name, qty, unit_price, modifiers, note, line_total)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            cart_id, item_id, item["name"], qty, unit_price,
            json.dumps(modifier_ids), note, _line_total(item_id, qty, modifier_ids),
        ),
    )
    _touch(cart_id)
    return summary(phone)


def remove_item(phone: str, item_id: str, qty: int | None = None) -> dict:
    cart_id = open_cart(phone)
    rows = db.q(
        "SELECT * FROM cart_items WHERE cart_id=%s AND item_id=%s ORDER BY id DESC",
        (cart_id, item_id),
    )
    if not rows:
        item = CATALOG.item(item_id)
        label = item["name"] if item else item_id
        raise CartError(f"{label} no está en el pedido.")
    if len({_row_key(row) for row in rows}) > 1:
        raise LineChoiceNeeded(rows[0]["name"], [_row_label(row) for row in rows])
    remaining = qty if isinstance(qty, int) and qty > 0 else None
    for row in rows:
        if remaining is None:
            db.run("DELETE FROM cart_items WHERE id=%s", (row["id"],))
            continue
        if remaining <= 0:
            break
        if row["qty"] <= remaining:
            remaining -= row["qty"]
            db.run("DELETE FROM cart_items WHERE id=%s", (row["id"],))
        else:
            new_qty = row["qty"] - remaining
            remaining = 0
            modifier_ids = db.jloads(row["modifiers"], [])
            db.run(
                "UPDATE cart_items SET qty=%s, line_total=%s WHERE id=%s",
                (new_qty, _line_total(row["item_id"], new_qty, modifier_ids), row["id"]),
            )
    _touch(cart_id)
    return summary(phone)


def set_qty(phone: str, item_id: str, qty: int, line: tuple | None = None) -> dict:
    if not isinstance(qty, int) or qty < 0 or qty > 50:
        raise CartError("La cantidad debe estar entre 0 y 50.")
    cart_id = open_cart(phone)
    rows = db.q("SELECT * FROM cart_items WHERE cart_id=%s AND item_id=%s ORDER BY id", (cart_id, item_id))
    if not rows:
        if qty == 0:
            raise CartError("Ese producto no está en el pedido.")
        return add_item(phone, item_id, qty, *(line or ([], None)))
    if line is not None:
        key = _line_key(*line)
        targets = [row for row in rows if _row_key(row) == key]
        if not targets:
            raise LineChoiceNeeded(rows[0]["name"], [_row_label(row) for row in rows], matched_nothing=True)
    elif len({_row_key(row) for row in rows}) > 1:
        raise LineChoiceNeeded(rows[0]["name"], [_row_label(row) for row in rows])
    else:
        targets = rows
    keep = targets[0]
    for row in targets[1:]:
        db.run("DELETE FROM cart_items WHERE id=%s", (row["id"],))
    if qty == 0:
        db.run("DELETE FROM cart_items WHERE id=%s", (keep["id"],))
    else:
        modifier_ids = db.jloads(keep["modifiers"], [])
        db.run(
            "UPDATE cart_items SET qty=%s, line_total=%s WHERE id=%s",
            (qty, _line_total(keep["item_id"], qty, modifier_ids), keep["id"]),
        )
    _touch(cart_id)
    return summary(phone)


def clear(phone: str) -> dict:
    cart_id = open_cart(phone)
    db.run("DELETE FROM cart_items WHERE cart_id=%s", (cart_id,))
    _touch(cart_id)
    return summary(phone)


def summary(phone: str, zone: str | None = None, fulfilment: str = "PICKUP") -> dict:
    """The one function that produces a total. Code only."""
    cart_id = open_cart(phone)
    rows = db.q("SELECT * FROM cart_items WHERE cart_id=%s ORDER BY id", (cart_id,))
    lines = []
    for row in rows:
        modifier_ids = db.jloads(row["modifiers"], [])
        lines.append({
            "item_id": row["item_id"],
            "name": row["name"],
            "qty": row["qty"],
            "unit_price": row["unit_price"],
            "modifiers": [
                CATALOG.modifier(m)["name"] for m in modifier_ids if CATALOG.modifier(m)
            ],
            "note": row["note"],
            "line_total": row["line_total"],
        })
    subtotal = total_of([line["line_total"] for line in lines])
    fee, matched_zone = (0, None)
    if fulfilment == "DELIVERY":
        fee, matched_zone = CATALOG.delivery_fee_for(zone)
    return {
        "cart_id": cart_id,
        "lines": lines,
        "subtotal": subtotal,
        "delivery_fee": fee,
        "zone": matched_zone or zone,
        "total": total_of([line["line_total"] for line in lines], fee),
        "empty": not lines,
    }


def render(summary_dict: dict) -> str:
    """Human text for WhatsApp. Built from the numbers code already computed."""
    if summary_dict["empty"]:
        return "El pedido está vacío."
    parts = []
    for line in summary_dict["lines"]:
        extras = f" ({', '.join(line['modifiers'])})" if line["modifiers"] else ""
        note = f" — {line['note']}" if line["note"] else ""
        parts.append(
            f"• {line['qty']}x {line['name']}{extras}{note}  {fmt(line['line_total'])}"
        )
    parts.append(f"\nSubtotal: {fmt(summary_dict['subtotal'])}")
    if summary_dict["delivery_fee"]:
        zone = f" ({summary_dict['zone']})" if summary_dict.get("zone") else ""
        parts.append(f"Domicilio{zone}: {fmt(summary_dict['delivery_fee'])}")
    parts.append(f"*Total: {fmt(summary_dict['total'])}*")
    return "\n".join(parts)
