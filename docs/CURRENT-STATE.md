# Current state

Snapshot: code last changed 2026-10-21 on branch `feature/task-e`: Task E (changes after
acceptance, the staff's Editar, durable notices, G43, photos, voice notes, sticker answers,
location, files) is
coded and waits for its hand test; Task D (WhatsApp through Chatwoot, PostgreSQL) is merged
into `main`. Tests run 2026-10-22: 444 pass, 1 expected failure (G16). Notion checklists ✅: Task A 2026-09-23, Tasks B and C
2026-09-30, Task C2 Part 1 2026-10-06, Task C2 Part 2 2026-10-11, Task D Part 2 2026-10-18
(16/16, local self-hosted Chatwoot, pure Cloud API number) (Bogotá time).

---

## 1. What works today

### Customer side (WhatsApp, through Chatwoot)

- Several messages in a row get one answer: the bot waits `MESSAGE_WAIT_SECONDS` (6) after
  the last one. A photo or a sticker is answered on its own.
- The bot answers only while the conversation is Pending in Chatwoot. Staff take over by
  opening it or replying, and give it back with Pending or "Ya lo atendí"; the bot then reads
  what was said meanwhile (`CHATWOOT-SETUP.md` §6).

- Free-text questions and orders. The bot maps words to exact menu ids; code computes
  every line, subtotal, delivery fee and total. Items not on the menu are refused, never
  invented; `available: false` items are refused as sold out.
- Modifiers per burger, priced per unit. Identical lines merge; when an item sits on two
  different lines, the bot asks which one before changing anything.
- Pickup or delivery; the fee comes from a zone table (accent- and case-insensitive,
  partial match), unknown zone = default fee.
- Summary → the customer's "sí" → the order appears in Nuevos.
- A change before the restaurant accepts updates **the same order** (same code, same
  card, "Actualizado"). After acceptance the bot shows a proposal with the new total; on
  the customer's "sí" an authorized drink is added at once, anything else waits on the card
  for the restaurant to accept or reject (`ORDER-LIFECYCLE.md` §3). Code-built messages.
- A separate order while the first one is accepted (warns about a second delivery fee).
- Cancel by chat before acceptance; after acceptance it goes to a person.
- "Quiero hablar con una persona": asks the reason once; a covered reason runs its tool;
  anything else opens the amber box when the customer has a card.
- A photo is checked as a receipt only if the reader does not say it isn't one; any other
  photo the bot answers like a message, with its caption. Voice notes are transcribed
  (ElevenLabs) and handled like typed text; an unclear one gets "please repeat or write it".
  The bot sees stickers; a 👍/👎 answers the one yes/no question it was shown (confirm the
  summary or a change, discard a change; never cancels, never payment). A shared pin is kept
  for the driver's map; a PDF or video with a live order gets "send it as a photo". After a
  finished order the bot starts a fresh conversation (G43).
- Payment: cash, transfer or mixed (split validated by code), or inferred from a clean
  receipt. Every receipt image is its own row with its own checks. Cash registered by
  staff counts toward what is owed by transfer. The bot says "received, a person will
  review it" or "there is a problem, a person will check"; never "confirmed" unless a
  person confirmed it.
- What the bot says about money (after an edit, a clean receipt, a method choice, a payment
  question) is a sentence code builds from the real state and the bot copies; every reply
  that speaks about payment is checked before it goes out (`payments.md` §7). A payment
  question reads the state fresh; a delivered or cancelled order's payment is "not verified".
- Automatic messages: accepted, ready (pickup), out for delivery; AI-written messages for
  staff reject/cancel and discarded receipts, from the staff's reason; code-built messages
  when staff confirm a receipt, edit an order or decide a change request.

### Staff panel (`/kitchen`, also `/`)

- Columns **Nuevos** · **Recibidos** · **Listos** · **En camino**, refreshed every 2 s.
  Orders waiting for the customer's "sí" are not shown.
- Buttons per column; reject/cancel need a reason (`ORDER-LIFECYCLE.md` §2).
- Tags: method, receipt status ("Sin comprobante" only on transfer/mixed), Sin pagar /
  Pago incompleto · faltan $X / Pago completo, Efectivo por cobrar, Sobrepago, Acordar pago,
  Actualizado (any column), pedido aparte, delivery fee, 📍 Ver en el mapa, Aviso no enviado.
- **Editar** on every card; «✏️ Cambio solicitado» with Aceptar cambio / Rechazar; «➕ El
  cliente agregó …» with Visto; «⚠ Avisos sin enviar» in the header with Reenviar.
- "Pagos" block: receipts and cash registrations, oldest first. A receipt row has time,
  amount, status, 👁 photo, grey notes, red box with reasons, Confirmar and ✕ while it waits
  for a person; a cash row has time, amount, "Efectivo recibido" and ✕. ✕ needs a reason; a
  discarded row leaves the card (a receipt's customer is told, cash tells nobody).
- "Registrar efectivo recibido": prefilled, capped at what is owed, one row per
  registration, no warning. Amount boxes read "22.400" and "$22.400" as $22.400 and refuse
  decimals.
- Amber "🙋 Necesita a una persona" box with "Ya lo atendí". With Chatwoot it also hands
  the conversation to a person there (private note, Open); «Chatwoot sin actualizar» while
  that fails. "Ya lo atendí" gives it back to the bot first.
- "ver conversación" on each card opens the order's conversation in Chatwoot.
- Red pulse for a Nuevos order older than 600 s (`/kitchen?alert=20` for demos).
- Green dot + "WhatsApp por Chatwoot" (or "WhatsApp directo"); grey dot + banner = not
  connected, or Chatwoot settings missing.
- "Reiniciar demo" wipes **all** data (photos included) after a warning.

### Reports (`/reports`)

- Reports for today (Bogotá calendar day), 7 or 30 days: sales, orders, average ticket,
  delivery fees, top 5 items, how they paid (by money that really came in), confirmed
  overpayments, payments on cancelled/rejected orders, pickup vs delivery, lost orders,
  orders waiting for the "sí", receipts flagged and awaiting (these two all time).

---

## 2. Task history

Each task has a Notion "Task X Plan" checklist that Miguel tests by hand (`WORKFLOW.md`).

| Task | Scope | Status |
|---|---|---|
| A | `UNSPECIFIED` method; receipts for unspecified/cash orders; checks on date, amount and account; customer told when staff confirm; paid/unpaid/incomplete on the panel; overpayment | Done: 6/6 ✅ |
| B | Remove "En preparación"; edit an unaccepted order in place; hand off changes to accepted orders; payment follows an edit; second receipt; payment-method changes. Fix round 1 (28 Sep): code-decided routing, `request_handoff` gate, line merge and targeting, one reply per message, "aceptado por el local" wording, grey "Revisado". Fix round 2 (29 Sep): "hablar con una persona", ESTADO REAL | Done: 55/55 ✅ (147 tests) |
| C | One row per receipt with its own checks, photo and Confirmar; database-checked reference for every receipt; resend vs reuse; Descartar with reason; cash capped at what is owed; surplus counts cash; cash settles an unspecified method; ESTADO REAL payment line (P-2465, P-0458, P-6455, P-4766 fixes) | Done: 40/40 ✅ (210 tests) |
| C2 Part 1 | Fixes for the 1 Oct code review, R1–R12 and R3b (§3): cash counts toward what is owed by transfer, confirm message, a modifier added twice, non-text messages, Spanish errors, order creation in one transaction, replies cleaned of the order code and "cocina", stop instruction, last model call, amounts typed by staff. Follow-up (4 Oct): R2a and R7 failed the first manual run; the bot's payment text was fixed | Done: 18/18 ✅ (263 tests). Re-runs 4 and 6 Oct |
| C2 Part 2 | "Pagos" block: one row per cash registration, ✕ to discard a receipt or cash (reason; a method only cash set goes back with the last cash row), no cash warning; code-built payment sentence copied verbatim and checked at the end of every turn (G29, G30, R1 wording; Gaps 1–7 decided 7 Oct); "Cómo pagaron" by money received (G27). Follow-up (8 and 13 Oct): two models, Haiku first when `ANTHROPIC_MODEL` is empty | Done: 8/8 ✅ 2026-10-11 (306 tests; 13 of the 21 planned rows left to automated tests) |
| D | Integration only (decided 2026-10-14). Part 1: SQLite → PostgreSQL. Part 2: WhatsApp through a Chatwoot inbox (pure Cloud API or Coexistence; Chatwoot Cloud or self-hosted); Zolvion is its Agent Bot and answers only while a conversation is Pending; a handoff hands it to a person; consecutive messages answered together; a worker thread answers (webhooks only store); "ver conversación" on the card; simulator removed. Step 0 (16 Oct) recorded 84 real Chatwoot events | Part 1 coded 2026-10-15. Part 2: Done, 16/16 ✅ 2026-10-18 (356 tests) |
| E | Before looking for the first client (decided 2026-10-21): changes after acceptance (an authorized drink added on the customer's "sí"; anything else a request staff accept or reject on the card, revision-guarded); staff **Editar** on every card; notices saved with their change and resendable; G43; wait 6 s; photos classified (caption kept), stickers seen, pin on the card, files ask for a screenshot. Addendum (same day): voice notes transcribed (ElevenLabs Scribe v2), stickers answer the one pending yes/no question. A GPT review of the plan (`outputs/task-e-review.md`) was checked gap by gap | Coded 2026-10-21 on `feature/task-e` (444 tests; quoted replies added 2026-10-22). Notion "Task E Plan" (50 rows) not run yet |
| — | Only if a client in production asks: a notifications bell (G23) | Not planned |
| — | After the first client says yes: server, login, backups; opening hours, delivery on/off, timers; a live Coexistence test | Not started |

Task B rows about the "Llegó otro comprobante…" box and "Confirmar pago manualmente"
describe behaviour that Task C replaced. Only what is written here is known about the
tasks after E; their scope is Miguel's decision.

---

## 3. Code review defects (2026-10-01), fixed in Task C2 Part 1

Found by reading and probing the code on 2026-10-01; reproduced by hand on 2026-10-02
(Notion "Bug Check (code review 1 Oct)"); fixed in Task C2 Part 1; checked by hand after
the fix in the Notion "Task C2 Plan (Part 1: bug fixes)" (2–6 Oct). Tests named below
are in `tests/test_review_bugs.py`. "Automated only" = no manual row: nothing on the
panel or on a phone shows it.

| # | What happened before the fix | Where | Bug Check (2 Oct) | After the fix |
|---|---|---|---|---|
| R1 | Order fully paid in cash (no receipt yet), then a transfer for the total arrives: the receipt is **clean**, the method becomes TRANSFER with no cash part, and the bot says "el pago quedó por transferencia por el total". Task C's row "fully paid in cash, then a transfer → red 'No se esperaba otro pago'" only held when the order already had a receipt | `payments._expected_for_new_receipt` ignored `cash_received` for a first receipt | Yes, P-7304: clean receipt, method → Transferencia, "Sobrepago $47.300" | ✅ by hand, three variants (P-9450, P-5786; re-run 6 Oct). Tests: `test_r1_transfer_after_full_cash_is_red`, `test_r1_full_transfer_after_partial_cash_is_red` |
| R2 | A TRANSFER order where staff registered cash: the bot's state says "todavía falta por transferir" and "ya está pagado completo" together, the card shows "Sin comprobante" next to "Pago completo", and a later transfer for the remainder is checked against the full agreed amount (red) | `payments.current().transfer_due` ignored `cash_received` | Yes. P-1948: card "Transferencia · Sin comprobante · Pago completo"; asked "¿Cómo va mi pedido?", the bot said no receipt had arrived and asked for $21.900 already paid in cash. P-1033: red "se esperaban $7.000" while the card says "faltan $5.000" | R2a failed the first C2 run (P-0644: the bot still asked for a transfer); the 4 Oct follow-up fixed the bot's text. ✅ by hand: R2a 4 Oct, no-method variant 6 Oct, R2b 6 Oct. Tests: `test_r2a_cash_on_a_transfer_order_leaves_nothing_to_transfer`, `test_r2b_transfer_for_the_rest_after_cash_is_clean`, `test_r2_r7_partial_cash_is_named_in_the_real_state`, `test_r2a_cash_paid_all_still_shows_a_red_or_discarded_receipt`, `test_r2a_cash_paid_all_with_a_receipt_still_waiting` |
| R3 | Staff confirm a partial transfer on a TRANSFER order: the customer is told "Quedan $X por pagar en efectivo al recibir", although they agreed to transfer | `main._payment_confirm_message` | Yes, P-3964: "Confirmamos su transferencia de $2.500. Quedan $2.500 por pagar en efectivo al recibir." | ✅ by hand: transfer (P-9233), mixed (P-0751), full transfer. Test: `test_r3_partial_confirm_on_a_transfer_order` |
| R3b | The "Confirmamos su transferencia" message counts receipts nobody has confirmed in "envió $X de más" | `main._payment_confirm_message` read `overpayment_amount`, which includes waiting receipts | Not testable | Automated only. Test: `test_r3b_confirm_message_ignores_unconfirmed_surplus` |
| R4 | Editing an order whose line has a modifier added twice (e.g. double queso) keeps it once: the line silently gets cheaper | `cart.fill_from_order` rebuilt modifier ids by name | Yes, hidden by the bot. P-4438's final order is right, but its stored lines show the bot removed and re-added the burger ("Hubo un lío con el queso…") | ✅ by hand (P-3545). A first try read "doble queso adicional" as one queso (P-5135, G31). Test: `test_r4_edit_keeps_a_modifier_added_twice` |
| R5 | A voice note, sticker or location (or an image that failed to download) is sent to Claude as an empty message; the API refuses it and the customer gets "Perdón, se me cruzaron los cables" | `main.handle` / `wa.parse_webhook` | Not tested (needs real WhatsApp) | Automated only; by hand with a real phone in Task D (`TEST.md` Part B). Tests: `test_r5_non_text_message_reaches_the_bot_as_a_note`, `test_r5_reaction_gets_no_reply` |
| R6 | Some errors reach staff and the bot in English ("floats are not allowed as money", "'food_ready' is not legal from CONFIRMED") | `money.pesos`, `orders.apply_event` messages | In part. No English seen; the two-tab case couldn't be tested; decimals → R12 | Automated only (R12 rows cover the panel). Test: `test_r6_errors_reach_staff_in_spanish` |
| R7 | With a config that does not list `CASH`, after staff register cash on an unspecified order every later edit fails ("CASH no está habilitado") | `payments.follow_total` → `set_method` | Yes, P-8284: "no pude agregar la gaseosa personal… por un problema del sistema", then offered Nequi though it was paid in cash | The edit worked in the first C2 run, but the bot did not say what was paid and owed (P-3198, P-5300); the 4 Oct follow-up fixed the bot's text. ✅ by hand 6 Oct: Caserita, Caserita Madurita, and a mixed order (G29). Tests: `test_r7_edit_after_cash_without_cash_in_config`, `test_r7_cash_line_never_forbids_a_transfer_still_owed`, `test_r2_r7_partial_cash_is_named_in_the_real_state` |
| R8 | Creating an order writes the order, its lines, the cart and the payment without a transaction, and checks the total with an `assert` after writing | `orders.create_from_cart` | Not testable by hand | Automated only. Tests: `test_r8_order_creation_is_all_or_nothing`, `test_r8_total_is_checked_before_writing` |
| R9 | Only discard messages are guarded in code against the order code and "cocina"; `add_item`'s note says "nota breve para la cocina" | `main._discard_message`, `brain.TOOLS` | Not reproduced in 3 tries | ✅ by hand: no code or "cocina" in any customer message of the C2 checklist. Tests: `test_r9_replies_never_carry_the_code_or_cocina`, `test_r9_cleaning_is_grammatical`, `test_r9_tool_descriptions_never_say_cocina` |
| R10 | The reject/cancel instruction is broken Spanish ("El local rechazado el pedido…", "El local cancelado por el local el pedido…") | `main._stop_message` | Not reproduced; both messages were correct | ✅ by hand. Test: `test_r10_stop_instruction_says_who_did_what` |
| R11 | After 8 tool rounds the customer gets text written before the tools answered, or "Perdón, ¿me lo repite?", and the history ends on a tool result | `brain._reply_locked` | Not testable | Automated only. Tests: `test_r11_text_before_tools_never_reaches_the_customer`, `test_r11_empty_final_answer_falls_back` |
| R12 | The panel amount boxes are `<input type="number">`, and in Chromium: "26000.5" is refused with an English error; the arrow/wheel turns it into 26001; "26.000" or "26,000" registers $26; `money.pesos("26000.5")` returns 260005 | `static/kitchen.html` modal, `money.pesos` | Yes, P-4438 stored `cash_received` 26001; no decimal confirmed amount was stored | ✅ by hand: "47.300" and "$47.300" (P-9950, P-2805), decimals refused, more than owed refused. Tests: `test_r12_decimal_amounts_are_refused`, `test_r12_panel_amount_box_is_text`, `test_r12_staff_amounts_in_colombian_format`, `test_r12_receipt_amounts_still_read` |

---

## 4. Tests

`tests/test_flow.py`: Tasks A–C plus the C2 Part 2 section (cash rows and the ✕, Gaps 1–7,
the payment sentence, the end-of-turn check with a scripted model, reports, schema, the two models), **255
cases**, all passing (Task D Part 1 dropped 4 tests of the SQLite upgrade steps and added 2).
`tests/test_review_bugs.py` (Task C2 Part 1): one or more tests per defect of §3 plus the
4 Oct follow-up, **54 cases**: 53 pass, and `test_g16_order_code_loop_is_bounded` stays an
expected failure (G16, out of scope); R5 and R9 now go through the worker.
`tests/test_chatwoot.py` (Task D Part 2): **48 cases** on the Chatwoot integration, with
recorded events and a fake Chatwoot. `tests/test_task_e.py` (Task E): **76 cases** (changes
after acceptance, Editar, notices, G43, photos, voice notes, stickers, location, files; two Task B tests
of the old window-3 handoff were rewritten for proposals). They run on PostgreSQL
(`tests/conftest.py`). Together **444 passed, 1 xfailed** (2026-10-22; the warnings are FastAPI's `on_event` and
one from Starlette's test client, G10). No AI calls happen in tests. Areas:
`BUILD-AND-RUN.md` §5.
