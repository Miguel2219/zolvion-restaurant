"""WhatsApp Cloud API adapter.

If WHATSAPP_TOKEN is missing the adapter runs in FAKE mode: outbound messages are
still recorded in the database, so the browser simulator shows them. That is what
makes the demo survive a dead internet connection."""

import base64
import hashlib
import hmac
import os

import httpx

from . import db

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


def _record(phone: str, body: str, status: str, error: str | None = None, kind: str = "text") -> None:
    db.run(
        "INSERT INTO outbound_messages(phone, body, kind, status, error, sent_at)"
        " VALUES(?,?,?,?,?,?)",
        (phone, body, kind, status, error, db.now()),
    )


def send_text(phone: str, body: str) -> bool:
    """Record first, then send (R9: a delivery record exists before any retry)."""
    if not body:
        return False
    if not live():
        _record(phone, body, "FAKE")
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
            _record(phone, body, "FAILED", response.text[:400])
            return False
        _record(phone, body, "SENT")
        return True
    except Exception as exc:  # noqa: BLE001
        _record(phone, body, "FAILED", str(exc)[:400])
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
