# Chatwoot setup (Task D Part 2)

The restaurant's WhatsApp number lives in a Chatwoot inbox, and Zolvion is that inbox's
**Agent Bot**. Customers and staff use WhatsApp and Chatwoot; orders stay in the kitchen panel.

- **Pending** (Chatwoot's "Pending" tab): the bot answers.
- **Open**: a person has the conversation and the bot stays quiet. Order notifications
  (aceptado, listo, en camino, rechazo, pagos) still go out.
- The status decides who answers, not the assignee.

How it works inside: `CLAUDE.md` (Chatwoot section) and `docs/ARCHITECTURE.md`.

## 1. Choose where Chatwoot runs

| | Chatwoot Cloud | Self-hosted (Docker) |
|---|---|---|
| WhatsApp inbox | Paid plan only (the free Hacker plan has none, no trial) | Yes, Community Edition |
| `CHATWOOT_URL` | `https://app.chatwoot.com` | e.g. `http://localhost:3000` |
| Agent Bot webhook | a public address for Zolvion | `http://host.docker.internal:8000/chatwoot/webhook` |
| Used for | a paying restaurant | tests on Miguel's laptop (now) |

Zolvion reads everything from `.env`; nothing points at Chatwoot Cloud by default.

## 2. Choose the WhatsApp inbox

- **Pure Cloud API**: a number registered only on the WhatsApp Business Platform. Tested in
  step 0 (Cloud and self-hosted).
- **Coexistence**: the restaurant keeps its WhatsApp Business app on the same number.
  Onboarding goes through Embedded Signup by a Meta Tech Provider (Chatwoot Cloud is one).
  A reply typed in the phone app reaches Zolvion as an echo and counts as a person taking
  over. Not tested live yet (`KNOWN-GAPS.md` G35), and WhatsApp for Windows is unlinked.

## 3. Self-hosted Chatwoot on the laptop

Folder `C:\Users\PC\chatwoot` (not in this repo): Chatwoot's official
`docker-compose.production.yaml` and `.env.example`, renamed to `docker-compose.yaml` and
`.env`. Changes made in step 0:

- PostgreSQL and Redis publish no port: 5432 is taken by the PostgreSQL 17 service Zolvion
  uses. Only `rails` is published, on `127.0.0.1:3000`.
- `.env`: generated `SECRET_KEY_BASE`, `POSTGRES_PASSWORD`, `REDIS_PASSWORD`;
  `FRONTEND_URL=https://<your ngrok domain>`; `RAILS_ENV=production`;
  `ENABLE_ACCOUNT_SIGNUP=false`; `FORCE_SSL=false`;
  **`SAFE_FETCH_ALLOW_PRIVATE_NETWORK=true`** (without it Chatwoot refuses to call a bot on a
  private address such as `host.docker.internal`).

With Docker Desktop running, in that folder:

```powershell
docker compose run --rm rails bundle exec rails db:chatwoot_prepare
docker compose up -d
```

It needs about 2–3 GB of RAM. Close browser tabs if the laptop is short.

**ngrok** points at Chatwoot, not at Zolvion: Meta must reach Chatwoot.

```powershell
ngrok http --url=<your-domain>.ngrok-free.dev 3000
```

With free ngrok, **open Chatwoot through the ngrok domain**, not `localhost`. A browser
otherwise gets ngrok's warning page instead of the customer's photos ("image no longer
available"). Click "Visit Site" once.

## 4. Inside Chatwoot

1. **Admin account** (first visit). The address then reads `…/app/accounts/<id>/…`: that
   number is `CHATWOOT_ACCOUNT_ID`.
2. **Inbox**: Settings › Inboxes › Add Inbox › WhatsApp › provider **WhatsApp Cloud**: inbox
   name, phone number (`+57…`), Phone number ID, Business Account ID, API key (permanent
   token). Add yourself as agent. Copy the inbox's **Webhook URL** and **verify token**
   (Configuration tab).
3. **Meta webhook**: developers.facebook.com › your app › WhatsApp › Configuration › Webhook ›
   Edit: paste both, **Verify and save**, subscribe to **messages**. If Meta can't verify,
   ngrok is not on port 3000. This disconnects any other Chatwoot from the number.
4. **Agent Bot**: Settings › Bots › Add Bot. Name "Zolvion", Webhook URL from §1. Right after
   creating it a box shows the **Webhook Secret** (also in the bot's settings later): that
   is `CHATWOOT_BOT_SECRET`. The bot's **Access Token** is `CHATWOOT_BOT_TOKEN`.
5. **Connect the bot**: Settings › Inboxes › your inbox › Bot Configuration › Zolvion ›
   Update. Do it before the first customer message: conversations created earlier start
   Open, and the bot never answers them.
6. Staff need both tabs: Chatwoot's list shows **Open** by default, and conversations the
   bot holds are under **Pending**.

## 5. Zolvion's `.env`

```
CHATWOOT_URL=http://localhost:3000
CHATWOOT_ACCOUNT_ID=1
CHATWOOT_BOT_TOKEN=<access token>
CHATWOOT_BOT_SECRET=<webhook secret>
MESSAGE_WAIT_SECONDS=4
```

All four `CHATWOOT_*` set = Chatwoot mode: Meta's `/webhook` in Zolvion answers 404 and the
`WHATSAPP_*` values are not used. Some missing = the banner and the panel say «FALTA …» and
every webhook is refused (503). `MESSAGE_WAIT_SECONDS`: how long the bot waits after a
customer's last message, so several messages in a row get one answer.

Start Zolvion (`uvicorn app.main:app --reload --port 8000`). The banner shows
`WhatsApp : por Chatwoot (…)` and `Firma webhook : verificando (secreto del bot de Chatwoot)`.
Port 8000 is also the step 0 recorder's (`tools/record_chatwoot.py`): stop it first.

## 6. Day to day, in Chatwoot

| Staff want to | They do | Zolvion |
|---|---|---|
| take a conversation | open it, or just reply on a Pending one | stops answering; a reply on Pending moves it to Open |
| give it back to the bot | set it to Pending, or press "Ya lo atendí" on the card | answers again, after reading what was said meanwhile |
| close it | Resolve | the next customer message comes back Pending |

- When the bot hands a case to a person (the amber box), Chatwoot gets a private note with the
  summary and the order code, and the conversation moves to Open. If Chatwoot fails, the card
  says «Chatwoot sin actualizar» and Zolvion retries every 30 s; the bot keeps answering
  until then.
- "Ya lo atendí" first moves the conversation to Pending; if Chatwoot fails the box stays
  and the panel says so.
- A receipt photo sent while a person has the conversation is still checked: it appears on the
  card, and Chatwoot gets a private note with the result. Confirming stays in the panel.
- Each card has **ver conversación**, which opens the conversation in Chatwoot.
- After a resolve, Chatwoot Cloud starts a new conversation; a self-hosted inbox with
  "lock to single conversation" reopens the same one. Zolvion follows either.

## 7. Check it with a phone

1. From a phone, write "Hola" to the restaurant's number: the conversation appears under
   Pending and the bot answers within a few seconds.
2. Send three messages in a row: one answer.
3. Reply from Chatwoot: the conversation moves to Open; write again from the phone: no bot.
4. Set it to Pending: the bot answers again without repeating what the person said.
5. Stop Zolvion and write from the phone: Chatwoot opens the conversation with "…due to an
   error with the agent bot". A down Zolvion leaves the customer with staff, never with no one.

The full hand-test list is the Notion page "Task D Plan (Part 2: WhatsApp + Chatwoot)".

## 8. When something is off

| Symptom | Cause |
|---|---|
| Conversations open by themselves, "error with the agent bot" | Zolvion was down, slow (over 5 s) or answered an error. Chatwoot never resends that message |
| `401` in the uvicorn window | `CHATWOOT_BOT_SECRET` is not the bot's secret, or the clocks differ by more than 5 minutes |
| `503` in the uvicorn window | a `CHATWOOT_*` setting is missing (see the banner) |
| The bot never answers a conversation | it is Open (or was created before the bot was connected) |
| Photos say "image no longer available" in Chatwoot | Chatwoot opened through `localhost` with free ngrok: use the ngrok domain |
| Meta can't verify the webhook | ngrok is not pointing at port 3000 |
