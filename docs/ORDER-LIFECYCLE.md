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

Every card also has **Editar** (§3, Task E). PENDING_CONFIRMATION orders have no column.
Per card: "Actualizado HH:MM" when the order changed after the customer's "sí" (any column);
"pedido aparte" for a separate order; «📍 Ver en el mapa» on a delivery order with the
customer's pin; «Acordar pago» (§3); the "Pagos" block (receipts with Confirmar, ✕ and 👁; cash
rows with ✕; `payments.md` §2); "Registrar efectivo recibido" while money is outstanding;
«➕ El cliente agregó …» with Visto; «✏️ Cambio solicitado» with Aceptar cambio / Rechazar; the
amber handoff box; «Aviso no enviado»; a red pulse once a Nuevos order is older than 600 s from
creation (`?alert=N` changes it). The header shows «⚠ Avisos sin enviar (N)» when a notice failed.

## 3. Changing an order: the windows

`orders.modification_window(phone)` decides; `brain._edit` applies it before any change
to items or delivery details (`add_item`, `remove_item`, `set_qty`, `create_order`).

| Window | Situation | What happens |
|---|---|---|
| 1 | An **open cart** exists | The cart changes. An open cart always wins, even next to a live order |
| 2 | Latest order is PENDING_CONFIRMATION or CONFIRMED | `orders.amend` changes **the same order** (row, code, card), "Actualizado HH:MM". One transaction: items, totals and `payments.follow_total` land together or not at all |
| 3 | Latest order is ACCEPTED, READY or OUT_FOR_DELIVERY | A **proposal** (below): nothing changes until the customer says "sí"; then it applies on its own (AUTO) or waits for the restaurant on the card (APPROVAL) |
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
  (`NotEditable`) and the change becomes a proposal instead.
- `start_separate_order` (only in window 3) opens a cart with `related_order_id`; for
  delivery the bot warns of a second fee and trip. No rule reads that link.
- Cancel by chat: before acceptance it cancels (`customer_declines` or
  `customer_cancels`) and opens a fresh cart; after acceptance it is handed off.
- Payment-method changes: `payments.md` §1.

### After acceptance (Task E, `changes.py`)

The bot builds the target the order would become (a staging cart, never the customer's
own) and shows it with the price and total code computed. The customer must write "sí"
(`confirm_change`) or "no" (`discard_change`), typed or in a voice note. A 👍/👎 sticker
counts only as the answer to the one question code knows was shown (`main._sticker`).

| State | Route |
|---|---|
| ACCEPTED | **AUTO** when the change only adds products the menu marks `add_after_accept` (available, no modifiers, no note, same delivery): «Agregar 1× Gaseosa personal ($3.500). Su pedido quedaría en $X. ¿Lo agregamos?». Anything else, or a mix, is one **APPROVAL** request |
| READY | APPROVAL (even a drink) |
| OUT_FOR_DELIVERY | APPROVAL; changing to pickup is refused («El pedido ya va en camino…») |

- AUTO on "sí": rechecked (order unchanged, still ACCEPTED, product still allowed), then
  applied in one transaction with the payment split and the customer's notice; the card shows
  «➕ El cliente agregó …» until Visto. A stale one is dropped and the bot asks again.
- APPROVAL on "sí": `PENDING` on the card («✏️ Cambio solicitado», what changes, total now →
  proposed). The order stays as it was. A new change from the customer reopens it (`PROPOSED`,
  `revision` + 1) until they confirm again. One open request per order (a unique index).
- **Aceptar cambio**: preview (the exact message and new total), then save. Refused when the
  customer changed the request meanwhile (the revision), when the order changed after the
  request, or when the order or its payment changed after the preview (the state token).
  En camino needs «Ya coordiné con el domiciliario…»; Listos reminds «Si hay que preparar
  algo, use Volver a preparar» (staff decide). **Rechazar** needs a reason.
- An order that ends with a request waiting closes it; if it was delivered the customer gets
  «El cambio que pidió no se alcanzó a hacer: su pedido ya fue entregado.»
- Requests never open the amber box nor move the Chatwoot conversation.

**Editar** (any card, Nuevos to En camino): products, quantities, modifiers, notes, address,
barrio (fee from the zone table) and pickup ↔ delivery, plus «Quitar ubicación». «Revisar
cambios» shows the exact WhatsApp message; «Guardar y avisar al cliente» saves only if the
order and its payment are what the preview showed. En camino needs the driver box and can't
become pickup; an empty order is refused (use Cancelar or Rechazar); no change → «No cambió
nada». Saving closes an open request (the edit message covers it).

After any applied change that undoes a MIXED split (`follow_total`'s `ask_method`): if the
bot can record a method (`brain.method_lock_reason` is None) the message asks how they will
pay; otherwise it says «El local se comunicará con usted para acordar el pago.» and the card
shows «Acordar pago» until staff register cash or confirm a receipt.

## 4. Handoff to a person

- `_handoff` (a cancel after acceptance, or a payment-method change) and `request_handoff`
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
- With Chatwoot (Task D Part 2) the box also hands the conversation to a person there:
  after the bot's reply went out, Chatwoot gets a private note with the summary and the
  order code and the conversation moves to Open; the bot stays quiet while it is Open.
  If Chatwoot fails, the card says «Chatwoot sin actualizar» and the worker retries every
  30 s (the bot keeps answering until then). Pending or resolved in Chatwoot closes the box.
- "Ya lo atendí" resolves every open request on that order; nothing is sent to the
  customer. With Chatwoot it first sets the conversation to Pending; if that fails the box
  stays (502). Without Chatwoot the bot keeps answering while a box is open.
- Staff can also take any conversation in Chatwoot without a box (open it, or reply on a
  Pending one), and give it back with Pending (`CHATWOOT-SETUP.md` §6).

## 5. Who gets told what

| Event | Message to the customer |
|---|---|
| ACCEPTED | With the minutes staff picked: "¡Recibimos su pedido! Tardará aproximadamente 20 minutos." (never a clock time); without, or after Volver a preparar: "¡Recibimos su pedido! En un momento entra en preparación." |
| Cambiar tiempo (ACCEPTED) | "Disculpe la demora: a su pedido le faltan aproximadamente X minutos." / "Su pedido va más rápido: le faltan…"; nothing if it moved under 5 min |
| READY, pickup | "Su pedido ya está listo para recoger." |
| READY, delivery | Nothing; the "va en camino" message covers it |
| OUT_FOR_DELIVERY | "Su pedido va en camino." |
| COMPLETED | Nothing |
| REJECTED, or CANCELLED by staff | Short, kind AI message from the staff's reason; fixed template with the reason if the AI fails |
| CANCELLED by the customer | Nothing; they already know |
| Receipt confirmed by staff | Code-built message (`payments.md` §4) |
| Receipt discarded by staff | One AI message from the reason, guarded; fixed template on failure |
| Cash registered, or a cash row discarded | Nothing; ESTADO REAL and the payment sentence carry it when the customer asks |
| Staff **Editar** | «El local actualizó su pedido. Así quedó:» + lines, delivery and payment sentence (code) |
| AUTO addition | «Listo, agregamos …. Así quedó su pedido:» + the same (code), then a short bot reply |
| Request accepted | «El local aprobó su cambio. Así quedó:» + the same (code) |
| Request rejected | «El local no pudo hacer el cambio que pidió. Motivo: <reason>. Su pedido sigue igual.» |
| Delivered with a request waiting | «El cambio que pidió no se alcanzó a hacer: su pedido ya fue entregado.» |

The five Task E messages are queued in the same transaction as the change (`wa.queue`) and
sent after the commit; one left by a crash is sent by the worker after 15 s. A failed one, or
one sending for over 2 minutes, is listed under «⚠ Avisos sin enviar» for **Reenviar**: a
message about the order goes out as the order is now, a template as it was. A message over
3.900 characters is refused before anything is saved.

No customer message may contain the order code (`P-1234`) or the word "cocina"; tests
check every fixed notification. The bot's state answers come from ESTADO REAL
(`ARCHITECTURE.md` §7), never from its own earlier messages.
