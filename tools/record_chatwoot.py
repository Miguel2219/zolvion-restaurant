"""Task D step 0: record what Chatwoot really sends, before the integration is built.

    python tools/record_chatwoot.py                  # listen on :8000, save every webhook
    python tools/record_chatwoot.py --port 8001
    python tools/record_chatwoot.py bot-check 12     # what the bot token may do on conversation 12

Listen mode answers 200 at once (Chatwoot gives a bot 5 s, then hands the conversation to
a person), saves each request whole (headers and the exact body) under
recordings/chatwoot/, and prints one line per event. With CHATWOOT_BOT_SECRET in .env it
also checks the signature. For an attachment it tries to download data_url with no token
and keeps only the HTTP status, type and size, never the file.

bot-check calls the API with CHATWOOT_BOT_TOKEN: it reads the conversation, sends one
message to the customer and one private note, moves the conversation to "open" and back
to "pending". It really sends the message, so use a conversation with your own phone.

Recordings hold real phone numbers, names and messages: recordings/ is ignored by git.
Nothing here imports app/ or touches the database."""

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "recordings" / "chatwoot"
ENV = dotenv_values(ROOT / ".env")

_lock = threading.Lock()          # the file counter
_print_lock = threading.Lock()    # one event's lines stay together
_count = 0


def setting(name: str, default: str = "") -> str:
    return (os.environ.get(name) or ENV.get(name) or default).strip()


def _save(name: str, record: dict) -> Path:
    global _count
    with _lock:
        _count += 1
        number = _count
    path = OUT / f"{number:03d}-{name}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ---- listen mode ---------------------------------------------------------

def check_signature(raw: bytes, timestamp: str | None, signature: str | None) -> str:
    """The format read in Chatwoot's code: sha256=HMAC-SHA256(secret, "<timestamp>.<body>").
    On a mismatch it also tries the body alone, to learn what this Chatwoot really does."""
    if not signature:
        return "no X-Chatwoot-Signature header"
    secret = setting("CHATWOOT_BOT_SECRET")
    if not secret:
        return "not checked: no CHATWOOT_BOT_SECRET in .env"

    def digest(message: bytes) -> str:
        return "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()

    if hmac.compare_digest(digest(f"{timestamp}.".encode() + raw), signature):
        age = time.time() - int(timestamp) if (timestamp or "").isdigit() else None
        return "OK" + (f" (timestamp {age:.0f} s old)" if age is not None else "")
    if hmac.compare_digest(digest(raw), signature):
        return "MISMATCH with timestamp, but OK as HMAC of the body alone"
    return "MISMATCH"


def try_download(url: str) -> dict:
    try:
        response = httpx.get(url, follow_redirects=True, timeout=20)
        return {
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "bytes": len(response.content),
            "final_host": response.url.host,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}


def summarize(payload: dict) -> dict:
    """The fields the integration will decide on. Conversation events carry the
    conversation itself at the top level; message events carry it under "conversation"."""
    conversation = payload.get("conversation") or {}
    sender = payload.get("sender") or {}
    attributes = payload.get("content_attributes") or {}
    is_message = str(payload.get("event", "")).startswith("message_")
    return {
        "event": payload.get("event"),
        "message_type": payload.get("message_type"),
        "private": payload.get("private"),
        "sender_type": sender.get("type"),
        "conversation_id": (conversation.get("display_id") or conversation.get("id"))
        if is_message else payload.get("id"),
        "status": conversation.get("status") if is_message else payload.get("status"),
        "source_id": payload.get("source_id"),
        "external_echo": attributes.get("external_echo"),
        "attachments": [a.get("file_type") for a in payload.get("attachments") or []],
        "content": (payload.get("content") or "")[:60],
        "changed_attributes": payload.get("changed_attributes"),
    }


class Recorder(BaseHTTPRequestHandler):
    def do_GET(self):
        self._answer(b"recorder running\n", "text/plain")

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._answer(b'{"ok": true}', "application/json")     # first, inside the 5 s
        # The rest (an attachment download can take seconds) runs apart, so this
        # handler returns and the connection closes at once.
        threading.Thread(
            target=record, args=(raw, dict(self.headers), self.path), daemon=True
        ).start()

    def _answer(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def log_message(self, *args):     # one line per event is printed instead
        pass


def record(raw: bytes, headers: dict, request_path: str) -> None:
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    summary = summarize(payload) if isinstance(payload, dict) else {}
    attachments = (payload.get("attachments") or []) if isinstance(payload, dict) else []
    downloads = [
        {"file_type": a.get("file_type"), **try_download(a["data_url"])}
        for a in attachments if a.get("data_url")
    ]
    signature = check_signature(
        raw, headers.get("X-Chatwoot-Timestamp"), headers.get("X-Chatwoot-Signature")
    )
    name = "-".join(str(part) for part in (
        summary.get("event") or "unknown", summary.get("message_type"), summary.get("sender_type"),
    ) if part)
    shown = {k: v for k, v in summary.items() if v not in (None, "", [], {})}
    with _print_lock:                     # one event's lines stay together
        path = _save(name, {
            "received_at": datetime.now(timezone.utc).isoformat(),
            "path": request_path,
            "headers": headers,
            "signature_check": signature,
            "summary": summary,
            "download_checks": downloads,
            "body_base64": base64.b64encode(raw).decode(),
            "payload": payload,
        })
        print(f"{path.name}  signature: {signature}", flush=True)
        print(f"    {json.dumps(shown, ensure_ascii=False)}", flush=True)
        for check in downloads:
            print(f"    download without token: {json.dumps(check)}", flush=True)


def listen(port: int) -> None:
    secret = "set" if setting("CHATWOOT_BOT_SECRET") else "MISSING (signatures not checked)"
    print(f"Recording Chatwoot webhooks on http://127.0.0.1:{port} into {OUT}")
    print(f"CHATWOOT_BOT_SECRET: {secret}. Ctrl+C to stop.\n", flush=True)
    server = ThreadingHTTPServer(("127.0.0.1", port), Recorder)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\nStopped. {_count} request(s) saved in {OUT}")


# ---- bot-check mode ------------------------------------------------------

def bot_check(conversation_id: int) -> int:
    url = setting("CHATWOOT_URL").rstrip("/")
    account, token = setting("CHATWOOT_ACCOUNT_ID"), setting("CHATWOOT_BOT_TOKEN")
    if not url or not account or not token:
        print("Put CHATWOOT_URL, CHATWOOT_ACCOUNT_ID and CHATWOOT_BOT_TOKEN in .env first.")
        return 2
    base = f"{url}/api/v1/accounts/{account}/conversations/{conversation_id}"
    steps = [
        ("read the conversation", "GET", base, None),
        ("message to the customer", "POST", f"{base}/messages", {
            "content": "Mensaje de prueba de Zolvion (paso 0).",
            "message_type": "outgoing", "private": False,
        }),
        ("private note", "POST", f"{base}/messages", {
            "content": "Nota privada de prueba de Zolvion (paso 0).",
            "message_type": "outgoing", "private": True,
        }),
        ("status -> open (handoff)", "POST", f"{base}/toggle_status", {"status": "open"}),
        ("status -> pending (back to the bot)", "POST", f"{base}/toggle_status", {"status": "pending"}),
    ]
    failed = 0
    for label, method, target, body in steps:
        try:
            response = httpx.request(method, target, headers={"api_access_token": token},
                                     json=body, timeout=20)
            status, text = response.status_code, response.text
        except Exception as exc:  # noqa: BLE001
            status, text = "ERROR", f"{type(exc).__name__}: {exc}"
        failed += not (isinstance(status, int) and status < 300)
        print(f"{status}  {label}  {text[:150]}", flush=True)
        try:
            answer = json.loads(text)
        except ValueError:
            answer = text
        _save(f"bot-check-{label.split()[0]}-{status}", {
            "request": {"method": method, "path": target.replace(url, ""), "body": body},
            "status": status, "response": answer,
        })
        time.sleep(1)                  # leave the webhooks of each step in order
    print(f"\n{len(steps) - failed} of {len(steps)} calls worked. Responses saved in {OUT}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=8000)
    sub = parser.add_subparsers(dest="mode")
    check = sub.add_parser("bot-check", help="try the bot token on one conversation")
    check.add_argument("conversation_id", type=int)
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    global _count
    _count = len(list(OUT.glob("*.json")))         # numbering goes on across runs
    if args.mode == "bot-check":
        return bot_check(args.conversation_id)
    listen(args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
