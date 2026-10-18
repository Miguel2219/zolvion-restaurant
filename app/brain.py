"""The model reads what the customer wants and calls tools.

Hard rule: the model never produces a number that reaches the customer. It says
"add two Doble Parchada"; code looks up the price, multiplies, and writes the
total. Every tool below returns text that code built."""

import json
import os
import re
import threading
import traceback

from anthropic import Anthropic

from . import cart, db, handoff, orders, payments
from .catalog import CATALOG, _fold
from .money import fmt

_client = None
_model = None
_proof_model = None
_lock = threading.Lock()
_history: dict[str, list] = {}
HISTORY_TURNS = 24

# Haiku first: Sonnet 5.5 and Opus 5.5 refuse the forced order_status call that payment
# questions make (a 400 on every payment message), and Haiku 5.5 accepts it.
PREFERRED = ("haiku", "sonnet", "opus")


class NotConfigured(RuntimeError):
    pass


def client() -> Anthropic:
    global _client
    if _client is None:
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise NotConfigured(
                "Falta ANTHROPIC_API_KEY. Revisa que el archivo .env exista en la "
                "carpeta del proyecto, que tenga la línea ANTHROPIC_API_KEY=sk-ant-... "
                "y reinicia uvicorn."
            )
        _client = Anthropic(api_key=key)
    return _client


def model() -> str:
    """Use ANTHROPIC_MODEL if set; otherwise ask the API what exists and pick one.
    This avoids a hardcoded model id going stale."""
    global _model
    if _model:
        return _model
    from_env = os.environ.get("ANTHROPIC_MODEL", "").strip()
    if from_env:
        _model = from_env
        return _model
    available = [m.id for m in client().models.list(limit=50).data]
    for family in PREFERRED:
        for model_id in available:
            if family in model_id:
                _model = model_id
                return _model
    _model = available[0]
    return _model


def proof_model() -> str:
    """The model that reads receipt images (extract_proof): ANTHROPIC_PROOF_MODEL if
    set, otherwise the conversation model. A misread amount, date or account turns a
    good receipt red, so this one can be stronger than the conversation model."""
    global _proof_model
    if _proof_model:
        return _proof_model
    _proof_model = os.environ.get("ANTHROPIC_PROOF_MODEL", "").strip() or model()
    return _proof_model


def model_names() -> tuple[str, str]:
    """For the startup banner, without asking the API: the conversation model and the
    receipt model, as resolved so far or as .env says."""
    chat = _model or os.environ.get("ANTHROPIC_MODEL", "").strip() or "automático"
    proof = (_proof_model or os.environ.get("ANTHROPIC_PROOF_MODEL", "").strip()
             or f"{chat} (igual que conversación)")
    return chat, proof


SYSTEM = """{persona}

Eres un asistente de pedidos por WhatsApp. Respondes corto, como en un chat real:
1 o 2 frases, sin listas largas, sin markdown pesado, sin emojis salvo que el
cliente los use.

REGLAS QUE NO PUEDES ROMPER:
- NUNCA escribas precios, subtotales ni totales de tu cabeza. Los precios salen
  únicamente de las herramientas. Si necesitas mostrar un total, llama a la
  herramienta y copia el texto que te devuelve.
- NUNCA inventes productos. Si el cliente pide algo que no está en la carta,
  díselo con naturalidad y ofrécele lo más parecido que SÍ exista.
- Usa siempre el id exacto del producto al llamar herramientas.
- No confirmes un pedido por tu cuenta. Primero llama a create_order para que el
  cliente vea el resumen, y sólo llama a confirm_order cuando él diga que sí.
- Nunca digas que un pago quedó verificado. Eso lo decide una persona del local:
  sólo cuando el ESTADO REAL de abajo muestra un comprobante confirmado puedes
  contarle que el local ya lo confirmó.
- NUNCA le menciones al cliente el código interno del pedido (por ejemplo P-1234).
  Es para el personal del local, no para él. Habla siempre de "su pedido".
- Nunca digas "la cocina". Di "el local".
- Un pedido tiene dos momentos que NO debes confundir. CONFIRMADO POR EL
  CLIENTE: él dijo que sí, pero el local todavía no lo ha aceptado; todavía se
  puede cambiar. ACEPTADO POR EL LOCAL: el personal del local lo aceptó en el
  panel. Que el cliente haya confirmado NO significa que el local lo aceptó.
  Nunca le digas al cliente que el local ya aceptó su pedido si el ESTADO REAL
  de abajo o una herramienta no te lo dice.
- Si el cliente quiere cambiar algo de su pedido, usa SIEMPRE las herramientas
  de siempre (add_item, remove_item, set_qty, y create_order para la forma de
  entrega o la dirección). El sistema decide: si el local todavía no lo ha
  aceptado, cambia ESE MISMO pedido; si ya lo aceptó, se lo pasa a una
  persona. Nunca uses request_handoff para un cambio y nunca hagas un pedido
  nuevo para un cambio. Para cambiar un producto por otro, agrega primero el
  nuevo y después quita el anterior.
- Si una herramienta te responde PASADO A UNA PERSONA, el local ya quedó avisado.
  Dile al cliente con amabilidad que una persona del local va a revisar su
  solicitud y se comunica con él. Hay dos casos:
  · Si te respondió una herramienta de cambio (add_item, remove_item, set_qty,
    create_order, cancel_order o choose_payment), el sistema no hizo el cambio.
    No le prometas que el cambio se va a poder hacer.
  · Si te respondió request_handoff (topic OTHER), el cliente no pidió ningún
    cambio. No le menciones ningún cambio.
- Si el cliente pide hablar con una persona (alguien del local, un humano, un
  asesor) sin decir para qué, pregúntale una sola vez, con amabilidad, en qué le
  puedes ayudar. Todavía no le digas si se puede o no, ni en qué estado está su
  pedido. Cuando te diga el motivo: si una herramienta lo cubre, usa esa
  herramienta. Si ninguna lo cubre, o si no quiere decir el motivo, llama a
  request_handoff con topic OTHER. Llámala SIEMPRE, cada vez; nunca respondas de
  memoria si el intento anterior funcionó.
- NUNCA le prometas al cliente algo que va a pasar más adelante (por ejemplo que
  podrá hablar con una persona cuando el local acepte o confirme su pedido).
- Si el cliente pide un pedido NUEVO y aparte mientras el anterior ya fue
  aceptado por el local, llama a start_separate_order antes de agregar productos.

CARTA (id | nombre | precio | descripción):
{menu}

DATOS DEL NEGOCIO:
- Horario: {open} a {close}
- Domicilio: {delivery}
- Cuentas para transferencia:
{accounts}
"""


STATE_LABELS = {
    "PENDING_CONFIRMATION": "esperando su confirmación",
    "CONFIRMED": "confirmado; el local todavía no lo ha aceptado",
    "ACCEPTED": "aceptado por el local",
    "READY": "listo",
    "OUT_FOR_DELIVERY": "en camino",
    "COMPLETED": "entregado",
    "CANCELLED": "cancelado",
    "REJECTED": "no pudo ser tomado por el local",
    "EXPIRED": "vencido por falta de respuesta",
}


PAYMENT_STATE = {
    "UNSPECIFIED": "todavía no ha dicho cómo va a pagar",
    "CASH": "en efectivo al recibir",
    "TRANSFER": "por transferencia",
    "MIXED": "parte por transferencia y parte en efectivo",
}

PAYMENT_RULES = (
    "La forma de pago y los comprobantes de este bloque son los reales. Si aquí aparece "
    "un comprobante, nunca digas que no te aparece ninguno; si aquí no aparece, no lo des "
    "por recibido. Si aquí aparece uno confirmado por una persona del local, puedes "
    "decirle que el local ya lo confirmó; nunca digas que está confirmado uno que aquí "
    "no aparece confirmado. Un comprobante descartado por el local no cuenta como pago "
    "y ya no está en revisión: nunca digas que sigue en revisión. Si aquí ya dice cómo "
    "va a pagar, no se lo vuelvas a preguntar; sólo si el cliente quiere cambiarlo, usa "
    "choose_payment. Si aquí dice que ya está pagado completo, nunca le preguntes cómo "
    "va a pagar ni le digas que no tiene pago registrado. Si aquí dice que el local ya "
    "recibió el pago completo en efectivo, el pedido ya está pagado aunque antes haya "
    "elegido transferencia: nunca le pidas que transfiera ni que envíe un comprobante, "
    "ni le des las cuentas para pagar. "
    "Si aquí dice que el local anuló un registro de efectivo, no lo menciones si el cliente "
    "no pregunta por eso. Si pregunta por ese pago o por qué cambió lo que le falta, dile que "
    "el local anuló ese registro de efectivo. Nunca digas que se le devolvió dinero ni que no "
    "entregó el dinero. Si antes dijiste que el pedido estaba pagado y aquí ya no lo dice, "
    "vale lo de aquí. "
    "Para decir cuánto falta o cómo va el pago, usa sólo la frase de pago que te dio "
    "order_status en este mismo mensaje, copiada tal cual. Nunca uses montos de mensajes "
    "anteriores como lo que falta ahora."
)


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _payment_state(phone: str) -> str | None:
    """The newest live order's payment in words: the method, its receipts, what is
    still owed and whether it is fully paid. That order is where a receipt image
    lands (main.handle). Cash that paid it all comes first. No amounts: the numbers
    the customer sees come from tools and instructions."""
    order = _payment_order(phone)
    if order is None or "payment" not in order:
        return None
    pay = order["payment"]
    # Discarded receipts are listed too: left out, the line was silent about them
    # and the model kept the "en revisión" from the chat history.
    found = [r["status"] for r in pay["receipts"]]
    # Cash that paid it all comes first, with nothing about a transfer (C2 R2a, P-0644).
    cash_paid_all = 0 < pay["total"] <= pay["cash_received"]
    if cash_paid_all:
        chosen = {"TRANSFER": "había elegido transferencia, pero ",
                  "MIXED": "había elegido parte por transferencia y parte en efectivo, pero "}.get(pay["method"], "")
        parts = [chosen + "el local ya recibió el pago completo en efectivo"]
    else:
        parts = [PAYMENT_STATE.get(pay["method"], pay["method"])]
        if pay["cash_received"] > 0:
            parts.append("el local ya recibió una parte en efectivo")
    # Discarded cash is named apart from the cash that still counts (C2 Part 2, Gap 6):
    # left out, an earlier "ya está pagado" in the chat would survive the discard.
    annulled = sum(1 for c in pay.get("cash", []) if c["status"] == "DISCARDED")
    if annulled:
        parts.append(
            "el local anuló 1 registro de efectivo, que ya no cuenta como pago" if annulled == 1
            else f"el local anuló {annulled} registros de efectivo, que ya no cuentan como pago"
        )
    if not found:
        parts.append("no hace falta que transfiera ni que envíe comprobante" if cash_paid_all
                     else "no ha enviado ningún comprobante")
    else:
        kinds = [
            ("PROOF_RECEIVED", "recibido", "recibidos", "que revisa una persona del local"),
            ("CONFIRMED", "confirmado", "confirmados", "por una persona del local"),
            ("FLAGGED", "con un problema", "con un problema", "que revisa una persona del local"),
            ("DISCARDED", "descartado", "descartados", "por el local"),
        ]
        parts.append("comprobantes: " + ", ".join(
            f"{_count(found.count(status), one, many)} {tail}"
            for status, one, many, tail in kinds if status in found
        ))
    if pay["under_review"]:
        parts.append("hay un comprobante en revisión: no le pidas que pague otra vez")
    elif pay["method"] in ("TRANSFER", "MIXED") and not cash_paid_all:
        parts.append(
            "todavía falta por transferir" if pay["transfer_due"]
            else "ya no falta nada por transferir"
        )
    if pay["method"] in ("CASH", "MIXED") and pay["cash_due"]:
        parts.append("queda por pagar en efectivo al recibir")
    # Fully paid is the card's "Pago completo": confirmed money and registered cash.
    # Money that was sent but waits for a person is not "falta por pagar", or the
    # model asks for it again.
    if not pay["outstanding"]:
        parts.append("ya está pagado completo")
    elif pay["sent"] + pay["cash_received"] >= pay["total"]:
        parts.append("todavía falta que una persona confirme el pago")
    else:
        parts.append("todavía falta por pagar")
    return "- Pago del pedido más reciente: " + "; ".join(parts) + "."


def _real_state(phone: str) -> str:
    """What the orders really are right now, computed by code on every model call.
    Notifications and the panel's receipt confirmations never enter the chat
    history, so without this the model would answer from its own stale tool
    results. No order code and no totals go here."""
    live = orders.live_states(phone)
    if live:
        lines = [
            f"- {'Pedido más reciente' if i == 0 else 'Otro pedido'}: "
            f"{STATE_LABELS.get(state, state)}."
            for i, state in enumerate(live)
        ]
    else:
        latest = orders.latest_for(phone)
        lines = [
            f"- Pedido más reciente: {STATE_LABELS.get(latest['state'], latest['state'])}."
            if latest else "- No tiene pedidos."
        ]
    payment = _payment_state(phone)
    if payment:
        lines += [payment, PAYMENT_RULES]
    return (
        "ESTADO REAL AHORA (lo calcula el sistema en cada mensaje):\n"
        + "\n".join(lines) + "\n"
        "Este bloque vale más que cualquier cosa dicha antes en la conversación: si "
        "contradice un mensaje anterior o el resultado de una herramienta anterior, este "
        "bloque gana. Menciónale el estado al cliente sólo cuando importe. Nunca le "
        "menciones ningún código."
    )


def system_prompt(phone: str) -> str:
    fulfilment = CATALOG.fulfilment
    zones = ", ".join(
        f"{z['name']} {fmt(z['fee'])}" for z in fulfilment.get("delivery_zones", [])
    )
    base = SYSTEM.format(
        persona=CATALOG.business.get("persona", ""),
        menu=CATALOG.menu_for_prompt(),
        open=CATALOG.hours.get("open"),
        close=CATALOG.hours.get("close"),
        delivery=zones or "no disponible",
        accounts=CATALOG.accounts_for_prompt() or "  (ninguna configurada)",
    )
    return base + "\n" + _real_state(phone)


# ---- the payment sentence (C2 Part 2) ---------------------------------
#
# What the customer is told about money is built here from payments.current() and
# copied by the bot word for word. _finish_turn checks every reply that speaks about
# payment before it is sent: the sentence must be there, and nothing else in the reply
# may contradict it.

def _payment_order(phone: str):
    """The newest live order. ESTADO REAL's payment line, the payment sentence and a
    receipt image (main.handle) all speak about this one."""
    return orders.latest_for(phone, [*orders.EDITABLE, *orders.LOCKED])


def _accounts_text() -> str:
    return " o ".join(
        f"{a['provider']} {a['number']} a nombre de {a['holder']}" for a in CATALOG.accounts()
    )


def payment_sentence(pay: dict, several_live: bool = False) -> str:
    """What is paid and what is still owed, from current(): every number is current()'s,
    only formatted here. Each part only when it applies. A transfer nobody confirmed is
    never called paid, and a red receipt blocks any request for more money."""
    statuses = [r["status"] for r in pay["receipts"]]
    confirmed_n, waiting_n, red_n = (
        statuses.count(s) for s in ("CONFIRMED", "PROOF_RECEIVED", "FLAGGED")
    )
    parts = []
    if pay["cash_received"]:
        parts.append(f"Ya pagó {fmt(pay['cash_received'])} en efectivo.")
    if pay["confirmed"]:
        parts.append(
            f"El local ya confirmó su transferencia de {fmt(pay['confirmed'])}." if confirmed_n == 1
            else f"El local ya confirmó sus transferencias por {fmt(pay['confirmed'])}."
        )
    if pay["unconfirmed_amount"]:
        parts.append(
            f"Su comprobante por {fmt(pay['unconfirmed_amount'])} está pendiente de "
            "confirmación del local." if waiting_n == 1 else
            f"Sus comprobantes por {fmt(pay['unconfirmed_amount'])} están pendientes de "
            "confirmación del local."
        )
    if red_n:
        parts.append(
            "El local está revisando " + ("un comprobante" if red_n == 1 else "unos comprobantes")
            + " que nos envió y se comunicará con usted."
        )
    if not pay["outstanding"]:
        parts.append("Su pedido ya está pagado completo.")       # a red receipt never hides it
    elif not pay["to_pay"]:
        parts.append("Por ahora no necesita realizar otro pago.")  # a waiting receipt covers it
    elif pay["under_review"]:
        pass                                                         # no payment request while red
    elif pay["method"] == "UNSPECIFIED":
        parts.append(f"Falta por pagar {fmt(pay['to_pay'])}.")
    else:
        due = []
        if pay["transfer_due"]:
            due.append(f"{fmt(pay['transfer_due'])} por transferir")
        if pay["cash_due"]:
            due.append(f"{fmt(pay['cash_due'])} por pagar en efectivo al recibir")
        parts.append(f"Quedan {' y '.join(due)}.")
        if pay["transfer_due"] and CATALOG.accounts():
            parts.append(
                f"Puede transferir a {_accounts_text()}. "
                "Cuando transfiera, envíenos el comprobante."
            )
    if pay["confirmed_overpayment"]:
        parts.append(
            f"Pagó {fmt(pay['confirmed_overpayment'])} más que el total del pedido; el local "
            "se comunicará con usted por esa diferencia."
        )
    elif pay["overpayment_amount"]:
        # G32: the surplus counts a receipt nobody confirmed. Never an amount owed back.
        parts.append(
            "Con su comprobante pendiente, lo enviado supera el total del pedido; el local "
            "revisará la diferencia cuando lo confirme." if waiting_n <= 1 else
            "Con sus comprobantes pendientes, lo enviado supera el total del pedido; el local "
            "revisará la diferencia cuando los confirme."
        )
    text = " ".join(parts)
    if several_live:
        text = "En su pedido más reciente: " + text[0].lower() + text[1:]
    return text


def payment_sentence_for(phone: str) -> str | None:
    """The sentence for the newest live order, None without one."""
    order = _payment_order(phone)
    if order is None or "payment" not in order:
        return None
    return payment_sentence(order["payment"], len(orders.live_states(phone)) > 1)


class _Turn:
    """What one bot turn did, for _finish_turn. Tools write into it (_run_tool runs
    inside the turn, under _lock)."""

    def __init__(self, customer: bool = False, with_payment: bool = False):
        self.customer = customer             # the customer's own text, not a system instruction
        self.needs_sentence = with_payment   # the reply must carry the payment sentence
        self.question = False                # the customer asked about payment
        self.status_question = False         # ...about whether it is paid or what is left
        self.unverifiable = False            # the payment asked about can't be checked
        self.failed = False                  # reading the payment failed
        self.sentences: list[str] = []       # every payment sentence handed out in this turn
        self.actions: dict[str, str] = {}    # code-built confirmation of what was done
        self.code_text: list[str] = []       # code-built text of this turn (tool results)


_turn = _Turn()


def _action(kind: str, text: str) -> None:
    """What was done in this turn, as a code-built line the safe response keeps."""
    _turn.actions.pop(kind, None)            # the latest one of a kind wins, at the end
    _turn.actions[kind] = text


def _hand_out_sentence(phone: str, pay: dict, required: bool) -> str:
    sentence = payment_sentence(pay, len(orders.live_states(phone)) > 1)
    _turn.sentences.append(sentence)
    if required:
        _turn.needs_sentence = True
    return sentence


HANDOFF_ACTION = "Una persona del local va a revisar su solicitud y se comunicará con usted."
RECEIPT_ACTION = "Recibimos su comprobante; una persona del local lo va a revisar."
PAYMENT_UNVERIFIED = "No pude verificar el pago de ese pedido."
PAYMENT_UNAVAILABLE = (
    "En este momento no pude revisar el estado de su pago. "
    "Por favor escríbame de nuevo en unos minutos."
)
PAYMENT_CORRECTION = (
    "Tu borrador anterior decía sobre el pago algo que no coincide con el estado real. "
    "Responde de nuevo y usa, copiada tal cual, sólo esta frase sobre el pago: «{sentence}». "
    "No escribas otros montos ni digas otra cosa sobre si está pagado o cuánto falta."
)
UNVERIFIED_CORRECTION = (
    "No pudiste verificar el pago de ese pedido. Responde de nuevo: dile sólo que no pudiste "
    "verificar el pago de ese pedido. No digas si está pagado ni cuánto falta, y no escribas "
    "montos de pagos."
)
UNVERIFIED_TOOL = (
    "NO SE PUDO verificar el pago de ese pedido: no digas si está pagado ni cuánto falta; "
    "dile que no pudiste verificarlo. No le menciones ningún código."
)

# On folded text (lowercase, no accents). A customer message with one of these words
# is a payment question: the turn starts with a forced order_status (Gap 5).
PAYMENT_WORDS = re.compile(
    r"\b(pag\w*|debo|debe|debemos|saldo|comprobantes?|transfer\w*|consign\w*|efectivo|plata|"
    r"dinero|nequi|daviplata|bancolombia|devuelt\w*|devolu\w*|vueltos?|cobr\w*)\b"
    r"|\bcuanto\s+(me|le|nos)\s+(falta|faltan|queda|quedan|debo|sale)\b"
)
STATUS_QUESTION = re.compile(
    r"\b(quedo|esta|estaria|queda)\s+(todo\s+)?pag|como\s+va\s+(mi|el)\s+pago|"
    r"cuanto\s+(me\s+|le\s+)?(falta|faltan|queda|quedan|debo)|\bdebo\b|\bsaldo\b|"
    r"ya\s+(les\s+)?(llego|pague)|\bquedo\s+todo\b|\besta\s+todo\b"
)
# The customer means another order than the newest live one: its payment is not used.
OTHER_ORDER = re.compile(
    r"\b(pedido|orden)\s+(anterior|pasado|de\s+ayer|de\s+antes|viejo|primero)\b|\bprimer\s+pedido\b"
    r"|\botro\s+pedido\b|\b(el|la|lo)\s+(anterior|primero|primera|de\s+ayer|de\s+antes)\b|\bayer\b"
)
# Claims a reply can make about the payment, with or without an amount.
PAID_CLAIM = re.compile(
    r"\bya\s+(esta|quedo)\s+(pagad\w*|pago\b|cubierto)|\bpagad[oa]\s+complet|\bpago\s+complet"
    r"|\bno\s+(debe|adeuda)\s+nada|\bno\s+le\s+falta\s+nada|\bpaz\s+y\s+salvo"
    r"|\btodo\s+(esta|quedo)\s+(pago|pagado)\b|\bya\s+pago\b(?!\s+(una\s+)?parte)(?!\s*\$)(?!\s*\d)"
)
NO_MORE_CLAIM = re.compile(
    r"\bno\s+(necesita|tiene\s+que|hace\s+falta)\s+(realizar\s+|hacer\s+)?(otro|ningun|mas)\s+pago"
    r"|\bno\s+tiene\s+que\s+pagar\s+(nada\s+)?mas"
)
OWES_CLAIM = re.compile(
    r"\b(falta|faltan|quedan)\b[^.!?\n]{0,40}?\bpor\s+(pagar|transferir)\b|\bdebe\s+(pagar|transferir)\b"
    r"|\bnos\s+debe\b|\bno\s+ha\s+pagado\b|\bsin\s+pagar\b|\bpendiente\s+de\s+pago\b"
    r"|\bquedan?\s+debiendo\b"
)
BALANCE_WORDS = re.compile(r"\b(falta|faltan|quedan|debe|debo|saldo)\b")
AMOUNT = re.compile(r"\$\s?(\d{1,3}(?:[.,]\d{3})+|\d+)")
CLAUSE_END = re.compile(r"[.;,:!?](?=\s|$)|\n|\s+y\s+|\s+e\s+")
# What a clause says an amount is. The last one before the amount wins.
MEANINGS = [
    (re.compile(r"\banul\w*|\bdescart\w*"), "discarded"),
    (re.compile(r"\bde\s+mas\b|\bmas\s+que\s+el\s+total|\bexcedente|\bsobrante"), "surplus"),
    (re.compile(r"\b(falta|faltan|quedan|debe|debo|saldo)\b|\bpor\s+pagar\b|\bpor\s+transferir\b"
                r"|\bpendiente\s+de\s+pago\b"), "owed"),
    (re.compile(r"\bconfirm\w*"), "confirmed"),
    (re.compile(r"\b(pago|pagado|pagada|pagaron|abono|recibio|recibimos|recibido|recibida)\b"), "paid"),
    (re.compile(r"\bcomprobantes?\b|\bpendientes?\b|\benvio\b|\benviado\b|\bmando\b"), "receipt"),
    (re.compile(r"\b(total|subtotal|pedido|domicilio|valor|vale|cuesta|precio)\b"), "total"),
]
TRANSFER_WORDS = re.compile(r"transfer")
CASH_WORDS = re.compile(r"\befectivo\b|\bal\s+recibir\b|\bal\s+recoger\b")


def _norm(text: str) -> str:
    return " ".join(str(text).split())


def _amount_of(match: re.Match) -> int:
    return int(re.sub(r"[.,]", "", match.group(1)))


def _talks_payment(text: str) -> bool:
    """Does this reply claim anything about whether the order is paid or what is owed?"""
    folded = _fold(text)
    return bool(
        PAID_CLAIM.search(folded) or NO_MORE_CLAIM.search(folded) or OWES_CLAIM.search(folded)
        or (AMOUNT.search(folded) and BALANCE_WORDS.search(folded))
    )


def _meaning(prefix: str, suffix: str) -> str | None:
    """What a clause says one amount is: the last meaning word before it, else the
    first one after it. "Pagó $X más que el total" is a surplus, whatever comes first."""
    if MEANINGS[1][0].search(suffix):
        return "surplus"
    best = None
    for regex, meaning in MEANINGS:
        for found in regex.finditer(prefix):
            if best is None or found.end() > best[0]:
                best = (found.end(), meaning)
    if best:
        return best[1]
    first = None
    for regex, meaning in MEANINGS:
        found = regex.search(suffix)
        if found and (first is None or found.start() < first[0]):
            first = (found.start(), meaning)
    return first[1] if first else None


def _code_amounts(phone: str) -> set[int]:
    """Amounts code wrote in this turn (without the «…» payment sentences, which are
    checked by meaning), the newest live order's own figures and catalog prices."""
    found = set(CATALOG.prices())
    for text in _turn.code_text:
        found |= {_amount_of(m) for m in AMOUNT.finditer(re.sub(r"«[^»]*»", " ", str(text)))}
    try:
        order = _payment_order(phone)
    except Exception:  # noqa: BLE001 - a failed read only means fewer amounts are allowed
        order = None
    if order is not None:
        found |= {order["total"], order["subtotal"], order["delivery_fee"]}
        found |= {item["line_total"] for item in order["items"]}
    return {amount for amount in found if amount}


def _wrong_amount(text: str, pay: dict | None, phone: str) -> bool:
    """True when an amount in the reply does not match what its own clause says it is.
    An amount found somewhere in the data is not enough: "le faltan $5.000" must be
    what is owed now, even if $5.000 was once registered and later discarded. A
    discarded row's amount only explains a discard. `pay` None: nothing is verified,
    so no amount may describe a payment."""
    code = _code_amounts(phone)
    discarded, receipts = set(), set()
    if pay is not None:
        discarded = {c["amount"] for c in pay["cash"] if c["status"] == "DISCARDED"}
        for r in pay["receipts"]:
            target = discarded if r["status"] == "DISCARDED" else receipts
            target |= {a for a in (r["amount"], r["confirmed_amount"]) if a}
    for clause in CLAUSE_END.split(_fold(text)):
        found = list(AMOUNT.finditer(clause or ""))
        for i, match in enumerate(found):
            value = _amount_of(match)
            prefix = clause[:match.start()]
            suffix = clause[match.end():found[i + 1].start() if i + 1 < len(found) else len(clause)]
            meaning = _meaning(prefix, suffix)
            allowed = set()
            if MEANINGS[0][0].search(clause):
                allowed |= discarded                   # explains a discard, nothing more
            if meaning in (None, "total"):
                allowed |= code
            elif pay is None:
                pass                                   # unverified: no payment amount at all
            elif meaning == "surplus":
                allowed.add(pay["confirmed_overpayment"])
            elif meaning == "owed":
                # The channel named right after the amount, else right before it.
                if TRANSFER_WORDS.search(suffix):
                    allowed.add(pay["transfer_due"])
                elif CASH_WORDS.search(suffix):
                    allowed.add(pay["cash_due"])
                elif TRANSFER_WORDS.search(prefix):
                    allowed.add(pay["transfer_due"])
                elif CASH_WORDS.search(prefix):
                    allowed.add(pay["cash_due"])
                else:
                    allowed.add(pay["to_pay"])
            elif meaning == "paid":
                if CASH_WORDS.search(suffix + " " + prefix):
                    allowed.add(pay["cash_received"])
                elif TRANSFER_WORDS.search(suffix + " " + prefix):
                    allowed.add(pay["confirmed"])
                else:
                    allowed |= {pay["cash_received"], pay["confirmed"], pay["received"], pay["paid"]}
            elif meaning == "confirmed":
                allowed.add(pay["confirmed"])
                allowed |= {r["confirmed_amount"] for r in pay["receipts"]
                            if r["status"] == "CONFIRMED"}
            elif meaning == "receipt":
                allowed |= receipts | {pay["unconfirmed_amount"]}
                allowed |= code
            elif meaning == "discarded":
                pass                                   # only the discarded amounts above
            if value not in {a for a in allowed if a}:
                return True
    return False


def _problems(draft: str, sentence: str, pay: dict, phone: str) -> tuple[bool, bool]:
    """(the sentence is missing, something else in the reply is wrong)."""
    text, want = _norm(draft), _norm(sentence)
    missing = want not in text
    rest = text.replace(want, " ")
    # An earlier sentence of this turn that a later change made stale.
    wrong = any(_norm(s) != want and _norm(s) in rest for s in _turn.sentences)
    folded = _fold(rest)
    if PAID_CLAIM.search(folded) and pay["outstanding"] > 0:
        wrong = True
    if NO_MORE_CLAIM.search(folded) and pay["to_pay"] > 0 and pay["outstanding"] > 0:
        wrong = True
    if OWES_CLAIM.search(folded) and (
            not pay["outstanding"] or not pay["to_pay"] or pay["under_review"]):
        wrong = True
    if _wrong_amount(rest, pay, phone):
        wrong = True
    return missing, wrong


def _unverified_wrong(draft: str, phone: str) -> bool:
    folded = _fold(draft)
    return bool(
        PAID_CLAIM.search(folded) or NO_MORE_CLAIM.search(folded) or OWES_CLAIM.search(folded)
        or _wrong_amount(draft, None, phone)
    )


def _live_payment(phone: str) -> tuple[dict | None, bool]:
    """(the newest live order's current() or None, whether reading it failed)."""
    try:
        order = _payment_order(phone)
    except Exception:  # noqa: BLE001 - the caller never falls back to an old amount
        traceback.print_exc()
        return None, True
    return (order.get("payment") if order is not None else None), False


def _send(history: list, text: str) -> str:
    """The history says exactly what the customer gets."""
    history[-1] = {"role": "assistant", "content": [{"type": "text", "text": text}]}
    return text


def _safe(line: str) -> str:
    """Built by code: what was done in this turn, then one payment line."""
    return "\n\n".join([*_turn.actions.values(), line])


def _answer_again(phone: str, history: list, extra: str | None = None,
                  forced_status: bool = False) -> str:
    """The draft is never sent: it leaves the history and the model answers once more,
    with `extra` added to the system prompt. forced_status: a fresh order_status first."""
    if history and history[-1].get("role") == "assistant":
        history.pop()
    system = system_prompt(phone) + (f"\n\n{extra}" if extra else "")
    if forced_status:
        response = client().messages.create(
            model=model(), max_tokens=1024, system=system, tools=TOOLS, messages=history,
            tool_choice={"type": "tool", "name": "order_status"},
        )
        history.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use":
                output = _run_tool(phone, block.name, block.input or {})
                _log_tool(phone, block.name, block.input or {}, output)
                _turn.code_text.append(output)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        history.append({"role": "user", "content": results})
    response = client().messages.create(
        model=model(), max_tokens=1024, system=system, tools=TOOLS, messages=history,
        tool_choice={"type": "none"},
    )
    history.append({"role": "assistant", "content": response.content})
    return "\n\n".join(
        b.text.strip() for b in response.content if b.type == "text" and b.text.strip()
    ).strip()


def _finish_turn(phone: str, history: list, draft: str) -> str:
    """The last step of every turn: a reply that speaks about payment is checked
    against the payment as it is now, before anything is sent (C2 Part 2, Gaps 3-5).

    1. Backstop: a customer reply that claims something about the payment with no
       payment sentence read in this turn is replaced by a fresh answer.
    2. The payment can't be checked: no claim and no payment amount may go out.
    3. Otherwise the current sentence must be in the reply and nothing else may
       contradict it. A missing sentence alone is appended; anything else gets one more
       answer from the model, and then a safe reply built by code."""
    t = _turn
    talks = _talks_payment(draft)
    if t.customer and talks and not t.sentences and not t.unverifiable and not t.failed:
        pay, failed = _live_payment(phone)
        if failed:
            t.failed = True
        elif pay is not None:
            draft = _answer_again(phone, history, forced_status=True) or "Perdón, ¿me lo repite?"
            _send(history, draft)
        else:
            t.unverifiable = True
        talks = _talks_payment(draft)
    if not (t.needs_sentence or t.question or t.unverifiable or t.failed
            or (t.customer and talks)):
        return draft

    pay = None
    if not t.unverifiable:
        pay, failed = _live_payment(phone)       # read once more: the state the turn ends in
        if failed:
            # Never an old amount or a status nobody checked instead.
            if not draft or _unverified_wrong(draft, phone):
                return _send(history, _safe(PAYMENT_UNAVAILABLE))
            if (t.question or t.needs_sentence) and PAYMENT_UNAVAILABLE not in draft:
                return _send(history, f"{draft}\n\n{PAYMENT_UNAVAILABLE}")
            return draft
        if pay is None:
            t.unverifiable = True               # no live order left (e.g. cancelled now)

    if t.unverifiable:
        if _unverified_wrong(draft, phone):
            draft = _answer_again(phone, history, UNVERIFIED_CORRECTION)
            if not draft or _unverified_wrong(draft, phone):
                return _send(history, _safe(PAYMENT_UNVERIFIED))
        folded = _fold(draft)
        if t.status_question and "no pude verificar" not in folded \
                and "no puedo verificar" not in folded:
            return _send(history, f"{draft}\n\n{PAYMENT_UNVERIFIED}".strip())
        return _send(history, draft)

    sentence = payment_sentence(pay, len(orders.live_states(phone)) > 1)
    missing, wrong = _problems(draft, sentence, pay, phone)
    if not missing and not wrong:
        return draft
    if not wrong:
        return _send(history, f"{draft}\n\n{sentence}".strip())        # Gap 3
    draft = _answer_again(phone, history, PAYMENT_CORRECTION.format(sentence=sentence))
    missing, wrong = _problems(draft, sentence, pay, phone)
    if draft and not wrong:
        return _send(history, f"{draft}\n\n{sentence}" if missing else draft)
    return _send(history, _safe(sentence))


# request_handoff topics. Every topic except OTHER is covered by a tool, so only
# OTHER opens a handoff; the value names the tool the model should use instead.
HANDOFF_COVERED = {
    "ORDER_CHANGE": "add_item, remove_item, set_qty o create_order",
    "CANCEL": "cancel_order",
    "PAYMENT_METHOD": "choose_payment",
    "ORDER_INFO": "order_status o show_cart",
}

TOOLS = [
    {
        "name": "add_item",
        "description": (
            "Agrega un producto de la carta al pedido. Devuelve el pedido con los totales "
            "calculados. Si el cliente ya tiene un pedido que el local todavía no ha "
            "aceptado, el cambio se aplica a ESE pedido."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string", "description": "id exacto de la carta"},
                "qty": {"type": "integer", "minimum": 1, "default": 1},
                "modifier_ids": {"type": "array", "items": {"type": "string"}},
                "note": {"type": "string", "description": "nota breve del cliente sobre el producto (ej: bien asada)"},
            },
            "required": ["item_id"],
        },
    },
    {
        "name": "remove_item",
        "description": (
            "Quita un producto del pedido. Sin qty quita todas las unidades. Si el producto "
            "está en varias líneas distintas, no quita nada: usa set_qty con note y "
            "modifier_ids de la línea elegida (qty=0 la quita)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "qty": {"type": "integer", "minimum": 1},
            },
            "required": ["item_id"],
        },
    },
    {
        "name": "set_qty",
        "description": (
            "Fija la cantidad exacta de un producto. qty=0 lo elimina. Si el producto está "
            "en varias líneas distintas (por ejemplo una con adiciones o nota y otra sin), "
            "manda también note y modifier_ids de la línea que el cliente eligió."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "qty": {"type": "integer", "minimum": 0},
                "modifier_ids": {
                    "type": "array", "items": {"type": "string"},
                    "description": "adiciones de la línea elegida; [] si no tiene",
                },
                "note": {"type": "string", "description": "nota de la línea elegida; vacío si no tiene"},
            },
            "required": ["item_id", "qty"],
        },
    },
    {
        "name": "show_cart",
        "description": "Muestra el pedido actual con los totales calculados por el sistema.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_order",
        "description": (
            "Cierra el carrito y crea el pedido con su resumen y total final. "
            "Úsalo cuando el cliente ya eligió todo y dijo cómo lo quiere recibir. "
            "El pedido queda esperando su confirmación. Si el cliente ya tiene un pedido "
            "que el local todavía no ha aceptado, esto ACTUALIZA ese mismo pedido (por "
            "ejemplo nueva dirección, o recoger en vez de domicilio); nunca crea uno nuevo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fulfilment": {"type": "string", "enum": ["PICKUP", "DELIVERY"]},
                "address": {"type": "string", "description": "sólo si es DELIVERY"},
                "zone": {"type": "string", "description": "barrio, sólo si es DELIVERY"},
            },
            "required": ["fulfilment"],
        },
    },
    {
        "name": "start_separate_order",
        "description": (
            "Úsalo SÓLO cuando el pedido anterior ya fue aceptado por el local y el "
            "cliente pidió un pedido NUEVO e independiente (no un cambio), y aceptó "
            "dejarlo aparte. Abre un pedido nuevo e independiente. Después agrega los "
            "productos normalmente con add_item."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "confirm_order",
        "description": "El cliente confirmó el pedido. Sólo después de que él dijo que sí.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "cancel_order",
        "description": "El cliente rechazó o canceló el pedido pendiente.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
        },
    },
    {
        "name": "choose_payment",
        "description": (
            "Registra cómo va a pagar. MIXED requiere transfer_amount y cash_amount; "
            "el sistema valida que sumen el total."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["CASH", "TRANSFER", "MIXED"]},
                "transfer_amount": {"type": "integer"},
                "cash_amount": {"type": "integer"},
            },
            "required": ["method"],
        },
    },
    {
        "name": "order_status",
        "description": (
            "Consulta en qué va el último pedido del cliente y cómo va su pago. Úsala cada vez "
            "que el cliente pregunte por su pago o por cuánto le falta: devuelve la frase de "
            "pago que debes copiar tal cual."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "request_handoff",
        "description": (
            "Pasa el caso a una persona del local. OTHER es el único topic que lo pasa: "
            "úsalo cuando ninguna otra herramienta cubre lo que pide el cliente, o cuando "
            "pidió hablar con una persona y no quiere decir para qué. Los demás topics no "
            "pasan nada: te dirán qué herramienta usar, porque el sistema decide si cambia "
            "el pedido o se lo pasa a una persona. Sólo funciona si el pedido del cliente "
            "ya está en el panel del local."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "enum": [*HANDOFF_COVERED, "OTHER"],
                    "description": (
                        "ORDER_CHANGE: productos, cantidades, adiciones, recoger o domicilio, "
                        "dirección o barrio. CANCEL: cancelar el pedido. PAYMENT_METHOD: forma "
                        "de pago. ORDER_INFO: qué tiene el pedido o cómo va. OTHER: ninguna "
                        "herramienta lo cubre."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": "qué pide el cliente, en una frase, para el personal del local",
                },
            },
            "required": ["topic", "summary"],
        },
    },
]


# ---- tool implementations (all numbers produced here) ----------------

def _pending(phone: str):
    return orders.latest_for(phone, ["PENDING_CONFIRMATION"])


PAYMENT_LABELS = {"CASH": "efectivo", "TRANSFER": "transferencia", "MIXED": "pago mixto"}


def _order_text(order: dict) -> str:
    """Bullet lines, subtotal, delivery fee and total - same format as
    cart.render, but from an order instead of a cart."""
    parts = []
    for item in order["items"]:
        extras = f" ({', '.join(item['modifiers'])})" if item["modifiers"] else ""
        note = f" — {item['note']}" if item["note"] else ""
        parts.append(f"• {item['qty']}x {item['name']}{extras}{note}  {fmt(item['line_total'])}")
    parts.append(f"\nSubtotal: {fmt(order['subtotal'])}")
    if order["delivery_fee"]:
        zone = f" ({order['zone']})" if order.get("zone") else ""
        parts.append(f"Domicilio{zone}: {fmt(order['delivery_fee'])}")
    parts.append(f"*Total: {fmt(order['total'])}*")
    return "\n".join(parts)


def _handoff(order: dict, summary: str, why: str) -> str:
    handoff.request(order["phone"], order["id"], summary)
    _action("handoff", HANDOFF_ACTION)
    return (
        f"PASADO A UNA PERSONA: {why}, así que el sistema no hizo el cambio. El local ya "
        "quedó avisado. Dile al cliente con amabilidad que una persona del local va a "
        "revisar su solicitud y se comunica con él. No le prometas que el cambio se va a "
        "poder hacer. No le menciones ningún código."
    )


def _amended_text(phone: str, order: dict, change: dict) -> str:
    """The edit result. What the customer is told about money is the payment sentence,
    built by code from current() after the edit, copied as it is (C2 Part 2)."""
    state_line = (
        "El local ya ve el cambio; no hace falta que el cliente vuelva a confirmar."
        if order["state"] == "CONFIRMED"
        else "Pregúntale al cliente si lo confirma."
    )
    pay = order.get("payment") or payments.current(order["id"])
    sentence = _hand_out_sentence(phone, pay, required=True)
    tail = ""
    if change.get("ask_method"):
        tail += " Como cambió el total, pregúntale otra vez cómo va a pagar."
    if pay["under_review"]:
        tail += " No le pidas que pague otra vez."
    if pay["overpayment_amount"]:
        tail += " No le prometas devolución."
    _action("order", "Listo, su pedido quedó actualizado:\n" + _order_text(order) + (
        "\n¿Me confirma el pedido así?" if order["state"] == "PENDING_CONFIRMATION" else ""
    ))
    return (
        "Pedido actualizado. Es el MISMO pedido, no uno nuevo:\n"
        + _order_text(order) + "\n" + state_line
        + f" Copia tal cual, sin cambiar nada, esta frase sobre su pago: «{sentence}»"
        + tail + " No le menciones ningún código."
    )


def _edit(phone: str, attempt: str, mutate=None, **delivery) -> str:
    """Every tool that changes what was ordered goes through here."""
    window, order = orders.modification_window(phone)
    if window == 3:
        return _handoff(order, f"El cliente quiere {attempt}.", "el local ya aceptó este pedido")
    if window == 2:
        try:
            order, change = orders.amend(order["id"], phone, mutate, **delivery)
        except orders.NotEditable:
            return _handoff(orders.get(order["id"]), f"El cliente quiere {attempt}.",
                            "el local acaba de aceptar este pedido")
        except orders.EmptyEdit:
            return ("NO SE PUDO: el pedido no puede quedar vacío, así que no se cambió nada. "
                    "Pregúntale al cliente si quiere cancelar el pedido; sólo si dice que sí, usa "
                    "cancel_order. No lo pases a una persona: el local todavía no lo ha aceptado. "
                    "Si quiere cambiar un producto por otro, agrega primero el nuevo y después "
                    "quita el anterior.")
        return _amended_text(phone, order, change)
    rendered = cart.render(mutate())
    _action("order", "Listo, actualicé su pedido:\n" + rendered)
    return "Pedido actualizado:\n" + rendered


def _run_tool(phone: str, name: str, args: dict) -> str:
    """One tool call holds the write lock (db.write_lock): the panel writes from
    another thread. Tools only touch the database, so it is held briefly."""
    with db.write_lock:
        return _run_tool_locked(phone, name, args)


def _run_tool_locked(phone: str, name: str, args: dict) -> str:
    try:
        if name == "add_item":
            label = (CATALOG.item(args["item_id"]) or {}).get("name", args["item_id"])
            qty = int(args.get("qty", 1))
            attempt = f"agregar {qty}x {label}"
            mutate = lambda: cart.add_item(
                phone, args["item_id"], qty, args.get("modifier_ids") or [], args.get("note"),
            )
            return _edit(phone, attempt, mutate)

        if name == "remove_item":
            label = (CATALOG.item(args["item_id"]) or {}).get("name", args["item_id"])
            qty = args.get("qty")
            attempt = f"quitar {qty}x {label}" if qty else f"quitar {label}"
            mutate = lambda: cart.remove_item(phone, args["item_id"], qty)
            return _edit(phone, attempt, mutate)

        if name == "set_qty":
            label = (CATALOG.item(args["item_id"]) or {}).get("name", args["item_id"])
            qty = int(args["qty"])
            attempt = f"dejar {qty}x {label}"
            selector = "note" in args or "modifier_ids" in args
            line = (args.get("modifier_ids") or [], args.get("note") or "") if selector else None
            mutate = lambda: cart.set_qty(phone, args["item_id"], qty, line)
            return _edit(phone, attempt, mutate)

        if name == "show_cart":
            window, order = orders.modification_window(phone)
            if window == 2:
                return (
                    "Pedido actual (el local todavía no lo ha aceptado: un cambio se hace con "
                    "add_item, remove_item, set_qty o create_order sobre este mismo pedido):\n"
                    + _order_text(order) + " No le menciones ningún código."
                )
            if window == 3:
                return (
                    "Pedido actual (el local ya lo aceptó: un cambio pasa a una persona):\n"
                    + _order_text(order) + " No le menciones ningún código."
                )
            return cart.render(cart.summary(phone))

        if name == "start_separate_order":
            window, locked = orders.modification_window(phone)
            if window != 3:
                return (
                    "No hace falta: no hay ningún pedido aceptado por el local. "
                    "Sigue normal con add_item."
                )
            cart.open_cart(phone, related_order_id=locked["id"])
            _action("separate", "Listo, abrí un pedido aparte.")
            extra = ""
            if locked["fulfilment_type"] == "DELIVERY":
                fee, _ = CATALOG.delivery_fee_for(locked.get("zone"))
                extra = (
                    f" Avísale que esto significa un segundo domicilio de {fmt(fee)} "
                    "y un segundo viaje."
                )
            return (
                "Listo, abrí un pedido nuevo e independiente del anterior. "
                f"Agrega los productos con add_item.{extra}"
            )

        if name == "create_order":
            window, live = orders.modification_window(phone)
            if window in (2, 3):
                fulfilment = args["fulfilment"]
                address = args.get("address")
                zone = args.get("zone")
                if fulfilment == "DELIVERY":
                    attempt = f"cambiar la entrega: domicilio a {address} ({zone})"
                else:
                    attempt = "cambiar la entrega: recoger en el local"
                return _edit(phone, attempt, None, fulfilment=fulfilment, address=address, zone=zone)
            order = orders.create_from_cart(
                phone,
                args["fulfilment"],
                address=args.get("address"),
                zone=args.get("zone"),
            )
            aparte = (
                "\n(Este es un pedido APARTE del que el local ya aceptó.)"
                if order["related_order_id"] else ""
            )
            _action("order", "Este es su pedido:\n" + _order_text(order) + (
                "\nEs un pedido aparte del que el local ya aceptó." if order["related_order_id"]
                else "") + "\n¿Me lo confirma?")
            return (
                f"Pedido creado, esperando confirmación.{aparte}\n"
                f"{_order_text(order)}\n"
                "Pregúntale al cliente si lo confirma. No le menciones ningún código."
            )

        if name == "confirm_order":
            order = _pending(phone)
            if not order:
                return "No hay ningún pedido esperando confirmación. Usa create_order primero."
            order = orders.apply_event(order["id"], "customer_confirms")
            _action("confirm", "Su pedido quedó confirmado.")
            return (
                "Pedido confirmado. El local todavía no lo ha aceptado; hasta que lo acepte, "
                "cualquier cambio se hace sobre este mismo pedido con las herramientas de "
                "siempre. Dile al cliente sólo que su pedido quedó confirmado. "
                f"Total {fmt(order['total'])}. Pregúntale cómo va a pagar si aún no lo dijo. "
                "No le menciones ningún código."
            )

        if name == "cancel_order":
            order = _pending(phone) or orders.latest_for(phone, ["CONFIRMED"])
            if not order:
                locked = orders.latest_for(phone, list(orders.LOCKED))
                if locked:
                    reason = args.get("reason")
                    return _handoff(
                        locked,
                        "El cliente quiere cancelar el pedido"
                        + (f" (motivo: {reason})" if reason else "") + ".",
                        "el local ya aceptó este pedido",
                    )
                return "No hay pedido activo para cancelar."
            event = (
                "customer_declines"
                if order["state"] == "PENDING_CONFIRMATION"
                else "customer_cancels"
            )
            orders.apply_event(order["id"], event, args.get("reason"))
            orders.reopen_cart_after_edit(phone)
            _action("cancel", "Su pedido quedó cancelado.")
            return "Pedido cancelado. No le menciones ningún código."

        if name == "choose_payment":
            order = orders.latest_for(
                phone, ["PENDING_CONFIRMATION", "CONFIRMED", "ACCEPTED", "READY", "OUT_FOR_DELIVERY"]
            )
            if not order:
                return "Todavía no hay un pedido al cual asignarle un pago."
            method = args["method"]
            label = PAYMENT_LABELS.get(method, method)
            payment = order["payment"]
            requested_transfer = args.get("transfer_amount")
            same = payment["method"] == method and (
                method != "MIXED" or requested_transfer is None
                or requested_transfer == payment["transfer_amount"]
            )
            # Cash a person already collected settles the method like a receipt does.
            cash_registered = payment["cash_received"] > 0
            if same and (order["state"] == "OUT_FOR_DELIVERY" or payment["status"] != "PENDING"
                         or cash_registered):
                return "La forma de pago ya estaba registrada así; no cambió nada."
            if order["state"] == "OUT_FOR_DELIVERY":
                return _handoff(
                    order, f"El cliente quiere cambiar la forma de pago a {label}.",
                    "el pedido ya va en camino y el domiciliario no ve el panel",
                )
            if payment["status"] != "PENDING":
                return _handoff(
                    order, f"El cliente quiere cambiar la forma de pago a {label}.",
                    "el cliente ya envió un comprobante de pago",
                )
            if cash_registered:
                return _handoff(
                    order, f"El cliente quiere cambiar la forma de pago a {label}.",
                    "el local ya registró efectivo recibido para este pedido",
                )
            previous_method = payment["method"]
            record = payments.set_method(
                order["id"], method, requested_transfer, args.get("cash_amount"),
            )
            if previous_method != "UNSPECIFIED" and not same:
                orders.mark_edited(order["id"])
            _action("payment", "Listo, quedó registrada su forma de pago.")
            # The amounts, the accounts and the request for a receipt all come from the
            # payment sentence: a receipt is asked for only when a transfer is due.
            sentence = _hand_out_sentence(phone, record, required=True)
            return (
                f"Forma de pago registrada: {label}. Copia tal cual, sin cambiar nada, esta "
                f"frase sobre su pago: «{sentence}» No le menciones ningún código."
            )

        if name == "order_status":
            if _turn.unverifiable:
                return (
                    "El cliente pregunta por el pago de un pedido que no se puede verificar "
                    "desde aquí. " + UNVERIFIED_TOOL
                )
            unavailable = (
                "NO SE PUDO consultar el pago en este momento: no digas cuánto falta ni si "
                "ya está pagado. No le menciones ningún código."
            )
            try:
                live = _payment_order(phone)
                order = live or orders.latest_for(phone)
            except Exception:  # noqa: BLE001 - never an old amount instead
                traceback.print_exc()
                _turn.failed = True
                return unavailable
            if not order:
                return "El cliente no tiene pedidos."
            label = STATE_LABELS.get(order["state"], order["state"])
            head = f"Su pedido está {label}. Total {fmt(order['total'])}."
            if live is None:
                _turn.unverifiable = True       # a delivered or cancelled order: not checked here
                return f"{head} {UNVERIFIED_TOOL}"
            try:
                sentence = _hand_out_sentence(phone, live["payment"], required=False)
            except Exception:  # noqa: BLE001 - never an old amount instead
                traceback.print_exc()
                _turn.failed = True
                return f"{head} {unavailable}"
            return (
                f"{head} Frase de pago, cópiala tal cual, sin cambiar nada, cuando hables del "
                f"pago: «{sentence}» No le menciones ningún código."
            )

        if name == "request_handoff":
            topic = args.get("topic")
            if topic != "OTHER" and topic not in HANDOFF_COVERED:
                # An omitted topic must not reopen the "handoff instead of change" bug.
                return (
                    "NO SE PUDO: falta topic (ORDER_CHANGE, CANCEL, PAYMENT_METHOD, ORDER_INFO "
                    "u OTHER), así que no se pasó nada a una persona. Si lo que pide el cliente "
                    "lo cubre una herramienta, usa esa; si ninguna lo cubre, llama otra vez con "
                    "topic OTHER."
                )
            if topic != "OTHER":
                return (
                    f"NO SE PUDO: ese tema ya lo cubre una herramienta: usa {HANDOFF_COVERED[topic]}. "
                    "El sistema decide si cambia el pedido o se lo pasa a una persona; no uses "
                    "request_handoff para esto."
                )
            order = orders.latest_for(phone, list(orders.ON_BOARD))
            if not order:
                return (
                    "NO SE PUDO: el cliente no tiene un pedido en el panel del local, así que "
                    "no hay a quién avisar. Dile al cliente únicamente que por ahora no puedes "
                    "pasar esa solicitud a una persona. No le prometas nada para más adelante "
                    "(ni \"cuando confirme\" ni \"cuando el local acepte\"), no le digas nada del "
                    "estado de su pedido, y puedes ofrecerle ayuda con su pedido."
                )
            summary = (args.get("summary") or "").strip()
            if not summary:
                return (
                    "NO SE PUDO: falta summary (qué pide el cliente, en una frase, para el "
                    "personal del local), así que no se pasó nada a una persona. Llama otra "
                    "vez a request_handoff con topic OTHER y summary."
                )
            handoff.request(phone, order["id"], summary)
            _action("handoff", HANDOFF_ACTION)
            return (
                "PASADO A UNA PERSONA: el local ya quedó avisado. Esto no era un cambio del "
                "pedido y no se cambió nada: no le menciones ningún cambio. Dile que una "
                "persona del local va a revisar su solicitud y se comunica con él. No le "
                "prometas nada más. No le menciones ningún código."
            )

        return f"Herramienta desconocida: {name}"

    except cart.LineChoiceNeeded as exc:
        listing = "\n".join(f"• {line}" for line in exc.lines)
        first = (f"no hay una línea de {exc.item_name} con esa nota y esas adiciones"
                 if exc.matched_nothing else f"hay varias líneas distintas de {exc.item_name}")
        return (f"NO SE PUDO: {first}, así que no se cambió nada. Las líneas son:\n{listing}\n"
                "Pregúntale al cliente a cuál se refiere. Después llama set_qty con el item_id, "
                "la note y los modifier_ids de esa línea: qty es la cantidad que debe quedar y "
                "qty=0 la quita. No le muestres al cliente los ids ni este formato.")
    except cart.CartError as exc:
        return f"NO SE PUDO: {exc} Dile esto al cliente con naturalidad y ofrécele algo de la carta."
    except payments.PaymentError as exc:
        return f"NO SE PUDO: {exc}"
    except orders.IllegalTransition as exc:
        return f"NO SE PUDO: {exc}"
    except KeyError as exc:
        return (
            f"NO SE PUDO: {exc} no existe en la carta. No lo inventes: dile al cliente "
            "que no lo tienen y ofrécele algo parecido que sí esté."
        )
    except Exception as exc:  # noqa: BLE001 - demo safety net
        return f"NO SE PUDO: error interno ({exc})."


# ---- the conversation loop ------------------------------------------

def reply(phone: str, user_content, *, customer: bool = False, with_payment: bool = False,
          action: str | None = None) -> str:
    """user_content is a string, or a content-block list (for images).

    customer: it is the customer's own text (main.handle); a payment question starts
    with a forced order_status, and a reply that speaks about payment is checked.
    with_payment: a system instruction that carries the payment sentence (a clean
    receipt); the reply must carry it too. action: what that instruction reports as
    done, kept by the safe reply (_finish_turn)."""
    global _turn
    with _lock:
        history = _history.setdefault(phone, [])
        # If the call below fails, the history must go back exactly as it was.
        # A half-written turn leaves two user messages in a row, and every later
        # request then fails with "roles must alternate" — one bad key turns
        # into a conversation that never recovers.
        checkpoint = list(history)
        _turn = _Turn(customer=customer, with_payment=with_payment)
        if action:
            _action("instruction", action)
        if not customer and isinstance(user_content, str):
            _turn.code_text.append(user_content)
        try:
            if with_payment:
                sentence = payment_sentence_for(phone)
                if sentence:
                    _turn.sentences.append(sentence)
            return _reply_locked(phone, history, user_content)
        except Exception:
            history[:] = checkpoint
            raise
        finally:
            _turn = _Turn()


def _trim(history: list) -> None:
    """Keep the tail short WITHOUT cutting a tool_use/tool_result pair apart.

    An orphan tool_result at the front of the list is rejected by the API, so
    after trimming we drop leading messages until the first one is a plain user
    message."""
    del history[:-HISTORY_TURNS]
    while history:
        first = history[0]
        content = first.get("content")
        is_tool_result = isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
        if first.get("role") == "user" and not is_tool_result:
            break
        history.pop(0)


TOOL_LOG_CHARS = 160


def _log_tool(phone: str, name: str, args: dict, output: str) -> None:
    """One console line per tool call, so a wrong reply can be traced to what the
    model called and what it got back. Only the phone's last digits, and the
    result cut short."""
    result = " ".join(str(output).split())
    if len(result) > TOOL_LOG_CHARS:
        result = result[:TOOL_LOG_CHARS] + "..."
    line = f"[bot ...{phone[-4:]}] {name} {json.dumps(args, ensure_ascii=False)} -> {result}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # A console that can't print an accent or an emoji must never cost the
        # customer a reply.
        print(line.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


MAX_TOOL_ROUNDS = 8


def _payment_question(phone: str, text: str) -> dict | None:
    """Marks the turn when the customer asks about payment, and returns the tool_choice
    for the first model call: a fresh order_status, never an amount from older chat
    (Gap 5). A question about another order than the newest live one, or with no live
    order at all, can't be checked: the turn is unverifiable."""
    folded = _fold(text)
    if not PAYMENT_WORDS.search(folded):
        return None
    _turn.question = True
    _turn.status_question = bool(STATUS_QUESTION.search(folded))
    live, failed = _live_payment(phone)
    any_order = orders.latest_for(phone) is not None
    if not failed and (OTHER_ORDER.search(folded) or (live is None and any_order)):
        _turn.unverifiable = True
    if failed or live is not None or any_order:
        return {"type": "tool", "name": "order_status"}
    return None


def _reply_locked(phone: str, history: list, user_content) -> str:
    history.append({"role": "user", "content": user_content})
    _trim(history)
    force = None
    if _turn.customer and isinstance(user_content, str):
        force = _payment_question(phone, user_content)

    text_out = []
    for round_number in range(MAX_TOOL_ROUNDS):  # tool-use rounds
        extra = {"tool_choice": force} if force and round_number == 0 else {}
        response = client().messages.create(
            model=model(),
            max_tokens=1024,
            system=system_prompt(phone),
            tools=TOOLS,
            messages=history,
            **extra,
        )
        history.append({"role": "assistant", "content": response.content})

        # Text written before a tool answered is a guess, not a fact - only the
        # last response's text (replaced, not accumulated) may reach the customer.
        round_text = [b.text.strip() for b in response.content if b.type == "text" and b.text.strip()]
        if round_text:
            text_out = round_text

        if response.stop_reason != "tool_use":
            break

        results = []
        for block in response.content:
            if block.type == "tool_use":
                output = _run_tool(phone, block.name, block.input or {})
                _log_tool(phone, block.name, block.input or {}, output)
                _turn.code_text.append(output)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })
        history.append({"role": "user", "content": results})
    else:
        # The model still wanted tools after the last round, so the history ends on a
        # tool result and the text above is only what it wrote before the tools answered:
        # a guess. One more call with tools switched off makes it answer from what they
        # returned, and only that answer's text may reach the customer. No text at all
        # falls through to the fallback below.
        response = client().messages.create(
            model=model(),
            max_tokens=1024,
            system=system_prompt(phone),
            tools=TOOLS,
            messages=history,
            tool_choice={"type": "none"},
        )
        history.append({"role": "assistant", "content": response.content})
        text_out = [b.text.strip() for b in response.content if b.type == "text" and b.text.strip()]

    text = "\n\n".join(text_out).strip() or "Perdón, ¿me lo repite?"
    text = _finish_turn(phone, history, text)
    _trim(history)
    return text


def correct_last_reply(phone: str, text: str) -> None:
    """The customer was sent `text` instead of the model's last reply (main.py's
    discard guard). The history must say what was actually sent, or the model
    later builds on words the customer never saw."""
    with _lock:
        history = _history.get(phone) or []
        if history and history[-1].get("role") == "assistant":
            history[-1] = {"role": "assistant", "content": [{"type": "text", "text": text}]}


def forget(phone: str) -> None:
    _history.pop(phone, None)


def reset_all() -> None:
    _history.clear()


# ---- payment proof: OCR only, no verdict ----------------------------

PROOF_SYSTEM = """Eres un extractor de datos de comprobantes de pago colombianos
(Nequi, Daviplata, Bancolombia, etc.).

Devuelve SOLO un objeto JSON, sin texto alrededor, con estas llaves:
  amount              número entero en pesos, sin puntos ni símbolos (ej: 47000)
  destination_account número o alias de la cuenta que RECIBE el dinero
  destination_holder  nombre de quien RECIBE el dinero, tal cual aparece
  origin_account      número o alias de la cuenta que ENVÍA el dinero
  origin_holder       nombre de quien ENVÍA el dinero, tal cual aparece
  reference           número de comprobante / referencia / CUS
  date                fecha que aparece, tal cual
  date_iso            la misma fecha y hora en ISO 8601 con desfase horario
                       (ej: 2026-09-22T19:29:00-05:00); null si no aparece
  provider            Nequi, Daviplata, Bancolombia...

destination_* es quien RECIBE la plata; origin_* es quien la ENVÍA. En un
comprobante de Nequi, "Enviaste a <nombre>" nombra al que RECIBE (destination_*),
no al que envía.

Si un dato no aparece, pon null. NO juzgues si el pago es válido: eso no es tu
trabajo. Sólo lee lo que dice la imagen."""


def extract_proof(image_block: dict) -> dict:
    """image_block is an Anthropic image content block. Returns the raw fields."""
    response = client().messages.create(
        model=proof_model(),
        max_tokens=512,
        system=PROOF_SYSTEM,
        messages=[{
            "role": "user",
            "content": [image_block, {"type": "text", "text": "Extrae los datos."}],
        }],
    )
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                pass
    return {"amount": None, "destination_account": None, "reference": None,
            "_raw": text[:400]}
