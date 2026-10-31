"""WhatsApp: every message to a customer leaves through send_text.

With Chatwoot configured (chatwoot.py) it goes through the restaurant's Chatwoot inbox;
otherwise straight to the WhatsApp Cloud API. With neither (WHATSAPP_TOKEN empty) the
adapter runs in FAKE mode: messages are only recorded in outbound_messages, which is
how the tests read them."""

import base64
import hashlib
import hmac
import os

import httpx

from . import chatwoot, db

GRAPH = "https://graph.facebook.com/v21.0"


def token() -> str:
    return os.environ.get("WHATSAPP_TOKEN", "").strip()


def phone_number_id() -> str:
    return os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()


def app_secret() -> str:
    return os.environ.get("WHATSAPP_APP_SECRET", "").strip()


def verify_token() -> str:
    return os.environ.get("WHATSAPP_VERIFY_TOKEN", "zolvion-demo").strip()


def live() -> bool:
    return bool(token() and phone_number_id())


def check_signature(raw: bytes, header: str | None) -> bool:
    """Constant-time check over the RAW body, before anything else touches it."""
    secret = app_secret()
    if not secret:
        return True  # not configured: demo mode
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


def record(phone: str, body: str, status: str, conversation_id: int | None = None,
           chatwoot_message_id: int | None = None) -> int:
    """One outbound_messages row. Also used for what is never sent by Zolvion: a
    SUPPRESSED reply and a STAFF message (a person's, in Chatwoot)."""
    return db.insert(
        "INSERT INTO outbound_messages(phone, body, kind, status, sent_at, conversation_id,"
        " chatwoot_message_id) VALUES(%s,%s,'text',%s,%s,%s,%s) RETURNING id",
        (phone, body, status, db.now(), conversation_id, chatwoot_message_id),
    )


def _settle(row_id: int, status: str, error: str | None = None, message_id: int | None = None,
            wa_message_id: str | None = None) -> None:
    db.run(
        "UPDATE outbound_messages SET status=%s, error=%s, chatwoot_message_id=%s,"
        " wa_message_id=%s WHERE id=%s",
        (status, error, message_id, wa_message_id, row_id),
    )


def send_text(phone: str, body: str, conversation_id: int | None = None) -> bool:
    """Record first, then send (R9: a delivery record exists before any retry).
    `conversation_id`: the Chatwoot conversation the message answers; None means the
    phone's newest one. Notifications go out even while a person has the conversation."""
    if not body:
        return False
    if chatwoot.enabled():
        conversation_id = conversation_id or chatwoot.conversation_for(phone)
        return _send_row(record(phone, body, "SENDING", conversation_id), phone, body,
                         conversation_id)
    if not live():
        record(phone, body, "FAKE")
        return True
    return _send_row(record(phone, body, "SENDING"), phone, body, None)


# ---- Task E: notices queued with the change they report ----------------------
#
# A message that tells the customer how their order changed is written in the same
# transaction as the change (queue) and sent only after the commit (send_queued): a
# change can never be saved without its message, and a crash in between leaves a
# QUEUED row the worker sends. A FAILED one, or one SENDING for too long (the result
# is unknown), is listed on the panel for a person to resend.

MAX_NOTICE = 3900          # send_text cuts at 4000; a cut message could lose its total
TOO_LONG = "El mensaje para el cliente quedaría demasiado largo. Acorte las notas o junte líneas."
STUCK_SECONDS = 120


class TooLong(Exception):
    pass


def queue(phone: str, body: str, order_id: int, kind: str) -> int:
    """A QUEUED notice, inside the caller's transaction (no network). kind ORDER_STATE
    (how the order is now) makes that order's older unsent ORDER_STATE notices
    SUPERSEDED: only the latest state goes out. kind FIXED: a template."""
    if len(body) > MAX_NOTICE:
        raise TooLong(TOO_LONG)
    if kind == "ORDER_STATE":
        db.run(
            "UPDATE outbound_messages SET status='SUPERSEDED' WHERE order_id=%s"
            " AND notice_kind='ORDER_STATE' AND status IN ('QUEUED','FAILED')",
            (order_id,),
        )
    return db.insert(
        "INSERT INTO outbound_messages(phone, body, kind, status, sent_at, order_id, notice_kind)"
        " VALUES(%s,%s,'text','QUEUED',%s,%s,%s) RETURNING id",
        (phone, body, db.now(), order_id, kind),
    )


def send_queued(row_id: int) -> bool:
    """Claims a QUEUED notice and sends it once: two callers can never both send it.
    Never call it under db.write_lock or inside a transaction (it talks to the network)."""
    row = db.q1(
        "UPDATE outbound_messages SET status='SENDING', sent_at=%s WHERE id=%s AND status='QUEUED'"
        " RETURNING phone, body",
        (db.now(), row_id),
    )
    if row is None:
        return False
    if chatwoot.enabled():
        conversation_id = chatwoot.conversation_for(row["phone"])
        db.run("UPDATE outbound_messages SET conversation_id=%s WHERE id=%s", (conversation_id, row_id))
        return _send_row(row_id, row["phone"], row["body"], conversation_id)
    if not live():
        _settle(row_id, "FAKE")
        return True
    return _send_row(row_id, row["phone"], row["body"], None)


def send_queued_for(phone: str | None = None, older_than: str | None = None) -> int:
    """Sends the QUEUED notices of one phone (the worker, after a turn), or the ones
    queued before `older_than` (left by a crash after the commit). How many went out."""
    sql, args = "SELECT id FROM outbound_messages WHERE status='QUEUED'", []
    if phone is not None:
        sql, args = sql + " AND phone=%s", [*args, phone]
    if older_than is not None:
        sql, args = sql + " AND sent_at <= %s", [*args, older_than]
    return sum(send_queued(row["id"]) for row in db.q(sql + " ORDER BY id", args))


def stuck_notices(stuck_before: str) -> list[dict]:
    """Notices that did not reach the customer: FAILED, or SENDING since before
    `stuck_before` (the send's result is unknown), for any order, on the board or not."""
    return [dict(row) for row in db.q(
        "SELECT m.id, m.phone, m.body, m.status, m.sent_at, m.order_id, m.notice_kind, m.error,"
        " o.code FROM outbound_messages m LEFT JOIN orders o ON o.id = m.order_id"
        " WHERE m.notice_kind IS NOT NULL AND (m.status='FAILED'"
        " OR (m.status='SENDING' AND m.sent_at < %s)) ORDER BY m.id",
        (stuck_before,),
    )]


def _send_row(row_id: int, phone: str, body: str, conversation_id: int | None) -> bool:
    """The network part of a recorded row: Chatwoot when configured, else Meta."""
    if chatwoot.enabled():
        if conversation_id is None:
            _settle(row_id, "FAILED", "Este número no tiene conversación en Chatwoot.")
            return False
        try:
            message_id = chatwoot.send(conversation_id, body[:4000])
        except chatwoot.ChatwootError as exc:
            _settle(row_id, "FAILED", str(exc)[:400])
            return False
        _settle(row_id, "SENT", message_id=message_id)
        return True
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"preview_url": False, "body": body[:4000]},
    }
    try:
        response = httpx.post(
            f"{GRAPH}/{phone_number_id()}/messages",
            headers={"Authorization": f"Bearer {token()}"},
            json=payload,
            timeout=20,
        )
        if response.status_code >= 300:
            _settle(row_id, "FAILED", response.text[:400])
            return False
        # Graph's wamid for it (messages[0].id, as Chatwoot reads it): a customer's reply
        # that quotes this message names it in context.id. An odd body still means sent.
        try:
            wamid = response.json()["messages"][0]["id"]
        except (ValueError, KeyError, IndexError, TypeError):
            wamid = None
        _settle(row_id, "SENT", wa_message_id=wamid)
        return True
    except Exception as exc:  # noqa: BLE001
        _settle(row_id, "FAILED", str(exc)[:400])
        return False


def download_media(media_id: str) -> tuple[bytes, str] | None:
    """Two hops: ask for the URL, then fetch it with the same bearer token."""
    if not live():
        return None
    headers = {"Authorization": f"Bearer {token()}"}
    try:
        meta = httpx.get(f"{GRAPH}/{media_id}", headers=headers, timeout=20).json()
        url = meta.get("url")
        if not url:
            return None
        binary = httpx.get(url, headers=headers, timeout=60)
        return binary.content, meta.get("mime_type", "image/jpeg")
    except Exception:  # noqa: BLE001
        return None


def image_block(raw: bytes, mime: str) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime if mime in
            ("image/jpeg", "image/png", "image/gif", "image/webp") else "image/jpeg",
            "data": base64.standard_b64encode(raw).decode(),
        },
    }


def parse_webhook(payload: dict) -> list[dict]:
    """Flatten Meta's envelope into the few fields we care about."""
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {
                c.get("wa_id"): c.get("profile", {}).get("name")
                for c in value.get("contacts", [])
            }
            for message in value.get("messages", []):
                phone = message.get("from")
                quoted = (message.get("context") or {}).get("id")   # a reply that quotes one
                item = {
                    "wa_message_id": message.get("id"),
                    "phone": phone,
                    "name": names.get(phone),
                    "kind": message.get("type"),
                    "text": None,
                    "media_id": None,
                    "location": None,
                    "reply_to": {"id": quoted} if quoted else None,
                }
                if message.get("type") == "text":
                    item["text"] = message.get("text", {}).get("body")
                elif message.get("type") == "image":
                    item["media_id"] = message.get("image", {}).get("id")
                    item["text"] = message.get("image", {}).get("caption")
                elif message.get("type") == "sticker":
                    item["media_id"] = message.get("sticker", {}).get("id")
                elif message.get("type") == "audio":
                    item["media_id"] = message.get("audio", {}).get("id")
                elif message.get("type") == "location":
                    item["location"] = message.get("location") or {}
                elif message.get("type") == "interactive":
                    interactive = message.get("interactive", {})
                    item["text"] = (
                        interactive.get("button_reply", {}).get("title")
                        or interactive.get("list_reply", {}).get("title")
                    )
                out.append(item)
    return out
