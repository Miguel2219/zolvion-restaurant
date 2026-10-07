# Workflow

How work on the MVP is organised, who does what, and how a task goes from idea to
"done".

---

## 1. Who does what

| Who | Role |
|---|---|
| Miguel | Decides what gets built and in what order. Tests every change by hand with the running system and real payment receipts. Fills in the verdicts |
| Claude (the Claude project, in chat) | Turns Miguel's decisions into specifications and documentation. Explains options and trade-offs. **Does not build** |
| Claude Code (in the repo) | Builds, runs the automated tests, investigates bugs in the code and database, reports back, keeps `CLAUDE.md`, `run.md` and `TEST.md` current |

## 2. Lettered tasks

Work is split into tasks named by letter. Done: A (payments), B (editing orders and
handoffs), C (one row per receipt), C2 Part 1 (fixes from the 1 Oct code review).
Planned: C2 Part 2 (a "Pagos" block), D (Chatwoot) and a notifications bell
(`CURRENT-STATE.md` §2).

A code review can start a task: Claude Code reviews the code, Miguel reproduces each
finding by hand on a "Bug Check" page, and the findings become the task's rows (C2).

A task moves through these steps:

1. **Decision.** Miguel says what the task must achieve.
2. **Specification.** Written in the Claude project, for Claude Code to execute against.
3. **Build.** Claude Code implements it and makes the automated tests pass. Its plan file
   is attached to the task's Notion page.
4. **Manual test.** Miguel works through the Notion checklist.
5. **Fix round.** Rows marked ❌, or bugs found while testing (named by order code, e.g.
   P-2465), go back to Claude Code with Miguel's notes. Claude Code checks the cause in
   the code and database before changing anything, then adds a test for it. New rows
   cover behaviour that came with the fix (Task B had two fix rounds).
6. **Re-run.** After a fix round, run the failed rows again, plus the passed rows the fix
   could break. Write the result in Notes as "Re-run <date>: …" under the first result,
   never over it. A row about what the bot says closes only when the bot's real answer
   matches; passing automated tests alone do not close it.
7. **Close.** Every row ✅, then `CURRENT-STATE.md` and any affected document are updated.

## 3. The Notion checklist

Location: Notion › "Zolvion - Demo Documentation" › Tasks › "Task X Plan"
(https://app.notion.com/p/3e4ee8d8c28780c9a1a8fc08b4d861f9).

Each page has a short intro (what changes, known gaps not fixed by the task,
preparation) and a table:

| Column | Content |
|---|---|
| Box | Empty until tested. ✅ passed, ❌ failed |
| Description | One observable behaviour, written so a person can check it on the panel, the simulator or a phone |
| Notes | How to test it, or what was observed (order code, message sent, what went wrong, "before the fix") |

Rules for writing rows:

- One behaviour per row. Something Miguel can see, not an internal detail.
- Name the panel column, the button and the exact customer message where they matter
  ("the same card in Nuevos shows 'Actualizado HH:MM'").
- Include an example message to type when it helps ("Agrégueme una gaseosa").
- If a row needs special material, say so (e.g. a new real transfer for an exact amount,
  made after the order is confirmed).
- A known gap that the task does not fix is stated in the intro, not as a row.
- A behaviour nobody can see on screen gets no row; the intro names it under "checked
  only by automated tests" (C2 Part 1: R3b, R5, R8, R11).
- End with a row "All automated tests pass", noting the count (Task B: 147; Task C: 210;
  Task C2 Part 1: 263).

## 4. Specifications for Claude Code

A specification is something Claude Code executes against, not an essay:

- What must change, stated as behaviour.
- Which rules from `rules.md` apply and must stay true.
- Acceptance criteria that map one-to-one to the Notion checklist rows.
- What is explicitly out of scope for the task.
- Nothing invented: if a fact about the code is not known, say so and ask Claude Code to
  verify it in the code before assuming.

## 5. Documentation rules

- All documentation in English; customer- and staff-facing text quoted in Spanish.
- Every document under 200 lines. Split into more, smaller files instead of growing one
  (Claude Code reads short files better). This applies to `CLAUDE.md`, `run.md` and
  `TEST.md` too.
- The code is the source of truth. When a document and the code disagree, check the
  code and fix the document.
- When a task closes, update `CURRENT-STATE.md` (task table and open defects) and any
  document whose behaviour changed. `TEST.md` keeps the regression checks short; the
  full rows and verdicts stay in Notion.

## 6. Talking with Miguel

- Plain language, no technical jargon in explanations. His English is limited; the
  documentation stays in English, but explanations to him are simple.
- Give a recommendation when asked for one, with the reason.
- Say "I don't know" rather than fill a gap with a guess.
