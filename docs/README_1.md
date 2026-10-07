# Zolvion Restaurant — MVP

Start here. This file says what the system is, why it exists, and which document answers
which question.

Snapshot these docs describe: code last changed **2026-10-04** (local folder
`C:\Users\PC\Desktop\zolvion-demo`, git branch `bugfix/notion-bugs`), checked on
**2026-10-07** with 263 automated tests passing and 1 expected failure (G16); Notion task
checklists A, B, C and C2 Part 1 all ✅. Anything newer is not reflected here until the
docs are updated.

---

## 1. What the system is

A WhatsApp ordering system for restaurants.

1. A customer writes to the restaurant's WhatsApp number in normal language.
2. An AI assistant (Claude, using tools) understands what they want and builds the order.
   Code, never the AI, looks up every price and computes every total.
3. The customer confirms. The order appears on a live panel in the restaurant. Until
   staff accept it, the customer can still change it; after that, changes go to a person.
4. Staff move the order forward with buttons. The customer is told automatically at the
   moments that matter (accepted, ready, on the way).
5. If the customer pays by transfer, they send the receipt screenshot. The AI only reads
   it; code checks it; a person confirms or discards it. The bot never confirms a payment.
6. The owner sees sales reports for the day, the week or the month.

Everything a customer or staff member reads is Colombian Spanish, addressing the
customer as *usted*.

## 2. Why it exists

Miguel sells this as a SaaS to restaurants. This MVP is what gets demoed to restaurant
owners and, once it runs reliably, what the first restaurants use.

- First prospect: **Caseritas Burger**, a burger restaurant in Bucaramanga owned by a
  family member. The current menu config is theirs.
- A new restaurant is a new config file, not a code change (`CONFIG.md`).
- The demo's behaviours are default rules; each business will adjust them through its
  own configuration.

## 3. This project vs. `zolvion-restaurant`

| | `zolvion-restaurant` | This MVP (`zolvion-demo` folder) |
|---|---|---|
| What it is | Full target architecture: SQS FIFO, ECS Fargate, RDS Postgres, Terraform, React kitchen display | One FastAPI process, SQLite, static HTML panels, Claude with tool use |
| Status | Documented design, not being built as part of this MVP | **The product.** This is the system being sold and completed |
| Reference for behaviour | No: its rules are not the reference here | Yes: decisions taken here are final for the MVP |

Consequences:

- Do not import rules, states or architecture from `zolvion-restaurant` unless Miguel
  asks. Example: that design cancels and recreates an order on a change; this MVP edits
  the same order in place. Never copy code from here into that repo.
- The repo's `CLAUDE.md` and `run.md` now describe the code as the MVP. Only the
  `app/main.py` docstring still calls it "a convincing six-minute demo" (code, not docs:
  `KNOWN-GAPS.md` G9).
- Code comments cite documents that only exist in `zolvion-restaurant` (`payments.md`,
  `order-modification.md`, "R3", "R9", "Invariant 6", …) and task-plan labels (Q1–Q3,
  D3). `rules.md` §4 explains each one so nobody has to go looking for them.

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
| Payment methods, receipts, checks, confirm/discard, cash | `payments.md` |
| The restaurant config file and onboarding a new restaurant | `CONFIG.md` |
| Stack, setup, commands, tests, connecting WhatsApp | `BUILD-AND-RUN.md` |
| Rules the code must never break; conventions; old references | `rules.md` |
| What stands between today's code and a production MVP | `KNOWN-GAPS.md` |
| How the work is organised (tasks, Notion, Claude vs Claude Code) | `WORKFLOW.md` |
| Text to paste into the Claude project settings | `PROJECT-INSTRUCTIONS.md` |

In the repo root: `CLAUDE.md` (Claude Code's guide), `run.md` (setup and demo-day
script), `TEST.md` (manual regression script).

## 6. Links

- Notion: "Zolvion - Demo Documentation" —
  https://app.notion.com/p/Zolvion-Demo-Documentation-3e4ee8d8c287804dbe69f9c25fa115ff
  › Tasks (Task A, B, C Plan; Bug Check; Task C2 Plan Part 1) —
  https://app.notion.com/p/3e4ee8d8c28780c9a1a8fc08b4d861f9
- Full-architecture repo (not this MVP): https://github.com/Miguel2219/zolvion-restaurant
- The MVP folder is a local git repository with no remote yet (`KNOWN-GAPS.md` G7).
