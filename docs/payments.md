# Payments

Payment runs beside the order and **never blocks it**: a red receipt, an unpaid balance
or an overpayment never stops a panel button. Three roles, never mixed:

- **The AI reads** the receipt image and returns raw fields.
- **Code decides** whether those fields make sense, and writes every sentence about money.
- **A person confirms** the money arrived (or discards a receipt or a cash row). The bot
  never says a payment is confirmed unless ESTADO REAL shows a person confirmed it.

Code: `app/payments.py`, called from `main.handle`, the panel routes and `orders.amend`.
Every receipt is its own row (`payment_receipts`, Task C); every cash registration is its
own row (`cash_payments`, Task C2 Part 2).

---

## 1. Methods

| Method | Meaning | Agreed split |
|---|---|---|
| `UNSPECIFIED` | Default on every new order | 0 / 0 |
| `CASH` | Pays on delivery or pickup | transfer 0, cash = total |
| `TRANSFER` | Whole total by transfer | transfer = total, cash 0 |
| `MIXED` | Part transfer, part cash | Code checks transfer + cash = total, none negative |

`choose_payment` (bot tool) calls `set_method`; only methods in the config's
`payments.methods` are accepted. It goes to a person instead (amber box, method
unchanged) once a receipt has arrived, cash was registered, or the order is out for
delivery; repeating the current method answers "no cambió nada".

The method also changes without the customer: a clean first receipt **infers** it (§3), a
person confirming on an `UNSPECIFIED`/`CASH` order sets it, and registering cash on an
`UNSPECIFIED` order sets `CASH` with `payments.method_from_cash = 1`. Discarding the **last**
cash row of such an order puts it back to `UNSPECIFIED`; while another cash row remains it
stays `CASH`. Any other write that changes the method clears the flag, so a method the
customer chose, or that a receipt set later, is never reset (decision Gap 1, 7 Oct).

## 2. The "Pagos" block (kitchen card)

One list, oldest first: every receipt and every cash registration that is not discarded.
A discarded row leaves the card entirely and counts nowhere (money tags, status, reports).

| Row | Shows | Actions |
|---|---|---|
| Receipt `PROOF_RECEIVED` | time, amount, "Comprobante recibido", 👁, grey notes | Confirmar, ✕ |
| Receipt `FLAGGED` | same, "Comprobante por revisar", red box with reasons | Confirmar, ✕ |
| Receipt `CONFIRMED` | "Comprobante confirmado · $X"; flags as grey "✓ Revisado" | none |
| Cash `RECEIVED` | time, amount, "Efectivo recibido" | ✕ (no Confirmar: staff hold it) |

The order-level receipt tag is derived: the worst receipt wins (`FLAGGED` >
`PROOF_RECEIVED` > `CONFIRMED`), discarded ones are ignored, none left = `PENDING` ("Sin
comprobante", shown only on `TRANSFER`/`MIXED` orders with a transfer due). The money tag
reads **Sin pagar / Pago incompleto · faltan $X / Pago completo**, plus "Efectivo por cobrar
$X" (CASH and MIXED) and "Sobrepago $X".

## 3. When an image arrives (`main.handle` → `check_proof`)

1. No live order → the image goes to the conversation like any message.
2. Otherwise it lands on the **newest** live order (PENDING_CONFIRMATION…OUT_FOR_DELIVERY):
   `extract_proof` (AI) → `check_proof` (code) → a new row with its own flags, notes,
   OCR data, reference and photo → the bot gets a bracketed instruction.

**Expected amount** (`_expected_for_new_receipt`). Registered cash comes off first:
`left` = total − `cash_received`.
- First receipt, or every earlier one is red/discarded: `_expectation`. `left` ≤ 0 → red
  "No se esperaba otro pago." A transfer was agreed (TRANSFER, or MIXED with a transfer
  part) → that amount, capped at `left`. Otherwise → `left`, and a clean receipt infers the
  method: the whole total (no cash) → TRANSFER, up to `left` → MIXED, more → red "El
  comprobante dice $X y el pedido es de $Y." / "… y faltaban $Y." (with cash).
- Later receipt: `transfer_due`; nothing left to transfer on MIXED with cash owed →
  `cash_due`; nothing owed → red "No se esperaba otro pago."

**Checks** (each failure adds a Spanish flag; any flag → `FLAGGED`): amount via
`read_ocr_amount` (the one place AI output becomes a number; unreadable → flag); date within
*order created − skew* … *message received + skew* (grey note past `proof_notice_minutes`);
destination (exact digits or masked prefix + last four; holder name only when no number is
shown); grey note "Llegó mientras otro comprobante está en revisión"; reference (unique
index). On a repeated reference: same order and same readable amount = **resend** (no row);
if that row was discarded the bot gives the staff's reason again, never "ya lo teníamos"
(Gap 2); anything else → red "Este comprobante ya fue usado antes (referencia X)".

**What the bot is told:** red → there is a problem, a person will review it, never "paid".
Clean → "recibimos su comprobante", a person will review it, the payment sentence (§7) to
copy as is, and never ask again how they will pay.

## 4. People only: confirm, discard, cash

- **Confirmar** (`confirm_receipt`): the amount actually received (prefilled with the OCR
  amount); 0 is refused ("…descártelo con la X."). On UNSPECIFIED/CASH it sets TRANSFER or
  MIXED from the confirmed total; it never rewrites an agreed TRANSFER/MIXED split. The
  customer gets a code-built message (`main._payment_confirm_message`): "Confirmamos su
  transferencia de $X." plus "El otro comprobante que envió sigue en revisión" while another
  receipt waits; otherwise "Quedan $A por transferir y $B por pagar en efectivo al recibir."
  (each part only if owed) and, with a transfer owed, "Cuando haga la transferencia,
  envíenos el comprobante."; then, only when confirmed receipts plus cash exceed the total,
  "Vimos que envió $Y de más; el local se va a comunicar con usted por el excedente."
- **✕ on a receipt** (`discard_receipt`): only `PROOF_RECEIVED`/`FLAGGED`; reason required
  (400 without it). The row leaves the card, keeps its reference (still refused on another
  order) and stays in ESTADO REAL as "descartado". The customer gets one model-written message
  from the reason; the template is sent if the model fails or writes "confirmad", "cocina",
  "$" or the order code.
- **Registrar efectivo recibido** (`register_cash`): one `cash_payments` row per registration,
  never above `outstanding` («El monto es mayor de lo que falta ($X)…»). Prefilled with
  `cash_due` on CASH/MIXED. No warning about waiting receipts: cash registered by mistake is
  fixed with its ✕.
- **✕ on cash** (`discard_cash`): reason required («Hace falta el motivo.»); nobody is told.
  The row counts nothing from then on; the method follows §1. Receipts are never re-checked:
  one that went red because that cash paid it all stays red, and a person confirms it.
- **Amount boxes** are text boxes with one rule (`money.pesos` / `parsePesos`): plain digits,
  or groups of three with one separator. A decimal is refused, never rounded.

## 5. The numbers (`current()`, the only money math)

| Field | Meaning |
|---|---|
| `cash`, `cash_received` | Every cash row; the sum of those not discarded |
| `sent` | What receipts count for: confirmed at `confirmed_amount`, clean waiting ones at OCR amount, red/discarded 0 |
| `confirmed`, `unconfirmed_amount` | Confirmed receipts; clean receipts nobody confirmed yet |
| `transfer_due` | `max(min(transfer_amount, total − cash_received) − sent, 0)` |
| `cash_due` | `max(total − sent − cash_received − transfer_due, 0)` |
| `to_pay` | `max(total − sent − cash_received, 0)` = `transfer_due + cash_due` |
| `paid` / `outstanding` | Confirmed + cash, capped at the total / `total − paid` |
| `received` | Confirmed + cash, **not** capped (Reportes "Dinero recibido") |
| `overpayment_amount` | `max(sent + cash_received − total, 0)`: the card's "Sobrepago" (counts waiting receipts, G32) |
| `confirmed_overpayment` | The same with confirmed receipts only |
| `status`, `under_review` | Derived order-level status; true while any receipt is red |

## 6. When the order total changes (`follow_total`)

Called inside `orders.amend`'s transaction; it only moves the agreed split. With no receipt
that counts: CASH/TRANSFER follow the new total; MIXED keeps its transfer (the rest goes to
cash) or, if the transfer now covers everything, resets to UNSPECIFIED and the bot asks
again (`ask_method`). With a receipt that counts: TRANSFER moves to the new total, MIXED
keeps its agreed transfer. What the customer hears is the payment sentence (§7).

## 7. What the bot says about money

**The payment sentence** (`brain.payment_sentence`, from `current()`, for the newest live
order; "En su pedido más reciente:" first when there are several). Each part only when it
applies, in this order: «Ya pagó $C en efectivo.» · «El local ya confirmó su transferencia de
$K.» · «Su comprobante por $W está pendiente de confirmación del local.» · «El local está
revisando un comprobante que nos envió y se comunicará con usted.» · then the first of
«Su pedido ya está pagado completo.» (outstanding 0, even with a red receipt) / «Por ahora no
necesita realizar otro pago.» (to_pay 0) / nothing (red receipt: no payment request) / «Falta
por pagar $X.» (UNSPECIFIED) / «Quedan $A por transferir y $B por pagar en efectivo al
recibir.» plus, with a transfer due, «Puede transferir a {cuentas}. Cuando transfiera,
envíenos el comprobante.» · then «Pagó $S más que el total del pedido; el local se comunicará
con usted por esa diferencia.» (confirmed surplus) or, for a surplus that counts a waiting
receipt, «Con su comprobante pendiente, lo enviado supera el total del pedido; el local
revisará la diferencia cuando lo confirme.» (no amount, never "pagó").

**Where it goes:** edit results, a successful `choose_payment`, `order_status` and the
clean-receipt instruction, each telling the bot to copy it as is. A customer message with
payment words (pago, debo, saldo, comprobante, transferencia, efectivo, Nequi, "cuánto me
falta"…) forces `order_status` on the turn's first call.

**The end-of-turn check** (`brain._finish_turn`) runs on every reply that speaks about
payment: a draft that claims a payment status with no fresh read is replaced (forced
`order_status`, then a new answer). The sentence must be in the reply; a missing one alone is
appended. A contradiction ("ya está pagado" while owed, "falta" while nothing is owed or a
receipt is red), an earlier sentence a later change made stale, or an amount that does not
match what its clause says it is (owed by transfer = `transfer_due`, in cash = `cash_due`,
owed = `to_pay`, paid in cash = `cash_received`…; a discarded row's amount only explains a
discard) → one more answer; still wrong → a code-built reply: what was done in the turn
(order updated with its summary, confirmed, cancelled, handoff, receipt received) plus the
sentence. A delivered or cancelled order, or "el pedido anterior", can't be checked here:
the reply says «No pude verificar el pago de ese pedido.» and claims nothing. A failed read
sends «En este momento no pude revisar el estado de su pago…», never an old amount.

**ESTADO REAL** (`brain._payment_state`, no amounts): method, receipts by status (received,
confirmed, with a problem, discarded), "el local ya recibió una parte en efectivo", "el
local anuló N registro(s) de efectivo, que ya no cuenta(n) como pago", what is still owed,
and "ya está pagado completo" / "todavía falta que una persona confirme el pago" /
"todavía falta por pagar". `PAYMENT_RULES` make it win over the chat, keep an annulled cash
row unmentioned unless asked (then: the local annulled that registration; never a refund,
never "no entregó el dinero"), and send every amount through `order_status`.

## 8. Reportes "Cómo pagaron"

Per order, by what really came in (`received`): cash only → Efectivo, confirmed transfers
only → Transferencia, both → Mixto, nothing → "Sin pago registrado" (count only). Waiting
receipts and discarded rows never count; amounts are "Dinero recibido", not capped. Two more
panels: "Pagos de más confirmados" (order code, total, received, surplus) and "Pagos de
pedidos cancelados/rechazados" ("Pagos registrados", to review with staff; no refund
records exist).

Open gaps in this area: `KNOWN-GAPS.md` G32. Closed by C2 Part 2: G27, G29, G30.
