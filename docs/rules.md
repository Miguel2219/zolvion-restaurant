# Rules

What the code must never break, the conventions it follows, and what the old
references in its comments mean.

---

## 1. Non-negotiable rules

1. **The AI never produces a number the customer sees.** It calls tools with menu ids
   and quantities. Code looks up prices, multiplies and writes the text with the totals.
   The only place AI output becomes a number is `payments.read_ocr_amount`; an
   unreadable amount is a flag, never a guess.
2. **The bot never confirms a payment.** Only
   `POST /api/orders/{id}/receipts/{rid}/confirm` (a person pressing Confirmar on a
   receipt) sets `CONFIRMED`. The bot may say the restaurant confirmed a receipt only
   when ESTADO REAL shows one confirmed. A receipt never blocks an order's transitions.
3. **Customer messages never contain the order code (`P-1234`) or the word "cocina".**
   The system prompt forbids it, tool results repeat "No le menciones ningún código",
   tests check every fixed notification, and every bot reply is cleaned in code
   (`main._clean_for_customer`: codes removed, "cocina" said as "el local"). The discard
   message keeps its own template guard.
4. **Uniqueness lives in the database, not in Python.** A repeated WhatsApp message id
   and a reused receipt reference are rejected by constraints; code decides resend vs.
   reuse only after the `IntegrityError` (`db.IntegrityError`). Outside a transaction a
   failed `db.run()` leaves nothing behind (autocommit). `db.transaction()` groups writes
   that must land together; code inside it must not catch a failed write and carry on:
   PostgreSQL has aborted the transaction, and every later statement in it fails.
5. **Money is `int` pesos.** Never a float. `money.pesos()` refuses floats and bools;
   `money.fmt()` prints `$47.300`. `payments.current()` is the only place that computes
   paid, owed and surplus.
6. **An order's total always equals the sum of its lines plus the delivery fee**, as
   computed by `cart.summary()`, the only function that produces a total.
   `orders.create_from_cart` and `orders.amend` check it.
7. **The AI never invents products.** Unknown ids are refused by code with a message
   telling the model to offer something real.
8. **Every order move goes through `orders.TRANSITIONS`** and is logged in
   `order_transitions`. That table is never updated or deleted from (except the reset).
9. **Staff reject/cancel, receipt discards and cash discards need a reason.** The customer
   receives it for the first three; a cash discard tells nobody and the reason stays on the row.
10. **Webhook signature is checked on the raw body before anything else** (when
    `WHATSAPP_APP_SECRET` is set).
11. **The system decides, not the model.** Whether a change edits the order or goes to a
    person is decided in code by the order's column (`_edit`, the `request_handoff`
    topic gate). The order's and payment's state reach the model from ESTADO REAL on
    every call, never from its own earlier messages.

## 2. Language and vocabulary

- Code, comments, identifiers, documentation: **English**.
- Everything a customer or staff member reads (bot replies, tool-result text, panel
  labels, API error messages): **Colombian Spanish**, customer addressed as *usted*.
- "Aceptado por el local" is the only phrase for ACCEPTED and later states. A CONFIRMED
  order was confirmed by the customer: never "recibido" or "enviado al local".

## 3. Conventions

### Adding or changing a bot tool

1. Add the schema to `brain.TOOLS` and a branch in `brain._run_tool`.
2. The handler returns **Spanish text for the model**, often with a bracketed
   instruction, e.g. `(… No le menciones ningún código.)`.
3. Failures return `NO SE PUDO: …` instead of raising.
4. Anything that changes what was ordered goes through `brain._edit`, which applies the
   modification window (`ORDER-LIFECYCLE.md` §3), never around it.

### Messages after a system event

`main.handle`, `main.notify` and the receipt routes give the model a bracketed Spanish
instruction saying what happened and what it may and may not say. Numbers inside those
instructions are formatted by code. A model-written message that must not fail has a
fixed template fallback.

### Database changes

- Add the column to `SCHEMA` **and** a line to `db.MIGRATIONS`, so an existing database
  upgrades on startup instead of needing deletion.
- Constraints that protect correctness go in the schema, not in Python checks.
- Never write the legacy `payments` receipt columns (`payments.status` especially): the
  receipt migration relies on it.

### Tests

- `tests/conftest.py` picks the `_test` database before `app` is imported. Read expected
  prices from `CATALOG`.
- New behaviour gets a test in `tests/test_flow.py`; a code-review defect gets one in
  `tests/test_review_bugs.py`, named after it (`test_r7_…`). The suite must stay green.

### Chat history

On any error during a turn the history is restored to its checkpoint. Trimming never
separates a `tool_use` from its `tool_result`. Keep both when touching `brain.reply`.

## 4. Old references in the code (glossary)

Comments cite documents from the separate `zolvion-restaurant` project and labels from
task plans. Those documents are **not** part of this project. What each means here:

| Reference in comments | Meaning in this code |
|---|---|
| `R3` (`db.py`) | Duplicate protection lives in the database (unique WhatsApp message id) |
| `R9` (`wa.py`) | A record of every outbound message is written first (`outbound_messages`). The retries that rule also asks for are not built |
| `R1` (`payments.py`), `C2 R2a` (`brain.py`); `test_r1_…` to `test_r12_…` | Not the old design's rules: defects R1–R12 of the 1 Oct code review, `CURRENT-STATE.md` §3 |
| Gap 1 … Gap 7 (`payments.py`, `brain.py`) | Miguel's 7 Oct decisions for C2 Part 2: 1 the method after discarding cash, 2 a discarded receipt sent again, 3 the sentence copied verbatim, 4 what it says, 5 fresh answers to payment questions, 6 discarded cash in ESTADO REAL, 7 "Cómo pagaron" (`payments.md`) |
| `Invariant 6` | Rule 6 above: total = lines + fee, computed by code |
| `order-modification.md` / "window 1/2/3" | The modification windows, `ORDER-LIFECYCLE.md` §3 |
| `notifications-and-followups.md §1` | Who gets told what, `ORDER-LIFECYCLE.md` §5 |
| `payments.md` / `payments.md §3` in code | The old design's payment doc and reused-reference rule. This repo's `docs/payments.md` describes the MVP |
| Q1 / Q2 / Q3 / D3 | Task C plan decisions: Q1 expectation after only red receipts, Q2 grey note while another is red, Q3 transferring the cash part / nothing owed, D3 resend vs. reuse of a reference |
| P-0458, P-0644, P-3564, P-4766, P-6455, P-9950 | Orders from Miguel's manual runs whose bugs a fix or test refers to (`CURRENT-STATE.md` §3, Notion checklists). P-4438 also serves as a sample code in the R9 cleaning tests |
| "Task A / A' / B / C / C2 / D" | Lettered work packages, `CURRENT-STATE.md` §2 |

Where the MVP and the old design differ, the MVP wins. Examples: the MVP edits an
unaccepted order in place (old design: cancel and recreate); the MVP has no
`IN_PREPARATION` state; the MVP models cash/transfer/mixed payments and one row per
receipt.
