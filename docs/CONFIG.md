# Restaurant config

`config/restaurant.json` is the product's per-client part: menu, prices, modifiers,
delivery zones, hours, payment accounts and the bot's personality. A new restaurant is
a new config file, not a code change.

Path: `RESTAURANT_CONFIG` env var, default `config/restaurant.json`.

---

## 1. Structure

| Key | Contents | Used by code? |
|---|---|---|
| `business.name`, `short_name` | Panel titles, simulator header, startup banner | Yes |
| `business.city`, `currency` | Descriptive | Not read by code |
| `business.timezone` | `America/Bogota`. "Today" in reports and receipt dates. Falls back to UTC−5 if timezone data is missing (Windows without `tzdata`) | Yes |
| `business.persona` | First paragraph of the bot's system prompt: tone, *usted*, what comes with every burger | Yes (prompt) |
| `hours.open`, `hours.close` | Written into the system prompt | Prompt only, **not enforced** |
| `hours.open_days`, `hours.closed_message` | — | Not read by code |
| `fulfilment.pickup`, `fulfilment.delivery` | On/off flags | Not read by code |
| `fulfilment.default_delivery_fee` | Fee for a zone not in the list | Yes |
| `fulfilment.delivery_zones[]` | `{name, fee}`; matched folded (no accents/case), exact first, then partial | Yes (fee + prompt) |
| `payments.methods` | Allowed of `CASH`, `TRANSFER`, `MIXED`; checked by `set_method` | Yes |
| `payments.proof_clock_skew_minutes` | Receipt date tolerance (default 2) | Yes |
| `payments.proof_notice_minutes` | Receipt age that earns a grey note (default 5) | Yes |
| `payments.accounts[]` | `{provider, number, holder}`: shown to customers, used to verify receipt destinations | Yes |
| `menu.categories[].items[]` | `{id, name, price, available, description}` | Yes |
| `menu.modifiers[]` | `{id, name, price, applies_to: [category ids]}`; price 0 for removals | Yes |

Prices are integers in pesos. The prompt lists each item as
`id | name | price COP | description`, with `[AGOTADO HOY]` when unavailable.

"Not enforced / not read" means the key exists but no code path checks it
(`KNOWN-GAPS.md` G18–G19).

Note on `payments.methods`: the customer can only choose a listed method, but registering
cash writes `CASH` even when it is not listed (staff already hold the money); discarding
the last cash row takes that `CASH` back (`payments.md` §1). Later edits of that order
still work (`CURRENT-STATE.md` R7, fixed in C2 Part 1 and checked by hand on 2026-10-06
with `CASH` removed).

## 2. Current content (Caseritas Burger)

- 4 burgers: Caserita $18.900, Caserita Madurita $21.900, Caserita Doble con Pechuga
  $24.900, Caserita Especial $29.900. Every burger includes fries and house sauce.
- 6 drinks: gaseosa personal $3.500, mini $2.500, litro $7.000, soda personal $3.500,
  cerveza nacional $5.000, premium $7.000.
- 8 modifiers (burgers only): tarro de salsa $1.000, queso $2.000, huevo $2.000, queso
  cheddar $3.000, jalapeños $4.000, sin cebolla / sin tomate / cebolla grille $0.
- 6 delivery zones: Cabecera $5.000, Sotomayor $5.000, Provenza $6.000, Centro $6.000,
  Floridablanca $8.000, Girón $9.000; any other zone $6.000.
- Hours 17:00–23:00, every day. Methods: CASH, TRANSFER, MIXED. Skew 2 min, notice 5 min.
- One Nequi account: Miguel's own, set up for testing with real receipts. It must be
  replaced with the restaurant's accounts before real use.

Values still to confirm with the restaurant (`run.md`, `TEST.md`):

- Burger prices were read from a marketing image by another AI: unverified. "Especial"
  being the chicken one and "Doble con Pechuga" beef + chicken looks backwards.
- Delivery zones, fees, the default fee and the hours are invented test values.

## 3. Onboarding a new restaurant (today's procedure)

1. Copy `config/restaurant.json` and replace every section with the new restaurant's
   data. Keep item ids stable once orders exist.
2. Point `RESTAURANT_CONFIG` at the new file.
3. **Restart uvicorn.** `POST /api/reload-config` re-reads the file but only updates the
   bot's copy; carts, payments and reports keep the old prices until a restart.
4. Run the automated tests. Expected numbers are read from the config, so a price change
   moves the tests on purpose. Tests use fixed ids (`b_caserita`, `b_especial`,
   `d_gaseosa_personal`, …), so a different menu needs the tests adjusted.
5. Run `TEST.md` Part A in the simulator before connecting WhatsApp.

One process serves one config. Several restaurants means several processes, each with
its own config, database and WhatsApp number (`KNOWN-GAPS.md` G21).
