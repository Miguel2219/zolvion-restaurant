# Payments

Payment runs beside the order and **never blocks it**: a red receipt, an unpaid balance
or an overpayment never stops a panel button. Three roles, never mixed:

- **The AI reads** the receipt image and returns raw fields.
- **Code decides** whether those fields make sense.
- **A person confirms** the money arrived (or discards the receipt). The bot never says
  a payment is confirmed unless ESTADO REAL shows a person confirmed it.

Code: `app/payments.py`, called from `main.handle`, the panel routes and `orders.amend`.
Since Task C every receipt is its own row (`payment_receipts`).

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
delivery; repeating the current method answers "no cambió nada" with no box.

The method also changes without the customer: a clean first receipt **infers** it (§3), a
person confirming on an `UNSPECIFIED`/`CASH` order sets it, and registering cash on an
`UNSPECIFIED` order sets `CASH`.

## 2. Receipt status

| Receipt status | Panel label | Meaning |
|---|---|---|
| `PROOF_RECEIVED` | Comprobante recibido | Passed every check; waits for a person |
| `FLAGGED` | Comprobante por revisar (red box with reasons) | At least one check failed; waits for a person |
| `CONFIRMED` | Comprobante confirmado · $X | A person confirmed it and set `confirmed_amount` (final) |
| `DISCARDED` | Descartado (grey, "Motivo: …") | A person said it is not a payment (final); counts 0 |

The order-level tag is derived: the worst receipt wins (`FLAGGED` > `PROOF_RECEIVED` >
`CONFIRMED`), discarded ones are ignored, none left = `PENDING` ("Sin comprobante",
shown only on `TRANSFER`/`MIXED` orders). Separately, the money tag reads **Sin pagar /
Pago incompleto · faltan $X / Pago completo**, plus "Efectivo por cobrar $X" (CASH and
MIXED) and "Sobrepago $X".

## 3. When an image arrives (`main.handle` → `check_proof`)

1. No live order → the image goes to the conversation like any message.
2. Otherwise it lands on the **newest** live order (PENDING_CONFIRMATION…OUT_FOR_DELIVERY):
   `extract_proof` (AI) → `check_proof` (code) → a new row with its own flags, notes,
   OCR data, reference and photo → the bot gets a bracketed instruction.

**Expected amount** (`_expected_for_new_receipt`):
- First receipt, or every earlier one is red/discarded: `_expectation`. A transfer was
  agreed (TRANSFER, or MIXED with a transfer part) → that amount. Otherwise → the order
  total, and a clean receipt infers the method: equal → TRANSFER, less → MIXED with the
  rest in cash, more → flag.
- Later receipt: `transfer_due` (the "Faltan $X" the bot asked for); with nothing left to
  transfer on a MIXED order with cash still owed → `cash_due`; nothing owed → red "No se
  esperaba otro pago."

**Checks** (each failure adds a Spanish flag; any flag → `FLAGGED`):
1. **Amount** via `read_ocr_amount`, the one place AI output becomes a number (`47300`,
   `"47.300"`, `"$ 47.300 COP"`, `"47.300,00"`). Unreadable → flag, never a guess.
2. **Date.** `date_iso` within *order created − skew* … *message received + skew*
   (`proof_clock_skew_minutes`, default 2). No readable date → flag. In range but older than
   `proof_notice_minutes` (default 5) → grey note, not a flag.
3. **Destination.** Exact digits, or a masked number matched on visible prefix **and** last
   four, never the last four alone. Only when no number is shown is the receiving holder's
   name compared (folded text, ≥2 words in common, or the one word of a one-word
   holder). The name never rescues a wrong number. Nothing shown → flag.
4. **Another receipt is red** → grey note "Llegó mientras otro comprobante está en revisión".
5. **Reference** (unique index). On a repeat: same order and same readable amount =
   **resend** (no row, no photo; the bot says it already had it). Anything else → red row
   "Este comprobante ya fue usado antes (referencia X)", stored without the reference.

**What the bot is told:** red → there is a problem, a person will review it, never "paid".
Clean → "recibimos su comprobante por $X", a person will review it, how the payment ended
up when it was inferred ("por transferencia por el total" / "en parte y parte", cash still
due), and never ask again how they will pay.

## 4. People only: confirm, discard, cash

- **Confirmar** (`confirm_receipt`): the amount actually received (prefilled with the OCR
  amount); 0 is refused ("use Descartar"). On UNSPECIFIED/CASH it sets TRANSFER or MIXED
  from the confirmed total; it never rewrites an agreed TRANSFER/MIXED split. A red row's
  flags stay as a grey "✓ Revisado" line. The customer gets a code-built message
  (`main._payment_confirm_message`): "Confirmamos su transferencia de $X." plus "El otro
  comprobante… sigue en revisión" / the balance still due / "…envió $Y de más; el local se
  va a comunicar…", or "Confirmamos su pago. ¡Gracias!".
- **Descartar** (`discard_receipt`): reason required (400 without it). The row turns grey,
  counts 0, is ignored by status, review and reports, and keeps its reference. The customer
  gets one model-written message from the reason; the fixed template is sent instead if the
  model fails or writes "confirmad", "cocina", "$" or the order code.
- **Registrar efectivo recibido** (`register_cash`): adds to `cash_received`, never above
  `outstanding` («El monto es mayor de lo que falta ($X). Registre solo lo que falta; el
  cambio lo maneja el local.»). The dialog is prefilled with `cash_due` on CASH/MIXED and
  warns "Hay comprobantes sin confirmar por $X…" when clean receipts wait.

## 5. The numbers (`current()`, the only money math)

| Field | Meaning |
|---|---|
| `sent` | What receipts count for: confirmed at `confirmed_amount`, clean waiting ones at OCR amount, red/discarded 0 |
| `transfer_due` | `max(transfer_amount − sent, 0)` |
| `cash_due` | `max(total − sent − cash_received − transfer_due, 0)`: cash still to collect |
| `paid` | Confirmed receipts + `cash_received`, capped at the total |
| `outstanding` | `max(total − paid, 0)` |
| `overpayment_amount` | `max(sent + cash_received − total, 0)`: money owed to the customer; a person resolves it, no automatic refund |
| `unconfirmed_amount` | Clean receipts nobody confirmed yet |
| `status`, `under_review` | Derived order-level status; true while any receipt is red |

## 6. When the order total changes (`follow_total`)

Called inside `orders.amend`'s transaction. With no receipt that counts: CASH/TRANSFER
follow the new total; MIXED keeps its transfer (the rest goes to cash) or, if the transfer
now covers everything, resets to UNSPECIFIED and the bot asks again. With a receipt that
counts: TRANSFER moves to the new total, MIXED keeps its agreed transfer. The bot's line
(`brain._payment_line`) says what is missing (with the accounts), the cash due, or the
surplus ("Ya envió $X" / "Ya pagó $X en efectivo" / "Entre transferencias y efectivo ya
pagó $X", "$Y más que el nuevo total", no refund promised). A red receipt sets
`under_review`: the bot never asks for that money again.

## 7. What the bot knows (ESTADO REAL)

`brain._payment_state` adds one line for the newest live order, in words with no amounts:
method, receipts by status (received, confirmed, with a problem, discarded), whether a
transfer or cash is still owed, and "ya está pagado completo" / "todavía falta que una
persona confirme el pago" / "todavía falta por pagar". `PAYMENT_RULES` tell the model to
trust it over the chat (built after the manual runs P-2465, P-0458 and P-6455).

Known defects in this area: `CURRENT-STATE.md` §3.
