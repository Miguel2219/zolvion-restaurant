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
| Database | SQLite (standard library), WAL mode | — |
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

The virtualenv folder is `venv/` (ignored by git). If PowerShell blocks activation:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

## 3. Environment (`.env`)

| Variable | Meaning |
|---|---|
| `ANTHROPIC_API_KEY` | Required. Missing → the startup banner says "FALTA" and the bot answers "sistema en mantenimiento" |
| `ANTHROPIC_MODEL` | Optional. Empty → the first available sonnet, then opus, then haiku |
| `WHATSAPP_TOKEN` | Empty → **FAKE mode**: nothing goes to WhatsApp, replies are only stored for the simulator |
| `WHATSAPP_PHONE_NUMBER_ID` | The sending number's id in Meta (live mode needs both this and the token) |
| `WHATSAPP_APP_SECRET` | Checks Meta's webhook signature. Empty → the check is skipped |
| `WHATSAPP_VERIFY_TOKEN` | Word for Meta's webhook handshake (default `zolvion-demo`) |
| `RESTAURANT_CONFIG` | Config path (default `config/restaurant.json`) |
| `DEMO_DB` | Database path (default `demo.sqlite3`) |

`.env` is read once at import. Restart after any change. Never commit it.

## 4. Commands

```powershell
uvicorn app.main:app --reload --port 8000

python -m pytest tests -q                                    # all tests (263 pass, 1 xfail)
python -m pytest tests -q -k wrong_amount                    # by name
python -m pytest tests/test_flow.py::test_happy_path_pickup  # one test
```

At startup the console prints whether the Anthropic key is present, whether WhatsApp is
live or in simulator mode, and whether the webhook signature is verified. During a
conversation it prints one line per tool call: `[bot ...1234] <tool> <args> -> <result>`.

Pages: `http://localhost:8000/simulator`, `/kitchen` (also `/`), `/reports`.
`/kitchen?alert=20` fires the unaccepted-order alert after 20 s instead of 600 s.

## 5. Tests

- Two files: `tests/test_flow.py` (behaviour by area) and `tests/test_review_bugs.py`
  (one or more tests per code-review defect R1–R12, `CURRENT-STATE.md` §3). Each sets
  `DEMO_DB` to a temporary file **before** importing `app`; the database path is fixed at
  import time. `test_review_bugs.py` refuses to run against the real `demo.sqlite3`.
- An autouse fixture wipes every table between tests.
- No AI calls: tests call cart, order, payment, report and tool functions directly
  (`brain._run_tool`, `main.handle` with `extract_proof` and `reply` replaced), and read
  notifications back from `outbound_messages` in FAKE mode. Some tests read
  `static/kitchen.html` to check panel strings.
- Expected amounts come from the config, so a price change fails tests on purpose.
- Areas: money; cart, line merge and targeting; state machine; payments and receipt
  checks; receipt dates; account matching; confirm, discard and cash; cash settling the
  method and ESTADO REAL; reports; modification windows; the removed preparation step;
  editing in place; handoffs and the `request_handoff` gate; "hablar con una persona";
  vocabulary; payment after an edit; one row per receipt; the clean-receipt reply;
  payment-method changes; final response text; notifications.
- Not covered: what the AI decides (tool choice, wording). The Notion checklists and
  `TEST.md` test that by hand.
- Result on 2026-10-07: 263 passed, 1 xfailed (the G16 expected failure in
  `tests/test_review_bugs.py`), 3 deprecation warnings (two from `on_event`, one from
  Starlette's test client).

## 6. Connecting real WhatsApp

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
