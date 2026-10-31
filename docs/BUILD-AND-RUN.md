# Build and run

The repo's `run.md` is the full step-by-step guide (including the demo-day script) and
`TEST.md` is the manual regression script with exact expected totals. This file is the
short reference.

---

## 1. Stack

| Piece | Choice | Installed in `venv/` (2026-10-01) |
|---|---|---|
| Language | Python | 3.13.7 (Windows) |
| Web server | FastAPI + uvicorn (Starlette) | fastapi 0.141.1, uvicorn 0.53.0, starlette 1.6.0 |
| AI | Anthropic SDK (Claude, tool use + image input) | anthropic 1.7.0 |
| HTTP client | httpx (WhatsApp Graph API v21.0) | 0.28.1 |
| Database | PostgreSQL 17 through psycopg 3 (autocommit, rows as dicts) | psycopg 3.3.6 (binary) |
| Config | JSON file + `.env` via python-dotenv | python-dotenv 1.2.3 |
| Front end | 3 static HTML pages, vanilla JS, no build | — |
| Tests | pytest | 9.1.1 |
| Timezone data on Windows | `tzdata` | — |

`requirements.txt` pins minimum versions only (`fastapi>=0.115`, `anthropic>=0.40`, …).
No linter or formatter is configured.

## 2. Setup (Windows, PowerShell)

```powershell
cd C:\Users\PC\Desktop\zolvion-demo
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # then fill it in
```

PostgreSQL 17 is installed on the laptop as the Windows service `postgresql-x64-17` on
port 5432. Once, with the `postgres` password chosen at install time:

```powershell
& 'C:\Program Files\PostgreSQL\17\bin\psql.exe' -U postgres -h localhost -p 5432 -c "CREATE ROLE zolvion LOGIN PASSWORD 'zolvion' CREATEDB;" -c "CREATE DATABASE zolvion OWNER zolvion;"
```

The app creates its tables at startup (`db.init()`); the tests create `zolvion_test`.
`zolvion`/`zolvion` is a local-only login. Without a local PostgreSQL,
`docker compose up -d` starts one on port 5433 (`docker-compose.yml`; untested here:
Docker Desktop 4.77 crashed on this laptop, 2026-10-14) and `DATABASE_URL` must say 5433.

The virtualenv folder is `venv/` (ignored by git). If PowerShell blocks activation:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

## 3. Environment (`.env`)

| Variable | Meaning |
|---|---|
| `ANTHROPIC_API_KEY` | Required. Missing → the startup banner says "FALTA" and the bot answers "sistema en mantenimiento" |
| `ANTHROPIC_MODEL` | The conversation model (`claude-haiku-5-5`). It must accept a forced `tool_choice`: Sonnet 5.5 and Opus 5.5 don't, and payment questions fail with a 400 (G34). Empty → the first available haiku, then sonnet, then opus |
| `ANTHROPIC_PROOF_MODEL` | The model that reads receipt images (`claude-sonnet-5-5`). Empty → the same as `ANTHROPIC_MODEL` |
| `CHATWOOT_URL`, `CHATWOOT_ACCOUNT_ID`, `CHATWOOT_BOT_TOKEN`, `CHATWOOT_BOT_SECRET` | All four → WhatsApp through Chatwoot (`CHATWOOT-SETUP.md`); the `WHATSAPP_*` values are then unused. Some missing → the banner says "FALTA" and every webhook is refused |
| `ELEVENLABS_API_KEY` | Voice notes (ElevenLabs Scribe v2). Empty: the bot asks the customer to write instead. Tests always blank it |
| `VOICE_MAX_MB` | Largest voice note sent to be transcribed (default 2) |
| `MESSAGE_WAIT_SECONDS` | Seconds after a customer's last message before the bot answers, so several messages get one answer (default 6) |
| `WHATSAPP_TOKEN` | Without Chatwoot: empty → **FAKE mode**, no message leaves (replies are only stored in `outbound_messages`, as the tests read them) |
| `WHATSAPP_PHONE_NUMBER_ID` | The sending number's id in Meta (Meta mode needs both this and the token) |
| `WHATSAPP_APP_SECRET` | Checks Meta's webhook signature. Empty → the check is skipped |
| `WHATSAPP_VERIFY_TOKEN` | Word for Meta's webhook handshake (default `zolvion-demo`) |
| `RESTAURANT_CONFIG` | Config path (default `config/restaurant.json`) |
| `DATABASE_URL` | PostgreSQL (default `postgresql://zolvion:zolvion@localhost:5432/zolvion`) |

`.env` is read once at import. Restart after any change. Never commit it.

## 4. Commands

```powershell
uvicorn app.main:app --reload --port 8000

python -m pytest tests -q                                    # all tests (448 pass, 1 xfail)
python -m pytest tests -q -k wrong_amount                    # by name
python -m pytest tests/test_flow.py::test_happy_path_pickup  # one test
```

At startup the console prints whether the Anthropic key is present, how WhatsApp is
connected (por Chatwoot, directo, sin conexión, or FALTA …), whether the webhook signature is
verified, the wait before answering and the database (host:port/name), then starts the worker
thread. No connection to PostgreSQL → the app does not start and says why. During a
conversation it prints one line per tool call: `[bot ...1234] <tool> <args> -> <result>`.

Pages: `http://localhost:8000/kitchen` (also `/`), `/reports`. There is no simulator: the
customer side is a real phone. `/kitchen?alert=20` fires the unaccepted-order alert after 20 s.

## 5. Tests

- Three files: `tests/test_flow.py` (behaviour by area), `tests/test_review_bugs.py`
  (one or more tests per code-review defect R1–R12, `CURRENT-STATE.md` §3) and
  `tests/test_chatwoot.py` (Task D Part 2, below).
- `tests/conftest.py` runs first: it points `DATABASE_URL` at the same server with `_test`
  added to the database name (`TEST_DATABASE_URL` overrides it), creates that database if
  missing, and stops unless the name ends in `_test`. PostgreSQL must be running.
- An autouse fixture wipes every table between tests (`db.reset()`). conftest also blanks
  the four `CHATWOOT_*` settings, so `.env`'s Chatwoot is never reached.
- `test_chatwoot.py` fakes Chatwoot (`chatwoot._request`) and replays the step 0 recordings
  from `tests/fixtures/chatwoot/` (phone, names, WhatsApp ids and signed links replaced),
  signed like Chatwoot signs them, then runs `worker.run_once()` with
  `MESSAGE_WAIT_SECONDS=0`. It covers the signature, each event, grouping, the wait, order
  across phones, restarts, takeover (also mid-turn), handoff and "Ya lo atendí" with Chatwoot
  failing, the catch-up note, receipts while a person has it, downloads and the upgrade.
- No AI calls: tests call cart, order, payment, report and tool functions directly
  (`brain._run_tool`, `main.handle` with `extract_proof` and `reply` replaced), or replace
  the model with a scripted one (`_scripted`: fixed answers and tool calls, every call's
  arguments kept) to test whole turns. Notifications are read back from
  `outbound_messages` in FAKE mode. Some tests read `static/*.html` for panel strings.
- Expected amounts come from the config, so a price change fails tests on purpose.
- Areas: money; cart, line merge and targeting; state machine; payments and receipt
  checks; receipt dates; account matching; confirm, discard and cash; cash settling the
  method and ESTADO REAL; reports; modification windows; the removed preparation step;
  editing in place; handoffs and the `request_handoff` gate; "hablar con una persona";
  vocabulary; payment after an edit; one row per receipt; the clean-receipt reply;
  payment-method changes; final response text; notifications; C2 Part 2 (cash rows and
  the ✕, the method after a cash discard, the payment sentence, the end-of-turn payment
  check, "Cómo pagaron").
- Not covered: what the real AI decides (tool choice, wording). The Notion checklists and
  `TEST.md` test that by hand.
- Result on 2026-10-22 (Task E): 448 passed, 1 xfailed (the G16 expected failure
  in `tests/test_review_bugs.py`) in about 45 s; `tests/test_task_e.py` covers Task E; the warnings are `on_event` deprecations
  and one from Starlette's test client (G10).

## 6. Connecting real WhatsApp

Through Chatwoot (the normal way): `CHATWOOT-SETUP.md`. Without Chatwoot, straight to Meta:

1. Meta developer app → WhatsApp → API Setup: token and phone number id; App Settings →
   Basic: app secret. Put them in `.env`, restart.
2. Expose the laptop: `ngrok http 8000` (the free URL changes on every restart).
3. Meta → WhatsApp → Configuration → Webhook: callback `https://<ngrok-url>/webhook`,
   verify token, **Verify and save**, then subscribe to the `messages` field.
4. The panel dot turns green.

The temporary Meta token expires after 24 hours; a System User token lasts longer.

| Symptom | Cause |
|---|---|
| Nothing in the uvicorn console | Meta is not reaching the laptop: ngrok, URL, or `messages` not subscribed |
| `403` | Wrong `WHATSAPP_APP_SECRET` |
| Message arrives, no reply | Bad Anthropic key or no credit |
| Reply generated but never arrives | Wrong phone number id or expired token (`outbound_messages.error`) |
| "Perdón, se me cruzaron los cables" | The code raised; the traceback is in the console |

## 7. Before any demo

Click "Reiniciar demo" (wipes everything), run the flow once end to end, record it, and
have two receipts ready: a wrong one (any old screenshot) and a right one, which must be
a **new transfer made after confirming the order** (an older one fails the date check).
Full checklist and the 6-minute script: `run.md` Step 9 and "Checklist for demo day".
