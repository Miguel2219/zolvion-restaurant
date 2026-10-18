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


def _settle(row_id: int, status: str, error: str | None = None, message_id: int | None = None) -> None:
    db.run(
        "UPDATE outbound_messages SET status=%s, error=%s, chatwoot_message_id=%s WHERE id=%s",
        (status, error, message_id, row_id),
    )


def send_text(phone: str, body: str, conversation_id: int | None = None) -> bool:
    """Record first, then send (R9: a delivery record exists before any retry).
    `conversation_id`: the Chatwoot conversation the message answers; None means the
    phone's newest one. Notifications go out even while a person has the conversation."""
    if not body:
        return False
    if chatwoot.enabled():
        conversation_id = conversation_id or chatwoot.conversation_for(phone)
        row_id = record(phone, body, "SENDING", conversation_id)
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
    if not live():
        record(phone, body, "FAKE")
        return True
    row_id = record(phone, body, "SENDING")
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
        _settle(row_id, "SENT")
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
                item = {
                    "wa_message_id": message.get("id"),
                    "phone": phone,
                    "name": names.get(phone),
                    "kind": message.get("type"),
                    "text": None,
                    "media_id": None,
                    "location": None,
                }
                if message.get("type") == "text":
                    item["text"] = message.get("text", {}).get("body")
                elif message.get("type") == "image":
                    item["media_id"] = message.get("image", {}).get("id")
                    item["text"] = message.get("image", {}).get("caption")
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
