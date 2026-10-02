# Order lifecycle

How an order moves, what staff can press, what happens when the customer wants to
change something, and who gets told what. Code: `orders.py`, `cart.py`, `brain.py`
(`_edit`, `_handoff`, `request_handoff`), `handoff.py`, `main.notify`.

---

## 1. States

```
PENDING_CONFIRMATION ──customer "sí"──> CONFIRMED ──Aceptar──> ACCEPTED ──Listo──> READY
                                                                                    │
                            pickup:   READY ──Entregado──────────────────────> COMPLETED
                            delivery: READY ──Sale a domicilio──> OUT_FOR_DELIVERY ──Entregado──> COMPLETED
```

Terminal: `COMPLETED`, `CANCELLED`, `REJECTED`, `EXPIRED`; a terminal order never moves
again. Anything not in `orders.TRANSITIONS` raises `IllegalTransition` (HTTP 409):

| From | Event | To | Actor |
|---|---|---|---|
| PENDING_CONFIRMATION | `customer_confirms` / `customer_declines` / `expire` | CONFIRMED / CANCELLED / EXPIRED | customer / customer / system |
| CONFIRMED | `staff_accepts` / `staff_rejects` / `customer_cancels` | ACCEPTED / REJECTED / CANCELLED | staff / staff / customer |
| ACCEPTED | `food_ready` / `staff_cancels` | READY / CANCELLED | staff |
| READY | `mark_in_error` / `hand_over` / `driver_leaves` | ACCEPTED / COMPLETED / OUT_FOR_DELIVERY | staff (`driver_leaves` on delivery only) |
| OUT_FOR_DELIVERY | `delivered` | COMPLETED | staff |

- Every move is appended to `order_transitions` with actor and reason.
- `staff_rejects` and `staff_cancels` need a reason (HTTP 400 without one).
- `expire` exists, but nothing triggers it (no timers).
- Task B removed `IN_PREPARATION` (a button press with no meaning for the customer). Do
  not bring it back unless Miguel decides so.
- Vocabulary: `CONFIRMED` = confirmed by the customer; "aceptado por el local" = ACCEPTED
  and later. The bot never says "recibido" or "enviado al local" for a CONFIRMED order.

## 2. Panel columns and buttons

| Column | State | Buttons |
|---|---|---|
| Nuevos | CONFIRMED | Aceptar · Rechazar (asks reason) |
| Recibidos | ACCEPTED | Listo · Cancelar (asks reason) |
| Listos | READY | Entregado (pickup) or Sale a domicilio (delivery) · Volver a preparar |
| En camino | OUT_FOR_DELIVERY | Entregado |

PENDING_CONFIRMATION orders have no column. Per card: "Actualizado HH:MM" when a Nuevos
order changed after confirmation; "pedido aparte" for a separate order; the receipts
block with Confirmar / Descartar per receipt and 👁 (`payments.md`); "Registrar efectivo
recibido" while money is outstanding; the amber handoff box; a red pulse once a Nuevos
order is older than 600 s from creation (`?alert=N` changes it).

## 3. Changing an order: the windows

`orders.modification_window(phone)` decides; `brain._edit` applies it before any change
to items or delivery details (`add_item`, `remove_item`, `set_qty`, `create_order`).

| Window | Situation | What happens |
|---|---|---|
| 1 | An **open cart** exists | The cart changes. An open cart always wins, even next to a live order |
| 2 | Latest order is PENDING_CONFIRMATION or CONFIRMED | `orders.amend` changes **the same order** (row, code, card), "Actualizado HH:MM". One transaction: items, totals and `payments.follow_total` land together or not at all |
| 3 | Latest order is ACCEPTED, READY or OUT_FOR_DELIVERY | Locked. `_handoff` opens the amber box with a code-written summary; the bot says a person will contact the customer and promises nothing |
| 0 | Nothing live | Normal new order |

Details:
- An edit copies the order's lines into a staging cart, applies the change and writes
  the result back (re-priced from the current catalog). The customer never repeats the
  order. After an edit to a CONFIRMED order the customer does not confirm again.
- Identical lines merge (same item, same modifiers in any order, same note ignoring case
  and spaces): "3× Gaseosa personal", not three lines. A modifier added twice is a
  different line.
- If an item sits on several different lines and the request does not say which,
  `LineChoiceNeeded`: nothing changes and the bot asks which line, without showing ids.
- Swapping a product = add the new one, then remove the old one.
- An edit that would leave the order empty is refused (`EmptyEdit`); the bot asks
  whether to cancel and never hands it off.
- If staff accept between the check and the write, the guarded update fails
  (`NotEditable`) and the request is handed off instead.
- `start_separate_order` (only in window 3) opens a cart with `related_order_id`; for
  delivery the bot warns of a second fee and trip. No rule reads that link.
- Cancel by chat: before acceptance it cancels (`customer_declines` or
  `customer_cancels`) and opens a fresh cart; after acceptance it is handed off.
- Payment-method changes: `payments.md` §1.

## 4. Handoff to a person

- `_handoff` (a locked change, cancel or payment-method change) and `request_handoff`
  with topic `OTHER` call `handoff.request`; an identical open request is not duplicated.
- `request_handoff` is gated in code: topics `ORDER_CHANGE`, `CANCEL`, `PAYMENT_METHOD`,
  `ORDER_INFO` answer `NO SE PUDO` and name the tool to use instead, so the model can
  never hand off a change the system should make. A missing topic or a blank summary is
  also `NO SE PUDO`.
- `OTHER` puts the box on the newest order that has a card (Nuevos to En camino). With
  no card (no order, only an open cart, only PENDING_CONFIRMATION, or only terminal
  orders) the bot says that for now it cannot pass the request to a person, promises
  nothing for later, and no box appears.
- "Quiero hablar con una persona" with no reason: the bot asks the reason once. If a tool
  covers it, that tool runs; otherwise, or if the customer won't say, `OTHER`.
- "Ya lo atendí" resolves every open request on that order; nothing is sent. The bot
  keeps answering while a box is open; staff contact the customer by hand.
- Task D (planned) moves this into Chatwoot and silences the bot until the person
  closes it.

## 5. Who gets told what

| Event | Message to the customer |
|---|---|
| ACCEPTED | "¡Recibimos su pedido! En un momento entra en preparación." |
| READY, pickup | "Su pedido ya está listo para recoger." |
| READY, delivery | Nothing; the "va en camino" message covers it |
| OUT_FOR_DELIVERY | "Su pedido va en camino." |
| COMPLETED | Nothing |
| REJECTED, or CANCELLED by staff | Short, kind AI message from the staff's reason; fixed template with the reason if the AI fails |
| CANCELLED by the customer | Nothing; they already know |
| Receipt confirmed by staff | Code-built message (`payments.md` §4) |
| Receipt discarded by staff | One AI message from the reason, guarded; fixed template on failure |

No customer message may contain the order code (`P-1234`) or the word "cocina"; tests
check every fixed notification. The bot's state answers come from ESTADO REAL
(`ARCHITECTURE.md` §7), never from its own earlier messages.
