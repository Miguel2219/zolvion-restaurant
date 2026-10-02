"""Payment runs beside the order and never blocks it (payments.md).

The model reads the screenshot. Code decides whether it is coherent.
A person gives the final confirmation — the bot never confirms a payment."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from . import db
from .catalog import CATALOG, _fold
from .money import MoneyError, fmt, pesos


class PaymentError(Exception):
    pass


UNSPECIFIED = "UNSPECIFIED"


def set_method(order_id: int, method: str, transfer_amount=None, cash_amount=None) -> dict:
    order = db.q1("SELECT total FROM orders WHERE id=?", (order_id,))
    if order is None:
        raise PaymentError("order not found")
    total = order["total"]
    if method not in CATALOG.payments.get("methods", []):
        raise PaymentError(f"{method} no está habilitado en este negocio.")

    if method == "CASH":
        transfer, cash = 0, total
    elif method == "TRANSFER":
        transfer, cash = total, 0
    elif method == "MIXED":
        try:
            transfer = pesos(transfer_amount) if transfer_amount is not None else None
            cash = pesos(cash_amount) if cash_amount is not None else None
        except MoneyError as exc:
            raise PaymentError(str(exc)) from exc
        if transfer is None and cash is not None:
            transfer = total - cash
        if cash is None and transfer is not None:
            cash = total - transfer
        if transfer is None or cash is None:
            raise PaymentError("Falta indicar cuánto va por transferencia y cuánto en efectivo.")
        # Code validates the split. The model's arithmetic is never trusted.
        if transfer + cash != total:
            raise PaymentError(
                f"La suma no cuadra: transferencia + efectivo debe dar {total}."
            )
        if transfer < 0 or cash < 0:
            raise PaymentError("Los montos no pueden ser negativos.")
    else:
        raise PaymentError(f"método desconocido: {method}")

    db.run(
        "UPDATE payments SET method=?, transfer_amount=?, cash_amount=?, updated_at=?"
        " WHERE order_id=?",
        (method, transfer, cash, db.now(), order_id),
    )
    return current(order_id)


def _expectation(order_id: int) -> tuple[int, bool]:
    """(amount the proof should match, whether the method may be inferred from it).

    A transfer that was actually agreed - TRANSFER, or MIXED with a transfer part -
    is the expectation, and the method stays as the customer chose it. When no
    transfer was agreed (UNSPECIFIED, or CASH), the customer is transferring anyway,
    so the order total is the expectation and the method follows the proof."""
    row = db.q1(
        "SELECT p.method AS method, p.transfer_amount AS agreed, o.total AS total"
        " FROM payments p JOIN orders o ON o.id = p.order_id WHERE p.order_id=?",
        (order_id,),
    )
    if row is None:
        raise PaymentError("no payment record")
    if row["method"] in ("TRANSFER", "MIXED") and row["agreed"] > 0:
        return row["agreed"], False
    return row["total"], True


def _check_date(extracted: dict, received_at: str, created_at: str) -> tuple[list[str], list[str]]:
    """(flags, notes) for the receipt's own timestamp.

    Bounded by the order at one end and the message at the other: a receipt
    cannot predate the order it pays for, and cannot postdate the message that
    delivered it. `proof_clock_skew_minutes` absorbs the disagreement between
    the bank's clock and ours - without it the customer who pays fastest is the
    one who gets flagged."""
    zone = CATALOG.timezone()
    skew = CATALOG.payments.get("proof_clock_skew_minutes", 2)
    notice_minutes = CATALOG.payments.get("proof_notice_minutes", 5)

    receipt_dt = None
    date_iso = extracted.get("date_iso")
    if date_iso:
        try:
            receipt_dt = datetime.fromisoformat(str(date_iso))
        except ValueError:
            receipt_dt = None
    if receipt_dt is None:
        return ["No se pudo leer la fecha del comprobante."], []
    if receipt_dt.tzinfo is None:
        receipt_dt = receipt_dt.replace(tzinfo=zone)

    received_dt = datetime.fromisoformat(received_at)
    if received_dt.tzinfo is None:
        received_dt = received_dt.replace(tzinfo=timezone.utc)

    created_dt = None
    if created_at:
        try:
            created_dt = datetime.fromisoformat(created_at)
        except ValueError:
            created_dt = None
        if created_dt is not None and created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)

    flags: list[str] = []
    if receipt_dt > received_dt + timedelta(minutes=skew):
        flags.append(
            f"La fecha del comprobante ({receipt_dt.astimezone(zone).strftime('%d/%m %H:%M')}) "
            "es posterior al momento en que llegó."
        )
    if created_dt is not None and receipt_dt < created_dt - timedelta(minutes=skew):
        flags.append(
            f"El comprobante es de las {receipt_dt.astimezone(zone).strftime('%H:%M')}, "
            "de antes de que se hiciera el pedido."
        )

    notes: list[str] = []
    if not flags and received_dt - receipt_dt > timedelta(minutes=notice_minutes):
        gap = int((received_dt - receipt_dt).total_seconds() // 60)
        notes.append(
            f"comprobante de las {receipt_dt.astimezone(zone).strftime('%H:%M')} ({gap} min antes)"
        )
    return flags, notes


def _holder_matches(holder: str) -> bool:
    """A proof that shows only the receiver's name still identifies the account.
    Compared on folded text, and only on a strong overlap of name words — so
    'CASERITAS BURGER' matches 'Caseritas Burger SAS', and a stranger does not."""
    if not holder:
        return False
    target = set(_fold(holder).split())
    if not target:
        return False
    for account in CATALOG.accounts():
        mine = set(_fold(str(account.get("holder", ""))).split())
        if mine and len(mine & target) >= min(2, len(mine)):
            return True
    return False


def read_ocr_amount(value) -> int:
    """Lenient parser for the ONE place model output becomes a number.

    `money.pesos` stays strict on purpose — it guards every internal peso. But
    OCR output is messy: '$ 47.300 COP', '47,300', 47300.0. This normalises that
    into a plain int and then hands it to the strict path. Anything ambiguous
    raises, and an unreadable amount is a flag, never a silent pass."""
    if value is None:
        raise MoneyError("no amount")
    if isinstance(value, bool):
        raise MoneyError("bool is not an amount")
    if isinstance(value, float):
        if value != int(value):
            raise MoneyError(f"fractional pesos: {value}")
        return pesos(int(value))
    if isinstance(value, str):
        digits = "".join(ch for ch in value if ch.isdigit())
        if not digits:
            raise MoneyError(f"no digits in {value!r}")
        # '47.300,00' / '47,300.00' — drop a trailing 2-digit cents group.
        stripped = value.strip()
        if len(digits) > 3 and (stripped.endswith(",00") or stripped.endswith(".00")):
            digits = digits[:-2]
        return pesos(digits)
    return pesos(value)


def _ocr_amount(extracted: dict | None) -> int | None:
    """The receipt's amount as read_ocr_amount reads it; None when unreadable."""
    try:
        return read_ocr_amount((extracted or {}).get("amount"))
    except MoneyError:
        return None


def receipts(order_id: int) -> list[dict]:
    """Every receipt of an order, oldest first. It never carries the image bytes:
    `has_photo` says whether one exists and photo() fetches it."""
    rows = db.q(
        "SELECT r.*, EXISTS(SELECT 1 FROM receipt_photos ph WHERE ph.receipt_id = r.id)"
        " AS has_photo FROM payment_receipts r WHERE r.order_id=? ORDER BY r.id",
        (order_id,),
    )
    found = []
    for row in rows:
        extracted = db.jloads(row["extracted"], None)
        amount = _ocr_amount(extracted)
        if row["status"] == "CONFIRMED":
            counted = row["confirmed_amount"] or 0
        elif row["status"] == "PROOF_RECEIVED":
            counted = amount or 0
        else:
            counted = 0
        found.append({
            "id": row["id"], "order_id": row["order_id"], "status": row["status"],
            "confirmed_amount": row["confirmed_amount"], "reference": row["reference"],
            "received_at": row["received_at"], "confirmed_at": row["confirmed_at"],
            "discarded_at": row["discarded_at"], "discard_reason": row["discard_reason"],
            "flags": db.jloads(row["flags"], []), "notes": db.jloads(row["notes"], []),
            "extracted": extracted, "amount": amount, "counted": counted,
            "has_photo": bool(row["has_photo"]),
        })
    return found


def photo(receipt_id: int) -> tuple[bytes, str] | None:
    row = db.q1("SELECT data, mime FROM receipt_photos WHERE receipt_id=?", (receipt_id,))
    return (bytes(row["data"]), row["mime"]) if row else None


def _expected_for_new_receipt(order_id: int, before: dict) -> tuple[int | None, bool]:
    """(amount the new receipt should match, whether the method may be inferred).

    `before` is current() taken before the new receipt is stored. None means
    nothing is owed by transfer."""
    if not any(r["status"] in ("PROOF_RECEIVED", "CONFIRMED") for r in before["receipts"]):
        return _expectation(order_id)          # first receipt, or only red/discarded ones before (Q1)
    if before["transfer_due"] > 0:
        return before["transfer_due"], False   # the "Faltan $X" the bot asked for
    if before["method"] == "MIXED" and before["cash_due"] > 0:
        return before["cash_due"], False       # Q3: transferring the cash part instead
    return None, False                         # Q3: nothing owed at all


def _store_receipt(
    order_id: int, status: str, flags: list[str], notes: list[str], reference: str | None,
    extracted: dict, received_at: str, photo: tuple[bytes, str] | None, inferred,
) -> int:
    """The receipt, its photo and the method it inferred land together or not at
    all. A duplicate reference rolls everything back and the IntegrityError
    propagates to check_proof."""
    with db.transaction():
        cursor = db.run(
            "INSERT INTO payment_receipts(order_id, status, flags, notes, reference, extracted,"
            " received_at) VALUES(?,?,?,?,?,?,?)",
            (order_id, status, json.dumps(flags), json.dumps(notes), reference,
             json.dumps(extracted), received_at),
        )
        receipt_id = cursor.lastrowid
        if photo is not None:
            db.run(
                "INSERT INTO receipt_photos(receipt_id, mime, data) VALUES(?,?,?)",
                (receipt_id, photo[1], photo[0]),
            )
        if inferred is not None:
            method, transfer, cash = inferred
            db.run(
                "UPDATE payments SET method=?, transfer_amount=?, cash_amount=?, updated_at=?"
                " WHERE order_id=?",
                (method, transfer, cash, db.now(), order_id),
            )
    return receipt_id


def check_proof(
    order_id: int, extracted: dict, photo: tuple[bytes, str] | None = None,
    received_at: str | None = None,
) -> dict:
    """`extracted` is the model's OCR output. It is data, never a verdict.

    `received_at` is when the inbound message arrived (db.now() format); it is
    the reference point for the date check, not the moment this function runs -
    OCR latency must not eat into the customer's window. None falls back to
    db.now() so existing callers and tests keep working.

    `photo` is (bytes, mime) or None; it is stored with the receipt.

    Every receipt becomes its own row with its own checks. Returns the payment
    record plus `receipt` (the new row), `resent` (True when this order already
    has a receipt with the same reference and the same readable amount: nothing is
    stored then) and `inferred` (True when this receipt set the payment method).

    Never call it inside db.transaction(): it catches the reference's
    IntegrityError, and a rolled-back transaction cannot carry on."""
    if received_at is None:
        received_at = db.now()
    row = db.q1(
        "SELECT o.created_at FROM payments p JOIN orders o ON o.id = p.order_id"
        " WHERE p.order_id=?",
        (order_id,),
    )
    if row is None:
        raise PaymentError("no payment record")
    before = current(order_id)
    expected, may_infer = _expected_for_new_receipt(order_id, before)
    flags: list[str] = []
    inferred = None                            # (method, transfer, cash) - written only if clean

    # 1. amount
    amount = None
    try:
        if extracted.get("amount") is not None:
            amount = read_ocr_amount(extracted["amount"])
    except MoneyError:
        flags.append("El monto del comprobante no se pudo leer como un número.")

    if expected is None:
        flags.append("No se esperaba otro pago.")
    elif amount is None:
        flags.append("No se pudo leer el monto en el comprobante.")
    elif may_infer:
        # No transfer was agreed. The proof itself says what they paid.
        if amount == expected:
            inferred = ("TRANSFER", amount, 0)
        elif 0 < amount < expected:
            inferred = ("MIXED", amount, expected - amount)
        else:
            flags.append(
                f"El comprobante dice {fmt(amount)} y el pedido es de {fmt(expected)}."
            )
    elif amount != expected:
        flags.append(
            f"El monto no coincide: el comprobante dice {fmt(amount)} "
            f"y se esperaban {fmt(expected)}."
        )

    # 2. the receipt's own timestamp against when it arrived
    date_flags, notes = _check_date(extracted, received_at, row["created_at"])
    flags.extend(date_flags)

    # 3. destination belongs to the restaurant. The holder name is a fallback
    #    for when Nequi shows only "Enviaste a <nombre>" and no account number -
    #    it never rescues an account number that is actually shown and wrong.
    destination = extracted.get("destination_account")
    holder = (extracted.get("destination_holder") or "").strip()
    if not destination and not holder:
        flags.append("El comprobante no muestra a quién se le transfirió.")
    elif destination:
        if not CATALOG.owns_account(destination):
            flags.append(f"La cuenta de destino ({destination}) no es del restaurante.")
    elif not _holder_matches(holder):
        flags.append(f"La cuenta de destino ({holder}) no es del restaurante.")

    # Q2: another receipt still needs a person. A discarded one does not count.
    if any(r["status"] == "FLAGGED" for r in before["receipts"]):
        notes.append("Llegó mientras otro comprobante está en revisión")

    # 4. reference not seen before - enforced by the database, not by this code.
    #    The method is inferred only from a clean receipt.
    reference = (extracted.get("reference") or "").strip() or None
    status = "FLAGGED" if flags else "PROOF_RECEIVED"
    if status != "PROOF_RECEIVED":
        inferred = None
    try:
        receipt_id = _store_receipt(
            order_id, status, flags, notes, reference, extracted, received_at, photo, inferred,
        )
    except sqlite3.IntegrityError:
        owner = db.q1(
            "SELECT order_id, extracted FROM payment_receipts WHERE reference=?", (reference,)
        ) if reference else None
        if owner is None:
            raise
        # D3: a resend is the same receipt again on this order (even if discarded):
        # same reference AND the same amount, both readable. Any other repeat is red.
        if (owner["order_id"] == order_id and amount is not None
                and amount == _ocr_amount(db.jloads(owner["extracted"], None))):
            return {**current(order_id), "receipt": None, "resent": True, "inferred": False}
        flags.append(f"Este comprobante ya fue usado antes (referencia {reference}).")
        # Stored without the reference (it belongs to the row that has it, on this
        # order or another); it stays readable in `extracted` and in the flag.
        inferred = None
        receipt_id = _store_receipt(
            order_id, "FLAGGED", flags, notes, None, extracted, received_at, photo, None
        )
    record = current(order_id)
    record["receipt"] = next(r for r in record["receipts"] if r["id"] == receipt_id)
    record["resent"] = False
    # True when this receipt set the method (only a clean one does): the bot must
    # say so instead of asking how the customer will pay.
    record["inferred"] = inferred is not None
    return record


def confirm_receipt(order_id: int, receipt_id: int, amount=None) -> dict:
    """Only a person calls this, from the panel. `amount` is what they actually
    received with this receipt; None falls back to the amount read from it."""
    r = db.q1("SELECT * FROM payment_receipts WHERE id=?", (receipt_id,))
    if r is None or r["order_id"] != order_id:
        raise PaymentError("Ese comprobante no es de este pedido.")
    if r["status"] == "CONFIRMED":
        raise PaymentError("Ese comprobante ya está confirmado.")
    if r["status"] == "DISCARDED":
        raise PaymentError("Un comprobante descartado no se puede confirmar.")

    if amount is not None:
        try:
            received = pesos(amount)
        except MoneyError as exc:
            raise PaymentError(str(exc)) from exc
    else:
        try:
            received = read_ocr_amount((db.jloads(r["extracted"], None) or {}).get("amount"))
        except MoneyError as exc:
            raise PaymentError(
                "No hay un monto para confirmar: indique cuánto se recibió."
            ) from exc
    if received <= 0:
        raise PaymentError(
            "El monto debe ser mayor que cero. Si el comprobante no sirve, use Descartar."
        )

    with db.transaction():
        db.run(
            "UPDATE payment_receipts SET status='CONFIRMED', confirmed_amount=?, confirmed_at=?"
            " WHERE id=?",
            (received, db.now(), receipt_id),
        )
        pay = db.q1(
            "SELECT p.method, o.total FROM payments p JOIN orders o ON o.id = p.order_id"
            " WHERE p.order_id=?",
            (order_id,),
        )
        if pay["method"] in ("UNSPECIFIED", "CASH"):
            # The same inference check_proof makes from a clean receipt - now
            # sanctioned by a person.
            confirmed = db.q1(
                "SELECT COALESCE(SUM(confirmed_amount),0) AS n FROM payment_receipts"
                " WHERE order_id=? AND status='CONFIRMED'",
                (order_id,),
            )["n"]
            if confirmed >= pay["total"]:
                db.run(
                    "UPDATE payments SET method='TRANSFER', transfer_amount=?, cash_amount=0,"
                    " updated_at=? WHERE order_id=?",
                    (pay["total"], db.now(), order_id),
                )
            else:
                db.run(
                    "UPDATE payments SET method='MIXED', transfer_amount=?, cash_amount=?,"
                    " updated_at=? WHERE order_id=?",
                    (confirmed, pay["total"] - confirmed, db.now(), order_id),
                )
    # It never writes payments.transfer_amount on a TRANSFER/MIXED order: the
    # agreed split stays, and the money received lives on the receipt.
    record = current(order_id)
    record["receipt"] = next(x for x in record["receipts"] if x["id"] == receipt_id)
    return record


def discard_receipt(order_id: int, receipt_id: int, reason: str | None) -> dict:
    """Only a person calls this, from the panel: the receipt is not a payment (a
    wrong screenshot). `reason` is required: the customer is told it (main.py
    sends the message) and the card shows it. It keeps its reference, so the
    same screenshot is still refused on another order and still a resend on
    this one."""
    reason = (reason or "").strip()
    if not reason:
        raise PaymentError("Hace falta el motivo para avisarle al cliente.")
    r = db.q1("SELECT order_id, status FROM payment_receipts WHERE id=?", (receipt_id,))
    if r is None or r["order_id"] != order_id:
        raise PaymentError("Ese comprobante no es de este pedido.")
    if r["status"] == "CONFIRMED":
        raise PaymentError("Un comprobante confirmado no se puede descartar.")
    if r["status"] == "DISCARDED":
        raise PaymentError("Ese comprobante ya está descartado.")
    db.run(
        "UPDATE payment_receipts SET status='DISCARDED', discarded_at=?, discard_reason=?"
        " WHERE id=?",
        (db.now(), reason, receipt_id),
    )
    record = current(order_id)
    record["receipt"] = next(x for x in record["receipts"] if x["id"] == receipt_id)
    return record


def follow_total(order_id: int) -> dict:
    """Called only by orders.amend, inside its transaction, once the order's new
    total is already written. Returns code-computed numbers for the bot to
    describe what changed - the model never invents them.

    A red receipt counts nothing, so it only follows the agreed split;
    under_review keeps the bot from ever asking for that money again while a
    person reviews it by hand."""
    before = current(order_id)
    total, method = before["total"], before["method"]
    change = {
        "sent": 0, "cash": 0, "missing": 0, "cash_due": 0, "surplus": 0,
        "under_review": before["under_review"], "ask_method": False,
    }

    if not any(r["status"] in ("PROOF_RECEIVED", "CONFIRMED") for r in before["receipts"]):
        # Nothing counts yet (no receipt, or only red/discarded ones).
        if method in ("CASH", "TRANSFER"):
            set_method(order_id, method)
        elif method == "MIXED":
            agreed = before["transfer_amount"]
            if agreed < total:
                set_method(order_id, "MIXED", transfer_amount=agreed)
            else:
                db.run(
                    "UPDATE payments SET method='UNSPECIFIED', transfer_amount=0, cash_amount=0,"
                    " updated_at=? WHERE order_id=?",
                    (db.now(), order_id),
                )
                change["ask_method"] = True
        after = current(order_id)
        change.update(cash=after["cash_received"], surplus=after["overpayment_amount"])
        return change

    if method == "MIXED":
        transfer = min(before["transfer_amount"], total)
        db.run(
            "UPDATE payments SET transfer_amount=?, cash_amount=?, updated_at=? WHERE order_id=?",
            (transfer, total - transfer, db.now(), order_id),
        )
    else:
        db.run(
            "UPDATE payments SET method='TRANSFER', transfer_amount=?, cash_amount=0,"
            " updated_at=? WHERE order_id=?",
            (total, db.now(), order_id),
        )
    after = current(order_id)
    change.update(
        sent=after["sent"], cash=after["cash_received"], missing=after["transfer_due"],
        surplus=after["overpayment_amount"],
    )
    if method == "MIXED":
        change["cash_due"] = after["cash_due"]
    return change


def register_cash(order_id: int, amount) -> dict:
    """Staff collected cash at handover. Never more than what is still owed: the
    change the customer gets back is the restaurant's business and never enters
    the system. On an order whose method was never chosen, a person collecting
    cash settles it: the method becomes CASH, in the same transaction."""
    try:
        amount = pesos(amount)
    except MoneyError as exc:
        raise PaymentError(str(exc)) from exc
    if amount <= 0:
        raise PaymentError("El monto debe ser mayor que cero.")
    before = current(order_id)
    owed = before["outstanding"]
    if amount > owed:
        raise PaymentError(
            f"El monto es mayor de lo que falta ({fmt(owed)}). "
            "Registre solo lo que falta; el cambio lo maneja el local."
        )
    with db.transaction():
        db.run(
            "UPDATE payments SET cash_received = cash_received + ?, updated_at=? WHERE order_id=?",
            (amount, db.now(), order_id),
        )
        # Written directly, not through set_method: a config without CASH among its
        # methods must not stop staff from registering cash they already hold.
        db.run(
            "UPDATE payments SET method='CASH', transfer_amount=0, cash_amount=?"
            " WHERE order_id=? AND method='UNSPECIFIED'",
            (before["total"], order_id),
        )
    return current(order_id)


def current(order_id: int) -> dict:
    # The legacy payments columns are named out of this query on purpose, so they
    # never leak into the JSON the panel reads.
    row = db.q1(
        "SELECT p.id, p.order_id, p.method, p.transfer_amount, p.cash_amount,"
        " p.cash_received, p.updated_at, o.total AS total FROM payments p"
        " JOIN orders o ON o.id = p.order_id WHERE p.order_id=?",
        (order_id,),
    )
    if row is None:
        raise PaymentError("no payment record")
    data = dict(row)
    total = data["total"]
    found = receipts(order_id)
    states = {r["status"] for r in found}

    sent = sum(r["counted"] for r in found)
    transfer_due = max(data["transfer_amount"] - sent, 0)
    cash_due = max(total - sent - data["cash_received"] - transfer_due, 0)
    # The one place that says how much is paid - every caller reads this instead
    # of computing its own. Confirmed receipts count, and cash counts once staff
    # register it; a discarded receipt counts nothing. `paid` is capped at the total;
    # what goes beyond it is overpayment_amount (below), which counts receipts and
    # registered cash. Cash is never registered above `outstanding` (register_cash),
    # so a surplus that involves cash comes from a transfer that overlaps it, or from
    # an edit that lowered the total afterwards. Either way it is money owed to the
    # customer, and a person resolves it. `sent` already counts clean receipts that
    # still wait, so the surplus shows as soon as cash goes on top of one.
    confirmed = sum(r["confirmed_amount"] or 0 for r in found if r["status"] == "CONFIRMED")
    paid = min(confirmed + data["cash_received"], total)
    # Clean receipts nobody has confirmed yet, at their OCR amount: the cash dialog
    # warns about them.
    unconfirmed = sum(r["counted"] for r in found if r["status"] == "PROOF_RECEIVED")

    # The order-level status is derived: the worst receipt wins, discarded ones are ignored.
    for status in ("FLAGGED", "PROOF_RECEIVED", "CONFIRMED"):
        if status in states:
            break
    else:
        status = "PENDING"
    return {
        **data, "receipts": found, "sent": sent, "transfer_due": transfer_due,
        "cash_due": cash_due, "paid": paid, "outstanding": max(total - paid, 0),
        "overpayment_amount": max(sent + data["cash_received"] - total, 0),
        "unconfirmed_amount": unconfirmed, "status": status,
        "under_review": "FLAGGED" in states,
    }
