# CLAUDE.md

Guidance for Claude Code in this repository. Details live in `docs/` (start at
`docs/README_1.md`); this file keeps what you need before touching code.

## What this is

The MVP of Zolvion Restaurant: WhatsApp ordering for restaurants, which Miguel demos and
sells; launch city Ocaña (Norte de Santander). Caseritas Burger is a test restaurant, not a
prospect. One FastAPI process with one worker thread, PostgreSQL, Claude with tool use, two
static HTML pages. Customers write on WhatsApp through Chatwoot, where Zolvion is the Agent Bot.
Target architecture (queue, worker, Terraform): sibling repo `zolvion-restaurant`. Never
copy code into it; don't bring its rules, states or architecture here unless Miguel asks.

Comments cite design docs (`order-modification.md`, `notifications-and-followups.md`,
`payments.md`, "R3"/"R9", "Invariant 6") and Task C decision labels (Q1–Q3, D3). The
design docs live in `zolvion-restaurant`; don't search for them. `docs/rules.md` §4 says
what each label means here. `docs/payments.md` is this repo's own payment doc.

Code, comments, identifiers and docs are English. Everything a customer or staff member
reads (bot replies, tool-result text, panel labels, API error strings) is Colombian
Spanish, addressing the customer as *usted*. Every `.md` file stays under 200 lines.

## Commands

Windows/PowerShell. The virtualenv is `venv/` (ignored by git, like `.venv/`). No linter.
PostgreSQL 17 runs as the Windows service `postgresql-x64-17` (setup: `docs/BUILD-AND-RUN.md`).

```powershell
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
python -m pytest tests -q                                   # 356 pass, 1 xfail (G16)
python -m pytest tests -q -k wrong_amount                   # by name
python -m pytest tests/test_flow.py::test_happy_path_pickup # one test
```

Pages: `/kitchen` (staff board, also `/`), `/reports`; no simulator since Task D: test with a
phone (`docs/CHATWOOT-SETUP.md`). `/kitchen?alert=20` fires the unaccepted-order alert at 20 s.

- `POST /api/reset` ("Reiniciar demo") wipes **every** table, photos included, and the chat
  history. `POST /api/reload-config` only rebinds `brain.CATALOG` (`cart`, `payments`, `reports`
  keep theirs): restart uvicorn to change prices.
- `.env` is read once, at import, by `app/__init__.py`. All four `CHATWOOT_*` = Chatwoot mode
  (Meta's `/webhook` answers 404); none = Meta directly, or FAKE mode with `WHATSAPP_TOKEN`
  empty (replies only go to `outbound_messages`: the tests); some = every webhook refused.
  `MESSAGE_WAIT_SECONDS` (4), `RESTAURANT_CONFIG` and `DATABASE_URL` are optional.
- Manual tests: `TEST.md` and the Notion "Task X Plan" checklists; `run.md`: setup + demo day.

## Architecture

```
POST /chatwoot/webhook (Agent Bot, signed) ─┬─> inbound_messages RECEIVED, 200 at once
POST /webhook (Meta; only without Chatwoot) ┘   (status events -> conversations.chatwoot_status)
worker.py thread, every 0.5 s: one phone whose newest message is MESSAGE_WAIT_SECONDS old
   a person has the conversation ──> HUMAN (a photo is still checked: private note to staff)
   consecutive texts, one turn ────> main.handle ─> brain.reply() ─ Claude + tools
   each photo, order active ───────> brain.extract_proof() (OCR only) → payments.check_proof()
                                     (code gives the verdict) → brain.reply() with an instruction
   status read again: still Pending → wa.send_text() (Chatwoot | Meta | FAKE), else SUPPRESSED
```

| Module (dependency order) | Owns |
|---|---|
| `__init__.py` | loads `.env` before any module reads `os.environ` |
| `money.py` | `pesos()`, `total_of()`, `fmt()`; money is `int` COP, never float |
| `catalog.py` | `CATALOG` from `config/restaurant.json`: prices, modifiers, zones, accounts, `owns_account`, `timezone()` |
| `db.py` | PostgreSQL schema (`init()`: `SCHEMA` + idempotent `UPGRADE`), one autocommit psycopg connection per thread, `run()/q()/q1()/insert()`, `transaction()`, `write_lock`, `IntegrityError`; `DATABASE_URL` fixed at import |
| `chatwoot.py` | settings, `verify`, `classify`, `parse_customer`, `remember`/`bot_holds` (who has each phone), `send`/`set_status`, `download` |
| `cart.py` | cart mutations; identical lines merge; `LineChoiceNeeded`; `summary()` is the only producer of a total |
| `orders.py` | `TRANSITIONS`, modification windows, `amend()`, `board()`, `live_states()` |
| `payments.py` | `set_method`, `check_proof`, `confirm_receipt`/`discard_receipt`/`register_cash`/`discard_cash` (human only), `follow_total`, `current()` (the only money math) |
| `handoff.py` | `request`/`resolve`/`close_for_phone`/`open_for`: the amber "Necesita a una persona" box |
| `wa.py` | `send_text` (the single exit: Chatwoot, Meta or FAKE), `record`, Meta signature, `parse_webhook` |
| `brain.py` | `SYSTEM`, `TOOLS`, `_run_tool`, per-phone history, ESTADO REAL block, payment sentence and `_finish_turn`, `extract_proof` |
| `reports.py` | SQL aggregates, payments via `current()`; "today" = calendar day in `America/Bogota` |
| `worker.py` | the thread that answers: `run_once` (grouping, who answers), `recover`, `sync_chatwoot`, catch-up note |
| `main.py` | routes, `handle()`, `receive_receipt()`, `notify()`, `_stop_message`, `_discard_message`; `db.init()` at import |

`static/` pages poll the JSON API (kitchen 2 s, reports 4 s). Kitchen
buttons send event names from `orders.TRANSITIONS`. Routes and tables: `docs/ARCHITECTURE.md`.

## Rules the code is built around (`docs/rules.md`)

1. **The model never produces a number that reaches the customer.** It calls tools with
   item ids; code prices, multiplies and renders. The one place model output becomes a
   number is `payments.read_ocr_amount`; an unreadable amount is a flag, never a guess.
2. **The bot never confirms a payment.** Only `POST /api/orders/{id}/receipts/{rid}/confirm`
   (a person in the panel) sets `CONFIRMED`. A receipt never blocks an order transition.
3. **Customer messages never contain the order code (`P-1234`) or the word "cocina".** Tests
   assert it; `SYSTEM` forbids it; every bot reply is cleaned in code (`main._clean_for_customer`:
   codes removed, "cocina" → "el local"); discard keeps its own template guard.
4. **Uniqueness lives in the database.** `inbound_messages.wa_message_id` is the PK;
   `payment_receipts.reference` (a discarded receipt keeps it) and `outbound_messages.
   chatwoot_message_id` have unique partial indexes; code reacts after the `IntegrityError`.
   A failed `db.run()` leaves nothing; `db.transaction()` groups writes that land together
   (`orders.amend`, `_store_receipt`, `register_cash`, `discard_cash`, `confirm_receipt`). Never call
   `check_proof` inside a transaction: it catches the `IntegrityError`.
5. **Vocabulary:** "aceptado por el local" is the only phrase for `ACCEPTED` and later. A
   `CONFIRMED` order is confirmed by the customer, never "recibido" or "enviado al local".

## Chatwoot (Task D Part 2, `docs/CHATWOOT-SETUP.md`)

- The bot answers only while Chatwoot says Pending (the status, not the assignee). A person's
  reply or a Coexistence echo on Pending is a takeover (`open` now; Chatwoot told via
  `push_status`). `remember` ignores older events and conversation ids. Notifications always go.
- Handoff box: `OPENING`, then (after the bot's reply) private note + `open` = `OPEN`, retried
  every 30 s («Chatwoot sin actualizar»). "Ya lo atendí" sets Pending first (502 on failure);
  Pending/resolved in Chatwoot closes it. A `webp` image is a sticker, never a receipt.

## Orders (`docs/ORDER-LIFECYCLE.md`)

`PENDING_CONFIRMATION → CONFIRMED → ACCEPTED → READY → COMPLETED` (pickup via `hand_over`;
delivery via `OUT_FOR_DELIVERY` then `delivered`). Terminal: `COMPLETED`, `CANCELLED`, `REJECTED`,
`EXPIRED`. Any `(state, event)` not in `TRANSITIONS` raises `IllegalTransition` (409).
`staff_rejects`/`staff_cancels` need a reason (400). No `IN_PREPARATION`; nothing fires `expire`.

Modification windows (`orders.modification_window`, enforced by `brain._edit` before a change):
- **1** an OPEN cart exists: it always wins.
- **2** latest order `PENDING_CONFIRMATION`/`CONFIRMED`: `orders.amend` edits IN PLACE (same
  row, code, card) inside one `db.transaction()`, `payments.follow_total` included.
- **3** latest order `ACCEPTED..OUT_FOR_DELIVERY`: locked; `_handoff` opens the amber box with a
  code-written summary. `start_separate_order` opens a cart with `related_order_id` (informational).

`request_handoff` is gated in code by `topic`: `ORDER_CHANGE`, `CANCEL`, `PAYMENT_METHOD`,
`ORDER_INFO` answer `NO SE PUDO` naming the tool to use, as do a missing topic or `summary`.
Only `OTHER` opens a box, on `orders.latest_for(phone, ON_BOARD)`; no card → `NO SE PUDO`, no
promise. A customer who asks for a person without a reason is asked the reason once (`SYSTEM`).

Notifications (`main.notify`): `ACCEPTED`, `READY` (pickup only) and `OUT_FOR_DELIVERY`
send fixed `NOTIFY` texts. `REJECTED` and staff `CANCELLED` send a model-written message
from the staff's reason (`_stop_message`, template fallback). `COMPLETED` and customer
`CANCELLED` (`orders.last_actor`) send nothing.

## Payments (`docs/payments.md`)

- `payments` row per order: method (`UNSPECIFIED`, `CASH`, `TRANSFER`, `MIXED`), agreed split
  (`transfer_amount`, `cash_amount`; `MIXED` validated by code), `method_from_cash`.
- **One `payment_receipts` row per image** (photo in `receipt_photos`): `PROOF_RECEIVED`, `FLAGGED`,
  `CONFIRMED`, `DISCARDED` (last two final); order status = worst receipt, discarded ignored, none =
  `PENDING`. **One `cash_payments` row per registration** (`RECEIVED`/`DISCARDED`). "Pagos" lists
  both, oldest first; a discarded row leaves the card and counts nowhere (ESTADO REAL names it).
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

- History is in memory, per phone, last `HISTORY_TURNS` (24), never splitting a `tool_use`/
  `tool_result` pair, restored on any exception. A restart forgets it; orders stay in PostgreSQL.
- One global lock serializes turns (`extract_proof` runs outside it). `db.write_lock` is held
  by each tool call, receipt store, webhook and panel write, never around a model or Chatwoot
  call; panel routes send in a thread. `orders.amend` keeps its guarded `UPDATE … WHERE state`.
- Only the last round's text reaches the customer. If the model still wants tools after
  `MAX_TOOL_ROUNDS` (8), one last call with `tool_choice` none answers; only its text is sent.
- `system_prompt(phone)` appends `ESTADO REAL AHORA` on every model call: the live orders'
  states plus one "Pago del pedido más reciente" line (`_payment_state`, no amounts) and
  `PAYMENT_RULES`. It wins over the chat history. No codes or totals in it.
- **Payment sentence** (`payment_sentence`, from `current()`): edits, `choose_payment`, `order_status`
  and the clean-receipt instruction hand it out to copy verbatim; payment words force `order_status`
  first. `_finish_turn` checks every payment reply: a missing sentence is appended; a contradiction,
  stale sentence or mismatched amount gets one more answer, then a code-built reply. Delivered or
  "el pedido anterior" → «No pude verificar el pago de ese pedido.» (`docs/payments.md` §7)
- Each tool call prints `[bot ...1234] <tool> <args> -> <result>` (160 chars): the only record.
- `ANTHROPIC_MODEL` (chat; empty → first `models.list()` id with haiku, sonnet, opus) must accept a
  forced `tool_choice` (Sonnet/Opus 5.5 400s). `ANTHROPIC_PROOF_MODEL` (receipts) empty → the chat model.

## Adding or changing a tool

Add the schema to `TOOLS` and a branch in `_run_tool_locked`. Handlers return Spanish text for
the model, often a bracketed instruction (`(… No le menciones ningún código.)`), and report
failures as `NO SE PUDO: …` instead of raising. A change to what was ordered goes through `_edit`;
a done action records its confirmation with `_action`; a payment change uses `_hand_out_sentence`.

## Tests (`test_flow.py`; R1–R12 in `test_review_bugs.py`; Chatwoot in `test_chatwoot.py`)

- `tests/conftest.py` points `DATABASE_URL` at `<name>_test` on the same server before `app`
  is imported (it is fixed at import) and creates that database. PostgreSQL must be running.
- Expected numbers come from `config/restaurant.json`; changing a price moves tests on purpose.
- No model calls. `notify()` runs in FAKE mode, read back from `outbound_messages`; tables reset
  between tests. conftest blanks `CHATWOOT_*`; `test_chatwoot.py` fakes `chatwoot._request` and
  replays `tests/fixtures/chatwoot/` (sanitized step 0 recordings) through `worker.run_once()`.

## Config is the product

`config/restaurant.json` holds menu, prices, modifiers, zones, hours, payment accounts and the
bot persona. A new client is a new file, not a code change (`docs/CONFIG.md`). Burger prices
are unverified; zones, hours and default fee are invented test values.
