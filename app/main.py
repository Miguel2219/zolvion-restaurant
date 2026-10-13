"""The whole demo in one process: webhook in, kitchen and reports out.

This is deliberately not the real architecture. No queue, no worker, no AWS —
the real design lives in the zolvion-restaurant repo. This file exists to make a
convincing six-minute demo, nothing more."""

import base64
import uuid
import os
import re
import sqlite3
import traceback
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import brain, cart, db, handoff, orders, payments, reports, wa
from .catalog import CATALOG, reload as reload_catalog
from .money import fmt

STATIC = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Zolvion demo")
db.init()


@app.on_event("startup")
def _startup_check() -> None:
    """Say out loud what is and is not configured, before the first message
    arrives instead of after it fails."""
    key = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    chat_model, proof_model = brain.model_names()
    lines = [
        "",
        "  " + CATALOG.business["name"] + "  —  demo",
        f"  Anthropic key : {'OK' if key else 'FALTA  -> revisa .env y reinicia'}",
        f"  Conversación  : {chat_model}",
        f"  Comprobantes  : {proof_model}",
        f"  WhatsApp      : {'conectado' if wa.live() else 'modo simulador'}",
        f"  Firma webhook : {'verificando' if wa.app_secret() else 'sin APP_SECRET (no verifica)'}",
        "",
        "  Simulador  http://localhost:8000/simulator",
        "  Cocina     http://localhost:8000/kitchen",
        "  Reportes   http://localhost:8000/reports",
        "",
    ]
    print("\n".join(lines), flush=True)


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


def handle(
    phone: str, text: str | None, image: dict | None, received_at: str | None = None,
    customer: bool = True,
) -> str:
    """Returns the reply text. Caller decides how to deliver it.

    `received_at` is when the message actually arrived (both webhook handlers
    stamp inbound_messages with it) - the reference point for the proof's date
    check, so the OCR call's own latency never eats into the customer's window.
    `customer` is False when `text` is a note code wrote (_note_for), not the
    customer's words: only the customer's words can be a payment question."""
    if image is None:
        if not (text or "").strip():
            # The API refuses an empty message; the model still has to answer something.
            text = (
                "(El cliente envió un mensaje sin texto. Pregúntale con amabilidad en qué "
                "le puedes ayudar.)"
            )
            customer = False
        return brain.reply(phone, text, customer=customer)

    order = orders.latest_for(
        phone,
        ["PENDING_CONFIRMATION", "CONFIRMED", "ACCEPTED", "READY", "OUT_FOR_DELIVERY"],
    )
    if order is None:
        return brain.reply(
            phone,
            [image, {"type": "text", "text": "(el cliente envió una imagen y no tiene pedido activo)"}],
        )

    extracted = brain.extract_proof(image)          # model: OCR only
    src = image.get("source") or {}
    photo = (
        (base64.b64decode(src["data"]), src.get("media_type") or "image/jpeg")
        if src.get("data") else None
    )
    record = payments.check_proof(
        order["id"], extracted, photo=photo, received_at=received_at
    )                                               # code: the verdict

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


# ---- WhatsApp webhook -----------------------------------------------

@app.get("/webhook")
def verify(request: Request):
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == wa.verify_token():
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    return Response(status_code=403)


@app.post("/webhook")
async def inbound(request: Request):
    raw = await request.body()
    if not wa.check_signature(raw, request.headers.get("x-hub-signature-256")):
        return Response(status_code=403)          # rejected before any side effect

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return {"ok": True}

    for message in wa.parse_webhook(payload):
        wa_id = message.get("wa_message_id")
        phone = message.get("phone")
        if not wa_id or not phone:
            continue
        received_at = db.now()
        try:
            db.run(
                "INSERT INTO inbound_messages(wa_message_id, phone, body, kind, received_at)"
                " VALUES(?,?,?,?,?)",
                (wa_id, phone, message.get("text"), message.get("kind", "text"), received_at),
            )
        except sqlite3.IntegrityError:
            continue                              # duplicate delivery: exactly one row
        if message.get("kind") == "reaction":
            continue                              # an emoji on a message needs no answer

        image = None
        image_failed = False
        if message.get("media_id"):
            downloaded = wa.download_media(message["media_id"])
            if downloaded:
                image = wa.image_block(*downloaded)
            else:
                image_failed = True
        # Not text the bot can read: it gets a note instead of the (empty) text.
        note = _note_for(message, image_failed)
        text = note or message.get("text")
        try:
            reply = handle(phone, text, image, received_at, customer=note is None)
        except brain.NotConfigured as exc:
            print(f"\n!! {exc}\n", flush=True)
            reply = "Perdón, el sistema está en mantenimiento. Intente en unos minutos."
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            reply = "Perdón, se me cruzaron los cables. ¿Me repite el pedido?"
        wa.send_text(phone, _customer_safe(phone, reply))

    return {"ok": True}


# ---- browser simulator (Plan B) -------------------------------------

@app.post("/sim/message")
async def sim_message(request: Request):
    body = await request.json()
    phone = str(body.get("phone") or "573000000000")
    text = body.get("text")
    image = None
    data_url = body.get("image")
    if data_url:
        header, _, encoded = data_url.partition(",")
        mime = header.split(":", 1)[-1].split(";", 1)[0] or "image/jpeg"
        image = wa.image_block(base64.b64decode(encoded), mime)

    # Store it exactly like a real inbound message, so the conversation survives
    # leaving the page. The image itself is not kept, only that one was sent.
    received_at = db.now()
    db.run(
        "INSERT INTO inbound_messages(wa_message_id, phone, body, kind, received_at)"
        " VALUES(?,?,?,?,?)",
        (
            f"sim-{uuid.uuid4().hex}", phone,
            text or ("📎 comprobante" if image else ""),
            "image" if image else "text", received_at,
        ),
    )
    try:
        reply = handle(phone, text, image, received_at)
    except brain.NotConfigured as exc:
        print(f"\n!! {exc}\n", flush=True)
        return JSONResponse({"error": str(exc)}, status_code=503)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        return JSONResponse({"error": str(exc)}, status_code=500)
    reply = _customer_safe(phone, reply)
    wa.send_text(phone, reply)
    return {"reply": reply}


@app.get("/sim/outbound")
def sim_outbound(phone: str, after: int = 0):
    rows = db.q(
        "SELECT id, body, status, sent_at FROM outbound_messages"
        " WHERE phone=? AND id>? ORDER BY id",
        (phone, after),
    )
    return {"messages": [dict(r) for r in rows]}


@app.get("/sim/conversation")
def sim_conversation(phone: str):
    """The whole thread, both directions. The simulator loads this on open, so
    switching to the panel and coming back no longer loses what you typed."""
    thread = [
        {"role": "customer", "body": row["body"], "at": row["received_at"], "id": 0}
        for row in db.q(
            "SELECT body, received_at FROM inbound_messages WHERE phone=? ORDER BY rowid",
            (phone,),
        )
    ] + [
        {"role": "bot", "body": row["body"], "at": row["sent_at"], "id": row["id"]}
        for row in db.q(
            "SELECT id, body, sent_at FROM outbound_messages WHERE phone=? ORDER BY id",
            (phone,),
        )
    ]
    # Same second: the customer's message comes before the reply to it.
    thread.sort(key=lambda m: (m["at"], m["role"] == "bot"))
    return {
        "messages": thread,
        "last_bot_id": max((m["id"] for m in thread if m["role"] == "bot"), default=0),
    }


# ---- panel API -------------------------------------------------------

@app.get("/api/config")
def config():
    return {
        "business": CATALOG.business,
        "live_whatsapp": wa.live(),
        "accounts": CATALOG.accounts(),
    }


@app.get("/api/board")
def board():
    return {"orders": orders.board()}


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
        order = orders.apply_event(order_id, event, reason or None)
    except orders.IllegalTransition as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    notify(order, reason or None)
    return order


@app.post("/api/orders/{order_id}/handoff/resolve")
async def resolve_handoff(order_id: int):
    handoff.resolve(order_id)
    try:
        return orders.get(order_id)
    except KeyError:
        return JSONResponse({"error": "Pedido no encontrado."}, status_code=404)


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
        record = payments.confirm_receipt(order_id, receipt_id, body.get("amount"))
    except payments.PaymentError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    order = orders.get(order_id)
    wa.send_text(order["phone"], _payment_confirm_message(record))
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
        record = payments.discard_receipt(order_id, receipt_id, reason)
    except payments.PaymentError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    order = orders.get(order_id)
    wa.send_text(order["phone"], _discard_message(order, record["receipt"]["discard_reason"]))
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
        record = payments.discard_cash(order_id, cash_id, reason)
    except payments.PaymentError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return record


@app.get("/api/report")
def report(days: int = 1):
    return reports.snapshot(days)


@app.post("/api/reset")
def reset():
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


@app.get("/simulator")
def simulator():
    return FileResponse(STATIC / "simulator.html")
