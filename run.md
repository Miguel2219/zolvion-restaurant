# Running Zolvion — setup and demo day

Zolvion as it runs today: one process on your laptop, plus Chatwoot in Docker. The menu in
`config/restaurant.json` is Caseritas Burger, the test restaurant.

Do the steps in order. Do not skip step 4: if the tests and the panel work before
WhatsApp is connected, a later failure is the connection, not your code.

---

## Step 1 — Install (5 min)

Open **PowerShell** in this folder.

```powershell
cd C:\Users\PC\Desktop\zolvion-demo
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

PostgreSQL 17 must be running (Windows service `postgresql-x64-17`). The first time only,
create its login and database: the command is in `docs/BUILD-AND-RUN.md` §2.

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
chat model, they fail on payment questions. Leave every `WHATSAPP_*` and `CHATWOOT_*` line
empty for now: no message leaves.

## Step 3 — Start it

```powershell
uvicorn app.main:app --reload --port 8000
```

Leave this window open. The banner says whether the key is there, which models answer
the chat and read receipts, how WhatsApp is connected and whether the webhook signature is
checked.

## Step 4 — Check before connecting (5 min) — do not skip

`python -m pytest tests -q` → **448 passed, 1 xfailed**. Open
http://localhost:8000/kitchen (staff panel) and http://localhost:8000/reports (owner's
reports): both load, the panel says WhatsApp is not connected. There is no simulator: the
customer side is a real phone from Step 7. **If anything fails here, fix it now.**

## Steps 5–6 — Chatwoot and WhatsApp (30 min the first time)

Follow `docs/CHATWOOT-SETUP.md` §3–5: Chatwoot in Docker, ngrok pointed at Chatwoot (port
3000, not Zolvion), the WhatsApp inbox with a **permanent** Meta token (a temporary one dies
after 24 hours), Meta's webhook pointed at Chatwoot, the Agent Bot connected to the inbox,
and the four `CHATWOOT_*` values in `.env`. Then **stop uvicorn (Ctrl+C) and start it
again**: `.env` is only read at startup. The panel now says "WhatsApp por Chatwoot".

## Step 7 — Test with a real phone (10 min)

Write to the Claro business number from another phone; keep Chatwoot open beside the panel.

1. `Quiero dos caseritas maduritas y una gaseosa personal` → **$47.300** (21.900 × 2 + 3.500)
2. `¿Tienen pizza hawaiana?` → it says no, never invents it
3. `Para domicilio en Cabecera, calle 48 #34-20` → adds $5.000
4. `Sí, confirmo` → the order appears in **Nuevos** within 2 seconds
5. Aceptar → Listo → Sale a domicilio → Entregado: the messages reach the phone
6. Reply once from Chatwoot: the bot stops; set the conversation to Pending: it is back

Full checks: `TEST.md`. When something is off: `docs/CHATWOOT-SETUP.md` §8.

## Step 8 — Before the demo

Docker Desktop and Chatwoot running, ngrok on port 3000, the recorder
(`tools/record_chatwoot.py`) stopped: it uses port 8000 too.

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
- [ ] Chatwoot running in Docker; ngrok pointing at it (port 3000); the bot connected to the inbox
- [ ] Banner: "WhatsApp : por Chatwoot"; Chatwoot opened through the ngrok domain
- [ ] Sent yourself a test order end to end
- [ ] Wrong screenshot ready (any old one, or another amount)
- [ ] Correct receipt = a **new real transfer made after confirming the order**, for the
      exact amount. An old screenshot is red: it predates the order.
- [ ] Clicked "Reiniciar demo"
- [ ] **Recorded a full run as a video**: your backup, and your asset for the next client
- [ ] Laptop charger

## What it does NOT do yet

Say this if they ask. Honesty buys more credibility than the demo itself. Each one is added
when a client needs it (`docs/KNOWN-GAPS.md`).

- No server yet: one process on your laptop, plus Chatwoot in Docker.
- No timers: carts do not expire, unanswered orders do not die.
- Chat history is in memory: restarting uvicorn makes the bot forget the conversation.
  Orders and reports survive (PostgreSQL).
- No staff login, no multi-branch, no printer.
- Voice notes are transcribed only with `ELEVENLABS_API_KEY` in `.env`; without it the bot
  asks the customer to write. Try a few real notes before the demo (accents, noise: G45).
- The 10-minute "unaccepted order" alert will not fire in a 6-minute demo. To show it,
  open `/kitchen?alert=20`: it fires after 20 seconds.

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
