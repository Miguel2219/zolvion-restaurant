# Zolvion Restaurant

Start here. This file says what the system is, why it exists, and which document answers
which question.

Snapshot these docs describe: code last changed **2026-10-21**, Task E (changes after
acceptance, the staff's Editar, notices, photos, voice notes, stickers, location; local folder
`C:\Users\PC\Desktop\zolvion-demo`, git branch `feature/task-e`, on top of Task D merged
into `main`), with 448 automated tests passing and 1 expected failure (G16); Notion task
checklists A, B, C, C2 Part 1, C2 Part 2 and D Part 2 all ✅; Task E's not run yet.
Anything newer is not reflected here until the docs are updated.

---

## 1. What the system is

A WhatsApp ordering system for restaurants.

1. A customer writes to the restaurant's WhatsApp number in normal language. The number
   lives in Chatwoot, where staff see every conversation and can take any of them over.
2. An AI assistant (Claude, using tools) understands what they want and builds the order.
   Code, never the AI, looks up every price and computes every total.
3. The customer confirms. The order appears on a live panel in the restaurant. Until
   staff accept it, the customer can still change it; after that, changes go to a person.
4. Staff move the order forward with buttons. The customer is told automatically at the
   moments that matter (accepted, ready, on the way).
5. If the customer pays by transfer, they send the receipt screenshot. The AI only reads
   it; code checks it; a person confirms or discards it. Cash staff collect is registered
   on the card. What the bot says about money is a sentence code writes; the bot never
   confirms a payment.
6. The owner sees sales reports for the day, the week or the month.

Everything a customer or staff member reads is Colombian Spanish, addressing the
customer as *usted*.

## 2. Why it exists

Miguel sells this as a SaaS to restaurants. It is the product, not a demo: what Miguel
shows restaurant owners and what the first restaurants use. What is still missing
(`KNOWN-GAPS.md`) is added when a client needs it or asks for it.

- Launch city: **Ocaña** (Norte de Santander).
- Test restaurant: **Caseritas Burger**, a burger restaurant in Bucaramanga owned by a
  family member. The current menu config is theirs; it is not a prospect.
- A new restaurant is a new config file, not a code change (`CONFIG.md`).
- The demo's behaviours are default rules; each business will adjust them through its
  own configuration.

## 3. Name and history

- The code lives on GitHub in `Miguel2219/zolvion-restaurant`; the local folder keeps its
  old name, `zolvion-demo`.
- An earlier design (SQS queues, AWS ECS, Terraform, a React panel) was dropped on
  2026-10-21. It is not a reference: decisions taken here are final. Do not bring back its
  states or rules (for example `IN_PREPARATION`, or cancel-and-recreate on an edit) unless
  Miguel asks.
- Code comments still cite that design's document names ("R3", "R9", "Invariant 6",
  `order-modification.md`, …) and task-plan labels (Q1–Q3, D3). `rules.md` §4 explains
  each one; the documents themselves no longer exist.

## 4. Source of truth

When sources disagree, in this order:

1. The code in the repo.
2. The Notion task checklists (what Miguel tested by hand, with real receipts).
3. These documents.

If a document is wrong, fix the document.

## 5. Document map

| Question | Document |
|---|---|
| What works today, what is broken, what is planned? | `CURRENT-STATE.md` |
| How is the code organised and how does a message flow? | `ARCHITECTURE.md` |
| Order states, panel buttons, changing an order, handoffs, notifications | `ORDER-LIFECYCLE.md` |
| Payment methods, receipts, checks, confirm/discard, cash rows, the payment sentence, "Cómo pagaron" | `payments.md` |
| The restaurant config file and onboarding a new restaurant | `CONFIG.md` |
| Stack, setup, commands, tests, connecting WhatsApp | `BUILD-AND-RUN.md` |
| Chatwoot: where it runs, the inbox, the Agent Bot, `.env`, how staff use it | `CHATWOOT-SETUP.md` |
| Rules the code must never break; conventions; old references | `rules.md` |
| What stands between today's code and a production MVP | `KNOWN-GAPS.md` |
| How the work is organised (tasks, Notion, Claude vs Claude Code) | `WORKFLOW.md` |
| Text to paste into the Claude project settings | `PROJECT-INSTRUCTIONS.md` |

In the repo root: `CLAUDE.md` (Claude Code's guide), `run.md` (setup and demo-day
script), `TEST.md` (manual regression script).

## 6. Links

- Notion: "Zolvion - Demo Documentation" —
  https://app.notion.com/p/Zolvion-Demo-Documentation-3e4ee8d8c287804dbe69f9c25fa115ff
  › Tasks (Task A, B, C Plan; Bug Check; Task C2 Plan Parts 1 and 2; Task D Plan Part 2;
  Task E Plan) —
  https://app.notion.com/p/3e4ee8d8c28780c9a1a8fc08b4d861f9
- Code: https://github.com/Miguel2219/zolvion-restaurant (this folder's `origin`; Task D
  was merged there as PR #2, 2026-10-18). The database has no backup (`KNOWN-GAPS.md` G7).
