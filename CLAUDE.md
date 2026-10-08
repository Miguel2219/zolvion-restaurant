# CLAUDE.md

Guidance for Claude Code in this repository. Details live in `docs/` (start at
`docs/README_1.md`); this file keeps what you need before touching code.

## What this is

The MVP of Zolvion Restaurant: WhatsApp ordering for restaurants, which Miguel demos and
sells. First prospect: Caseritas Burger, Bucaramanga. One FastAPI process, SQLite, Claude
with tool use, three static HTML pages. The full target architecture (queue, worker,
Postgres, Terraform) is the sibling repo `zolvion-restaurant`: never copy code from here
into it, and don't bring its rules, states or architecture here unless Miguel asks.
(`app/main.py`'s docstring still says "a convincing six-minute demo"; that is outdated.)

Comments cite design docs (`order-modification.md`, `notifications-and-followups.md`,
`payments.md`, "R3"/"R9", "Invariant 6") and Task C decision labels (Q1–Q3, D3). The
design docs live in `zolvion-restaurant`; don't search for them. `docs/rules.md` §4 says
what each label means here. `docs/payments.md` is this repo's own payment doc.

Code, comments, identifiers and docs are English. Everything a customer or staff member
reads (bot replies, tool-result text, panel labels, API error strings) is Colombian
Spanish, addressing the customer as *usted*. Every `.md` file stays under 200 lines.

## Commands

Windows/PowerShell. The virtualenv is `venv/` (ignored by git, like `.venv/`).
No linter or formatter is configured.

```powershell
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
python -m pytest tests -q                                   # 306 pass, 1 xfail (G16)
python -m pytest tests -q -k wrong_amount                   # by name
python -m pytest tests/test_flow.py::test_happy_path_pickup # one test
```

Pages: `/simulator` (fake WhatsApp, you type as the customer), `/kitchen` (staff board,
also `/`), `/reports`. `/kitchen?alert=20` fires the "unaccepted order" alert after 20 s
instead of 600.

- `POST /api/reset` ("Reiniciar demo") wipes **every** table, photos included, and the
  in-memory chat history.
- `POST /api/reload-config` only rebinds `brain.CATALOG`; `cart`, `payments` and
  `reports` keep their import-time `CATALOG`. Restart uvicorn to change prices.
- `.env` is read once, at import, by `app/__init__.py`. Empty `WHATSAPP_TOKEN` = FAKE
  mode: replies only go to `outbound_messages`, which the simulator reads.
  `RESTAURANT_CONFIG` and `DEMO_DB` override the config and database paths.
- Manual tests: `TEST.md` (regression script) and the Notion "Task X Plan" checklists.
  `run.md` is setup plus the demo-day script.

## Architecture

```
POST /webhook (Meta, HMAC-checked) ─┐
                                    ├─> main.handle(phone, text, image)
POST /sim/message (browser)        ─┘         │
   text, or image with no active order ───────┼─> brain.reply() ─ Claude + tools ─> wa.send_text()
   image while an order is active ────────────┴─> brain.extract_proof()   (OCR only, no verdict)
                                                   → payments.check_proof() (code gives the verdict)
                                                   → brain.reply() with a bracketed instruction
```

| Module (dependency order) | Owns |
|---|---|
| `__init__.py` | loads `.env` before any module reads `os.environ` |
| `money.py` | `pesos()`, `total_of()`, `fmt()`; money is `int` COP, never float |
| `catalog.py` | `CATALOG` from `config/restaurant.json`: prices, modifiers, zones, accounts, `owns_account`, `timezone()` |
| `db.py` | schema (WAL), thread-local connection, `run()/q()/q1()`, `transaction()`, migrations in `init()`; `DB_PATH` fixed at import |
| `cart.py` | cart mutations; identical lines merge; `LineChoiceNeeded`; `summary()` is the only producer of a total |
| `orders.py` | `TRANSITIONS`, modification windows, `amend()`, `board()`, `live_states()` |
| `payments.py` | `set_method`, `check_proof`, `confirm_receipt`/`discard_receipt`/`register_cash`/`discard_cash` (human only), `follow_total`, `current()` (the only money math) |
| `handoff.py` | `request`/`resolve`/`open_for`: the amber "Necesita a una persona" box |
| `wa.py` | WhatsApp Cloud API, FAKE mode, signature check, `parse_webhook` |
| `brain.py` | `SYSTEM`, `TOOLS`, `_run_tool`, per-phone history, ESTADO REAL block, payment sentence and `_finish_turn`, `extract_proof` |
| `reports.py` | SQL aggregates, payments via `current()`; "today" = calendar day in `America/Bogota` |
| `main.py` | routes, `handle()`, `notify()`, `_stop_message`, `_discard_message`; `db.init()` at import |

`static/` pages poll the JSON API (kitchen 2 s, simulator 1.5 s, reports 4 s). Kitchen
buttons send event names from `orders.TRANSITIONS`. Routes and tables: `docs/ARCHITECTURE.md`.

## Rules the code is built around (`docs/rules.md`)

1. **The model never produces a number that reaches the customer.** It calls tools with
   item ids; code prices, multiplies and renders. The one place model output becomes a
   number is `payments.read_ocr_amount`; an unreadable amount is a flag, never a guess.
2. **The bot never confirms a payment.** Only `POST /api/orders/{id}/receipts/{rid}/confirm`
   (a person in the panel) sets `CONFIRMED`. A receipt never blocks an order transition.
3. **Customer messages never contain the order code (`P-1234`) or the word "cocina".**
   Tests assert it on every notification; `SYSTEM` forbids it; every bot reply is cleaned in
   code (`main._clean_for_customer`: codes removed, "cocina" → "el local"); discard keeps its
   own template guard.
4. **Uniqueness lives in the database.** `inbound_messages.wa_message_id` is the PK;
   `payment_receipts.reference` has a unique partial index (a discarded receipt keeps its
   reference). Code tells a resend from a reuse only after the `IntegrityError`.
   `db.run()` rolls back on failure; `db.transaction()` groups writes that land together
   (`orders.amend`, `_store_receipt`, `register_cash`, `discard_cash`, `confirm_receipt`). Never call
   `check_proof` inside a transaction: it catches the `IntegrityError`.
5. **Vocabulary:** "aceptado por el local" is the only phrase for `ACCEPTED` and later. A
   `CONFIRMED` order is confirmed by the customer, never "recibido" or "enviado al local".

## Orders (`docs/ORDER-LIFECYCLE.md`)

`PENDING_CONFIRMATION → CONFIRMED → ACCEPTED → READY → COMPLETED` (pickup via `hand_over`;
delivery via `OUT_FOR_DELIVERY` then `delivered`). Terminal: `COMPLETED`, `CANCELLED`,
`REJECTED`, `EXPIRED`. Any `(state, event)` not in `TRANSITIONS` raises `IllegalTransition`
(409). `staff_rejects`/`staff_cancels` need a reason (400). There is no `IN_PREPARATION`
(Task B removed it; `db.init()` moves old rows to `ACCEPTED`). Nothing fires `expire`.

Modification windows (`orders.modification_window`, enforced by `brain._edit` before any
item or delivery change):
- **1** an OPEN cart exists: it always wins.
- **2** latest order `PENDING_CONFIRMATION`/`CONFIRMED`: `orders.amend` edits IN PLACE (same
  row, code, card) inside one `db.transaction()`, `payments.follow_total` included.
- **3** latest order `ACCEPTED..OUT_FOR_DELIVERY`: locked; `_handoff` opens the amber box with a
  code-written summary (keeps `handoff.request`'s dedupe working). `start_separate_order`
  opens a cart with `related_order_id` (informational only).

`request_handoff` is gated in code by `topic`: `ORDER_CHANGE`, `CANCEL`, `PAYMENT_METHOD`,
`ORDER_INFO` answer `NO SE PUDO` naming the tool to use; a missing topic or blank `summary`
is its own `NO SE PUDO`. Only `OTHER` opens a box, on `orders.latest_for(phone, ON_BOARD)`
(the kitchen columns). No card → `NO SE PUDO`: the bot promises nothing and no box appears.
A customer who asks for a person without a reason is asked the reason once (`SYSTEM`).

Notifications (`main.notify`): `ACCEPTED`, `READY` (pickup only) and `OUT_FOR_DELIVERY`
send fixed `NOTIFY` texts. `REJECTED` and staff `CANCELLED` send a model-written message
from the staff's reason (`_stop_message`, template fallback). `COMPLETED` and customer
`CANCELLED` (`orders.last_actor`) send nothing.

## Payments (`docs/payments.md`)

- `payments` row per order: method (`UNSPECIFIED`, `CASH`, `TRANSFER`, `MIXED`), agreed split
  (`transfer_amount`, `cash_amount`; `MIXED` validated by code), `method_from_cash`. Its
  `cash_received` and old receipt columns are legacy: never written (`db.init()` copied the
  receipt ones once into `payment_receipts`, which keeps that migration idempotent).
- **One `payment_receipts` row per image** (photo in `receipt_photos`): `PROOF_RECEIVED`,
  `FLAGGED`, `CONFIRMED`, `DISCARDED` (last two final); order status = worst receipt,
  discarded ignored, none = `PENDING`. **One `cash_payments` row per cash registration**
  (`RECEIVED`/`DISCARDED`). The card's "Pagos" block lists both, oldest first; a discarded row
  leaves the card and counts nowhere (ESTADO REAL still names it).
- Expected amount and checks (`_expected_for_new_receipt`, `_expectation`): `docs/payments.md`
  §3. Registered cash comes off first; all paid → red "No se esperaba otro pago."
- Repeated reference: same order and amount = resend (no row; a discarded one returns
  `resent_discard_reason` and the bot gives the reason again); anything else is a red row
  "Este comprobante ya fue usado antes (referencia X)", stored without the reference.
- Human only: `confirm_receipt` (refuses 0; sets the method only on `UNSPECIFIED`/`CASH`);
  `discard_receipt(reason)` (one guarded `_discard_message`); `register_cash` (≤ `outstanding`;
  `UNSPECIFIED` → `CASH` with `method_from_cash`); `discard_cash(reason)` (nobody is told; the
  last cash row takes back a method only cash set). Any other method write clears the flag.
- `current()`: `cash` rows, `cash_received`, `sent`, `transfer_due`, `cash_due`, `to_pay` (their
  sum), `paid` (capped), `outstanding`, `confirmed`, `received` (uncapped, Reportes),
  `overpayment_amount`, `confirmed_overpayment`, `unconfirmed_amount`, `status`, `under_review`.
- `choose_payment` goes to a person once a receipt arrived, cash was registered or the order is
  out for delivery; repeating the current method answers "no cambió nada".

## Conversation state (`brain.py`)

- History is in memory, per phone, last `HISTORY_TURNS` (24), trimmed without splitting a
  `tool_use`/`tool_result` pair, restored to its checkpoint on any exception. Restarting
  uvicorn forgets conversations; orders survive in SQLite.
- One global lock serializes turns for all phones (`extract_proof` runs outside it). Every
  route that writes is `async` with no `await` around database work, so writes run one at a
  time; `orders.amend` still keeps a guarded `UPDATE … WHERE state IN (…)`.
- Only the last round's text reaches the customer. If the model still wants tools after
  `MAX_TOOL_ROUNDS` (8), one last call with `tool_choice` none answers; only its text is sent.
- `system_prompt(phone)` appends `ESTADO REAL AHORA` on every model call: the live orders'
  states plus one "Pago del pedido más reciente" line (`_payment_state`, no amounts) and
  `PAYMENT_RULES`. It wins over the chat history. No codes or totals in it.
- **Payment sentence** (`payment_sentence`, from `current()`): edits, `choose_payment`,
  `order_status` and the clean-receipt instruction hand it out to be copied verbatim. Payment
  words in the customer's text force `order_status` first. `_finish_turn` checks every reply
  that speaks about payment: a missing sentence is appended; a contradiction, a stale sentence
  or an amount that doesn't match its clause's meaning gets one more answer, then a code-built
  reply (`_Turn.actions` + sentence). Delivered/cancelled or "el pedido anterior" →
  «No pude verificar el pago de ese pedido.» Details: `docs/payments.md` §7.
- Each tool call prints `[bot ...1234] <tool> <args> -> <result>` (cut at 160 chars), the
  only record of what the model called.
- `ANTHROPIC_MODEL` empty → first id from `models.list()` matching sonnet, opus, haiku.

## Adding or changing a tool

Add the schema to `TOOLS` and a branch in `_run_tool`. Handlers return Spanish text for the
model, often a bracketed instruction (`(… No le menciones ningún código.)`), and report
failures as `NO SE PUDO: …` instead of raising. Anything that changes what was ordered goes
through `_edit` first. A completed action records its code-built confirmation with `_action`;
a change to the payment hands out the sentence with `_hand_out_sentence`.

## Tests (`tests/test_flow.py`; code-review defects R1–R12 in `tests/test_review_bugs.py`)

- Set `DEMO_DB` **before** importing `app`; `db.DB_PATH` is resolved at import.
- Expected numbers come from `config/restaurant.json`; changing a price moves tests on purpose.
- No model calls. `notify()` runs in FAKE mode and is read back from `outbound_messages`. An
  autouse fixture resets every table between tests.

## Config is the product

`config/restaurant.json` holds menu, prices, modifiers, zones, hours, payment accounts and the
bot persona. A new client is a new file, not a code change (`docs/CONFIG.md`). Burger prices
are unverified; zones, hours and default fee are invented test values.
