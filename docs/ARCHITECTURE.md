# Architecture

One Python process (FastAPI + uvicorn), one SQLite file, the Anthropic API for
understanding messages and reading receipts, and the WhatsApp Cloud API for sending
and receiving. Three static HTML pages poll a JSON API.

---

## 1. The whole system

```
 Customer's WhatsApp ──> Meta Cloud API ──> POST /webhook ─┐
                                                           ├─> main.handle(phone, text, image)
 Browser simulator ─────────────────────> POST /sim/message┘            │
                                                                        ▼
                                    text, or image with no active order ─> brain.reply()
                                    image while an order is active ─────> brain.extract_proof()  (AI reads)
                                                                          payments.check_proof()  (code decides)
                                                                          brain.reply() with an instruction
                                                                        │
                        wa.send_text() ── live: Meta API ── always: row in outbound_messages
                                                                        │
 Staff panel /kitchen  ── polls GET /api/board every 2 s ───────────────┤
 Reports /reports      ── polls GET /api/report every 4 s ──────────────┤  SQLite (demo.sqlite3, WAL)
 Simulator /simulator  ── polls GET /sim/outbound every 1.5 s ──────────┘
```

`brain.reply()` runs up to 8 rounds: send the conversation to Claude with the tool
list → run every tool it asks for → send the results back → stop when it answers
without a tool. Tools are the only way the AI can touch an order. Only the last
round's text is sent to the customer. If the model still wants tools after round 8,
one more call with tools switched off writes the answer.

What the bot cannot read (voice note, sticker, location, video, document, an image that
failed to download) reaches `handle()` as a bracketed note for the model
(`main._note_for`) instead of empty text; a reaction gets no reply and no model call.
Every reply is cleaned of order codes and the word "cocina" (`main._clean_for_customer`)
before it is sent.

## 2. Modules (`app/`, in dependency order)

| Module | Owns |
|---|---|
| `__init__.py` | Loads `.env` before any other module reads the environment |
| `money.py` | `pesos()`, `total_of()`, `fmt()`: money is `int` COP, never float |
| `catalog.py` | `CATALOG` from `config/restaurant.json`: prices, modifiers, zones, accounts, timezone, account matching |
| `db.py` | Schema, thread-local connection, `run()/q()/q1()`, `transaction()`, startup migrations |
| `cart.py` | Cart changes, identical-line merge, `LineChoiceNeeded`; `summary()` is the only producer of a total |
| `orders.py` | State machine, modification windows, `amend()`, `board()`, `live_states()` |
| `payments.py` | Methods, receipts and their checks, cash rows, confirm/discard of receipts and cash (people only), `follow_total`, `current()` |
| `handoff.py` | `request` / `resolve` / `open_for`: the amber "Necesita a una persona" box |
| `wa.py` | WhatsApp Cloud API (Graph v21.0): send, download media, signature, parse webhook, FAKE mode |
| `brain.py` | System prompt, ESTADO REAL block, `TOOLS`, `_run_tool`, chat history, the payment sentence and the end-of-turn payment check (`_finish_turn`), `extract_proof` |
| `reports.py` | SQL aggregates for the reports page; payments per order through `payments.current()` |
| `main.py` | HTTP routes, `handle()`, `_note_for()`, `notify()`, customer messages for confirm/discard, `_clean_for_customer()`; creates the database on import |

`static/`: `kitchen.html`, `simulator.html`, `reports.html`: plain HTML + JS, no build
step. Panel buttons send event names straight from `orders.TRANSITIONS`.

## 3. HTTP routes

| Route | Purpose |
|---|---|
| `GET /webhook` | Meta's verification handshake (`WHATSAPP_VERIFY_TOKEN`) |
| `POST /webhook` | Inbound WhatsApp. Signature checked on the raw body first (403); duplicates skipped by the database; reactions stored and ignored |
| `POST /sim/message` | Inbound from the simulator (text and/or base64 image) |
| `GET /sim/outbound`, `GET /sim/conversation` | Simulator reads replies / rebuilds the thread |
| `GET /api/config` | Business data, WhatsApp live or not, accounts |
| `GET /api/board` | Non-terminal orders with items, payment (receipts and cash rows included, discarded ones too), transitions, open handoffs |
| `POST /api/orders/{id}/event` | Staff button. 409 if illegal; 400 if reject/cancel has no reason |
| `POST /api/orders/{id}/handoff/resolve` | "Ya lo atendí" |
| `POST /api/orders/{id}/receipts/{rid}/confirm` | A person confirms one receipt (amount received); the customer is told |
| `POST /api/orders/{id}/receipts/{rid}/discard` | A person discards one receipt with a reason; the customer is told |
| `GET /api/receipts/{rid}/photo` | The receipt image ("Sin foto." 404 when none) |
| `POST /api/orders/{id}/payment/cash` | Staff registers cash received: one row, never above what is owed |
| `POST /api/orders/{id}/cash/{cid}/discard` | A person discards one cash row with a reason; nobody is told |
| `GET /api/report?days=N` | Reports data |
| `POST /api/reset` | Deletes every row in every table and all chat history |
| `POST /api/reload-config` | Re-reads the config, but only `brain` sees it (`CONFIG.md`) |
| `GET /`, `/kitchen`, `/reports`, `/simulator` | The three pages |

No route requires a login (`KNOWN-GAPS.md` G1).

## 4. Data model (SQLite)

| Table | What a row is |
|---|---|
| `conversations` | A customer phone seen by the system (`display_name` is never filled) |
| `inbound_messages` | One received message; `wa_message_id` is the primary key, so a repeated delivery is rejected |
| `carts`, `cart_items` | Work in progress (`OPEN`, `CONVERTED`). `related_order_id` marks a separate order opened next to a locked one |
| `orders`, `order_items` | The order: code `P-1234`, state, pickup/delivery, address, zone, subtotal, fee, total, `edited_at` |
| `order_transitions` | Append-only log: from, to, actor (`customer`/`staff`/`system`), reason, time |
| `payments` | One per order: method, agreed `transfer_amount`/`cash_amount`, `method_from_cash`. Older columns (`cash_received`, `status`, `flags`, `notes`, `overpayment_amount`, `unreviewed_proofs`, `proof_*`, `extracted`) are legacy |
| `payment_receipts` | One per receipt image: status, flags, notes, reference, OCR JSON, `received_at`, `confirmed_amount`/`_at`, `discarded_at`, `discard_reason` |
| `receipt_photos` | The image bytes, one per receipt, kept apart so listing receipts never loads them |
| `cash_payments` | One per cash registration: amount, `RECEIVED`/`DISCARDED`, `registered_at`, `discarded_at`, `discard_reason` |
| `outbound_messages` | Every message sent (or faked): `SENT`/`FAILED`/`FAKE`, error text |
| `handoff_requests` | Requests passed to a person; `resolved_at` set by "Ya lo atendí" |

`payment_receipts.reference` has a unique partial index (when not null): a reused
reference is rejected by the database and code decides resend vs. reuse. Times are UTC
ISO strings with microseconds.

`db.init()` on every start: creates missing tables, adds missing columns
(`db.MIGRATIONS`), moves `IN_PREPARATION` orders to `ACCEPTED`, copies each legacy
payment with a receipt status into a first `payment_receipts` row (once; idempotent),
and drops the old `payments_reference_unique` index.

## 5. Where state lives

| State | Where | Survives a restart? |
|---|---|---|
| Orders, payments, receipts, photos, carts, messages, handoffs | SQLite | Yes |
| Chat history the AI sees (last 24 messages per phone) | Python memory | **No** |
| Restaurant config | Loaded at import | Re-read only on restart |
| `.env` | Read once at import | Re-read only on restart |

## 6. Concurrency

- One global lock runs conversation turns one at a time, for all phones. Receipt
  reading (`extract_proof`) runs outside it.
- Every route that writes is `async` and does its work without awaiting, so writes run
  one after another on the event-loop thread. `orders.amend` relies on this and keeps a
  guarded `UPDATE … WHERE state IN (…)` as a second defence.
- The cost: while one bot turn runs (Claude calls, up to 8 rounds), the whole server
  waits, panel polling and buttons included (`KNOWN-GAPS.md` G11–G12).

## 7. The AI's two jobs

1. **Conversation** (`brain.reply`): system prompt = persona + fixed rules + menu with
   ids and prices + hours + zones + accounts + the `ESTADO REAL AHORA` block, recomputed
   on every model call (live orders' states and the newest order's payment in words, no
   amounts, no codes). What the bot says about money is a code-built sentence it copies;
   `_finish_turn` checks every reply that speaks about payment (`payments.md` §7). Model: `ANTHROPIC_MODEL`, or the first available id containing
   `sonnet`, then `opus`, then `haiku`. Each tool call is printed to the console.
2. **Receipt reading** (`brain.extract_proof`): raw JSON only (amount, destination and
   origin account and holder, reference, date, `date_iso`, provider). It never judges
   the payment; `payments.md` covers the checks.
