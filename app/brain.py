"""The model reads what the customer wants and calls tools.

Hard rule: the model never produces a number that reaches the customer. It says
"add two Doble Parchada"; code looks up the price, multiplies, and writes the
total. Every tool below returns text that code built."""

import json
import os
import threading

from anthropic import Anthropic

from . import cart, db, handoff, orders, payments
from .catalog import CATALOG
from .money import fmt

_client = None
_model = None
_lock = threading.Lock()
_history: dict[str, list] = {}
HISTORY_TURNS = 24

PREFERRED = ("sonnet", "opus", "haiku")


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
    "va a pagar ni le digas que no tiene pago registrado."
)


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _payment_state(phone: str) -> str | None:
    """The newest live order's payment in words: the method, its receipts, what is
    still owed and whether it is fully paid. That order is where a receipt image
    lands (main.handle). No amounts: the numbers the customer sees come from tools
    and instructions."""
    order = orders.latest_for(phone, [*orders.EDITABLE, *orders.LOCKED])
    if order is None or "payment" not in order:
        return None
    pay = order["payment"]
    # Discarded receipts are listed too: left out, the line was silent about them
    # and the model kept the "en revisión" from the chat history.
    found = [r["status"] for r in pay["receipts"]]
    parts = [PAYMENT_STATE.get(pay["method"], pay["method"])]
    if not found:
        parts.append("no ha enviado ningún comprobante")
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
    elif pay["method"] in ("TRANSFER", "MIXED"):
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
                "note": {"type": "string", "description": "nota breve para la cocina"},
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
        "description": "Consulta en qué va el último pedido del cliente.",
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
    return (
        f"PASADO A UNA PERSONA: {why}, así que el sistema no hizo el cambio. El local ya "
        "quedó avisado. Dile al cliente con amabilidad que una persona del local va a "
        "revisar su solicitud y se comunica con él. No le prometas que el cambio se va a "
        "poder hacer. No le menciones ningún código."
    )


def _payment_line(change: dict) -> str:
    if change.get("under_review"):
        return (
            " El local está revisando el comprobante que envió y se comunica con él. "
            "NO le pidas que pague otra vez."
        )
    if change.get("surplus"):
        cash, sent = change.get("cash", 0), change.get("sent", 0)
        if cash and sent:
            paid_in = f"Entre transferencias y efectivo ya pagó {fmt(sent + cash)}"
        elif cash:
            paid_in = f"Ya pagó {fmt(cash)} en efectivo"
        else:
            paid_in = f"Ya envió {fmt(sent)}"
        return (
            f" {paid_in}, {fmt(change['surplus'])} más que el nuevo total. No le prometas "
            "devolución: dile que el local revisa la diferencia y se comunica con él."
        )
    if change.get("missing"):
        return (
            f" Ya envió {fmt(change['sent'])}. Faltan {fmt(change['missing'])}: pídele el "
            f"comprobante de esa diferencia.\nCuentas del restaurante:\n{CATALOG.accounts_for_prompt()}"
        )
    if change.get("cash_due"):
        return (
            f" Ya envió {fmt(change['sent'])} por transferencia; quedan "
            f"{fmt(change['cash_due'])} en efectivo al recibir."
        )
    if change.get("ask_method"):
        return " Como cambió el total, pregúntale otra vez cómo va a pagar."
    return ""


def _amended_text(order: dict, change: dict) -> str:
    state_line = (
        "El local ya ve el cambio; no hace falta que el cliente vuelva a confirmar."
        if order["state"] == "CONFIRMED"
        else "Pregúntale al cliente si lo confirma."
    )
    return (
        "Pedido actualizado. Es el MISMO pedido, no uno nuevo:\n"
        + _order_text(order) + "\n" + state_line + _payment_line(change)
        + " No le menciones ningún código."
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
        return _amended_text(order, change)
    return "Pedido actualizado:\n" + cart.render(mutate())


def _run_tool(phone: str, name: str, args: dict) -> str:
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
            if record["method"] == "CASH":
                return f"Pago en efectivo registrado: {fmt(record['cash_amount'])} al recibir."
            accounts = CATALOG.accounts_for_prompt()
            return (
                f"Registrado. Debe transferir {fmt(record['transfer_amount'])}"
                + (f" y pagar {fmt(record['cash_amount'])} en efectivo" if record["cash_amount"] else "")
                + f".\nCuentas del restaurante:\n{accounts}\n"
                "Pídele que envíe la foto del comprobante."
            )

        if name == "order_status":
            order = orders.latest_for(phone)
            if not order:
                return "El cliente no tiene pedidos."
            label = STATE_LABELS.get(order["state"], order["state"])
            return (
                f"Su pedido está {label}. Total {fmt(order['total'])}. "
                "No le menciones ningún código."
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

def reply(phone: str, user_content) -> str:
    """user_content is a string, or a content-block list (for images)."""
    with _lock:
        history = _history.setdefault(phone, [])
        # If the call below fails, the history must go back exactly as it was.
        # A half-written turn leaves two user messages in a row, and every later
        # request then fails with "roles must alternate" — one bad key turns
        # into a conversation that never recovers.
        checkpoint = list(history)
        try:
            return _reply_locked(phone, history, user_content)
        except Exception:
            history[:] = checkpoint
            raise


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


def _reply_locked(phone: str, history: list, user_content) -> str:
    history.append({"role": "user", "content": user_content})
    _trim(history)

    text_out = []
    for _ in range(8):  # tool-use rounds
        response = client().messages.create(
            model=model(),
            max_tokens=1024,
            system=system_prompt(phone),
            tools=TOOLS,
            messages=history,
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
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })
        history.append({"role": "user", "content": results})

    _trim(history)
    return "\n\n".join(text_out).strip() or "Perdón, ¿me lo repite?"


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
        model=model(),
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
