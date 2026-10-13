# Caseritas demo — step by step

This is the MVP of Zolvion Restaurant, not the full architecture: that design (queue,
worker, Postgres, Terraform) lives in `zolvion-restaurant`. Never copy code from here
into that repo.

Do the steps in order. Do not skip step 4: testing offline first means that if
something breaks later, you know it is WhatsApp and not your code.

---

## Step 1 — Install (5 min)

Open **PowerShell** in this folder.

```powershell
cd C:\Users\PC\Desktop\zolvion-demo
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

The virtualenv on this laptop is `venv/` (it already exists; skip `python -m venv`).
If PowerShell blocks the activate script, run once:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

## Step 2 — Anthropic key (2 min)

```powershell
copy .env.example .env
notepad .env
```

Put `ANTHROPIC_API_KEY=sk-ant-...` and save. Keep `ANTHROPIC_MODEL=claude-haiku-5-5` and
`ANTHROPIC_PROOF_MODEL=claude-sonnet-5-5` as they come: Sonnet 5.5 and Opus 5.5 can't be the
chat model, they fail on payment questions. Leave every `WHATSAPP_*` line empty for now.
Empty = offline mode.

## Step 3 — Start it

```powershell
uvicorn app.main:app --reload --port 8000
```

Leave this window open. The banner says whether the key is there, which models answer
the chat and read receipts, whether WhatsApp is connected and whether the webhook
signature is checked.

## Step 4 — Test offline first (10 min) — do not skip

| Tab | What it is |
|---|---|
| http://localhost:8000/simulator | Fake WhatsApp: you type as the customer |
| http://localhost:8000/kitchen | Staff panel |
| http://localhost:8000/reports | Owner's reports |

1. `Quiero dos caseritas maduritas y una gaseosa personal` → **$47.300** (21.900 × 2 + 3.500)
2. `¿Tienen pizza hawaiana?` → it says no, never invents it
3. `Para domicilio en Cabecera, calle 48 #34-20` → adds $5.000
4. `Sí, confirmo` → the order appears in **Nuevos** within 2 seconds
5. Aceptar → Listo → Sale a domicilio → Entregado: the messages appear in the simulator
6. Reports → sales, top item, payment split

Then `python -m pytest tests -q` → **310 passed, 1 xfailed**. Full checks: `TEST.md`.
**If anything fails here, fix it now.** Do not move on to WhatsApp.

## Step 5 — Connect WhatsApp Cloud API (15 min)

<https://developers.facebook.com> → your App → **WhatsApp → API Setup**:

| In Meta | Into `.env` |
|---|---|
| Temporary access token | `WHATSAPP_TOKEN` |
| Phone number ID (under your Claro number) | `WHATSAPP_PHONE_NUMBER_ID` |
| App Settings → Basic → App Secret (Show) | `WHATSAPP_APP_SECRET` |

Also `WHATSAPP_VERIFY_TOKEN=zolvion-demo` (any word, just remember it). Save, then
**stop uvicorn (Ctrl+C) and start it again**: `.env` is only read at startup.

> The temporary token dies after 24 hours. Generate a fresh one the **morning of the
> demo**. For a longer life, create a System User token in Meta Business Settings.

## Step 6 — Put your laptop on the internet (5 min)

In a **second** PowerShell window: `ngrok http 8000`. Copy the
`https://....ngrok-free.app` line. **Do not close it and do not restart ngrok**: the free
URL changes every time, and then you redo step 7.

## Step 7 — Point Meta at your laptop (5 min)

Meta → **WhatsApp → Configuration → Webhook → Edit**:

- Callback URL: `https://YOUR-NGROK-URL.ngrok-free.app/webhook`
- Verify token: `zolvion-demo`, then **Verify and save** (must go green; if not,
  uvicorn is not running or the URL is wrong)
- **Manage** → subscribe to the **messages** field. Everyone forgets this step.

Reload the kitchen tab: green dot = WhatsApp connected; grey = offline mode.

## Step 8 — Test with a real phone (5 min)

Write to the Claro business number from another phone and order something.

| Symptom | Cause |
|---|---|
| Nothing in the uvicorn window | Meta is not reaching you: webhook URL, ngrok, or `messages` not subscribed |
| `403` in the uvicorn window | Wrong `WHATSAPP_APP_SECRET` |
| Message arrives, no reply | Bad `ANTHROPIC_API_KEY` or no credit |
| Reply fails silently | Token expired, or wrong `WHATSAPP_PHONE_NUMBER_ID` (see `outbound_messages.error`) |
| "Perdón, se me cruzaron los cables" | The code threw; the traceback is in the uvicorn window |

## Step 9 — The demo (6 minutes)

Click **Reiniciar demo** first so reports start clean. Panel on your laptop, big.
Your cousin writes from his own phone.

**Beat 1 — ordering (2 min).** Let him type freely, then:
- *"Quiero dos caseritas maduritas y una gaseosa"* → a total. Say: **"Ese total no lo
  escribió la inteligencia artificial. Lo calculó el sistema contra tu carta. La IA sólo
  entiende lo que el cliente quiere decir."**
- *"Mejor una sin cebolla y con queso cheddar"* → same order, recalculated.
- *"¿Tienen pizza hawaiana?"* → no. Say: **"Un bot normal se la inventa. Este no puede:
  si no está en tu carta, no existe."**

**Beat 2 — payment (1.5 min).** Delivery, confirm, transfer. Send the **wrong**
screenshot first → its row on the card turns red with the reason. Say: **"Nadie tiene
que revisar comprobante por comprobante. El sistema te avisa sólo cuando algo no cuadra.
Y nunca da un pago por bueno solo: eso lo confirma una persona del local."** Then the
correct one → a clean row "Comprobante recibido"; press **Confirmar** on it.

**Beat 3 — kitchen (1.5 min).** Aceptar → Listo. His phone buzzes each time. Say:
**"Tu cocina sólo toca botones. El cliente se entera solo. Nadie escribe 'ya salió'
cincuenta veces al día."**

**Beat 4 — reports (1 min).** Say: **"Esto es lo que no te da un chatbot: al final del
día sabés cuánto vendiste, qué se vendió más, y cuántos pedidos se cayeron."** Then stop
talking and let him ask.

**If he asks the price:** you are finishing the first version and want one restaurant to
launch it with, at a special price, in exchange for feedback. No unconsidered number.

## Step 10 — Use it for the next client

Edit `config/restaurant.json` (menu, prices, zones, accounts, hours, bot personality),
then **restart**. No code changes: that file is the product (`docs/CONFIG.md`).

## Checklist for demo day

- [ ] Prices in `config/restaurant.json` verified with your cousin
- [ ] The Nequi account in the config is the restaurant's, not your test account
- [ ] Fresh Meta token generated **today**
- [ ] ngrok running, webhook verified green, subscribed to `messages`
- [ ] Sent yourself a test order end to end
- [ ] Wrong screenshot ready (any old one, or another amount)
- [ ] Correct receipt = a **new real transfer made after confirming the order**, for the
      exact amount. An old screenshot is red: it predates the order.
- [ ] Clicked "Reiniciar demo"
- [ ] **Recorded a full run as a video**: your backup, and your asset for the next client
- [ ] Laptop charger

## What this demo does NOT do

Say this if he asks. Honesty buys more credibility than the demo itself.

- No queue, no retries, no AWS. One process on your laptop.
- No timers: carts do not expire, unanswered orders do not die.
- Chat history is in memory: restarting uvicorn makes the bot forget the conversation.
  Orders and reports survive (SQLite).
- No staff login, no multi-branch, no printer.
- The 10-minute "unaccepted order" alert will not fire in a 6-minute demo. To show it,
  open `/kitchen?alert=20`: it fires after 20 seconds.

The full design for all of that is `zolvion-restaurant`.

## Where the menu came from

`config/restaurant.json` was built from the Caseritas source in NotebookLM
(`WhatsApp Image 2026-09-18 ... .pdf`): 4 burgers, 6 drinks, 8 modifiers.

**Check before the demo** (read off a marketing image by another model):
- The four burger prices: 18.900 / 21.900 / 24.900 / 29.900
- That "Caserita Especial" ($29.900) is the chicken one and "Caserita Doble con Pechuga"
  ($24.900) is beef + shredded chicken. That ordering looks odd.

**Invented test values** (the source has none):
- Delivery zones and fees (Cabecera $5.000 … Girón $9.000; unknown zone $6.000)
- Opening hours (5:00 p.m. – 11:00 p.m.)
- The Nequi account is Miguel's own, set up to test with real receipts
