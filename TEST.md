# How to test the system

- **Part A** — browser only, no phone. If it fails, the problem is code or config.
- **Part B** — a real phone, only after Part A is green.
- **Part C** — regression by area, after any change. Each line: what to do → what you see.
- **Part D** — automated tests.

The full per-task checklists, with Miguel's verdicts, are the Notion pages
"Task A/B/C Plan" and "Task C2 Plan" Parts 1 and 2 (Zolvion - Demo Documentation › Tasks).
Expected totals come from `config/restaurant.json`: change a price and they change too.

---

## Part A — in the browser (15 min)

`uvicorn app.main:app --reload --port 8000`, then open `/simulator` (you type as the
customer) and `/kitchen` side by side. Click **Reiniciar demo** first.

**A1 · Menu.** `Buenas, ¿qué tienen?` → Caserita $18.900 · Madurita $21.900 · Doble con
Pechuga $24.900 · Especial $29.900. Any other price = the bot invented it: stop.

**A2 · Totals.** `Quiero dos caseritas maduritas y una gaseosa personal` → exactly
`2x Caserita Madurita $43.800`, `1x Gaseosa personal $3.500`, `Total: $47.300`.

**A3 · Invention trap (most important).** `¿Tienen pizza hawaiana?` → no, and it offers
something real. `¿Tienen porción de papas aparte?` → the burgers already come with papas;
no invented price.

**A4 · Change.** `Mejor quita la gaseosa` → total **$43.800**.

**A5 · Delivery.** `Es para domicilio en Cabecera, calle 48 #34-20` → `Domicilio
(Cabecera): $5.000`, total **$48.800**. Then `Sí, confirmo` → card in **Nuevos** within
2 s, $48.800, 🛵 Domicilio.

**A6 · Buttons talk back.** Aceptar → Listo → Sale a domicilio → Entregado → a message in
the simulator after Aceptar and Sale a domicilio (none after Listo on delivery, none after
Entregado); the card moves and disappears after Entregado.

**A7 · Wrong receipt.** New order, confirm, `Voy a pagar $20.000 por transferencia y el
resto en efectivo` → the split and the Nequi account. Send (📎) a screenshot that is **not**
$20.000 → its row is red ("El monto no coincide…"); the bot says a person will review it,
never that the payment is confirmed.

**A8 · Right receipt.** Make a **new real transfer after confirming the order**, for the
exact agreed amount (an old screenshot is red: it predates the order). → row "Comprobante
recibido", no red box; the bot says it was received and a person will review it. Press
**Confirmar** on the row → "Comprobante confirmado" and the customer gets a message.

**A9 · Reuse.** The same screenshot on another order → red "Este comprobante ya fue usado
antes (referencia …)".

**A10 · Reports.** `/reports` → sales, orders, average ticket, top item, how they paid,
pickup vs delivery, lost orders, receipts flagged / awaiting, all from what you just did.

## Part B — a real phone (10 min)

1. The dot on the kitchen tab is **green** (grey = WhatsApp not connected).
2. From a phone that is not the business number, write to the Claro number.
3. Repeat A1, A2, A3, A5, A6 (same numbers), then A7 with a screenshot from the gallery.
4. Messages that are not text (R5), one by one:
   - **Voice note** → the bot says it can't listen to voice notes, asks you to write it and
     carries on with the order; it never acts as if it understood.
   - **Sticker** → the same, for stickers.
   - **Location with an address** (pick a place) → it uses that address for delivery, asks
     for the barrio if you didn't say it, and never invents an address from the coordinates.
   - **Location without an address** (a dropped pin) → it asks for the written address
     (street, number, barrio).
   - **👍 reaction** on a bot message → no reply at all, and no error in the uvicorn window.

| What you see in the uvicorn window | Meaning |
|---|---|
| Nothing at all | Meta is not reaching you: ngrok died, old webhook URL, or `messages` not subscribed |
| `403` | Wrong `WHATSAPP_APP_SECRET` |
| A message arrives but no reply | Bad `ANTHROPIC_API_KEY`, or no credit |
| Reply generated but never arrives | Wrong `WHATSAPP_PHONE_NUMBER_ID`, or invalid token |
| `Perdón, se me cruzaron los cables` | The code threw; the traceback is there |

---

## Part C — regression by area

Restart uvicorn first (it clears the bot's chat memory). Suggested order: pickup,
1 Caserita + 1 Gaseosa personal = $22.400; another Gaseosa personal adds $3.500.

### C1 · Changing an order in Nuevos (same card, never a person)
- Confirmed order, `Agrégueme una gaseosa personal` → same card and code, "Actualizado
  HH:MM", new total; the bot shows it and does not ask for a new "sí". No amber box.
- `Mejor para domicilio en Cabecera, calle 48 #34-20` / `Cámbieme la zona a Girón` → same
  card, new delivery fee.
- `En vez de dos hamburguesas dame una` → same card, 1x, new total.
- Only one product, `quíteme la especial` → one reply asking whether to cancel; `sí` →
  cancelled, no amber box.
- `¿qué tengo en mi pedido?` then an add → one card, no "ya fue enviado al local".
- Two more identical gaseosas → one line "3× Gaseosa personal"; `quíteme una gaseosa` → 2×.
- `Una caserita con doble queso`, confirm, then add a gaseosa → the card keeps "Caserita
  (Queso, Queso)"; the bot mentions no problem with the queso.
- 1 Caserita + 1 Caserita sin cebolla, `déjeme dos caseritas` → the bot asks which line,
  without ids; only the chosen line changes. A line that does not exist → it says so.
- Reports › "Pedidos que no se concretaron" stays at zero after these edits.

### C2 · After Aceptar (goes to a person)
- `Quiero cambiar la gaseosa por una cerveza` → the bot says a person will contact them,
  promises nothing; amber "🙋 Necesita a una persona" box; the order is unchanged.
- Repeating the same request adds no second line; "Ya lo atendí" clears the box and sends
  nothing; cancelling an accepted order also goes to the box.
- A new, separate order → a second card; for delivery the bot warns of a second fee.

### C3 · "Quiero hablar con una persona"
- Without a reason → the bot asks what it is about, once; says nothing about the state.
- Reason is a change (`es para agregar una gaseosa`) → the change is made, no box.
- Reason no tool covers (`necesito factura electrónica con NIT`) → amber box on the card in
  Nuevos or Recibidos; the bot mentions no change and promises no time.
- `no, sólo quiero hablar con alguien` → box saying the customer gave no reason.
- No card (new `?phone=`, or only a delivered order) → "por ahora no puedo pasar esa
  solicitud"; no box, no promise. After Aceptar it never says "no ha sido aceptado".

### C4 · What the customer receives
- Aceptar → "¡Recibimos su pedido! En un momento entra en preparación." · Listo (pickup)
  → "Su pedido ya está listo para recoger." · Sale a domicilio → "Su pedido va en camino."
  · Listo (delivery), Entregado, customer cancels → nothing.
- Rechazar/Cancelar ask for a reason (required) → a kind message built from it.
- After `Sí, confirmo` the bot only says it is confirmed, never "enviado al local".
- No message ever shows `P-1234`, the word "cocina", or says the bot confirmed a payment.

### C5 · Receipts (each one is its own row in "Pagos": time, amount, status, 👁)
- 👁 opens the photo; click outside or Esc closes it.
- Add a gaseosa after a clean receipt → the bot asks for exactly $3.500 and shows the
  accounts; a $3.500 transfer → a second clean row. Another amount → only that row is red.
- Same screenshot again on the same order → no new row; "ya lo teníamos" (if staff had
  discarded it: the bot gives their reason again, never "ya lo teníamos").
- Same reference, different amount (edited image) → red "ya fue usado antes", photo kept.
- No method chosen: old screenshot, then a new one for the total → second row clean with the
  grey note "Llegó mientras otro comprobante está en revisión"; the method tag changes only
  after the clean one.
- `Parte y parte` with no amounts, then a receipt for the total → "Transferencia", clean
  row; the bot does not ask the split again. `¿Ya les llegó mi comprobante?` → yes.
- Mixed order: also transferring the exact cash part → clean row; both confirmed → "Pago
  completo" with no cash registered.
- All rows confirmed and another receipt → red "No se esperaba otro pago".
- Full total registered in cash, then a transfer → red "No se esperaba otro pago.", the
  method stays "Efectivo", no "Sobrepago".
- $5.000 order with $2.500 cash: a $5.000 transfer → red "El comprobante dice $5.000 y
  faltaban $2.500."; a $2.500 transfer instead → clean row (also on a transfer order).

### C6 · Staff actions on a receipt
- Confirmar opens with that receipt's amount ("Monto que realmente llegó…"); $0 is refused
  with a pointer to the X. Confirming one of two → "Confirmamos su transferencia de $X.
  El otro comprobante que envió sigue en revisión." and "Pago incompleto · faltan $Y".
- Confirming a red row → its text stays as a grey "✓ Revisado" line.
- Partial transfer confirmed on a transfer order → "…Quedan $X por transferir. Cuando haga
  la transferencia, envíenos el comprobante.", nothing about cash; on a mixed order →
  "…Quedan $Y por pagar en efectivo al recibir."
- ✕ on a waiting receipt asks for a reason (empty = refused, nothing sent) → the row leaves
  the card; one short message to the customer with the reason; `¿Cómo va mi pedido?` never
  says it is still under review; the same screenshot on a new order is red "ya fue usado
  antes". A confirmed receipt has no ✕.

### C7 · Cash
- Cash order $22.400: the dialog opens with $22.400; $30.000 is refused with «El monto es
  mayor de lo que falta ($22.400)…»; $22.400 → "Pago completo", no "Sobrepago".
- The amount box (Confirmar and Registrar efectivo) is a plain text box with no spinner
  arrows: `22.400`, `$22.400` and `22400` register $22.400; `22400.5` and `2,5` are refused with
  "Escriba el monto en pesos, sin decimales…" and nothing is stored.
- Each registration is its own row ("Efectivo recibido", ✕): $5.000 then $2.400 on a
  Caserita → two rows, "faltan $11.500". ✕ needs a reason, sends nothing; the tags go back.
  No method chosen: the tag returns to "Sin especificar" only when the last cash row goes.
- $52.300 order with a clean, unconfirmed $30.000 receipt → no warning in the dialog;
  registering $52.300 shows "Sobrepago $30.000" (G32); confirming the receipt sends
  "…envió $30.000 de más; el local se va a comunicar…".
- No method chosen, register the full total → "Efectivo" and "Pago completo", no "Sin
  comprobante"; the bot never asks how they pay; `mejor lo pago por transferencia` → amber
  box, method unchanged; `lo pago en efectivo` → "no cambió nada".
- Transfer order, full total registered in cash: `¿Ya quedó pago mi pedido?` → paid, the
  restaurant received the cash; no Nequi, no request for a transfer or a receipt.
- Config without `CASH` (restore it and restart after): Caserita, $18.900 cash, then add a
  gaseosa personal → same card, $22.400; the bot says $18.900 paid and $3.500 left in cash.
- Mixed $10.000 + cash, a $15.000 transfer confirmed at $15.000 → "Efectivo por cobrar
  $7.400". Removing a product below what was paid → "Sobrepago", no refund promised.

### C8 · Panel, reports, simulator
- Columns Nuevos · Recibidos · Listos · En camino; "Volver a preparar" returns to Recibidos.
- No receipt state ever blocks Aceptar, Listo, Sale a domicilio or Entregado.
- Reports: "Comprobantes marcados"/"por confirmar" count all time, discarded excluded;
  "Hoy" is the Bucaramanga calendar day; "Sin confirmar" shows orders waiting for the "sí";
  "Cómo pagaron" by money received (cash, confirmed transfers), plus confirmed
  overpayments and payments on cancelled/rejected orders.
- Leaving the simulator and coming back keeps the whole conversation.
- Last: copy a photo address, Reiniciar demo → that address shows "Sin foto".

## Part D — automated tests

`python -m pytest tests -q` → **306 passed, 1 xfailed** (Task B ended with 147, Task C
with 210, C2 Part 1 with 263; the xfail is G16, out of scope).

## Before the demo

Click **Reiniciar demo**, run the demo once end to end and record it. Still to confirm
with the restaurant: burger prices (read off an image), delivery zones and fees, hours,
and the Nequi account (today it is Miguel's test account).
