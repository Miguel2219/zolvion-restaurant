# Architecture

One Python process (FastAPI + uvicorn, plus one worker thread), one PostgreSQL database,
the Anthropic API for understanding messages and reading receipts, and WhatsApp through a
Chatwoot inbox where Zolvion is the Agent Bot (or the WhatsApp Cloud API directly, without
Chatwoot). Two static HTML pages poll a JSON API. Setup: `CHATWOOT-SETUP.md`.

---

## 1. The whole system

```
 Customer's WhatsApp ─> Meta ─> Chatwoot inbox ─> POST /chatwoot/webhook ─┐ (signed; stores,
                     (without Chatwoot: Meta ─> POST /webhook) ───────────┘  answers 200 at once)
                                                                          │ inbound_messages RECEIVED
 worker.py thread, every 0.5 s ◄──────────────────────────────────────────┘ conversations: status
   1. tell Chatwoot what it was not told yet (handoff note + open, takeover), retry every 30 s
   2. one phone whose newest message is MESSAGE_WAIT_SECONDS (6) old; a shared pin is
      kept on the cart or the order first (whoever answers)
   3. a person has the conversation (not Pending) ─> HUMAN; a receipt photo is still checked
                                                     and the result goes to a private note
      consecutive texts, one turn ─> main.handle ─> brain.reply(); a voice note in it is
                                     transcribed first (transcribe.py): clear = customer text
      each photo or sticker, live order ─> brain.extract_proof() (AI reads, says is_receipt)
         a receipt ─> payments.check_proof() (code decides) ─> brain.reply() with an instruction
         anything else ─> brain.reply() sees the image; a sticker that answers the one
                          shown yes/no question runs that one tool (main._sticker)
   4. queued notices of that phone go out; then the reply, if the status is still Pending
      (wa.send_text); otherwise SUPPRESSED, not sent. Queued notices older than 15 s too.
                                                                          │
 wa.send_text() ── Chatwoot API, or Meta API, or FAKE ── always: row in outbound_messages
                                                                          │
 Staff panel /kitchen  ── polls GET /api/board every 2 s ─────────────────┤ PostgreSQL
 Reports /reports      ── polls GET /api/report every 4 s ────────────────┘ (DATABASE_URL)
```

`brain.reply()` runs up to 8 rounds: send the conversation to Claude with the tool
list → run every tool it asks for → send the results back → stop when it answers
without a tool. Tools are the only way the AI can touch an order. Only the last
round's text is sent to the customer. If the model still wants tools after round 8,
one more call with tools switched off writes the answer.

What the bot cannot read (a voice note without a key or not heard clearly, location,
video, document, an image that failed to download) reaches `handle()` as a bracketed note for the model
(`main._note_for`) instead of empty text; a reaction gets no reply and no model call (and
through Chatwoot it never arrives). Chatwoot sends a sticker as a plain `image/webp`: any
webp image is a sticker, which the bot sees (only its first frame). A location note carries
no coordinates.

**Quoted replies** (recorded 2026-10-22, `tests/fixtures/chatwoot/quote_*.json`): a reply that
quotes an earlier message carries its reference, kept in `inbound_messages.reply_to`: through
Chatwoot `content_attributes.in_reply_to` (Chatwoot's id) and `in_reply_to_external_id` (the
wamid, Meta's `context.id`); from Meta directly `context.id`. In a text turn `main.quote_note`
looks it up in our own tables (`outbound_messages` by `chatwoot_message_id` or `wa_message_id`,
`inbound_messages` by wamid) and puts a note right before that message: who wrote it (the bot,
a person of the restaurant, the customer) and its text cut to 160 characters, as context and
never instructions; a photo by its date, and as a receipt when it made one, never an amount.
Not found: the bot is told it can't see it. The payment question never reads these notes
(`handle(said=…)`). A photo or sticker sent as a reply gets no note (G47).

**Stickers that answer** (`main._sticker`): `brain.pending_question` finds the one yes/no
question waiting (the newest order's summary, or a `PROPOSED` change; both or neither = none)
and checks a message went out after it was asked and before the sticker arrived;
`brain.read_sticker` says AFFIRM/DENY/THANKS/OTHER/UNCLEAR; code runs only the tool that answer
allows (`STICKER_TOOLS`: confirm the order, confirm or discard the change) through
`brain.run_for_sticker`, with that tool's own checks. A «no» to a summary never cancels.

**Voice notes** (`transcribe.py`): ElevenLabs Scribe v2, chosen 2026-10-21 (lowest error rate in
the Artificial Analysis benchmark, takes WhatsApp's OGG as it is, keyterms from the menu).
Code calls a note clear only with spoken words and a Spanish probability ≥ 0.5; the transcript
is kept in `inbound_messages.transcript`. Claude itself takes no audio.
Every reply is cleaned of order codes and the word "cocina" (`main._clean_for_customer`)
before it is sent.

## 2. Modules (`app/`, in dependency order)

| Module | Owns |
|---|---|
| `__init__.py` | Loads `.env` before any other module reads the environment |
| `money.py` | `pesos()`, `total_of()`, `fmt()`: money is `int` COP, never float |
| `catalog.py` | `CATALOG` from `config/restaurant.json`: prices, modifiers, zones, accounts, timezone, account matching |
| `transcribe.py` | Voice notes to text (ElevenLabs Scribe v2); `heard_from` is code's verdict on what came back |
| `db.py` | Schema (created, then upgraded, at startup), one autocommit psycopg connection per thread, `run()/q()/q1()/insert()`, `transaction()`, `write_lock`, `IntegrityError` |
| `chatwoot.py` | Settings, signature check, what each event means (`classify`), `parse_customer`, each phone's conversation and status (`remember`, `bot_holds`, `set_local`), API (`send`, `set_status`), `download` |
| `cart.py` | Cart changes, identical-line merge, `LineChoiceNeeded`; `summary()`/`summary_of()` is the only producer of a total; staging carts (never `OPEN`) |
| `orders.py` | State machine, modification windows, `amend()`, `apply_target()` (a change written back with its payment split), `state_token()`, `attach_location()`, `board()`, `live_states()` |
| `changes.py` | Task E: a customer's change after acceptance (`change_requests`): proposal, AUTO apply, staff accept/reject guarded by `revision`, close with the order |
| `payments.py` | Methods, receipts and their checks, cash rows, confirm/discard of receipts and cash (people only), `follow_total`, `current()` |
| `handoff.py` | `request` / `resolve` / `close_for_phone` / `open_for`: the amber "Necesita a una persona" box and its Chatwoot state |
| `wa.py` | `send_text`, the single exit (Chatwoot, Meta Graph v21.0, or FAKE); `queue`/`send_queued`/`stuck_notices` (notices saved with their change); `record`; Meta media download, signature, `parse_webhook` |
| `brain.py` | System prompt, ESTADO REAL block, `TOOLS`, `_run_tool`, chat history (cleared once after a finished order, G43), the payment sentence and the end-of-turn payment check (`_finish_turn`), `order_message`, `extract_proof`/`is_receipt` |
| `reports.py` | SQL aggregates for the reports page; payments per order through `payments.current()` |
| `worker.py` | The thread that answers: `run_once` (claim, group, who answers, send or suppress), `recover` (after a restart), `sync_chatwoot` (retries), the catch-up note, staff notes for receipts |
| `main.py` | HTTP routes, `handle()`, `receive_receipt()`, `_note_for()`, `quote_note()`, `notify()`, customer messages for confirm/discard, `_clean_for_customer()`; creates the database on import, starts the worker |

`static/`: `kitchen.html`, `reports.html`: plain HTML + JS, no build step. Panel buttons
send event names straight from `orders.TRANSITIONS`. The browser simulator was removed in
Task D Part 2; FAKE mode stays for the tests.

## 3. HTTP routes

| Route | Purpose |
|---|---|
| `POST /chatwoot/webhook` | The Agent Bot's events. 404 without Chatwoot, 503 with settings missing, 401 on a bad or old signature; otherwise stored and 200 at once, even if storing failed |
| `GET /webhook` | Meta's verification handshake (`WHATSAPP_VERIFY_TOKEN`); 404 in Chatwoot mode |
| `POST /webhook` | Inbound WhatsApp without Chatwoot (404 with it). Signature checked on the raw body first (403); stored for the worker; duplicates skipped by the database; reactions stored as done |
| `GET /api/config` | Business data, `channel` (`chatwoot`, `whatsapp`, `none`, `incomplete` + `missing`), accounts |
| `GET /api/board` | Non-terminal orders with items, payment (receipts and cash rows included, discarded ones too; `agree` for «Acordar pago»), transitions, open handoffs (with their Chatwoot state), `chatwoot_link`, `change` (open request, unseen AUTO additions), `map_url`, `notice_failed` |
| `POST /api/orders/{id}/event` | Staff button. 409 if illegal; 400 if reject/cancel has no reason. A terminal state closes an open request (delivered: the customer is told) |
| `GET /api/menu` | The menu for the panel's editor: items, allowed modifiers, zones (config values) |
| `POST /api/orders/{id}/edit` | Staff **Editar**: `preview` returns the new order, the message and a token; a save with that token queues the message. 400/409 with Spanish errors |
| `POST /api/orders/{id}/changes/{cid}/accept` | Accept a request (`revision`; `preview`, then save with its token and, en camino, `driver_ok`) |
| `POST /api/orders/{id}/changes/{cid}/reject` | Reject a request with a reason; the customer gets the template |
| `POST /api/orders/{id}/changes/{cid}/seen` | «Visto» on an automatic addition |
| `GET /api/notices/failed` | Notices that did not go out (`FAILED`, or `SENDING` over 2 min), any order |
| `POST /api/notices/{nid}/resend` | Resend once: an order notice as the order is now, a template as it was |
| `POST /api/orders/{id}/handoff/resolve` | "Ya lo atendí". With Chatwoot, Pending first: 502 if Chatwoot fails, and the box stays |
| `POST /api/orders/{id}/receipts/{rid}/confirm` | A person confirms one receipt (amount received); the customer is told |
| `POST /api/orders/{id}/receipts/{rid}/discard` | A person discards one receipt with a reason; the customer is told |
| `GET /api/receipts/{rid}/photo` | The receipt image ("Sin foto." 404 when none) |
| `POST /api/orders/{id}/payment/cash` | Staff registers cash received: one row, never above what is owed |
| `POST /api/orders/{id}/cash/{cid}/discard` | A person discards one cash row with a reason; nobody is told |
| `GET /api/report?days=N` | Reports data |
| `POST /api/reset` | Deletes every row in every table and all chat history |
| `POST /api/reload-config` | Re-reads the config, but only `brain` sees it (`CONFIG.md`) |
| `GET /`, `/kitchen`, `/reports` | The two pages |

No route requires a login (`KNOWN-GAPS.md` G1).

## 4. Data model (PostgreSQL)

| Table | What a row is |
|---|---|
| `conversations` | A customer phone: Chatwoot contact name, newest Chatwoot conversation and its status (`status_at` = Chatwoot's clock), a status still to push (`push_status`), `caught_up_at` |
| `inbound_messages` | One received message; `wa_message_id` (WhatsApp id) is the primary key, so a repeated delivery is rejected. `status`: `RECEIVED`, `PROCESSING`, `DONE`, `HUMAN`, `FAILED`, `INTERRUPTED`; `conversation_id`, `media_url`, `location`, `transcript` (a voice note), `reply_to` (the message it quotes), `error` |
| `carts`, `cart_items` | Work in progress (`OPEN`, `CONVERTED`; staging: `STAFF_EDIT`, `PROPOSAL`). `related_order_id` marks a separate order opened next to an accepted one; `location` the pin shared while building it |
| `orders`, `order_items` | The order: code `P-1234`, state, pickup/delivery, address, zone, subtotal, fee, total, `edited_at`, `edited_by` (`customer`/`staff`), `location`, `chatwoot_conversation_id` |
| `change_requests` | Task E: a change after acceptance: status `PROPOSED`/`PENDING`/`APPLIED`/`REJECTED`/`DISCARDED`/`CLOSED`, `revision`, `route` (`AUTO`/`APPROVAL`), target and diff JSON, `base_token`, totals, who resolved it, reason, `seen_at` |
| `order_transitions` | Append-only log: from, to, actor (`customer`/`staff`/`system`), reason, time |
| `payments` | One per order: method, agreed `transfer_amount`/`cash_amount`, `method_from_cash`, `updated_at` |
| `payment_receipts` | One per receipt image: status, flags, notes, reference, OCR JSON, `received_at`, `confirmed_amount`/`_at`, `discarded_at`, `discard_reason` |
| `receipt_photos` | The image bytes, one per receipt, kept apart so listing receipts never loads them |
| `cash_payments` | One per cash registration: amount, `RECEIVED`/`DISCARDED`, `registered_at`, `discarded_at`, `discard_reason` |
| `outbound_messages` | Every message: `SENDING`, `SENT`, `FAILED`, `FAKE`; `SUPPRESSED` (a person took over first) and `STAFF` (a person's, from Chatwoot); `QUEUED`/`SUPERSEDED` for Task E notices (`order_id`, `notice_kind` `ORDER_STATE`/`FIXED`); `conversation_id`, `chatwoot_message_id`, `wa_message_id` (Meta direct: Graph's id for it) |
| `handoff_requests` | Requests passed to a person; `chatwoot_state` `NONE`/`OPENING`/`OPEN`, note id, retry time, error; `resolved_at` |

`payment_receipts.reference` and `outbound_messages.chatwoot_message_id` have unique partial
indexes (when not null): a reused reference or a repeated Chatwoot event is rejected by the
database. `change_requests` allows one `PROPOSED`/`PENDING` row per order. Times are UTC ISO strings with microseconds (`TEXT`, compared as strings). Ids are
`BIGINT` identity columns; money is `INTEGER` pesos, so `SUM()` comes back as an `int`.

`db.init()` on every start runs `SCHEMA` (tables and indexes where missing) and then
`UPGRADE`: Task D Part 2's and Task E's columns and `change_requests`, added with `IF NOT
EXISTS` so an older database keeps its rows (older messages count as answered). There is no
migration tool.

## 5. Where state lives

| State | Where | Survives a restart? |
|---|---|---|
| Orders, payments, receipts, photos, carts, messages, handoffs, change requests, queued notices, who has each conversation | PostgreSQL | Yes |
| A message being answered when the app stopped | PostgreSQL (`PROCESSING`) | Becomes `INTERRUPTED`; the customer is asked to repeat it |
| Chat history the AI sees (last 24 messages per phone) | Python memory | **No** |
| Restaurant config | Loaded at import | Re-read only on restart |
| `.env` | Read once at import | Re-read only on restart |

## 6. Concurrency

- Bot turns run in the worker thread, one at a time for all phones (one global lock;
  `extract_proof` runs outside it). The server keeps answering webhooks and the panel
  meanwhile: Chatwoot hands a conversation to a person if its webhook takes over 5 s.
- One write at a time: `db.write_lock` is held by each tool call, each receipt store, each
  webhook and each panel write, and never around a model or Chatwoot call. Panel routes run
  what waits on the model or on Chatwoot (notifications, confirm/discard messages, "Ya lo
  atendí") in a thread. `orders.amend` keeps a guarded `UPDATE … WHERE state IN (…)`.
- The cost that remains: at a busy moment each customer waits for everyone ahead (G12).

## 7. The AI's two jobs

1. **Conversation** (`brain.reply`): system prompt = persona + fixed rules + menu with
   ids and prices + hours + zones + accounts + the `ESTADO REAL AHORA` block, recomputed
   on every model call (live orders' states and the newest order's payment in words, no
   amounts, no codes). What the bot says about money is a code-built sentence it copies;
   `_finish_turn` checks every reply that speaks about payment (`payments.md` §7). Model: `ANTHROPIC_MODEL`, or the first available id containing
   `haiku`, then `sonnet`, then `opus`. It must accept a forced `tool_choice` (payment
   questions force `order_status`): Haiku 5.5 does, Sonnet 5.5 and Opus 5.5 don't (G34).
   Each tool call is printed to the console.
2. **Receipt reading** (`brain.extract_proof`): raw JSON only (amount, destination and
   origin account and holder, reference, date, `date_iso`, provider, `is_receipt`). It never
   judges the payment; only a clear `is_receipt: false` keeps an image out of the checks
   (Task E); `payments.md` covers the checks. Model: `ANTHROPIC_PROOF_MODEL`, or the
   conversation model when empty.
