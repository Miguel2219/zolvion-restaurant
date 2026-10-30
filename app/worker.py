"""The one place customer messages are answered (Task D Part 2).

Webhooks only store what came in (inbound_messages, status RECEIVED) and answer at
once: Chatwoot gives a bot 5 s and then hands the conversation to a person. This
thread does the slow part, every half second:

1. tells Chatwoot what Zolvion decided and could not tell it yet (a handoff's note and
   "open", a takeover), retried every 30 s while Chatwoot fails;
2. takes one phone whose newest RECEIVED message is MESSAGE_WAIT_SECONDS old, so a
   customer who writes in several messages gets one answer;
3. answers them in arrival order: consecutive texts (and the notes for what the bot
   can't read) in one turn, each photo on its own;
4. reads who has the conversation before each step and again before sending: while a
   person has it the bot stays out (a receipt is still checked, and noted for staff),
   and a reply written meanwhile is not sent (SUPPRESSED).

One worker keeps each conversation in order and runs one turn at a time, as before.
Tests call run_once() and recover(); nothing starts the thread but the app's startup."""

import os
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from . import brain, chatwoot, db, orders, transcribe, wa
from .money import fmt

POLL_SECONDS = 0.5
RETRY_SECONDS = 30
DEFAULT_WAIT_SECONDS = 6.0
CATCH_UP_LINES = 20

INTERRUPTED_TEXT = (
    "Perdón, tuve un problema y no alcancé a responder su último mensaje. "
    "¿Me lo repite, por favor?"
)
FAILED_TEXT = "Perdón, se me cruzaron los cables. ¿Me repite el pedido?"
MAINTENANCE_TEXT = "Perdón, el sistema está en mantenimiento. Intente en unos minutos."
# What the bot's history says when its reply was not sent: the model must not build on
# words the customer never got.
NOT_SENT = "(Esta respuesta no se envió: una persona del local tomó la conversación antes.)"

# How a customer message the bot did not read is named in the catch-up note.
KIND_WORDS = {
    "image": "una imagen", "sticker": "un sticker", "audio": "una nota de voz",
    "video": "un video", "document": "un documento", "location": "una ubicación",
    "contacts": "un contacto", "unsupported": "un mensaje que no se puede ver",
}

_thread: threading.Thread | None = None


def wait_seconds() -> float:
    raw = os.environ.get("MESSAGE_WAIT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_WAIT_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_WAIT_SECONDS


def _at(seconds_from_now: float) -> str:
    """A moment in db.now()'s format, for comparing against stored timestamps."""
    moment = datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)
    return moment.isoformat(timespec="microseconds")


def start() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    recover()
    _thread = threading.Thread(target=_loop, name="zolvion-worker", daemon=True)
    _thread.start()


def _loop() -> None:
    while True:
        try:
            busy = run_once()
        except Exception:  # noqa: BLE001 - the worker must outlive any one message
            traceback.print_exc()
            busy = False
        if not busy:
            time.sleep(POLL_SECONDS)


def recover() -> int:
    """At startup: a turn the app stopped in the middle of is not run again (a tool
    may already have changed the cart). Its messages become INTERRUPTED and the
    customer gets one fixed message, if the bot still holds the conversation.
    RECEIVED messages are simply answered. Returns how many phones were told."""
    with db.write_lock:
        rows = db.q(
            "UPDATE inbound_messages SET status='INTERRUPTED', processed_at=%s"
            " WHERE status='PROCESSING' RETURNING phone, conversation_id, received_at",
            (db.now(),),
        )
    phones: dict[str, int | None] = {}
    for row in sorted(rows, key=lambda r: r["received_at"]):
        phones[row["phone"]] = row["conversation_id"]
    told = 0
    for phone, conversation_id in phones.items():
        if chatwoot.bot_holds(phone):
            wa.send_text(phone, INTERRUPTED_TEXT, conversation_id)
            told += 1
    return told


QUEUED_GRACE_SECONDS = 15


def run_once() -> bool:
    """One pass. True when a phone's messages were answered."""
    sync_chatwoot()
    # Task E: a notice queued with its change and never sent (the app stopped after the
    # commit) goes out now. A fresh one is left to whoever queued it.
    wa.send_queued_for(older_than=_at(-QUEUED_GRACE_SECONDS))
    batch = _claim()
    if not batch:
        return False
    _answer(batch)
    sync_chatwoot()            # a handoff this turn asked for, now that its reply is out
    return True


def _claim() -> list[dict]:
    """The phone whose messages waited longest, once its newest one is old enough.
    All its RECEIVED rows become PROCESSING together, in arrival order."""
    with db.write_lock:
        row = db.q1(
            "SELECT phone FROM inbound_messages WHERE status='RECEIVED' GROUP BY phone"
            " HAVING MAX(received_at) <= %s ORDER BY MIN(received_at) LIMIT 1",
            (_at(-wait_seconds()),),
        )
        if row is None:
            return []
        rows = db.q(
            "UPDATE inbound_messages SET status='PROCESSING' WHERE phone=%s AND status='RECEIVED'"
            " RETURNING *",
            (row["phone"],),
        )
    return sorted(rows, key=lambda r: (r["received_at"], r["wa_message_id"]))


def _steps(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """Consecutive messages that are not photos go in one turn; a photo (a possible
    receipt) is a step of its own, and so is a sticker (Task E: the bot sees it, and a
    webp receipt is checked like a photo)."""
    steps: list[tuple[str, list[dict]]] = []
    texts: list[dict] = []
    for row in rows:
        if row["kind"] in ("image", "sticker"):
            if texts:
                steps.append(("text", texts))
                texts = []
            steps.append(("image", [row]))
        else:
            texts.append(row)
    if texts:
        steps.append(("text", texts))
    return steps


def _answer(rows: list[dict]) -> None:
    phone = rows[0]["phone"]
    for kind, step in _steps(rows):
        status, error = "DONE", None
        try:
            # Task E: a shared pin is kept whoever answers (also while a person has the
            # chat), in arrival order, before this step's turn can create the order.
            for row in step:
                if row["kind"] == "location":
                    with db.write_lock:
                        orders.attach_location(phone, db.jloads(row["location"], None))
            if not chatwoot.bot_holds(phone):
                status = "HUMAN"
                if kind == "image":
                    _receipt_for_staff(phone, step[0])
            elif kind == "image":
                _bot_image(phone, step[0])
            else:
                _bot_text(phone, step)
        except brain.NotConfigured as exc:
            print(f"\n!! {exc}\n", flush=True)
            status, error = "FAILED", str(exc)[:400]
            _apologize(phone, step, MAINTENANCE_TEXT)
        except Exception as exc:  # noqa: BLE001 - one bad message must not stop the rest
            traceback.print_exc()
            status, error = "FAILED", f"{type(exc).__name__}: {exc}"[:400]
            _apologize(phone, step, FAILED_TEXT)
        with db.write_lock:
            db.run(
                "UPDATE inbound_messages SET status=%s, error=%s, processed_at=%s"
                " WHERE wa_message_id = ANY(%s)",
                (status, error, db.now(), [row["wa_message_id"] for row in step]),
            )


def _apologize(phone: str, step: list[dict], text: str) -> None:
    try:
        if chatwoot.bot_holds(phone):
            wa.send_text(phone, text, step[-1]["conversation_id"])
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def _message(row: dict) -> dict:
    """A stored row in the shape main._note_for reads."""
    return {"kind": row["kind"], "text": row["body"], "location": db.jloads(row["location"], None)}


def _bot_text(phone: str, rows: list[dict]) -> None:
    from . import main                     # main imports this module

    started = db.now()
    # parts: what the bot reads; said: the same without the notes about quoted messages,
    # so a payment word in a quoted message never counts as the customer's question.
    parts, said, customer = [], [], False
    has_order = orders.latest_for(phone, [*orders.EDITABLE, *orders.LOCKED]) is not None
    for row in rows:
        quote = main.quote_note(phone, row["reply_to"])
        if quote:
            parts.append(quote)             # right before the message that quotes it
        if row["kind"] == "audio" and transcribe.enabled():
            heard = _hear(row)
            if heard.clear:
                part = main.voice_text(heard.text)
                customer = True             # the customer's own words, like typed text
            else:
                part = main.VOICE_NOTES.get(heard.reason, main.VOICE_NOTES["unclear"])
        else:
            part = main._note_for(_message(row), image_failed=False, has_order=has_order)
            if not part and (row["body"] or "").strip():
                part = row["body"].strip()
                customer = True
        if part:
            parts.append(part)
            said.append(part)
    text = "\n".join(parts) or main.EMPTY_MESSAGE_NOTE
    words = "\n".join(said)
    catch_up = _catch_up(phone)
    if catch_up:
        text = f"{catch_up}\n\n{text}"
        words = f"{catch_up}\n\n{words}"
    reply = main.handle(phone, text, None, rows[0]["received_at"], customer=customer, said=words)
    # A notice the turn queued (an automatic addition) goes before the bot's reply.
    wa.send_queued_for(phone)
    _deliver(phone, reply, rows[-1]["conversation_id"])
    if chatwoot.enabled():
        with db.write_lock:
            db.run("UPDATE conversations SET caught_up_at=%s WHERE phone=%s", (started, phone))


def _hear(row: dict) -> transcribe.Heard:
    """A voice note, downloaded and transcribed outside any lock or transaction. What
    was heard is kept on the message (the text the bot acted on)."""
    downloaded = _download(row)
    if downloaded is None:
        return transcribe.Heard("", False, "error")
    heard = transcribe.transcribe(*downloaded)
    if heard.text:
        with db.write_lock:
            db.run("UPDATE inbound_messages SET transcript=%s WHERE wa_message_id=%s",
                   (heard.text, row["wa_message_id"]))
    return heard


def _bot_image(phone: str, row: dict) -> None:
    from . import main

    downloaded = _download(row)
    sticker = row["kind"] == "sticker"
    if downloaded is None and sticker:
        reply = brain.reply(phone, main._note_for(_message(row), image_failed=False), sticker=True)
    elif downloaded is None:
        note = main._note_for(_message(row), image_failed=True)
        reply = main.handle(phone, note, None, row["received_at"], customer=False)
    else:
        image = wa.image_block(*downloaded)
        reply = main.handle(phone, row["body"], image, row["received_at"], sticker=sticker)
    wa.send_queued_for(phone)
    _deliver(phone, reply, row["conversation_id"])


def _deliver(phone: str, reply: str, conversation_id: int | None) -> None:
    """Minimal point 1: the status is read again right before sending. A person who
    took the conversation during the turn gets it without a bot reply on top; what
    the turn's tools did stays (it is on the card)."""
    from . import main

    reply = main._customer_safe(phone, reply)
    if chatwoot.bot_holds(phone):
        wa.send_text(phone, reply, conversation_id)
        return
    wa.record(phone, reply, "SUPPRESSED", conversation_id)
    brain.correct_last_reply(phone, NOT_SENT)


def _download(row: dict) -> tuple[bytes, str] | None:
    url = row["media_url"] or ""
    if url.startswith("whatsapp:"):
        return wa.download_media(url.split(":", 1)[1])
    return chatwoot.download(url) if url else None


def _receipt_for_staff(phone: str, row: dict) -> None:
    """A photo while a person has the conversation: no bot. If there is a live order it
    is still checked as a receipt (the model reads, code decides), and the person gets
    the result as a private note. Confirming stays in the panel."""
    from . import main

    order = orders.latest_for(phone, [*orders.EDITABLE, *orders.LOCKED])
    if order is None:
        return
    downloaded = _download(row)
    if downloaded is None:
        if row["kind"] == "sticker":
            return
        note = (f"Zolvion · {order['code']}: el cliente envió una imagen y no se pudo "
                "descargar para revisarla como comprobante.")
    else:
        image = wa.image_block(*downloaded)
        try:
            extracted = brain.extract_proof(image)
        except brain.NotConfigured:
            raise
        except Exception:  # noqa: BLE001 - a sticker the API refuses is just a sticker
            if row["kind"] != "sticker":
                raise
            traceback.print_exc()
            return
        if not brain.is_receipt(extracted):
            return                 # Task E: only a receipt gets a row and a note
        record = main.receive_receipt(order, image, row["received_at"], extracted)
        note = staff_note(order, record)
    _private_note(row["conversation_id"] or chatwoot.conversation_for(phone), note)


def staff_note(order: dict, record: dict) -> str:
    """Built by code from check_proof's result. Only staff read it, so the order code
    is allowed here."""
    head = f"Zolvion · {order['code']}:"
    if record["resent"] and record.get("resent_discard_reason"):
        return (f"{head} el cliente volvió a enviar un comprobante que el local ya había "
                f"descartado (motivo: {record['resent_discard_reason']}).")
    if record["resent"]:
        return f"{head} el cliente volvió a enviar un comprobante que ya estaba registrado."
    receipt = record["receipt"]
    if receipt["status"] == "FLAGGED":
        return f"{head} comprobante con problema: {' '.join(receipt['flags'])} Revíselo en el panel."
    amount = fmt(receipt["amount"]) if receipt["amount"] is not None else "monto ilegible"
    return f"{head} comprobante por {amount} en revisión. Confírmelo en el panel."


def _private_note(conversation_id: int | None, text: str) -> None:
    if not chatwoot.enabled() or conversation_id is None:
        return
    try:
        chatwoot.send(conversation_id, text, private=True)
    except chatwoot.ChatwootError as exc:
        print(f"!! Chatwoot no recibió la nota privada: {exc}", flush=True)


def _finished_at(phone: str) -> str:
    """G43 (Task E, review gap 6): with nothing live, when the phone's latest order
    ended. What was written before belongs to that order and must not come back in the
    catch-up note of a fresh conversation. "" when there is no such boundary."""
    if orders.live_states(phone):
        return ""
    row = db.q1(
        "SELECT t.at FROM orders o JOIN order_transitions t ON t.order_id = o.id"
        " WHERE o.id = (SELECT id FROM orders WHERE phone=%s ORDER BY id DESC LIMIT 1)"
        " ORDER BY t.id DESC LIMIT 1",
        (phone,),
    )
    return (row or {}).get("at") or ""


def _catch_up(phone: str) -> str | None:
    """What the customer and a person wrote while the person had the conversation, as
    one bracketed note for the bot's next turn, so it neither repeats nor contradicts
    them. None when there is nothing new."""
    if not chatwoot.enabled():
        return None
    row = db.q1("SELECT caught_up_at FROM conversations WHERE phone=%s", (phone,))
    since = max((row or {}).get("caught_up_at") or "", _finished_at(phone))
    lines = [
        (r["received_at"], "Cliente: " + (
            f"«{r['body'].strip()}»" if r["kind"] == "text" and (r["body"] or "").strip()
            else f"(envió {KIND_WORDS.get(r['kind'], 'un mensaje')})"
        ))
        for r in db.q(
            "SELECT body, kind, received_at FROM inbound_messages"
            " WHERE phone=%s AND status='HUMAN' AND received_at > %s",
            (phone, since),
        )
    ] + [
        (r["sent_at"], f"Persona del local: «{r['body'].strip()}»")
        for r in db.q(
            "SELECT body, sent_at FROM outbound_messages"
            " WHERE phone=%s AND status='STAFF' AND sent_at > %s",
            (phone, since),
        )
    ]
    if not lines:
        return None
    lines.sort(key=lambda line: line[0])
    listing = "\n".join(f"- {text}" for _, text in lines[-CATCH_UP_LINES:])
    return (
        "(Mientras una persona del local atendía este chat, se escribió esto:\n"
        f"{listing}\n"
        "Ahora le respondes tú otra vez. Ten en cuenta lo que la persona ya le dijo: no lo "
        "repitas y no lo contradigas. No le menciones ningún código.)"
    )


# ---- telling Chatwoot -----------------------------------------------------

def sync_chatwoot() -> None:
    """Status changes Zolvion decided but Chatwoot has not been told yet: a takeover
    (a person replied while the bot held the conversation) and the opening of each
    handoff box. A failure is retried after RETRY_SECONDS."""
    if not chatwoot.enabled():
        return
    now = db.now()
    for row in db.q(
        "SELECT phone, chatwoot_conversation_id, push_status FROM conversations"
        " WHERE push_status IS NOT NULL AND (push_try_at IS NULL OR push_try_at <= %s)",
        (now,),
    ):
        try:
            chatwoot.set_status(row["chatwoot_conversation_id"], row["push_status"])
        except chatwoot.ChatwootError as exc:
            print(f"!! Chatwoot no cambió el estado de la conversación: {exc}", flush=True)
            with db.write_lock:
                db.run("UPDATE conversations SET push_try_at=%s WHERE phone=%s",
                       (_at(RETRY_SECONDS), row["phone"]))
            continue
        with db.write_lock:
            db.run(
                "UPDATE conversations SET push_status=NULL, push_try_at=NULL"
                " WHERE phone=%s AND push_status=%s",
                (row["phone"], row["push_status"]),
            )
    for box in db.q(
        "SELECT h.*, o.code FROM handoff_requests h JOIN orders o ON o.id = h.order_id"
        " WHERE h.chatwoot_state='OPENING' AND h.resolved_at IS NULL"
        " AND (h.chatwoot_try_at IS NULL OR h.chatwoot_try_at <= %s) ORDER BY h.id",
        (now,),
    ):
        _open_in_chatwoot(box)


def _open_in_chatwoot(box: dict) -> None:
    """The handoff in Chatwoot: a private note with the code-written summary (staff
    only, so the order code is allowed), then the status "open". Until both worked the
    box stays OPENING and the card says «Chatwoot sin actualizar»."""
    conversation_id = chatwoot.conversation_for(box["phone"])
    if conversation_id is None:
        with db.write_lock:
            db.run("UPDATE handoff_requests SET chatwoot_state='NONE' WHERE id=%s", (box["id"],))
        return
    try:
        if box["chatwoot_note_id"] is None:
            note_id = chatwoot.send(
                conversation_id,
                f"🙋 Necesita a una persona · pedido {box['code']}\n{box['summary']}",
                private=True,
            )
            with db.write_lock:
                db.run("UPDATE handoff_requests SET chatwoot_note_id=%s WHERE id=%s",
                       (note_id or 0, box["id"]))
        chatwoot.set_status(conversation_id, chatwoot.OPEN)
    except chatwoot.ChatwootError as exc:
        print(f"!! Chatwoot no abrió la conversación del pedido {box['code']}: {exc}", flush=True)
        with db.write_lock:
            db.run(
                "UPDATE handoff_requests SET chatwoot_try_at=%s, chatwoot_error=%s WHERE id=%s",
                (_at(RETRY_SECONDS), str(exc)[:300], box["id"]),
            )
        return
    with db.write_lock:
        db.run(
            "UPDATE handoff_requests SET chatwoot_state='OPEN', chatwoot_error=NULL WHERE id=%s",
            (box["id"],),
        )
        chatwoot.set_local(box["phone"], chatwoot.OPEN)
