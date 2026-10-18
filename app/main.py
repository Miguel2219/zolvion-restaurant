"""The MVP in one process: WhatsApp in (through Chatwoot, or straight from Meta),
kitchen panel and reports out.

The webhooks only store what arrived; worker.py answers it. Not the target
architecture (queue, worker service, AWS): that lives in the zolvion-restaurant repo."""

import base64
import json
import os
import re
import traceback
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import brain, chatwoot, db, handoff, orders, payments, reports, wa, worker
from .catalog import CATALOG, reload as reload_catalog
from .money import fmt

STATIC = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Zolvion demo")
db.init()


def channel() -> str:
    """How customers reach the bot: chatwoot, whatsapp (Meta directly), none (FAKE: no
    message leaves) or incomplete (some Chatwoot settings missing: nothing works)."""
    if chatwoot.incomplete():
        return "incomplete"
    if chatwoot.enabled():
        return "chatwoot"
    return "whatsapp" if wa.live() else "none"


@app.on_event("startup")
def _startup_check() -> None:
    """Say out loud what is and is not configured, before the first message
    arrives instead of after it fails."""
    key = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    chat_model, proof_model = brain.model_names()
    way = channel()
    whatsapp = {
        "chatwoot": f"por Chatwoot ({chatwoot.base_url()}, cuenta "
                    f"{chatwoot.setting('CHATWOOT_ACCOUNT_ID')})",
        "whatsapp": "conectado directo (Meta)",
        "none": "sin conexión: los mensajes al cliente no salen",
        "incomplete": f"FALTA {', '.join(chatwoot.missing())}  -> revisa .env y reinicia",
    }[way]
    signature = (
        "verificando (secreto del bot de Chatwoot)" if way == "chatwoot"
        else "verificando" if wa.app_secret() else "sin APP_SECRET (no verifica)"
    )
    lines = [
        "",
        "  " + CATALOG.business["name"] + "  —  demo",
        f"  Anthropic key : {'OK' if key else 'FALTA  -> revisa .env y reinicia'}",
        f"  Conversación  : {chat_model}",
        f"  Comprobantes  : {proof_model}",
        f"  WhatsApp      : {whatsapp}",
        f"  Firma webhook : {signature}",
        f"  Espera        : {worker.wait_seconds():g} s después del último mensaje",
        f"  Base de datos : PostgreSQL {db.describe()}",
        "",
        "  Cocina     http://localhost:8000/kitchen",
        "  Reportes   http://localhost:8000/reports",
    ]
    if way == "chatwoot":
        lines.append("  Webhook del Agent Bot: <dirección de Zolvion>/chatwoot/webhook")
    print("\n".join(lines + [""]), flush=True)


@app.on_event("startup")
def _start_worker() -> None:
    worker.start()


# ---- customer-facing notifications ----------------------------------

# notifications-and-followups.md §1. Exactly these states, and nothing else.
# No COMPLETED, no READY on a delivery order (OUT_FOR_DELIVERY carries it), and
# no CANCELLED when the customer did it themselves.
# The internal order code never appears here: it is for staff.
NOTIFY = {
    "ACCEPTED": "¡Recibimos su pedido! En un momento entra en preparación.",
    "READY": "Su pedido ya está listo para recoger.",
    "OUT_FOR_DELIVERY": "Su pedido va en camino.",
}

STOP_FALLBACK = {
    "REJECTED": "Lo sentimos, no pudimos tomar su pedido en este momento. {reason}",
    "CANCELLED": "Tuvimos que cancelar su pedido. {reason}",
}

# Rule 3: the customer never reads the order code or the word "cocina". SYSTEM forbids
# both, but the model is not trusted with it: every reply goes through here.
# Parentheses go only when they hold nothing but the code: "(su pedido P-4438)" keeps them.
_CODE = r"(?:\(\s*#?[Pp]-\d+\s*\)|#?\b[Pp]-\d+\b)"
ORDER_CODE = re.compile(r"[ \t]*" + _CODE)
# A line that began with the code: the word after it now starts the sentence.
CODE_AT_LINE_START = re.compile(r"(?m)^([ \t]*)" + _CODE + r"[ \t]*[:,.\-]?[ \t]*([¿¡]?)(\w)")
KITCHEN = re.compile(
    r"\b(?:(a|de)\s+)?(?:(la|una|esta|esa|nuestra|su|mi|tu)\s+)?cocina\b", re.IGNORECASE
)
DETERMINERS = {
    "la": "el", "una": "un", "esta": "este", "esa": "ese", "nuestra": "nuestro",
    "su": "su", "mi": "mi", "tu": "tu",
}


def _kitchen_to_local(match: re.Match) -> str:
    """'la cocina' -> 'el local', 'a la cocina' -> 'al local', 'de nuestra cocina' ->
    'de nuestro local': the determiner changes with the noun, so the sentence stays
    grammatical."""
    prep = (match.group(1) or "").lower()
    det = DETERMINERS.get((match.group(2) or "").lower(), "el")
    if prep and det == "el":
        words = {"a": "al", "de": "del"}[prep] + " local"
    else:
        words = " ".join(w for w in (prep, det, "local") if w)
    return words[0].upper() + words[1:] if match.group(0)[0].isupper() else words


def _clean_for_customer(text: str) -> str:
    """The text without order codes, and with 'cocina' said as 'el local'. Known
    limit: 'cocina' used as a verb ('el chef cocina') would become 'el local'."""
    text = CODE_AT_LINE_START.sub(lambda m: m.group(1) + m.group(2) + m.group(3).upper(), text)
    text = ORDER_CODE.sub("", text)
    text = re.sub(r"[ \t]+([,.;:!?])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return KITCHEN.sub(_kitchen_to_local, text)


def _customer_safe(phone: str, text: str) -> str:
    """The reply as the customer may read it. When it changed, the history is
    corrected too, so the model never builds on words the customer did not get."""
    clean = _clean_for_customer(text)
    if clean != text:
        brain.correct_last_reply(phone, clean)
    return clean


def _stop_message(order: dict, reason: str | None) -> str:
    """REJECTED and staff CANCELLED are mandatory messages, and a bare 'your
    order was cancelled' is not good enough. The model turns the staff's own
    words into something a customer can read, and falls back to a template if
    the model is unreachable."""
    what = (
        "rechazó el pedido del cliente antes de aceptarlo" if order["state"] == "REJECTED"
        else "canceló el pedido del cliente después de haberlo aceptado"
    )
    clean = (reason or "").strip()
    try:
        text = brain.reply(order["phone"], (
            f"(El local {what}. Motivo que escribió el personal: "
            f"\"{clean or 'sin motivo indicado'}\". Escríbele al cliente un mensaje corto, "
            "amable y claro explicándole el motivo en palabras sencillas, y discúlpate. "
            "No le menciones ningún código de pedido. No inventes un motivo distinto. "
            "Si aplica, ofrécele hacer otro pedido.)"
        ))
        return _customer_safe(order["phone"], text)
    except Exception:  # noqa: BLE001 - the customer must be told either way
        traceback.print_exc()
        template = STOP_FALLBACK[order["state"]]
        return template.format(reason=clean and f"Motivo: {clean}" or "").strip()


DISCARD_TEMPLATE = (
    "Revisamos el comprobante que nos envió y no lo tomamos como pago. Motivo: {reason}"
)

# The model's discard message is sent only if it contains none of these: it must
# not confirm a payment, write an amount, or show the order code / "cocina".
DISCARD_FORBIDDEN = ("confirmad", "cocina", "$")


def _guarded_reply(order: dict, instruction: str, template: str) -> str:
    """A model-written message about a discarded receipt, sent only if it contains none
    of DISCARD_FORBIDDEN nor the order code; the fixed template otherwise, or when the
    model fails."""
    try:
        text = brain.reply(order["phone"], instruction)
    except Exception:  # noqa: BLE001 - the customer must be told either way
        traceback.print_exc()
        return template
    low = text.lower()
    if order["code"].lower() in low or any(word in low for word in DISCARD_FORBIDDEN):
        brain.correct_last_reply(order["phone"], template)   # the history says what was sent
        return template
    return text


def _discard_message(order: dict, reason: str) -> str:
    """Staff discarded a receipt (it is not a payment). Like _stop_message, the
    model turns the staff's own reason into a short, kind message, and a fixed
    template is used if the model fails - or if what it wrote trips the guard.
    The template only quotes the staff's reason."""
    return _guarded_reply(order, (
        "(Una persona del local descartó un comprobante que envió el cliente: no cuenta "
        "como pago y ya no está en revisión. Motivo que escribió el personal: "
        f"\"{reason}\". Escríbele al cliente un mensaje corto y amable explicándole el "
        "motivo en palabras sencillas. No inventes un motivo distinto. No le digas que "
        "algún pago está confirmado. No escribas montos ni el signo $. Si el ESTADO REAL "
        "dice que todavía falta por transferir, puedes recordarle que falta, sin decir "
        "cuánto. No le menciones ningún código de pedido.)"
    ), DISCARD_TEMPLATE.format(reason=reason))


RESEND_DISCARDED_TEMPLATE = (
    "Ese comprobante ya lo habíamos revisado y no lo tomamos como pago. Motivo: {reason}"
)


def _resent_discarded_message(order: dict, reason: str) -> str:
    """The customer sent again a receipt staff had discarded (C2 Part 2, Gap 2): no new
    row, and the staff's reason again instead of "ya lo teníamos". Same guard as the
    discard message."""
    return _guarded_reply(order, (
        "(El cliente volvió a enviar un comprobante que el local ya había revisado y "
        "descartado: no cuenta como pago. Motivo que escribió el personal: "
        f"\"{reason}\". Dile en una o dos frases cortas y amables que ese comprobante ya lo "
        "habíamos revisado y no lo tomamos como pago, y explícale el motivo en palabras "
        "sencillas. No le digas que ya lo teníamos. No le digas que algún pago está "
        "confirmado. No escribas montos ni el signo $. No le menciones ningún código de pedido.)"
    ), RESEND_DISCARDED_TEMPLATE.format(reason=reason))


def notify(order: dict, reason: str | None = None) -> None:
    state = order["state"]

    if state in ("REJECTED", "CANCELLED"):
        # A customer who cancelled already knows. Only the restaurant's own
        # stops are announced (notifications-and-followups.md §1).
        if state == "CANCELLED" and orders.last_actor(order["id"]) != "staff":
            return
        wa.send_text(order["phone"], _stop_message(order, reason))
        return

    if state == "READY" and order["fulfilment_type"] == "DELIVERY":
        return

    body = NOTIFY.get(state)
    if body:
        wa.send_text(order["phone"], body)


# ---- one inbound message --------------------------------------------

# What a message the bot cannot read is called in its note (_note_for).
UNREADABLE = {
    "video": "un video",
    "document": "un documento",
    "contacts": "un contacto",
    "unsupported": "un mensaje que WhatsApp no deja ver",
}


def _note_for(message: dict, image_failed: bool) -> str | None:
    """A message that is not text the bot can read reaches it as a bracketed note,
    like the instructions in handle(): the API refuses an empty message, and the
    bot must never pretend it understood a voice note or a sticker. None means the
    message goes through as it is (text, a readable image, a button)."""
    kind = message.get("kind")
    if image_failed:
        caption = (message.get("text") or "").strip()
        wrote = f". Junto con ella escribió: «{caption}»" if caption else ""
        return (
            f"(El cliente envió una imagen, pero no se pudo descargar{wrote}. No digas que "
            "la recibiste ni que la revisaste. Pídele con amabilidad que la envíe otra vez.)"
        )
    if kind == "audio":
        return (
            "(El cliente envió una nota de voz. No la puedes escuchar y no sabes qué dice: "
            "nunca hagas como si la hubieras entendido. Dile con amabilidad que por ahora "
            "no puedes escuchar notas de voz, pídele que lo escriba y sigue con su pedido.)"
        )
    if kind == "sticker":
        return (
            "(El cliente envió un sticker. No lo puedes ver y no sabes qué muestra: nunca "
            "hagas como si lo hubieras entendido. Dile con amabilidad que por ahora no "
            "puedes ver stickers; si quería decir algo, pídele que lo escriba, y sigue con "
            "su pedido.)"
        )
    if kind in UNREADABLE:
        return (
            f"(El cliente envió {UNREADABLE[kind]}. No lo puedes abrir y no sabes qué "
            "contiene: nunca hagas como si lo hubieras entendido. Dile con amabilidad que "
            "por ahora solo puedes leer mensajes de texto y fotos, pídele que lo escriba y "
            "sigue con su pedido.)"
        )
    if kind == "location":
        place = message.get("location") or {}
        latitude, longitude = place.get("latitude"), place.get("longitude")
        details = [
            f"nombre: {place['name']}" if place.get("name") else None,
            f"dirección: {place['address']}" if place.get("address") else None,
            f"coordenadas: {latitude}, {longitude}"
            if latitude is not None and longitude is not None else None,
            f"enlace: {place['url']}" if place.get("url") else None,
        ]
        tail = (
            "Si quiere domicilio, usa esa dirección para la entrega; si no dice el barrio, "
            "pregúntaselo, porque el valor del domicilio depende del barrio. Confírmale la "
            "dirección."
            if place.get("address") else
            "No trae una dirección escrita: pídele con amabilidad la dirección escrita "
            "(calle, número y barrio)."
        )
        return (
            f"(El cliente compartió una ubicación de WhatsApp: {'; '.join(d for d in details if d)}. "
            f"{tail} Nunca inventes una dirección a partir de las coordenadas.)"
        )
    return None


# The API refuses an empty message; the model still has to answer something.
EMPTY_MESSAGE_NOTE = (
    "(El cliente envió un mensaje sin texto. Pregúntale con amabilidad en qué "
    "le puedes ayudar.)"
)


def receive_receipt(order: dict, image: dict, received_at: str | None) -> dict:
    """A receipt image for `order`: the model only reads it, code gives the verdict
    (payments.check_proof). No conversation: the worker also calls this while a person
    has the conversation in Chatwoot."""
    extracted = brain.extract_proof(image)          # model: OCR only
    src = image.get("source") or {}
    photo = (
        (base64.b64decode(src["data"]), src.get("media_type") or "image/jpeg")
        if src.get("data") else None
    )
    with db.write_lock:                             # code: the verdict
        return payments.check_proof(order["id"], extracted, photo=photo, received_at=received_at)


def handle(
    phone: str, text: str | None, image: dict | None, received_at: str | None = None,
    customer: bool = True,
) -> str:
    """Returns the reply text. Caller decides how to deliver it (worker._deliver).

    `received_at` is when the message actually arrived (both webhook handlers
    stamp inbound_messages with it) - the reference point for the proof's date
    check, so the OCR call's own latency never eats into the customer's window.
    `customer` is False when `text` is a note code wrote (_note_for), not the
    customer's words: only the customer's words can be a payment question."""
    if image is None:
        if not (text or "").strip():
            text = EMPTY_MESSAGE_NOTE
            customer = False
        return brain.reply(phone, text, customer=customer)

    order = orders.latest_for(phone, [*orders.EDITABLE, *orders.LOCKED])
    if order is None:
        return brain.reply(
            phone,
            [image, {"type": "text", "text": "(el cliente envió una imagen y no tiene pedido activo)"}],
        )

    record = receive_receipt(order, image, received_at)

    if record["resent"] and record.get("resent_discard_reason"):
        return _resent_discarded_message(order, record["resent_discard_reason"])
    if record["resent"]:
        return brain.reply(phone, (
            "(El cliente volvió a enviar un comprobante que ya habíamos recibido para este "
            "pedido. Dile en una frase corta y amable que ese comprobante ya lo teníamos. "
            "No le digas que el pago quedó confirmado. No le menciones ningún código de pedido.)"
        ))

    receipt = record["receipt"]
    if receipt["status"] == "FLAGGED":
        problems = " ".join(receipt["flags"])
        return brain.reply(phone, (
            f"(El sistema revisó el comprobante y encontró un problema: {problems} "
            "Avísale al cliente con amabilidad, sin acusarlo, y dile que una persona del "
            "local lo va a revisar. No digas que el pago quedó recibido. "
            "No le menciones ningún código de pedido.)"
        ))
    # What the customer is told about money is the code-built payment sentence, copied as
    # it is (C2 Part 2): the model never picks which parts to name. When the receipt
    # settled the method, the bot must not go back to asking it.
    settled = (
        " Con este comprobante quedó definida la forma de pago; no la vuelvas a preguntar."
        if record["inferred"] else ""
    )
    sentence = brain.payment_sentence_for(phone) or ""
    return brain.reply(phone, (
        f"(Mensaje del sistema, no del cliente. El sistema recibió un comprobante por "
        f"{fmt(receipt['amount'])} y los datos cuadran; quedó registrado y una persona del "
        f"local lo va a revisar.{settled} Dile al cliente que recibimos su comprobante y que "
        "una persona del local lo va a revisar, y copia tal cual, sin cambiar nada, esta frase "
        f"sobre su pago: «{sentence}» NO le digas que ya está confirmado ni que su "
        "transferencia está pagada. No le preguntes cómo va a pagar ni cuánto por "
        "transferencia o en efectivo: eso ya quedó definido. "
        "No le menciones ningún código de pedido.)"
    ), with_payment=True, action=brain.RECEIPT_ACTION)


# ---- incoming messages ----------------------------------------------
#
# Both webhooks only store what arrived and answer at once; worker.py answers the
# customer. With Chatwoot configured the Meta webhook is off (Meta talks to Chatwoot).

def _incomplete_setup() -> JSONResponse:
    return JSONResponse(
        {"error": f"Falta configurar Chatwoot: {', '.join(chatwoot.missing())}."},
        status_code=503,
    )


@app.get("/webhook")
def verify(request: Request):
    if chatwoot.enabled() or chatwoot.incomplete():
        return Response(status_code=404)
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == wa.verify_token():
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    return Response(status_code=403)


@app.post("/webhook")
async def inbound(request: Request):
    if chatwoot.incomplete():
        return _incomplete_setup()
    if chatwoot.enabled():
        return Response(status_code=404)
    raw = await request.body()
    if not wa.check_signature(raw, request.headers.get("x-hub-signature-256")):
        return Response(status_code=403)          # rejected before any side effect

    try:
        payload = json.loads(raw)
    except ValueError:
        return {"ok": True}

    with db.write_lock:
        for message in wa.parse_webhook(payload):
            wa_id = message.get("wa_message_id")
            phone = message.get("phone")
            if not wa_id or not phone:
                continue
            media = message.get("media_id")
            try:
                db.run(
                    "INSERT INTO inbound_messages(wa_message_id, phone, body, kind, received_at,"
                    " status, media_url, location) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        wa_id, phone, message.get("text"), message.get("kind") or "text",
                        db.now(),
                        # An emoji on a message needs no answer.
                        "DONE" if message.get("kind") == "reaction" else "RECEIVED",
                        f"whatsapp:{media}" if media else None,
                        json.dumps(message["location"]) if message.get("location") else None,
                    ),
                )
            except db.IntegrityError:
                continue                          # duplicate delivery: exactly one row
    return {"ok": True}


@app.post("/chatwoot/webhook")
async def chatwoot_webhook(request: Request):
    """The Agent Bot's webhook. No slow work here: past 5 s, or on an error, Chatwoot
    opens the conversation for a person and never sends the event again. So after the
    signature everything answers 200, even when storing failed (it is logged)."""
    if chatwoot.incomplete():
        return _incomplete_setup()
    if not chatwoot.enabled():
        return Response(status_code=404)
    raw = await request.body()
    headers = request.headers
    if not chatwoot.verify(raw, headers.get("x-chatwoot-timestamp"),
                           headers.get("x-chatwoot-signature")):
        return Response(status_code=401)          # rejected before any side effect
    try:
        payload = json.loads(raw)
        with db.write_lock:
            _chatwoot_event(payload)
    except Exception:  # noqa: BLE001 - see the docstring
        traceback.print_exc()
    return {"ok": True}


def _chatwoot_event(payload: dict) -> None:
    """One event, sorted by chatwoot.classify. Every event that carries a conversation
    updates its status first; Pending or resolved closes the amber box."""
    kind = chatwoot.classify(payload)
    if kind in ("own", "ignore"):
        return
    state = chatwoot.conversation_state(payload)
    phone = state["phone"]
    status = chatwoot.remember(
        phone, state["conversation_id"], state["status"], state["status_at"], state["name"]
    )
    if status == chatwoot.PENDING:
        handoff.close_for_phone(phone, ("OPEN",))
    elif status == "resolved":
        handoff.close_for_phone(phone, ("OPENING", "OPEN"))

    if kind == "customer":
        message = chatwoot.parse_customer(payload)
        if not phone:
            return
        try:
            db.run(
                "INSERT INTO inbound_messages(wa_message_id, phone, body, kind, received_at,"
                " status, conversation_id, media_url, location)"
                " VALUES(%s,%s,%s,%s,%s,'RECEIVED',%s,%s,%s)",
                (
                    message["wa_message_id"], phone, message["text"], message["kind"],
                    db.now(), message["conversation_id"], message["media_url"],
                    json.dumps(message["location"]) if message["location"] else None,
                ),
            )
        except db.IntegrityError:
            pass                                  # duplicate delivery: exactly one row
        return

    if kind == "staff" and status is not None:
        # A person wrote to the customer (in Chatwoot, or from the restaurant's phone
        # app under Coexistence). Kept for the bot's catch-up note; while the bot held
        # the conversation, it is a takeover.
        try:
            wa.record(phone, payload.get("content") or "(archivo adjunto)", "STAFF",
                      state["conversation_id"], payload.get("id"))
        except db.IntegrityError:
            return                                # duplicate delivery, or Zolvion's own message
        if status == chatwoot.PENDING:
            chatwoot.set_local(phone, chatwoot.OPEN, push=True)


# ---- panel API -------------------------------------------------------

# Panel routes: every write holds db.write_lock (the worker writes too). What waits on
# the model or on Chatwoot runs in a thread, so the server keeps answering webhooks
# inside Chatwoot's 5 s meanwhile.

@app.get("/api/config")
def config():
    way = channel()
    return {
        "business": CATALOG.business,
        "live_whatsapp": way in ("chatwoot", "whatsapp"),
        "channel": way,
        "missing": chatwoot.missing() if way == "incomplete" else [],
        "accounts": CATALOG.accounts(),
    }


@app.get("/api/board")
def board():
    found = orders.board()
    for order in found:
        # The conversation the order was made in; an older order, the phone's newest.
        order["chatwoot_link"] = chatwoot.link(
            order.get("chatwoot_conversation_id") or chatwoot.conversation_for(order["phone"])
        )
    return {"orders": found}


NEEDS_REASON = {"staff_rejects", "staff_cancels"}


@app.post("/api/orders/{order_id}/event")
async def order_event(order_id: int, request: Request):
    body = await request.json()
    event = body.get("event")
    reason = (body.get("reason") or "").strip()
    if event in NEEDS_REASON and not reason:
        return JSONResponse(
            {"error": "Hace falta el motivo para avisarle al cliente."}, status_code=400
        )
    try:
        with db.write_lock:
            order = orders.apply_event(order_id, event, reason or None)
    except orders.IllegalTransition as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    await run_in_threadpool(notify, order, reason or None)
    return order


RETURN_FAILED = "No se pudo devolver la conversación al bot en Chatwoot. Intente de nuevo."


@app.post("/api/orders/{order_id}/handoff/resolve")
async def resolve_handoff(order_id: int):
    """"Ya lo atendí". With Chatwoot the conversation goes back to Pending first, and
    only then the box closes: a box that closed while Chatwoot still shows a person
    would leave the customer with nobody answering."""
    try:
        order = orders.get(order_id)
    except KeyError:
        return JSONResponse({"error": "Pedido no encontrado."}, status_code=404)
    conversation_id = chatwoot.conversation_for(order["phone"]) if chatwoot.enabled() else None
    if conversation_id is not None and handoff.in_chatwoot(order_id):
        try:
            await run_in_threadpool(chatwoot.set_status, conversation_id, chatwoot.PENDING)
        except chatwoot.ChatwootError:
            traceback.print_exc()
            return JSONResponse({"error": RETURN_FAILED}, status_code=502)
        with db.write_lock:
            chatwoot.set_local(order["phone"], chatwoot.PENDING)
    with db.write_lock:
        handoff.resolve(order_id)
    return orders.get(order_id)


def _payment_confirm_message(record: dict) -> str:
    """Built from the numbers code just computed. Never claims more than was
    confirmed. While another receipt still waits for a person it says so instead of
    quoting a balance: that money may already be on its way. Otherwise it says what is
    still owed, split into what goes by transfer and what is paid in cash on delivery,
    and asks for a receipt only when a transfer is owed. A surplus is only what the
    confirmed receipts plus the registered cash cover beyond the total
    (confirmed_overpayment): a receipt nobody confirmed yet may turn out wrong."""
    amount = fmt(record["receipt"]["confirmed_amount"])
    parts = []
    pending = [r for r in record["receipts"] if r["status"] in ("PROOF_RECEIVED", "FLAGGED")]
    if pending:
        parts.append(
            f"Confirmamos su transferencia de {amount}. "
            "El otro comprobante que envió sigue en revisión."
        )
    elif record["outstanding"] > 0:
        text = f"Confirmamos su transferencia de {amount}."
        due = []
        if record["transfer_due"]:
            due.append(f"{fmt(record['transfer_due'])} por transferir")
        if record["cash_due"]:
            due.append(f"{fmt(record['cash_due'])} por pagar en efectivo al recibir")
        if due:
            text += f" Quedan {' y '.join(due)}."
        if record["transfer_due"]:
            text += " Cuando haga la transferencia, envíenos el comprobante."
        parts.append(text)
    if record["confirmed_overpayment"] > 0:
        surplus = (
            f"Vimos que envió {fmt(record['confirmed_overpayment'])} de más; "
            "el local se va a comunicar con usted por el excedente."
        )
        parts.append(surplus if parts else "Confirmamos su pago. " + surplus)
    if not parts:
        parts.append("Confirmamos su pago. ¡Gracias!")
    return " ".join(parts)


@app.post("/api/orders/{order_id}/receipts/{receipt_id}/confirm")
async def confirm_receipt(order_id: int, receipt_id: int, request: Request):
    body = await request.json()
    try:
        with db.write_lock:
            record = payments.confirm_receipt(order_id, receipt_id, body.get("amount"))
    except payments.PaymentError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    order = orders.get(order_id)
    await run_in_threadpool(wa.send_text, order["phone"], _payment_confirm_message(record))
    return record


@app.post("/api/orders/{order_id}/receipts/{receipt_id}/discard")
async def discard_receipt(order_id: int, receipt_id: int, request: Request):
    """A person discards a receipt with a required reason; the customer gets one
    message built from that reason (_discard_message)."""
    try:
        body = await request.json()
    except ValueError:
        body = {}
    reason = body.get("reason") if isinstance(body, dict) else None
    try:
        with db.write_lock:
            record = payments.discard_receipt(order_id, receipt_id, reason)
    except payments.PaymentError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    order = orders.get(order_id)

    def tell() -> None:
        wa.send_text(order["phone"], _discard_message(order, record["receipt"]["discard_reason"]))

    await run_in_threadpool(tell)
    return record


@app.get("/api/receipts/{receipt_id}/photo")
def receipt_photo(receipt_id: int):
    found = payments.photo(receipt_id)
    if found is None:
        return Response(content="Sin foto.", status_code=404, media_type="text/plain; charset=utf-8")
    data, mime = found
    return Response(content=data, media_type=mime, headers={"Cache-Control": "no-store"})


@app.post("/api/orders/{order_id}/payment/cash")
async def register_cash(order_id: int, request: Request):
    body = await request.json()
    try:
        with db.write_lock:
            record = payments.register_cash(order_id, body.get("amount"))
    except payments.PaymentError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return record


@app.post("/api/orders/{order_id}/cash/{cash_id}/discard")
async def discard_cash(order_id: int, cash_id: int, request: Request):
    """A person discards one cash registration (the X) with a required reason. Nobody
    is told: the customer never saw it registered."""
    try:
        body = await request.json()
    except ValueError:
        body = {}
    reason = body.get("reason") if isinstance(body, dict) else None
    try:
        with db.write_lock:
            record = payments.discard_cash(order_id, cash_id, reason)
    except payments.PaymentError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return record


@app.get("/api/report")
def report(days: int = 1):
    return reports.snapshot(days)


@app.post("/api/reset")
def reset():
    with db.write_lock:
        db.reset()
        brain.reset_all()
    return {"ok": True}


@app.post("/api/reload-config")
def reload_config():
    catalog = reload_catalog()
    brain.CATALOG = catalog
    return {"ok": True, "business": catalog.business["name"]}


# ---- static pages ----------------------------------------------------

@app.get("/")
def index():
    return FileResponse(STATIC / "kitchen.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/kitchen")
def kitchen():
    return FileResponse(STATIC / "kitchen.html")


@app.get("/reports")
def reports_page():
    return FileResponse(STATIC / "reports.html")
