"""Chatwoot: the restaurant's WhatsApp number lives in a Chatwoot inbox and Zolvion is
that inbox's Agent Bot (Task D Part 2, docs/CHATWOOT-SETUP.md).

Zolvion answers only while a conversation is Pending. The status decides who answers,
not the assignee: a person takes over by opening the conversation (or by replying in
it), and gives it back by setting it to Pending. Chatwoot Cloud and self-hosted send the
same events (step 0, recordings 001-084).

Every webhook is signed: X-Chatwoot-Signature is "sha256=" + HMAC-SHA256(secret,
"<X-Chatwoot-Timestamp>.<raw body>"). A webhook slower than 5 s, or failing, makes
Chatwoot open the conversation for a person and never send it again: the route only
stores what came in, and the worker (worker.py) does everything slow."""

import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from . import db

# All four set = Chatwoot mode; none = Meta or FAKE mode (wa.py); some = nothing works
# until they are fixed (the banner says which). There is no default URL: Chatwoot Cloud
# and a self-hosted Chatwoot are both the user's choice.
SETTINGS = ("CHATWOOT_URL", "CHATWOOT_ACCOUNT_ID", "CHATWOOT_BOT_TOKEN", "CHATWOOT_BOT_SECRET")
MAX_AGE_SECONDS = 300          # an older signed timestamp is a replay
TIMEOUT_SECONDS = 10

PENDING = "pending"            # the bot answers
OPEN = "open"                  # a person has the conversation

# message_type arrives as text in webhooks; as a number in some API answers.
MESSAGE_TYPES = {0: "incoming", 1: "outgoing", 2: "activity", 3: "template"}

# Chatwoot's attachment file_type -> inbound_messages.kind (the names wa.py uses, so
# main._note_for reads both channels the same way).
FILE_KINDS = {
    "audio": "audio", "video": "video", "file": "document", "location": "location",
    "contact": "contacts", "fallback": "unsupported",
}


class ChatwootError(Exception):
    pass


def setting(name: str) -> str:
    return os.environ.get(name, "").strip()


def missing() -> list[str]:
    return [name for name in SETTINGS if not setting(name)]


def enabled() -> bool:
    return not missing()


def incomplete() -> bool:
    """Some settings but not all: every webhook is refused until .env is fixed."""
    return 0 < len(missing()) < len(SETTINGS)


def base_url() -> str:
    return setting("CHATWOOT_URL").rstrip("/")


def verify(raw: bytes, timestamp: str | None, signature: str | None) -> bool:
    """Constant-time check over the RAW body. No secret, no timestamp or an old one:
    rejected."""
    secret = setting("CHATWOOT_BOT_SECRET")
    if not secret or not signature or not (timestamp or "").isdigit():
        return False
    if abs(time.time() - int(timestamp)) > MAX_AGE_SECONDS:
        return False
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256)
    return hmac.compare_digest("sha256=" + digest.hexdigest(), signature)


# ---- reading events ------------------------------------------------------

def _digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _clock(value) -> float:
    """Chatwoot's updated_at: epoch seconds in webhooks. Orders events of one
    conversation, so a late event never overwrites a newer status."""
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def _conversation(payload: dict) -> dict:
    """Message events carry the conversation under "conversation"; conversation
    events are the conversation itself."""
    return payload.get("conversation") or payload


def _customer_phone(payload: dict) -> str:
    conversation = _conversation(payload)
    sender = payload.get("sender") or {}
    meta_sender = (conversation.get("meta") or {}).get("sender") or {}
    incoming = _message_type(payload) == "incoming"
    for value in (
        sender.get("phone_number") if incoming else None,
        meta_sender.get("phone_number"),
        (conversation.get("contact_inbox") or {}).get("source_id"),
    ):
        if _digits(value):
            return _digits(value)
    return ""


def _message_type(payload: dict) -> str | None:
    value = payload.get("message_type")
    return MESSAGE_TYPES.get(value, value)


def classify(payload: dict) -> str:
    """customer, staff, own, status or ignore. Private notes, activity and template
    messages and every message_updated (send status, transcriptions) are ignored.

    staff: a person's reply in Chatwoot, or (Coexistence) a reply typed in the
    restaurant's WhatsApp Business app, which arrives as an outgoing echo with no
    Chatwoot user behind it. own: what the bot itself sent."""
    event = str(payload.get("event") or "")
    if event == "message_created":
        if payload.get("private"):
            return "ignore"
        kind = _message_type(payload)
        if kind == "incoming":
            return "customer"
        if kind == "outgoing":
            sender_type = (payload.get("sender") or {}).get("type")
            if sender_type == "agent_bot":
                return "own"
            echo = (payload.get("content_attributes") or {}).get("external_echo")
            if sender_type == "user" or echo or not payload.get("sender"):
                return "staff"
        return "ignore"
    if event.startswith("conversation_") and payload.get("status"):
        return "status"
    return "ignore"


def conversation_state(payload: dict) -> dict:
    """phone, conversation id, status and Chatwoot's clock, from any event."""
    conversation = _conversation(payload)
    return {
        "phone": _customer_phone(payload),
        "conversation_id": conversation.get("id"),
        "status": conversation.get("status"),
        "status_at": _clock(conversation.get("updated_at")),
        "name": ((conversation.get("meta") or {}).get("sender") or {}).get("name"),
    }


def parse_customer(payload: dict) -> dict:
    """A customer message, in the fields inbound_messages keeps. A sticker arrives as an
    ordinary image/webp with nothing marking it (step 0), so any webp image is a sticker.
    Its image is kept (Task E): the bot sees it, and a webp receipt is still checked."""
    attachment = (payload.get("attachments") or [{}])[0]
    file_type = attachment.get("file_type")
    text = payload.get("content")
    kind, media_url, location = "text", None, None
    if file_type == "image":
        webp = (attachment.get("content_type") == "image/webp"
                or attachment.get("extension") == "webp")
        kind = "sticker" if webp else "image"
        media_url = attachment.get("data_url")
    elif file_type == "location":
        kind = "location"
        location = {
            "latitude": attachment.get("coordinates_lat"),
            "longitude": attachment.get("coordinates_long"),
            "name": attachment.get("fallback_title") or None,
        }
    elif file_type:
        kind = FILE_KINDS.get(file_type, "unsupported")
        if kind == "audio":
            media_url = attachment.get("data_url")   # Task E addendum: the bot listens
    state = conversation_state(payload)
    source = payload.get("source_id")
    # A reply that quotes an earlier message (recorded 22 Oct): Chatwoot copies Meta's
    # context.id into in_reply_to_external_id, and in_reply_to is its own id for that
    # message when it has it in this conversation.
    quoted = payload.get("content_attributes") or {}
    reply_to = None
    if quoted.get("in_reply_to_external_id") or quoted.get("in_reply_to"):
        reply_to = {"id": quoted.get("in_reply_to_external_id"),
                    "chatwoot_id": quoted.get("in_reply_to")}
    return {
        **state,
        "wa_message_id": source or f"chatwoot-{payload.get('id')}",
        "chatwoot_message_id": payload.get("id"),
        "kind": kind,
        "text": text,
        "media_url": media_url,
        "location": location,
        "reply_to": reply_to,
    }


# ---- what Zolvion knows about each conversation --------------------------

def remember(phone: str, conversation_id: int, status: str | None, status_at: float,
             name: str | None = None) -> str | None:
    """Store the phone's conversation and its status, if this event is the newest, and
    return the status now stored. None: an older conversation (Chatwoot Cloud starts a
    new one after a resolve) or an older event. A status change Zolvion asked for and
    Chatwoot has not confirmed yet (push_status) is kept over a stale Pending."""
    if not phone or conversation_id is None or not status:
        return None
    with db.transaction():
        db.run(
            "INSERT INTO conversations(phone, display_name, created_at) VALUES(%s,%s,%s)"
            " ON CONFLICT (phone) DO NOTHING",
            (phone, name, db.now()),
        )
        row = db.q1("SELECT * FROM conversations WHERE phone=%s FOR UPDATE", (phone,))
        if name and name != row["display_name"]:
            db.run("UPDATE conversations SET display_name=%s WHERE phone=%s", (name, phone))
        current = row["chatwoot_conversation_id"]
        if current is not None and conversation_id < current:
            return None
        push = row["push_status"]
        if current == conversation_id:
            if row["status_at"] is not None and status_at < row["status_at"]:
                return None
            if push and status == PENDING and push != PENDING:
                status = push                 # our change has not landed yet
            elif push:
                push = None                   # it landed, or a person decided otherwise
        else:
            push = None                       # a new conversation starts clean
        db.run(
            "UPDATE conversations SET chatwoot_conversation_id=%s, chatwoot_status=%s,"
            " status_at=%s, push_status=%s WHERE phone=%s",
            (conversation_id, status, status_at, push, phone),
        )
    return status


def set_local(phone: str, status: str, push: bool = False) -> None:
    """Zolvion changed the status itself (or wants to: push=True leaves it for the
    worker to tell Chatwoot). Stamped with this machine's clock, so an event that
    left Chatwoot before the change can't undo it."""
    db.run(
        "UPDATE conversations SET chatwoot_status=%s, status_at=%s, push_status=%s,"
        " push_try_at=NULL WHERE phone=%s",
        (status, time.time(), status if push else None, phone),
    )


def conversation_for(phone: str) -> int | None:
    row = db.q1("SELECT chatwoot_conversation_id FROM conversations WHERE phone=%s", (phone,))
    return row["chatwoot_conversation_id"] if row else None


def bot_holds(phone: str) -> bool:
    """Whether the bot may answer this phone now. Outside Chatwoot mode, always."""
    if not enabled():
        return True
    row = db.q1("SELECT chatwoot_status FROM conversations WHERE phone=%s", (phone,))
    return row is None or row["chatwoot_status"] in (None, PENDING)


def link(conversation_id: int | None) -> str | None:
    """The conversation in Chatwoot's own interface, for the panel."""
    if not enabled() or conversation_id is None:
        return None
    return f"{base_url()}/app/accounts/{setting('CHATWOOT_ACCOUNT_ID')}/conversations/{conversation_id}"


# ---- calling Chatwoot (the bot's token) ----------------------------------

def _request(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{base_url()}/api/v1/accounts/{setting('CHATWOOT_ACCOUNT_ID')}{path}"
    try:
        response = httpx.request(
            method, url, headers={"api_access_token": setting("CHATWOOT_BOT_TOKEN")},
            json=body, timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise ChatwootError(f"{type(exc).__name__}: {exc}"[:300]) from exc
    if response.status_code >= 300:
        raise ChatwootError(f"HTTP {response.status_code}: {response.text[:200]}")
    try:
        return response.json()
    except json.JSONDecodeError:
        return {}


def send(conversation_id: int, text: str, private: bool = False) -> int | None:
    """A message to the customer, or a private note only staff read. Returns its id."""
    answer = _request("POST", f"/conversations/{conversation_id}/messages", {
        "content": text, "message_type": "outgoing", "private": private,
    })
    return answer.get("id")


def set_status(conversation_id: int, status: str) -> None:
    _request("POST", f"/conversations/{conversation_id}/toggle_status", {"status": status})


def download(url: str) -> tuple[bytes, str] | None:
    """An attachment, with no token (step 0: Cloud redirects to its file storage, and
    self-hosted serves it itself). A self-hosted Chatwoot writes its public address
    (FRONTEND_URL, an ngrok domain) into the link: the same path is fetched from
    CHATWOOT_URL instead, so it never goes out to the internet and back."""
    parts = urlsplit(url or "")
    if parts.path.startswith("/rails/active_storage/"):
        url = base_url() + parts.path + (f"?{parts.query}" if parts.query else "")
    try:
        response = httpx.get(url, follow_redirects=True, timeout=30)
    except httpx.HTTPError:
        return None
    mime = response.headers.get("content-type", "").split(";")[0].strip()
    if response.status_code >= 300 or not mime.startswith(("image/", "audio/", "application/ogg")):
        return None                   # e.g. ngrok's HTML warning page instead of the file
    return response.content, mime
