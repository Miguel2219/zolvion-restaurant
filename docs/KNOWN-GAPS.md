# Known gaps

What stands between the code at the snapshot (`README_1.md`, checked 2026-10-17 after Task
D Part 2) and an MVP a restaurant can run every day. Every item was observed in the code,
the repo docs or the Notion checklists.

These are **observations, not decisions.** Which ones get fixed, in what order and how
is Miguel's call; each becomes a lettered task only when he decides. Code defects found
in review are listed separately in `CURRENT-STATE.md` §3 (all fixed in C2 Part 1).
Numbers are never reused: a closed gap moves to §7 and keeps its number.

---

## 1. Access and safety

| # | Gap | Why it matters | Where |
|---|---|---|---|
| G1 | No login on the panel, reports or API | Anyone with the public URL can move orders, confirm or discard receipts, register or discard cash and read phone numbers | `main.py` routes |
| G2 | `POST /api/reset` deletes every order, payment, receipt photo and conversation, with no login | One click (after a warning) or one request erases a restaurant's data | `main.reset`, `db.reset` |
| G3 | Receipt photos are served by consecutive ids with no login | Anyone with the URL can open every customer's receipt (noted as a known gap in Task C) | `GET /api/receipts/{id}/photo` |
| G4 | Without Chatwoot, Meta's webhook signature check is skipped when `WHATSAPP_APP_SECRET` is empty (the Chatwoot webhook always checks) | Fake WhatsApp messages could be posted | `wa.check_signature` |

## 2. Hosting and operations

| # | Gap | Why it matters | Where |
|---|---|---|---|
| G5 | Runs on Miguel's laptop behind a free ngrok URL | Laptop off = restaurant offline; the URL changes on every ngrok restart, so Meta's webhook must be redone | `run.md` Steps 5–7 |
| G6 | Temporary Meta token (24 h) | Replies silently stop the next day | `run.md` Step 5 |
| G7 | The database has no backup (the code is on GitHub since Task C2 Part 2) | One disk failure loses the restaurant's data | the local PostgreSQL (`DATABASE_URL`) |
| G10 | FastAPI's `@app.on_event("startup")` is deprecated (most of the test run's warnings; one comes from Starlette's test client) | A future FastAPI upgrade may drop it | `main._startup_check`, `main._start_worker` |
| G36 | Chatwoot Cloud's free Hacker plan has no WhatsApp inbox and offered no trial | Chatwoot Cloud needs a paid plan per agent; tests run on a self-hosted Chatwoot (`CHATWOOT-SETUP.md`) | — |
| G40 | Local only: with free ngrok, Chatwoot opened through `localhost` (and the panel's "ver conversación", which uses `CHATWOOT_URL`) shows "image no longer available" | ngrok's warning page replaces the photo in the browser; open Chatwoot through the ngrok domain | `CHATWOOT-SETUP.md` §3 |

## 3. Reliability

| # | Gap | Why it matters | Where |
|---|---|---|---|
| G12 | One worker and one global lock: one conversation turn at a time, for all customers | At a busy moment each customer waits for everyone ahead | `worker.py`, `brain._lock` |
| G13 | Chat history is only in memory | A restart makes the bot forget every conversation mid-order (orders survive) | `brain._history` |
| G14 | Outbound messages are not retried and nobody is alerted on failure (only Chatwoot status changes are retried) | A failed "su pedido va en camino" is only visible as `FAILED` in the database | `wa.send_text` |
| G41 | A Zolvion that is down or slower than 5 s on a webhook: Chatwoot opens the conversation for a person and never resends that message | Fails safe (staff see it under Open), but the bot never reads that message, so its catch-up note lacks it | Chatwoot Agent Bot |
| G42 | Who answers is read before each turn and again right before sending (minimal point 1), not during the send | A person who takes over in that same instant can still get one bot reply on top | `worker._deliver` |
| G15 | Voice notes, stickers and locations now reach the bot as a note (`CURRENT-STATE.md` R5), but a voice note is not transcribed: the bot can only ask the customer to write it | Real customers send voice notes | `wa.parse_webhook`, `main._note_for` |
| G16 | Order codes are 4 random digits, retried until unused, and never freed. An expected-failure test exists: `test_g16_order_code_loop_is_bounded` | Collisions slow down creation as orders pile up; after 10 000 orders the loop never ends | `orders._code` |
| G34 | Payment messages force a fresh `order_status` call (`tool_choice` type `tool`), which Sonnet 5.5, Opus 5.5 and Fable 5.1 refuse with a 400. The chat model must be Haiku 5.5; an empty `ANTHROPIC_MODEL` picks Haiku first. Miguel plans to fix it before real use: code reads the order itself, so any model works | On a refusing model every payment message gets "Perdón, se me cruzaron los cables", and a newer chat model can't be used | `brain._payment_question`, `brain._answer_again` |

## 4. Business behaviour

| # | Gap | Why it matters | Where |
|---|---|---|---|
| G17 | No timers. `PENDING_CONFIRMATION` never expires (`EXPIRED` is unreachable), open carts never expire; the 10-minute alert is only a visual | Abandoned orders pile up; nobody is told when an order sits unaccepted | `orders.TRANSITIONS`, `kitchen.html` |
| G18 | Opening hours not enforced; `open_days` and `closed_message` unused | The bot takes orders at any hour | `CONFIG.md` §1 |
| G19 | `fulfilment.pickup` / `delivery` switches not enforced | A restaurant without delivery would still get delivery orders | `CONFIG.md` §1 |
| G20 | `/api/reload-config` only updates the bot's copy of the config | Prices can differ between what the bot sees and what carts, receipts and reports use until a restart | `main.reload_config` |
| G21 | One restaurant per running process (one config, database, number) | Several clients = several processes, or a multi-restaurant design (a SaaS decision) | `catalog.CATALOG`, `db.DATABASE_URL` |
| G22 | With two live orders (an accepted one plus a separate one), a receipt image always lands on the newest | A receipt meant for the first order is checked against the second | `main.handle` |
| G23 | A customer with no card who asks for a person reaches nobody through the bot (staff can still take any conversation in Chatwoot) | The notifications bell is planned as its own task | `brain` `request_handoff` |
| G35 | WhatsApp Coexistence (the restaurant's Business app on the same number) is not tested live; WhatsApp for Windows gets unlinked under it | The echo → takeover rule is built from Chatwoot's code, not from a recording | `chatwoot.classify` |
| G37 | Reactions never reach Chatwoot, so never Zolvion; a customer's message edit was not available to test | A 👍 meaning "sí" gets no answer; an edited message would be ignored (`message_updated`) | Chatwoot |
| G38 | A sticker arrives as a plain `image/webp`: any webp image is taken as a sticker | A receipt screenshot saved as webp is never checked; the bot says it can't see stickers | `chatwoot.parse_customer` |
| G43 | The bot's chat memory keeps a delivered order: asked for "una caserita" right after one, it raised the new cart to 2 (hand test 18 Oct, P-7286 then a new cart; fine after «Reiniciar demo» cleared the memory) | A returning customer can get a wrong quantity shown in the summary (they can still correct it before "sí") | `brain._history`, `SYSTEM` |
| G39 | After a resolve, Chatwoot Cloud starts a new conversation and a self-hosted inbox with "lock to single conversation" reopens the old one | Both work; the card's link opens the conversation the order was made in, which may be an older one | `chatwoot.remember`, `main.board` |
| G25 | A receipt whose reference cannot be read can't be recognised as a resend; it is checked as a new receipt | Same screenshot twice may become two rows (Task C known gap) | `payments.check_proof` |
| G31 | The bot can read "doble queso adicional" as one queso (P-5135, first R4 run); "una caserita con doble queso" worked (P-3545) | Code prices exactly what the model adds; here the bot showed the price and asked, so the customer could catch it | `brain.SYSTEM`, `add_item` |
| G32 | Cash registered on top of a clean receipt still waiting for a person: "Sobrepago" counts that receipt before anyone confirms the money arrived (`TEST.md` C7; kept as it is in C2 Part 2, which only stops the bot calling it money owed back) | The card can show a surplus for a transfer nobody has seen yet | `payments.current` (`overpayment_amount`) |

## 5. Reports

| # | Gap | Why it matters | Where |
|---|---|---|---|
| G26 | "Comprobantes marcados" and "por confirmar" count all time, not the selected period | They don't change with Hoy / 7 / 30 days (decided in Task C) | `reports.snapshot` |
| G28 | "Ventas" counts customer-confirmed orders not yet accepted | A later rejection removes it from sales; until then it looks sold | `reports.COUNTED` |
| G33 | No refund records. "Pagos de pedidos cancelados/rechazados" lists money registered on stopped orders, and confirmed overpayments are listed, but nothing records whether money went back | Staff must check by hand which customers were paid back | `reports._payments` |

## 6. Small items

- Without Chatwoot the customer's WhatsApp profile name is parsed but never stored; with
  Chatwoot the contact's name goes to `conversations.display_name`, which nothing shows yet.
- An image's caption is dropped when the image is read as a receipt.
- `run.md` "What this demo does NOT do" also lists: no staff login, no multi-branch, no
  printer. Those are feature decisions, not bugs.

## 7. Closed

- G8 (closed 2026-10-01, first commit): `.gitignore` ignores `venv/` as well as `.venv/`;
  no virtualenv file is tracked.
- G27, G29, G30 (closed 2026-10-07, Task C2 Part 2): "Cómo pagaron" counts what really came
  in (registered cash and confirmed transfers); after an edit the bot copies a code-built
  payment sentence that names the cash paid and the transfer and cash still owed
  (`payments.md` §7-8).
- G9, G11, G24 (closed 2026-10-17, Task D Part 2): `main.py`'s docstring no longer says
  "six-minute demo"; the webhooks only store messages and a worker thread answers them, so
  the server keeps answering meanwhile; while a person has the conversation in Chatwoot the
  bot stays quiet.
