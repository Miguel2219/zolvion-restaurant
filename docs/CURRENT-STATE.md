# Current state

Snapshot: code dated 2026-09-30 (checked 2026-10-01: 210 automated tests pass), Notion
checklists last edited Task A 2026-09-23, Task B and Task C 2026-09-30 (Bogotá time).

---

## 1. What works today

### Customer side (WhatsApp, or the browser simulator)

- Free-text questions and orders. The bot maps words to exact menu ids; code computes
  every line, subtotal, delivery fee and total. Items not on the menu are refused, never
  invented; `available: false` items are refused as sold out.
- Modifiers per burger, priced per unit. Identical lines merge; when an item sits on two
  different lines, the bot asks which one before changing anything.
- Pickup or delivery; the fee comes from a zone table (accent- and case-insensitive,
  partial match), unknown zone = default fee.
- Summary → the customer's "sí" → the order appears in Nuevos.
- A change before the restaurant accepts updates **the same order** (same code, same
  card, "Actualizado"). After acceptance the change goes to a person (amber box).
- A separate order while the first one is accepted (warns about a second delivery fee).
- Cancel by chat before acceptance; after acceptance it goes to a person.
- "Quiero hablar con una persona": asks the reason once; a covered reason runs its tool;
  anything else opens the amber box when the customer has a card.
- Payment: cash, transfer or mixed (split validated by code), or inferred from a clean
  receipt. Every receipt image is its own row with its own checks. The bot says
  "received, a person will review it" or "there is a problem, a person will check";
  never "confirmed" unless a person confirmed it.
- "¿Cómo va mi pedido?" and payment questions answer from the real state (ESTADO REAL),
  not from the chat history.
- Automatic messages: accepted, ready (pickup), out for delivery; AI-written messages for
  staff reject/cancel and discarded receipts, from the staff's reason; a code-built
  message when staff confirm a receipt.

### Staff panel (`/kitchen`, also `/`)

- Columns **Nuevos** · **Recibidos** · **Listos** · **En camino**, refreshed every 2 s.
  Orders waiting for the customer's "sí" are not shown.
- Buttons per column; reject/cancel need a reason (`ORDER-LIFECYCLE.md` §2).
- Tags: method, receipt status ("Sin comprobante" only on transfer/mixed), Sin pagar /
  Pago incompleto · faltan $X / Pago completo, Efectivo por cobrar, Sobrepago,
  Actualizado, pedido aparte, delivery fee.
- "Comprobantes" block: one row per receipt with time, amount, status, 👁 photo, grey
  notes, red box with reasons, Confirmar / Descartar while it waits for a person.
- "Registrar efectivo recibido": prefilled, capped at what is owed, warns about receipts
  not yet confirmed.
- Amber "🙋 Necesita a una persona" box with "Ya lo atendí".
- Red pulse for a Nuevos order older than 600 s (`/kitchen?alert=20` for demos).
- Green dot = WhatsApp connected; grey dot + banner = simulator mode.
- "Reiniciar demo" wipes **all** data (photos included) after a warning.

### Reports (`/reports`) and simulator (`/simulator`)

- Reports for today (Bogotá calendar day), 7 or 30 days: sales, orders, average ticket,
  delivery fees, top 5 items, how they paid, pickup vs delivery, lost orders, orders
  waiting for the "sí", receipts flagged and awaiting (these two all time).
- The simulator is a fake WhatsApp chat with image upload; the conversation is rebuilt
  from the database; `?phone=` picks the number (default `573001112233`).

---

## 2. Task history

Each task has a Notion "Task X Plan" checklist that Miguel tests by hand (`WORKFLOW.md`).

| Task | Scope | Status |
|---|---|---|
| A | `UNSPECIFIED` method; receipts for unspecified/cash orders; checks on date, amount and account; customer told when staff confirm; paid/unpaid/incomplete on the panel; overpayment | Done: 6/6 ✅ |
| B | Remove "En preparación"; edit an unaccepted order in place; hand off changes to accepted orders; payment follows an edit; second receipt; payment-method changes. Fix round 1 (28 Sep): code-decided routing, `request_handoff` gate, line merge and targeting, one reply per message, "aceptado por el local" wording, grey "Revisado". Fix round 2 (29 Sep): "hablar con una persona", ESTADO REAL | Done: 55/55 ✅ (147 tests) |
| C | One row per receipt with its own checks, photo and Confirmar; database-checked reference for every receipt; resend vs reuse; Descartar with reason; cash capped at what is owed; surplus counts cash; cash settles an unspecified method; ESTADO REAL payment line (P-2465, P-0458, P-6455, P-4766 fixes) | Done: 40/40 ✅ (210 tests) |
| D | Planned. Chatwoot: a handoff opens a conversation a person answers in the same WhatsApp chat; the bot stays silent for that customer until the person closes it | Not started |
| — | Planned as its own task: a notifications bell, so a customer with no card who asks for a person reaches someone | Not started |

Task B rows about the "Llegó otro comprobante…" box and "Confirmar pago manualmente"
describe behaviour that Task C replaced. Only what is written here is known about D and
the bell; their scope is Miguel's decision.

---

## 3. Open defects (code review, 2026-10-01)

Found by reading and probing the code; **not yet seen in a manual test** and not fixed.
Whether and when to fix them is Miguel's call.

| # | What happens | Where |
|---|---|---|
| R1 | Order fully paid in cash (no receipt yet), then a transfer for the total arrives: the receipt is **clean**, the method becomes TRANSFER with no cash part, and the bot says "el pago quedó por transferencia por el total". Task C's row "fully paid in cash, then a transfer → red 'No se esperaba otro pago'" only holds when the order already had a receipt | `payments._expected_for_new_receipt` ignores `cash_received` for a first receipt |
| R2 | A TRANSFER order where staff registered cash: the bot's state says "todavía falta por transferir" and "ya está pagado completo" together, the card shows "Sin comprobante" next to "Pago completo", and a later transfer for the remainder is checked against the full agreed amount (red) | `payments.current().transfer_due` ignores `cash_received` |
| R3 | Staff confirm a partial transfer on a TRANSFER order: the customer is told "Quedan $X por pagar en efectivo al recibir", although they agreed to transfer | `main._payment_confirm_message` |
| R4 | Editing an order whose line has a modifier added twice (e.g. double queso) keeps it once: the line silently gets cheaper | `cart.fill_from_order` rebuilds modifier ids by name |
| R5 | A voice note, sticker or location (or an image that failed to download) is sent to Claude as an empty message; the API refuses it and the customer gets "Perdón, se me cruzaron los cables" | `main.handle` / `wa.parse_webhook` |
| R6 | Some errors reach staff and the bot in English ("floats are not allowed as money", "'food_ready' is not legal from CONFIRMED") | `money.pesos`, `orders.apply_event` messages |
| R7 | With a config that does not list `CASH`, after staff register cash on an unspecified order every later edit fails ("CASH no está habilitado") | `payments.follow_total` → `set_method` |
| R8 | Creating an order writes the order, its lines, the cart and the payment without a transaction, and checks the total with an `assert` after writing | `orders.create_from_cart` |

---

## 4. Tests

`tests/test_flow.py`: 174 test functions, **210 test cases** with parameters, all passing
(2026-10-01, 3 deprecation warnings from FastAPI's `on_event`). No AI calls happen in
tests. Areas: `BUILD-AND-RUN.md` §5.
