"""Checks the parts that must not be wrong. Run: python -m pytest tests -q

Prices come from config/restaurant.json, so these numbers move when the menu
moves. That is deliberate: if a price changes and a test fails, the test was
right to fail."""

import json
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app import brain, cart, db, handoff, orders, payments, reports, wa
from app.catalog import CATALOG
from app.money import MoneyError, fmt, pesos, total_of

PHONE = "573001112233"
CASERITA = CATALOG.price_of("b_caserita")          # 18900
ESPECIAL = CATALOG.price_of("b_especial")          # 29900
GASEOSA = CATALOG.price_of("d_gaseosa_personal")   # 3500
QUESO = CATALOG.modifier_price("m_queso")          # 2000
ACCOUNT = CATALOG.accounts()[0]["number"]
HOLDER = CATALOG.accounts()[0]["holder"]


def _iso(minutes_ago: float = 0) -> str:
    """A receipt stamped `minutes_ago` before now, in the restaurant's timezone.
    Negative means the future."""
    now = datetime.now(CATALOG.timezone())
    return (now - timedelta(minutes=minutes_ago)).isoformat()


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    # A real extract_proof with no proof model set would ask the API for its models.
    monkeypatch.setattr(brain, "_proof_model", "test-model")
    db.init()
    db.reset()
    yield


# ---- money -----------------------------------------------------------

def test_floats_are_never_money():
    with pytest.raises(MoneyError):
        pesos(18900.0)
    with pytest.raises(MoneyError):
        total_of([18900, 3500.5])


def test_money_formats_colombian():
    assert fmt(29900) == "$29.900"
    assert fmt(1250000) == "$1.250.000"


def test_pesos_parses_typed_strings():
    assert pesos("18.900") == 18900
    assert pesos("$ 47,000") == 47000


# ---- cart ------------------------------------------------------------

def test_total_is_computed_from_the_catalog():
    cart.add_item(PHONE, "b_especial", 2)
    cart.add_item(PHONE, "d_gaseosa_personal", 1)
    view = cart.summary(PHONE)
    assert view["subtotal"] == ESPECIAL * 2 + GASEOSA
    assert view["total"] == view["subtotal"]


def test_modifier_price_is_added_per_unit():
    cart.add_item(PHONE, "b_caserita", 2, ["m_queso"])
    assert cart.summary(PHONE)["subtotal"] == (CASERITA + QUESO) * 2


def test_unknown_item_is_refused():
    with pytest.raises(cart.CartError):
        cart.add_item(PHONE, "pizza_hawaiana", 1)


def test_sold_out_item_is_refused():
    item = CATALOG.item("d_cerveza_premium")
    item["available"] = False
    try:
        with pytest.raises(cart.CartError):
            cart.add_item(PHONE, "d_cerveza_premium", 1)
    finally:
        item["available"] = True


def test_modifier_must_apply_to_the_item():
    with pytest.raises(cart.CartError):
        cart.add_item(PHONE, "d_gaseosa_personal", 1, ["m_queso"])


def test_delivery_fee_comes_from_the_zone():
    cart.add_item(PHONE, "b_caserita", 1)
    cabecera = cart.summary(PHONE, zone="cabecera", fulfilment="DELIVERY")
    assert cabecera["delivery_fee"] == 5000
    assert cabecera["total"] == CASERITA + 5000
    unknown = cart.summary(PHONE, zone="Marte", fulfilment="DELIVERY")
    assert unknown["delivery_fee"] == CATALOG.fulfilment["default_delivery_fee"]


def test_set_qty_zero_removes():
    cart.add_item(PHONE, "d_gaseosa_personal", 3)
    cart.set_qty(PHONE, "d_gaseosa_personal", 0)
    assert cart.summary(PHONE)["empty"]


# ---- order state machine ---------------------------------------------

def _pickup_order():
    cart.add_item(PHONE, "b_especial", 1)
    return orders.create_from_cart(PHONE, "PICKUP")


def test_happy_path_pickup():
    order = _pickup_order()
    assert order["state"] == "PENDING_CONFIRMATION"
    for event, state in [
        ("customer_confirms", "CONFIRMED"),
        ("staff_accepts", "ACCEPTED"),
        ("food_ready", "READY"),
        ("hand_over", "COMPLETED"),
    ]:
        order = orders.apply_event(order["id"], event)
        assert order["state"] == state
    assert len(order["transitions"]) == 5  # creation + four moves


def test_order_total_is_frozen_and_matches_its_lines():
    order = _pickup_order()
    assert order["total"] == sum(i["line_total"] for i in order["items"]) + order["delivery_fee"]


@pytest.mark.parametrize("event", ["staff_accepts", "food_ready", "hand_over", "mark_in_error"])
def test_illegal_transitions_raise(event):
    order = _pickup_order()
    with pytest.raises(orders.IllegalTransition):
        orders.apply_event(order["id"], event)


def test_terminal_state_never_moves():
    order = _pickup_order()
    orders.apply_event(order["id"], "customer_declines")
    with pytest.raises(orders.IllegalTransition):
        orders.apply_event(order["id"], "customer_confirms")


def test_out_for_delivery_impossible_on_pickup():
    order = _pickup_order()
    for event in ("customer_confirms", "staff_accepts", "food_ready"):
        orders.apply_event(order["id"], event)
    with pytest.raises(orders.IllegalTransition):
        orders.apply_event(order["id"], "driver_leaves")


def test_customer_cannot_cancel_once_accepted():
    order = _pickup_order()
    for event in ("customer_confirms", "staff_accepts"):
        orders.apply_event(order["id"], event)
    with pytest.raises(orders.IllegalTransition):
        orders.apply_event(order["id"], "customer_cancels")


# ---- payments --------------------------------------------------------

def _transfer_order():
    order = _pickup_order()                     # total = ESPECIAL
    orders.apply_event(order["id"], "customer_confirms")
    payments.set_method(order["id"], "TRANSFER")
    return order


def _order_created_ago(minutes: float) -> dict:
    """A confirmed transfer order whose created_at is `minutes` in the past."""
    order = _transfer_order()
    db.run("UPDATE orders SET created_at=%s WHERE id=%s", (_iso(minutes), order["id"]))
    return orders.get(order["id"])


def _receipt(order_id, amount, reference, minutes_ago=0, **fields):
    """A receipt to the restaurant's account, dated now; the amount decides the rest."""
    return payments.check_proof(order_id, {
        "amount": amount, "destination_account": ACCOUNT, "reference": reference,
        "date_iso": _iso(minutes_ago), **fields,
    })


def test_good_proof_is_received_not_confirmed():
    order = _transfer_order()
    record = payments.check_proof(order["id"], {
        "amount": ESPECIAL,
        "destination_account": ACCOUNT,
        "reference": "M123456",
        "date_iso": _iso(),
    })
    assert record["status"] == "PROOF_RECEIVED"
    assert record["receipt"]["flags"] == []
    # The bot never confirms: only the panel does.
    confirmed = payments.confirm_receipt(order["id"], record["receipt"]["id"])
    assert confirmed["status"] == "CONFIRMED"


def test_wrong_amount_is_flagged():
    order = _transfer_order()
    record = payments.check_proof(order["id"], {
        "amount": ESPECIAL - 10000,
        "destination_account": ACCOUNT,
        "reference": "M999",
        "date_iso": _iso(),
    })
    assert record["status"] == "FLAGGED"
    assert any("monto" in f.lower() for f in record["receipt"]["flags"])


def test_foreign_account_is_flagged():
    order = _transfer_order()
    record = payments.check_proof(order["id"], {
        "amount": ESPECIAL,
        "destination_account": "3009998877",
        "reference": "M888",
        "date_iso": _iso(),
    })
    assert record["status"] == "FLAGGED"
    assert any("cuenta" in f.lower() for f in record["receipt"]["flags"])


def test_reused_reference_is_rejected_by_the_database():
    first = _transfer_order()
    good = {
        "amount": ESPECIAL, "destination_account": ACCOUNT, "reference": "REPEATED-1",
        "date_iso": _iso(),
    }
    assert payments.check_proof(first["id"], good)["status"] == "PROOF_RECEIVED"

    cart.add_item(PHONE, "b_especial", 1)
    second = orders.create_from_cart(PHONE, "PICKUP")
    orders.apply_event(second["id"], "customer_confirms")
    payments.set_method(second["id"], "TRANSFER")
    record = payments.check_proof(second["id"], good)
    assert record["status"] == "FLAGGED"
    assert any("ya fue usado" in f for f in record["receipt"]["flags"])


@pytest.mark.parametrize("raw,expected", [
    (29900, 29900),
    (29900.0, 29900),
    ("29900", 29900),
    ("29.900", 29900),
    ("29,900", 29900),
    ("$ 29.900 COP", 29900),
    ("29.900,00", 29900),
    ("$29,900.00", 29900),
])
def test_ocr_amount_formats_are_all_read_the_same(raw, expected):
    assert payments.read_ocr_amount(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "no se ve", 29900.5, True])
def test_unreadable_ocr_amount_raises_rather_than_guessing(raw):
    with pytest.raises(MoneyError):
        payments.read_ocr_amount(raw)


def test_messy_but_correct_proof_is_not_flagged():
    order = _transfer_order()
    digits = "".join(ch for ch in str(ACCOUNT) if ch.isdigit())
    spaced = " ".join([digits[:3], digits[3:6], digits[6:8], digits[8:]]).strip()
    record = payments.check_proof(order["id"], {
        "amount": f"$ {ESPECIAL // 1000}.{ESPECIAL % 1000:03d} COP",
        "destination_account": spaced,               # spaced, as Nequi shows it
        "reference": "CUS-77",
        "date_iso": _iso(),
    })
    assert record["status"] == "PROOF_RECEIVED", record["receipt"]["flags"]


def test_proof_with_only_the_holder_name_is_accepted():
    order = _transfer_order()
    record = payments.check_proof(order["id"], {
        "amount": ESPECIAL,
        "destination_account": None,
        "destination_holder": HOLDER,
        "reference": "CUS-78",
        "date_iso": _iso(),
    })
    assert record["status"] == "PROOF_RECEIVED", record["receipt"]["flags"]


def test_proof_to_a_stranger_is_still_flagged():
    order = _transfer_order()
    record = payments.check_proof(order["id"], {
        "amount": ESPECIAL,
        "destination_account": None,
        "destination_holder": "Pedro Ramirez",
        "reference": "CUS-79",
        "date_iso": _iso(),
    })
    assert record["status"] == "FLAGGED"


def test_mixed_split_must_sum_to_the_total():
    order = _pickup_order()
    with pytest.raises(payments.PaymentError):
        payments.set_method(order["id"], "MIXED", transfer_amount=10000, cash_amount=5000)
    record = payments.set_method(
        order["id"], "MIXED", transfer_amount=10000, cash_amount=ESPECIAL - 10000
    )
    assert record["transfer_amount"] + record["cash_amount"] == ESPECIAL


def test_payment_never_blocks_the_order():
    order = _transfer_order()
    red = _receipt(order["id"], 1, "NB-1")                  # wrong amount: red
    clean = _receipt(order["id"], ESPECIAL, "NB-2")         # covers the total: clean
    extra = _receipt(order["id"], GASEOSA, "NB-3")          # nothing more is owed: red...
    payments.discard_receipt(order["id"], extra["receipt"]["id"], "Llegó por error")   # ...and discarded
    assert red["receipt"]["status"] == "FLAGGED"
    assert clean["receipt"]["status"] == "PROOF_RECEIVED"
    states = [r["status"] for r in payments.receipts(order["id"])]
    assert states == ["FLAGGED", "PROOF_RECEIVED", "DISCARDED"]
    for event, state in [("staff_accepts", "ACCEPTED"), ("food_ready", "READY"),
                         ("hand_over", "COMPLETED")]:
        order = orders.apply_event(order["id"], event)
        assert order["state"] == state           # red, clean and discarded receipts: the order moves anyway


def test_new_order_has_no_payment_method_yet():
    order = _pickup_order()
    payment = order["payment"]
    assert payment["method"] == "UNSPECIFIED"
    assert payment["transfer_amount"] == 0
    assert payment["cash_amount"] == 0


def test_proof_with_no_chosen_method_is_read_against_the_total():
    order = _pickup_order()                      # total = ESPECIAL, no method chosen
    record = payments.check_proof(order["id"], {
        "amount": ESPECIAL,
        "destination_account": ACCOUNT,
        "reference": "M-INFER-1",
        "date_iso": _iso(),
    })
    assert record["status"] == "PROOF_RECEIVED"
    assert record["method"] == "TRANSFER"
    assert record["receipt"]["flags"] == []


def test_cash_customer_who_transfers_is_not_flagged():
    """The reported bug: a customer who never announced a transfer still gets a
    clean card when a proof for the full total arrives."""
    order = _pickup_order()
    payments.set_method(order["id"], "CASH")
    record = payments.check_proof(order["id"], {
        "amount": ESPECIAL,
        "destination_account": ACCOUNT,
        "reference": "M-INFER-2",
        "date_iso": _iso(),
    })
    assert record["status"] == "PROOF_RECEIVED"
    assert record["method"] == "TRANSFER"


def test_partial_transfer_becomes_mixed():
    order = _pickup_order()
    record = payments.check_proof(order["id"], {
        "amount": ESPECIAL - 5000,
        "destination_account": ACCOUNT,
        "reference": "M-INFER-3",
        "date_iso": _iso(),
    })
    assert record["status"] == "PROOF_RECEIVED"
    assert record["method"] == "MIXED"
    assert record["transfer_amount"] == ESPECIAL - 5000
    assert record["cash_amount"] == 5000


def test_overpayment_is_flagged():
    order = _pickup_order()
    record = payments.check_proof(order["id"], {
        "amount": ESPECIAL + 1000,
        "destination_account": ACCOUNT,
        "reference": "M-INFER-4",
        "date_iso": _iso(),
    })
    assert record["status"] == "FLAGGED"


def test_agreed_transfer_amount_still_wins():
    order = _pickup_order()
    payments.set_method(order["id"], "MIXED", transfer_amount=10000, cash_amount=ESPECIAL - 10000)
    record = payments.check_proof(order["id"], {
        "amount": 10000,
        "destination_account": ACCOUNT,
        "reference": "M-INFER-5",
        "date_iso": _iso(),
    })
    assert record["status"] == "PROOF_RECEIVED"
    assert record["method"] == "MIXED"

    cart.add_item(PHONE, "b_especial", 1)
    second = orders.create_from_cart(PHONE, "PICKUP")
    payments.set_method(second["id"], "MIXED", transfer_amount=10000, cash_amount=ESPECIAL - 10000)
    record2 = payments.check_proof(second["id"], {
        "amount": ESPECIAL,
        "destination_account": ACCOUNT,
        "reference": "M-INFER-6",
        "date_iso": _iso(),
    })
    assert record2["status"] == "FLAGGED"


# ---- receipt date (Task A') -------------------------------------------

def test_receipt_without_a_date_is_flagged():
    order = _transfer_order()
    record = payments.check_proof(order["id"], {
        "amount": ESPECIAL, "destination_account": ACCOUNT, "reference": "DATE-1",
    })
    assert record["status"] == "FLAGGED"
    assert any("fecha" in f.lower() for f in record["receipt"]["flags"])


def test_receipt_dated_in_the_future_is_flagged():
    order = _transfer_order()
    record = payments.check_proof(order["id"], {
        "amount": ESPECIAL, "destination_account": ACCOUNT, "reference": "DATE-2",
        "date_iso": _iso(-10),                    # ten minutes ahead
    })
    assert record["status"] == "FLAGGED"


def test_receipt_from_before_the_order_is_flagged():
    order = _transfer_order()                     # created now
    record = payments.check_proof(order["id"], {
        "amount": ESPECIAL, "destination_account": ACCOUNT, "reference": "DATE-3",
        "date_iso": _iso(30),                      # well before the order existed
    })
    assert record["status"] == "FLAGGED"
    assert any("pedido" in f.lower() for f in record["receipt"]["flags"])


def test_recent_receipt_passes_the_date_check():
    order = _transfer_order()
    record = payments.check_proof(order["id"], {
        "amount": ESPECIAL, "destination_account": ACCOUNT, "reference": "DATE-4",
        "date_iso": _iso(0),
    })
    assert record["status"] == "PROOF_RECEIVED", record["receipt"]["flags"]


def test_receipt_after_the_message_arrived_is_flagged():
    """Measuring against the real clock instead of `received_at` would let this
    through - the receipt looks recent, but it is dated fifteen minutes after
    the message that supposedly carried it actually arrived."""
    order = _order_created_ago(40)
    received_at = _iso(20)
    record = payments.check_proof(
        order["id"],
        {
            "amount": ESPECIAL, "destination_account": ACCOUNT, "reference": "DATE-5",
            "date_iso": _iso(5),                   # fifteen minutes after arrival
        },
        received_at=received_at,
    )
    assert record["status"] == "FLAGGED"


def test_slow_customer_is_not_flagged():
    """The whole point of this change: under the old fixed window this genuine,
    unhurried customer was flagged."""
    order = _order_created_ago(40)
    record = payments.check_proof(order["id"], {
        "amount": ESPECIAL, "destination_account": ACCOUNT, "reference": "DATE-6",
        "date_iso": _iso(35),
    })
    assert record["status"] == "PROOF_RECEIVED", record["receipt"]["flags"]


def test_old_but_valid_receipt_gets_a_note():
    order = _order_created_ago(40)
    record = payments.check_proof(order["id"], {
        "amount": ESPECIAL, "destination_account": ACCOUNT, "reference": "DATE-7",
        "date_iso": _iso(35),
    })
    assert record["receipt"]["flags"] == []
    assert record["receipt"]["notes"]


def test_prompt_payment_gets_no_note():
    order = _order_created_ago(3)
    record = payments.check_proof(order["id"], {
        "amount": ESPECIAL, "destination_account": ACCOUNT, "reference": "DATE-8",
        "date_iso": _iso(2),
    })
    assert record["receipt"]["notes"] == []


def test_notes_survive_a_duplicate_reference():
    first = _order_created_ago(40)
    good = {
        "amount": ESPECIAL, "destination_account": ACCOUNT, "reference": "DATE-DUP",
        "date_iso": _iso(35),
    }
    payments.check_proof(first["id"], good)

    second = _order_created_ago(40)
    record = payments.check_proof(second["id"], good)
    assert record["status"] == "FLAGGED"
    assert any("ya fue usado" in f for f in record["receipt"]["flags"])
    assert record["receipt"]["notes"]


# ---- destination account matching (Task A') ----------------------------

def test_account_sharing_only_the_last_four_is_flagged():
    """The loophole: today this passes."""
    order = _transfer_order()
    record = payments.check_proof(order["id"], {
        "amount": ESPECIAL, "destination_account": "3001237389", "reference": "ACC-1",
        "date_iso": _iso(),
    })
    assert record["status"] == "FLAGGED"


def test_masked_account_is_accepted():
    digits = "".join(ch for ch in str(ACCOUNT) if ch.isdigit())
    masked = digits[:3] + "****" + digits[-4:]
    record = payments.check_proof(_transfer_order()["id"], {
        "amount": ESPECIAL, "destination_account": masked, "reference": "ACC-2",
        "date_iso": _iso(),
    })
    assert record["status"] == "PROOF_RECEIVED", record["receipt"]["flags"]


def test_holder_name_cannot_rescue_a_wrong_account():
    order = _transfer_order()
    record = payments.check_proof(order["id"], {
        "amount": ESPECIAL, "destination_account": "3009998877", "destination_holder": HOLDER,
        "reference": "ACC-3", "date_iso": _iso(),
    })
    assert record["status"] == "FLAGGED"


# ---- confirm_receipt() and cash (Task A', Task C) ----------------------

def test_confirm_records_the_amount_received():
    """P-3564: the receipt said more than the order total, and confirm_receipt()
    must record the real amount received instead of silently discarding it."""
    order = _pickup_order()                       # total = ESPECIAL
    record = _receipt(order["id"], ESPECIAL + 14200, "OVER-1")      # above the total: red
    confirmed = payments.confirm_receipt(order["id"], record["receipt"]["id"], ESPECIAL + 14200)
    assert confirmed["method"] == "TRANSFER"
    assert confirmed["transfer_amount"] == ESPECIAL
    assert confirmed["overpayment_amount"] == 14200
    assert confirmed["outstanding"] == 0
    assert confirmed["paid"] == ESPECIAL


def test_confirm_without_an_amount_uses_the_receipt():
    order = _transfer_order()
    record = payments.check_proof(order["id"], {
        "amount": ESPECIAL, "destination_account": ACCOUNT, "reference": "CONFIRM-1",
        "date_iso": _iso(),
    })
    confirmed = payments.confirm_receipt(order["id"], record["receipt"]["id"])
    assert confirmed["receipt"]["confirmed_amount"] == ESPECIAL
    assert confirmed["paid"] == ESPECIAL


def test_partial_transfer_leaves_an_outstanding_balance():
    order = _pickup_order()                       # total = ESPECIAL
    cash_part = 9000
    transfer_part = ESPECIAL - cash_part
    payments.set_method(order["id"], "MIXED", transfer_amount=transfer_part, cash_amount=cash_part)
    record = payments.check_proof(order["id"], {
        "amount": transfer_part, "destination_account": ACCOUNT, "reference": "CONFIRM-2",
        "date_iso": _iso(),
    })
    confirmed = payments.confirm_receipt(order["id"], record["receipt"]["id"])
    assert confirmed["method"] == "MIXED"
    assert confirmed["outstanding"] == cash_part
    assert confirmed["paid"] == transfer_part


def test_register_cash_completes_the_payment():
    order = _pickup_order()
    cash_part = 9000
    transfer_part = ESPECIAL - cash_part
    payments.set_method(order["id"], "MIXED", transfer_amount=transfer_part, cash_amount=cash_part)
    record = payments.check_proof(order["id"], {
        "amount": transfer_part, "destination_account": ACCOUNT, "reference": "CASH-1",
        "date_iso": _iso(),
    })
    payments.confirm_receipt(order["id"], record["receipt"]["id"])
    completed = payments.register_cash(order["id"], cash_part)
    # What the card's moneyTag shows as "Pago completo": paid > 0 and outstanding == 0.
    assert completed["outstanding"] == 0
    assert completed["paid"] == ESPECIAL
    assert completed["overpayment_amount"] == 0


def test_cash_above_what_is_owed_is_refused():
    """Change is the restaurant's business and never enters the system, so cash can
    only be registered up to what is still owed."""
    order = _pickup_order()
    cash_part = 9000
    transfer_part = ESPECIAL - cash_part
    payments.set_method(order["id"], "MIXED", transfer_amount=transfer_part, cash_amount=cash_part)
    record = payments.check_proof(order["id"], {
        "amount": transfer_part, "destination_account": ACCOUNT, "reference": "CASH-2",
        "date_iso": _iso(),
    })
    payments.confirm_receipt(order["id"], record["receipt"]["id"])
    before = payments.current(order["id"])
    assert before["outstanding"] == cash_part
    with pytest.raises(payments.PaymentError) as refused:
        payments.register_cash(order["id"], cash_part + 5000)
    assert str(refused.value) == (
        f"El monto es mayor de lo que falta ({fmt(cash_part)}). "
        "Registre solo lo que falta; el cambio lo maneja el local."
    )
    # Nothing was written: cash_received stays 0 and updated_at is unchanged.
    assert payments.current(order["id"]) == before


def test_cash_order_registers_exactly_the_total():
    order = _confirmed_order()
    payments.set_method(order["id"], "CASH")
    done = payments.register_cash(order["id"], ESPECIAL)
    assert done["outstanding"] == 0
    assert done["paid"] == ESPECIAL
    assert done["overpayment_amount"] == 0           # "Pago completo", no "Sobrepago"
    with pytest.raises(payments.PaymentError) as refused:
        payments.register_cash(order["id"], 1)
    assert f"({fmt(0)})" in str(refused.value)
    assert payments.current(order["id"])["cash_received"] == ESPECIAL


def test_cash_route_refuses_more_than_what_is_owed(monkeypatch):
    client = _client(monkeypatch)
    order = _confirmed_order()
    payments.set_method(order["id"], "CASH")
    url = f"/api/orders/{order['id']}/payment/cash"

    refused = client.post(url, json={"amount": ESPECIAL + 1000})
    assert refused.status_code == 400
    assert refused.json()["error"] == (
        f"El monto es mayor de lo que falta ({fmt(ESPECIAL)}). "
        "Registre solo lo que falta; el cambio lo maneja el local."
    )
    assert payments.current(order["id"])["cash_received"] == 0

    accepted = client.post(url, json={"amount": ESPECIAL})
    assert accepted.status_code == 200
    assert accepted.json()["outstanding"] == 0


def test_cash_then_a_confirmed_transfer_is_a_surplus(monkeypatch):
    """The P-4766 manual run: cash registered on top of a clean receipt that was not
    confirmed yet, then the receipt is confirmed. The customer paid twice and the
    surplus is real money owed to them."""
    client = _client(monkeypatch)
    cart.add_item(PHONE, "b_madurita", 2)
    cart.add_item(PHONE, "d_gaseosa_personal", 1)
    order = orders.create_from_cart(PHONE, "DELIVERY", address="Calle 48 #34-20", zone="Cabecera")
    orders.apply_event(order["id"], "customer_confirms")
    total = order["total"]                           # 52.300 at today's prices
    transfer = 30000
    receipt = _receipt(order["id"], transfer, "OVERLAP-1")["receipt"]     # clean: infers MIXED
    waiting = payments.current(order["id"])
    assert waiting["method"] == "MIXED"
    assert waiting["unconfirmed_amount"] == transfer             # what the dialog warns about

    # Allowed: outstanding only counts confirmed receipts, so it is still the whole total.
    payments.register_cash(order["id"], total)
    # Waiting receipts already count in `sent`, so the surplus shows before confirming.
    assert payments.current(order["id"])["overpayment_amount"] == transfer

    response = client.post(
        f"/api/orders/{order['id']}/receipts/{receipt['id']}/confirm", json={"amount": transfer}
    )
    assert response.status_code == 200
    record = response.json()
    assert record["overpayment_amount"] == transfer
    assert record["paid"] == total
    assert record["outstanding"] == 0
    last = db.q1(
        "SELECT body FROM outbound_messages WHERE phone=%s ORDER BY id DESC LIMIT 1", (PHONE,)
    )["body"]
    assert last == (
        f"Confirmamos su pago. Vimos que envió {fmt(transfer)} de más; "
        "el local se va a comunicar con usted por el excedente."
    )
    assert order["code"] not in last
    assert "cocina" not in last.lower()


def test_cash_up_to_outstanding_never_makes_a_surplus():
    order = _confirmed_order()
    payments.set_method(order["id"], "CASH")
    partial = payments.register_cash(order["id"], 10000)
    assert partial["overpayment_amount"] == 0
    completed = payments.register_cash(order["id"], partial["outstanding"])
    assert completed["overpayment_amount"] == 0
    assert completed["outstanding"] == 0
    assert completed["paid"] == ESPECIAL


def test_unconfirmed_amount_counts_only_clean_waiting_receipts():
    """Only clean receipts nobody has confirmed yet count, at their OCR amount. The payment
    sentence reads it; the cash dialog no longer warns about it (C2 Part 2)."""
    order = _confirmed_order()                       # UNSPECIFIED, total ESPECIAL
    def unconfirmed():
        return payments.current(order["id"])["unconfirmed_amount"]

    assert unconfirmed() == 0

    red = _receipt(order["id"], 1234, "WARN-1", destination_account="3009998877")
    assert red["receipt"]["status"] == "FLAGGED"
    assert unconfirmed() == 0

    clean = _receipt(order["id"], 10000, "WARN-2")   # infers MIXED
    assert clean["receipt"]["status"] == "PROOF_RECEIVED"
    assert unconfirmed() == 10000
    payments.confirm_receipt(order["id"], clean["receipt"]["id"])
    assert unconfirmed() == 0

    # The customer transfers the cash part (Q3): the receipt is clean.
    cash_part = _receipt(order["id"], ESPECIAL - 10000, "WARN-3")
    assert cash_part["receipt"]["status"] == "PROOF_RECEIVED"
    assert unconfirmed() == ESPECIAL - 10000
    payments.discard_receipt(order["id"], cash_part["receipt"]["id"], "Llegó por error")
    assert unconfirmed() == 0

    # Cash registered by mistake is fixed with the X on its row: no warning in the dialog.
    html = (Path(__file__).resolve().parent.parent / "static" / "kitchen.html").read_text(
        encoding="utf-8"
    )
    assert "Hay comprobantes sin confirmar" not in html
    assert "unconfirmed_amount" not in html


# ---- cash settles the method; ESTADO REAL says when it is paid (P-6455) ------

@pytest.mark.parametrize("amount", [ESPECIAL, 10000])
def test_cash_on_an_unspecified_order_settles_cash(amount):
    order = _confirmed_order()                       # no method chosen
    record = payments.register_cash(order["id"], amount)
    assert record["method"] == "CASH"
    assert record["transfer_amount"] == 0
    assert record["cash_amount"] == ESPECIAL
    assert record["cash_received"] == amount
    assert record["outstanding"] == ESPECIAL - amount
    assert record["cash_due"] == ESPECIAL - amount   # "Efectivo por cobrar" for the rest


def test_cash_and_the_method_land_together(monkeypatch):
    order = _confirmed_order()
    real_run = db.run

    class Boom(Exception):
        pass

    def failing_run(sql, args=()):
        if "method='CASH'" in sql:
            raise Boom("the method write failed")
        return real_run(sql, args)

    monkeypatch.setattr(db, "run", failing_run)
    with pytest.raises(Boom):
        payments.register_cash(order["id"], ESPECIAL)
    monkeypatch.undo()
    after = payments.current(order["id"])
    assert after["cash_received"] == 0               # the cash rolled back with it
    assert after["method"] == "UNSPECIFIED"


@pytest.mark.parametrize("method", ["TRANSFER", "MIXED"])
def test_cash_keeps_a_chosen_method(method):
    order = _confirmed_order()
    if method == "MIXED":
        payments.set_method(order["id"], "MIXED", transfer_amount=10000)
    else:
        payments.set_method(order["id"], "TRANSFER")
    before = payments.current(order["id"])
    after = payments.register_cash(order["id"], 5000)
    assert (after["method"], after["transfer_amount"], after["cash_amount"]) == (
        before["method"], before["transfer_amount"], before["cash_amount"]
    )
    assert after["cash_received"] == 5000


def test_real_state_says_whether_the_order_is_paid():
    """P-6455: cash registered on an order whose method was never chosen. The line
    said only "todavía no ha dicho cómo va a pagar", so the bot asked how the
    customer would pay and then said no payment was registered, and P-9950 (C2
    manual check: the bot asked for a Nequi transfer)."""
    order = _confirmed_order()
    head = "- Pago del pedido más reciente: "
    assert _payment_line(brain.system_prompt(PHONE)) == (
        head + "todavía no ha dicho cómo va a pagar; no ha enviado ningún comprobante; "
        "todavía falta por pagar."
    )
    payments.register_cash(order["id"], ESPECIAL)
    prompt = brain.system_prompt(PHONE)
    assert _payment_line(prompt) == (
        head + "el local ya recibió el pago completo en efectivo; no hace falta que transfiera "
        "ni que envíe comprobante; ya está pagado completo."
    )
    assert ("Si aquí dice que ya está pagado completo, nunca le preguntes cómo va a pagar ni "
            "le digas que no tiene pago registrado." in _real_state_block(prompt))

    # Sent but waiting for a person is not "falta por pagar", or the bot asks for it again.
    second = _second_transfer_order()
    _receipt(second["id"], ESPECIAL, "PAID-1")
    assert _payment_line(brain.system_prompt(PHONE)).endswith(
        "ya no falta nada por transferir; todavía falta que una persona confirme el pago."
    )


def test_card_shows_the_receipt_tag_only_where_a_transfer_is_expected():
    html = (Path(__file__).resolve().parent.parent / "static" / "kitchen.html").read_text(
        encoding="utf-8"
    )
    assert "['Sin comprobante', '']" in html
    assert "Pago pendiente" not in html
    assert "['TRANSFER', 'MIXED'].includes(pay.method)" in html
    assert "${showProof ?" in html


def test_method_change_after_cash_goes_to_a_person():
    order = _confirmed_order()
    payments.register_cash(order["id"], 10000)      # settles CASH
    result = brain._run_tool(PHONE, "choose_payment", {"method": "TRANSFER"})
    assert result.startswith("PASADO A UNA PERSONA: el local ya registró efectivo recibido")
    after = orders.get(order["id"])
    assert after["payment"]["method"] == "CASH"     # the method did not change
    assert [h["summary"] for h in after["handoffs"]] == [
        "El cliente quiere cambiar la forma de pago a transferencia."
    ]


def test_choosing_a_method_after_cash_goes_to_a_person():
    """An order that got cash before cash settled the method (P-6455 itself): even
    choosing one goes to a person."""
    order = _confirmed_order()
    db.run(                                          # a cash row that did not settle the method
        "INSERT INTO cash_payments(order_id, amount, registered_at) VALUES(%s,%s,%s)",
        (order["id"], ESPECIAL, db.now()),
    )
    result = brain._run_tool(PHONE, "choose_payment", {"method": "CASH"})
    assert result.startswith("PASADO A UNA PERSONA")
    assert orders.get(order["id"])["payment"]["method"] == "UNSPECIFIED"


def test_same_method_after_cash_changes_nothing():
    order = _confirmed_order()
    payments.register_cash(order["id"], 10000)
    result = brain._run_tool(PHONE, "choose_payment", {"method": "CASH"})
    assert result == "La forma de pago ya estaba registrada así; no cambió nada."
    after = orders.get(order["id"])
    assert after["handoffs"] == []
    assert after["payment"]["method"] == "CASH"


def test_overpayment_never_blocks_the_order():
    order = _confirmed_order()
    record = _receipt(order["id"], ESPECIAL + 5000, "OVER-2")       # above the total: red
    payments.confirm_receipt(order["id"], record["receipt"]["id"], ESPECIAL + 5000)
    accepted = orders.apply_event(order["id"], "staff_accepts")
    assert accepted["state"] == "ACCEPTED"
    assert accepted["payment"]["overpayment_amount"] == 5000


# ---- reports ---------------------------------------------------------

def test_report_counts_only_real_orders():
    first = _pickup_order()                                  # ESPECIAL, confirmed
    orders.apply_event(first["id"], "customer_confirms")
    cart.add_item(PHONE, "b_caserita", 1)
    second = orders.create_from_cart(PHONE, "PICKUP")        # never confirmed
    orders.apply_event(second["id"], "customer_declines")

    snapshot = reports.snapshot(1)
    assert snapshot["orders"] == 1
    assert snapshot["revenue"] == ESPECIAL
    assert snapshot["average_ticket"] == ESPECIAL
    assert snapshot["lost_orders"] == 1
    assert snapshot["top_items"][0]["name"] == "Caserita Especial"


# ---- modification windows (order-modification.md) --------------------

def _confirmed_order():
    order = _pickup_order()
    return orders.apply_event(order["id"], "customer_confirms")


def _accepted_order():
    order = _confirmed_order()
    return orders.apply_event(order["id"], "staff_accepts")


def test_window_1_open_cart_always_wins():
    _accepted_order()
    cart.open_cart(PHONE)                     # an open cart exists again
    assert orders.modification_window(PHONE)[0] == 1


def test_window_2_pending_and_confirmed():
    _pickup_order()
    assert orders.modification_window(PHONE)[0] == 2
    db.reset()
    _confirmed_order()
    assert orders.modification_window(PHONE)[0] == 2


def test_window_3_locked_once_accepted():
    _accepted_order()
    window, order = orders.modification_window(PHONE)
    assert window == 3 and order["state"] == "ACCEPTED"


def test_terminal_order_is_not_window_3():
    order = _pickup_order()
    orders.apply_event(order["id"], "customer_declines")
    assert orders.modification_window(PHONE)[0] == 0


def test_editing_a_pending_order_does_not_duplicate_it():
    """The bug Miguel hit: a change created a second live order."""
    first = _confirmed_order()
    result = brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    assert "error interno" not in result

    updated = orders.get(first["id"])
    assert updated["id"] == first["id"]
    assert updated["code"] == first["code"]
    assert updated["state"] == "CONFIRMED"
    # The items came across, so the customer did not have to repeat them.
    assert updated["total"] == ESPECIAL + GASEOSA
    assert len(orders.board()) == 1
    assert all(t["to_state"] != "CANCELLED" for t in updated["transitions"])
    assert reports.snapshot(1)["lost_orders"] == 0
    assert updated["edited_at"] is not None


def test_locked_order_is_never_mutated_and_the_new_one_is_separate():
    locked = _accepted_order()
    cart.open_cart(PHONE, related_order_id=locked["id"])
    cart.add_item(PHONE, "b_caserita", 1)
    second = orders.create_from_cart(PHONE, "PICKUP")

    assert orders.get(locked["id"])["state"] == "ACCEPTED"      # untouched
    assert orders.get(locked["id"])["total"] == ESPECIAL        # untouched
    assert second["related_order_id"] == locked["id"]
    assert second["id"] != locked["id"]
    assert len(orders.board()) == 2                             # two real orders


# ---- the preparation step is gone (Task B) -----------------------------

def test_ready_goes_back_to_recibidos():
    order = _accepted_order()
    order = orders.apply_event(order["id"], "food_ready")
    order = orders.apply_event(order["id"], "mark_in_error")
    assert order["state"] == "ACCEPTED"


# ---- editing an order in place (Task B) --------------------------------

def _confirmed_delivery_order(address="calle 48 #34-20", zone="Cabecera"):
    cart.add_item(PHONE, "b_especial", 1)
    order = orders.create_from_cart(PHONE, "DELIVERY", address=address, zone=zone)
    return orders.apply_event(order["id"], "customer_confirms")


def test_edit_keeps_delivery_address_and_fee():
    order = _confirmed_delivery_order()
    result = brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    assert "error interno" not in result
    updated = orders.get(order["id"])
    assert updated["address"] == "calle 48 #34-20"
    assert updated["delivery_fee"] == 5000
    assert updated["total"] == ESPECIAL + GASEOSA + 5000


def test_changing_the_zone_recomputes_the_fee_in_place():
    order = _confirmed_delivery_order()
    result = brain._run_tool(
        PHONE, "create_order",
        {"fulfilment": "DELIVERY", "address": order["address"], "zone": "Floridablanca"},
    )
    assert "error interno" not in result
    updated = orders.get(order["id"])
    assert updated["id"] == order["id"]
    fee, _ = CATALOG.delivery_fee_for("Floridablanca")
    assert updated["delivery_fee"] == fee


def test_edit_cannot_leave_the_order_empty():
    order = _confirmed_order()
    before_carts = db.q1("SELECT COUNT(*) AS n FROM carts")["n"]
    result = brain._run_tool(PHONE, "remove_item", {"item_id": "b_especial"})
    assert result.startswith("NO SE PUDO")
    assert "error interno" not in result
    updated = orders.get(order["id"])
    assert [i["item_id"] for i in updated["items"]] == [i["item_id"] for i in order["items"]]
    assert cart.peek_open_cart(PHONE) is None
    assert db.q1("SELECT COUNT(*) AS n FROM carts")["n"] == before_carts


def test_amend_is_all_or_nothing(monkeypatch):
    order = _transfer_order()          # confirmed, TRANSFER, total ESPECIAL
    before = orders.get(order["id"])
    before_carts = db.q1("SELECT COUNT(*) AS n FROM carts")["n"]
    real_write_items = orders._write_items

    def failing_write_items(order_id, lines):
        real_write_items(order_id, lines[:1])
        raise RuntimeError("forced failure")

    monkeypatch.setattr(orders, "_write_items", failing_write_items)
    with pytest.raises(RuntimeError):
        orders.amend(order["id"], PHONE, lambda: cart.add_item(PHONE, "d_gaseosa_personal", 1))

    after = orders.get(order["id"])
    assert after["total"] == before["total"]
    assert after["subtotal"] == before["subtotal"]
    assert after["delivery_fee"] == before["delivery_fee"]
    assert after["edited_at"] == before["edited_at"]
    assert after["cart_id"] == before["cart_id"]
    assert [i["id"] for i in after["items"]] == [i["id"] for i in before["items"]]
    assert after["payment"] == before["payment"]
    assert cart.peek_open_cart(PHONE) is None
    assert db.q1("SELECT COUNT(*) AS n FROM carts")["n"] == before_carts

    monkeypatch.undo()
    updated, _ = orders.amend(order["id"], PHONE, lambda: cart.add_item(PHONE, "d_gaseosa_personal", 1))
    assert updated["total"] == ESPECIAL + GASEOSA


def test_amend_checks_the_total_before_writing(monkeypatch):
    order = _confirmed_order()
    before = orders.get(order["id"])
    before_carts = db.q1("SELECT COUNT(*) AS n FROM carts")["n"]
    real_summary = orders.summary

    def bad_summary(*args, **kwargs):
        view = real_summary(*args, **kwargs)
        return {**view, "total": view["total"] + 1}

    monkeypatch.setattr(orders, "summary", bad_summary)
    with pytest.raises(ValueError):
        orders.amend(order["id"], PHONE, lambda: cart.add_item(PHONE, "d_gaseosa_personal", 1))

    after = orders.get(order["id"])
    assert after["total"] == before["total"]
    assert [i["id"] for i in after["items"]] == [i["id"] for i in before["items"]]
    assert db.q1("SELECT COUNT(*) AS n FROM carts")["n"] == before_carts


def test_transaction_rolls_back_every_write():
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.run(
                "INSERT INTO conversations(phone, created_at) VALUES(%s,%s)",
                ("tx-test-phone", db.now()),
            )
            raise RuntimeError("forced failure")
    assert db.q1("SELECT 1 FROM conversations WHERE phone=%s", ("tx-test-phone",)) is None
    db.run("INSERT INTO conversations(phone, created_at) VALUES(%s,%s)", ("after-tx", db.now()))
    assert db.q1("SELECT 1 FROM conversations WHERE phone=%s", ("after-tx",)) is not None


def test_init_on_an_existing_database_keeps_its_rows():
    """Every start runs init(): the schema is created only where it is missing."""
    order = _pickup_order()
    db.init()
    assert orders.get(order["id"])["total"] == order["total"]


def test_money_from_sql_sums_is_int():
    """SUM() over INTEGER columns comes back as int; a Decimal would break money.py."""
    payments.register_cash(_confirmed_order()["id"], 5000)
    snapshot = reports.snapshot(1)
    for key in ("revenue", "delivery_fees", "pending_amount", "average_ticket"):
        assert type(snapshot[key]) is int, key
    assert all(type(row["revenue"]) is int for row in snapshot["fulfilment"])


def test_swap_is_add_then_remove():
    order = _confirmed_order()          # has one ESPECIAL
    result = brain._run_tool(PHONE, "add_item", {"item_id": "b_caserita"})
    assert "error interno" not in result
    result = brain._run_tool(PHONE, "remove_item", {"item_id": "b_especial"})
    assert "error interno" not in result
    updated = orders.get(order["id"])
    assert len(updated["items"]) == 1
    assert updated["total"] == CASERITA


def test_show_cart_does_not_open_a_second_order():
    _confirmed_order()
    result = brain._run_tool(PHONE, "show_cart", {})
    assert "error interno" not in result
    assert cart.peek_open_cart(PHONE) is None
    result = brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    assert "error interno" not in result
    assert len(orders.board()) == 1


def test_amend_refuses_an_accepted_order():
    order = _accepted_order()
    with pytest.raises(orders.NotEditable):
        orders.amend(order["id"], PHONE, lambda: cart.add_item(PHONE, "d_gaseosa_personal", 1))


# ---- handoff to a person (Task B) --------------------------------------

def test_change_to_an_accepted_order_goes_to_a_person():
    order = _accepted_order()
    result = brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    assert "error interno" not in result
    assert "PASADO A UNA PERSONA" in result
    updated = orders.get(order["id"])
    handoffs = updated["handoffs"]
    assert len(handoffs) == 1
    assert "Gaseosa" in handoffs[0]["summary"]
    assert updated["total"] == order["total"]


def test_cancel_by_chat_after_acceptance_goes_to_a_person():
    order = _accepted_order()
    result = brain._run_tool(PHONE, "cancel_order", {})
    assert "error interno" not in result
    assert "PASADO A UNA PERSONA" in result
    updated = orders.get(order["id"])
    assert updated["state"] == "ACCEPTED"
    assert len(updated["handoffs"]) == 1


def test_handoff_is_not_duplicated_and_staff_resolve_it():
    order = _accepted_order()
    first = brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    second = brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    assert "error interno" not in first and "error interno" not in second
    assert len(orders.get(order["id"])["handoffs"]) == 1
    handoff.resolve(order["id"])
    assert orders.get(order["id"])["handoffs"] == []


# ---- request_handoff gate (Task B fixes) --------------------------------

def test_request_handoff_other_refused_before_confirmation():
    """PENDING_CONFIRMATION has no card on the panel, so a box would be invisible."""
    order = _pickup_order()
    assert order["state"] == "PENDING_CONFIRMATION"
    result = brain._run_tool(
        PHONE, "request_handoff", {"topic": "OTHER", "summary": "el cliente pregunta algo"}
    )
    assert result.startswith("NO SE PUDO")
    assert "No le prometas" in result
    updated = orders.get(order["id"])
    assert updated["handoffs"] == []
    assert updated["total"] == order["total"]
    assert [i["item_id"] for i in updated["items"]] == [i["item_id"] for i in order["items"]]


def test_request_handoff_other_reaches_a_person_in_nuevos():
    order = _confirmed_order()
    result = brain._run_tool(
        PHONE, "request_handoff",
        {"topic": "OTHER", "summary": "necesita factura electrónica con NIT"},
    )
    assert "PASADO A UNA PERSONA" in result
    updated = orders.get(order["id"])
    assert [h["summary"] for h in updated["handoffs"]] == ["necesita factura electrónica con NIT"]
    assert updated["state"] == "CONFIRMED"
    assert updated["total"] == order["total"]
    assert [i["item_id"] for i in updated["items"]] == [i["item_id"] for i in order["items"]]


def test_request_handoff_works_once_accepted():
    order = _accepted_order()
    result = brain._run_tool(
        PHONE, "request_handoff", {"topic": "OTHER", "summary": "quiere que lo llamen"}
    )
    assert "PASADO A UNA PERSONA" in result
    handoffs = orders.get(order["id"])["handoffs"]
    assert len(handoffs) == 1
    assert handoffs[0]["summary"] == "quiere que lo llamen"


def test_empty_edit_asks_to_cancel_not_handoff():
    order = _confirmed_order()
    result = brain._run_tool(PHONE, "remove_item", {"item_id": "b_especial"})
    assert result.startswith("NO SE PUDO")
    assert "cancelar" in result
    result = brain._run_tool(
        PHONE, "request_handoff", {"topic": "ORDER_CHANGE", "summary": "quitar la especial"}
    )
    assert result.startswith("NO SE PUDO")
    updated = orders.get(order["id"])
    assert updated["handoffs"] == []
    assert updated["state"] == "CONFIRMED"


# ---- "quiero hablar con una persona" (Task B, 29 de septiembre) ----------

def _ready_order():
    return orders.apply_event(_accepted_order()["id"], "food_ready")


def _order_on_board(state):
    builders = {
        "CONFIRMED": _confirmed_order,
        "ACCEPTED": _accepted_order,
        "READY": _ready_order,
        "OUT_FOR_DELIVERY": _delivery_order_out_for_delivery,
    }
    return builders[state]()


def _open_handoffs() -> int:
    return db.q1("SELECT COUNT(*) AS n FROM handoff_requests")["n"]


@pytest.mark.parametrize("state", ["CONFIRMED", "ACCEPTED", "READY", "OUT_FOR_DELIVERY"])
def test_request_handoff_other_puts_a_box_in_every_column(state):
    order = _order_on_board(state)
    assert order["state"] == state and state in orders.ON_BOARD
    result = brain._run_tool(
        PHONE, "request_handoff", {"topic": "OTHER", "summary": "¿me pueden llamar al fijo?"}
    )
    assert "PASADO A UNA PERSONA" in result
    assert "no le menciones ningún cambio" in result
    assert "no hizo el cambio" not in result
    assert order["code"] not in result
    updated = orders.get(order["id"])
    assert [h["summary"] for h in updated["handoffs"]] == ["¿me pueden llamar al fijo?"]
    assert updated["state"] == state
    assert updated["total"] == order["total"]


@pytest.mark.parametrize("state", ["CONFIRMED", "ACCEPTED"])
@pytest.mark.parametrize("topic, tool", [
    ("ORDER_CHANGE", "add_item"),
    ("CANCEL", "cancel_order"),
    ("PAYMENT_METHOD", "choose_payment"),
    ("ORDER_INFO", "order_status"),
])
def test_request_handoff_covered_topic_points_at_its_tool(state, topic, tool):
    order = _order_on_board(state)
    result = brain._run_tool(PHONE, "request_handoff", {"topic": topic, "summary": "lo que sea"})
    assert result.startswith("NO SE PUDO")
    assert tool in result
    updated = orders.get(order["id"])
    assert updated["handoffs"] == []
    assert updated["state"] == state
    assert updated["total"] == order["total"]
    assert [i["item_id"] for i in updated["items"]] == [i["item_id"] for i in order["items"]]


def _no_orders():
    pass


def _open_cart_only():
    cart.add_item(PHONE, "b_especial", 1)


def _completed_order():
    order = _ready_order()
    orders.apply_event(order["id"], "hand_over")


@pytest.mark.parametrize("build", [_no_orders, _open_cart_only, _pickup_order, _completed_order])
def test_request_handoff_other_without_a_card_promises_nothing(build):
    build()
    result = brain._run_tool(
        PHONE, "request_handoff", {"topic": "OTHER", "summary": "tengo una queja"}
    )
    assert result.startswith("NO SE PUDO")
    assert "No le prometas" in result
    assert _open_handoffs() == 0


def test_request_handoff_without_a_topic_does_nothing():
    """An omitted topic must not reopen the "handoff instead of change" bug."""
    order = _accepted_order()
    result = brain._run_tool(PHONE, "request_handoff", {"summary": "quitar la especial"})
    assert result.startswith("NO SE PUDO")
    assert "topic" in result
    assert orders.get(order["id"])["handoffs"] == []
    assert _open_handoffs() == 0


@pytest.mark.parametrize("args", [{"topic": "OTHER"}, {"topic": "OTHER", "summary": "   "}])
def test_request_handoff_without_a_summary_does_nothing(args):
    """Its own error, not the catch-all KeyError one about an item not on the menu."""
    order = _accepted_order()
    result = brain._run_tool(PHONE, "request_handoff", args)
    assert result.startswith("NO SE PUDO: falta summary")
    assert "carta" not in result
    assert orders.get(order["id"])["handoffs"] == []


def test_two_live_orders_state_block_and_handoff_card():
    """An accepted order plus a separate one: the block lists both, newest first, and
    OTHER's box goes on the newest order that has a card."""
    first = _accepted_order()
    brain._run_tool(PHONE, "start_separate_order", {})
    cart.add_item(PHONE, "d_gaseosa_personal", 1)
    second = orders.create_from_cart(PHONE, "PICKUP")
    assert second["related_order_id"] == first["id"]

    # The separate order still waits for its "sí" and has no card: the box goes on the first.
    block = _real_state_block(brain.system_prompt(PHONE))
    assert ("- Pedido más reciente: esperando su confirmación.\n"
            "- Otro pedido: aceptado por el local.") in block
    brain._run_tool(PHONE, "request_handoff", {"topic": "OTHER", "summary": "sobre el primero"})
    assert [h["summary"] for h in orders.get(first["id"])["handoffs"]] == ["sobre el primero"]
    assert orders.get(second["id"])["handoffs"] == []

    # Confirmed, it sits in Nuevos and is the newest card: the next box goes there.
    orders.apply_event(second["id"], "customer_confirms")
    block = _real_state_block(brain.system_prompt(PHONE))
    assert ("- Pedido más reciente: confirmado; el local todavía no lo ha aceptado.\n"
            "- Otro pedido: aceptado por el local.") in block
    brain._run_tool(PHONE, "request_handoff", {"topic": "OTHER", "summary": "sobre el segundo"})
    assert [h["summary"] for h in orders.get(second["id"])["handoffs"]] == ["sobre el segundo"]
    assert [h["summary"] for h in orders.get(first["id"])["handoffs"]] == ["sobre el primero"]


def _real_state_block(prompt: str) -> str:
    """Only the part code computes; SYSTEM itself already mentions both phrases."""
    return prompt.split("ESTADO REAL AHORA", 1)[1]


def test_system_prompt_carries_the_real_order_state():
    order = _confirmed_order()
    block = _real_state_block(brain.system_prompt(PHONE))
    assert "todavía no lo ha aceptado" in block
    assert "aceptado por el local" not in block

    orders.apply_event(order["id"], "staff_accepts")
    prompt = brain.system_prompt(PHONE)
    block = _real_state_block(prompt)
    assert "aceptado por el local" in block
    assert "todavía no lo ha aceptado" not in block
    assert order["code"] not in prompt


def _capturing_client(responses, seen):
    class FakeMessages:
        def create(self, **kwargs):
            seen.append(kwargs["system"])
            return responses.pop(0)
    return types.SimpleNamespace(messages=FakeMessages())


def test_reply_passes_the_fresh_order_state(monkeypatch):
    monkeypatch.setattr(brain, "_model", "test-model")
    seen = []
    responses = [
        types.SimpleNamespace(
            stop_reason="end_turn", content=[types.SimpleNamespace(type="text", text="Listo.")]
        )
        for _ in range(2)
    ]
    fake = _capturing_client(responses, seen)
    monkeypatch.setattr(brain, "client", lambda: fake)

    brain.forget(PHONE)
    try:
        order = _confirmed_order()
        brain.reply(PHONE, "hola")
        orders.apply_event(order["id"], "staff_accepts")   # notify() never enters the history
        brain.reply(PHONE, "¿y mi pedido?")
    finally:
        brain.forget(PHONE)

    assert "todavía no lo ha aceptado" in _real_state_block(seen[0])
    assert "aceptado por el local" in _real_state_block(seen[1])
    assert "todavía no lo ha aceptado" not in _real_state_block(seen[1])


# ---- vocabulary: aceptado por el local (Task B fixes) -------------------

def test_confirm_order_and_status_use_aceptado_por_el_local():
    order = _pickup_order()
    result = brain._run_tool(PHONE, "confirm_order", {})
    assert "enviado al local" not in result
    assert "no lo ha aceptado" in result

    result = brain._run_tool(PHONE, "order_status", {})
    assert "recibido" not in result

    orders.apply_event(order["id"], "staff_accepts")
    result = brain._run_tool(PHONE, "order_status", {})
    assert "aceptado por el local" in result


def test_show_cart_text_differs_by_window():
    _confirmed_order()
    result = brain._run_tool(PHONE, "show_cart", {})
    assert "todavía no lo ha aceptado" in result

    db.reset()
    _accepted_order()
    result = brain._run_tool(PHONE, "show_cart", {})
    assert "ya lo aceptó" in result


def test_no_leftover_vocabulary_in_system_or_tools():
    forbidden = ("enviado al local", "ya recibió", "no ha recibido", "recibido por el local")
    for phrase in forbidden:
        assert phrase not in brain.SYSTEM
    for tool in brain.TOOLS:
        for phrase in forbidden:
            assert phrase not in tool["description"]


# ---- payment follows an edit (Task B) ----------------------------------

def test_edit_moves_transfer_and_cash_to_the_new_total():
    order = _transfer_order()
    result = brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    assert "error interno" not in result
    payment = orders.get(order["id"])["payment"]
    assert payment["transfer_amount"] == ESPECIAL + GASEOSA

    db.reset()
    order = _confirmed_order()
    payments.set_method(order["id"], "CASH")
    result = brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    assert "error interno" not in result
    payment = orders.get(order["id"])["payment"]
    assert payment["cash_amount"] == ESPECIAL + GASEOSA


def test_edit_keeps_the_agreed_transfer_of_a_mixed_payment():
    order = _confirmed_order()          # total ESPECIAL
    payments.set_method(order["id"], "MIXED", transfer_amount=10000, cash_amount=ESPECIAL - 10000)
    result = brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    assert "error interno" not in result
    payment = orders.get(order["id"])["payment"]
    assert payment["transfer_amount"] == 10000
    assert payment["cash_amount"] == ESPECIAL + GASEOSA - 10000


def test_edit_after_a_clean_receipt_asks_for_the_difference():
    order = _pickup_order()             # total ESPECIAL, no method chosen
    orders.apply_event(order["id"], "customer_confirms")
    payments.check_proof(order["id"], {
        "amount": ESPECIAL, "destination_account": ACCOUNT, "reference": "EDIT-1",
        "date_iso": _iso(),
    })
    result = brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    assert "error interno" not in result
    assert fmt(GASEOSA) in result
    payment = orders.get(order["id"])["payment"]
    assert payment["transfer_amount"] == ESPECIAL + GASEOSA


def test_edit_below_a_receipt_records_the_surplus():
    cart.add_item(PHONE, "b_especial", 1)
    cart.add_item(PHONE, "d_gaseosa_personal", 1)
    order = orders.create_from_cart(PHONE, "PICKUP")
    orders.apply_event(order["id"], "customer_confirms")
    payments.check_proof(order["id"], {
        "amount": ESPECIAL + GASEOSA, "destination_account": ACCOUNT, "reference": "EDIT-2",
        "date_iso": _iso(),
    })
    result = brain._run_tool(PHONE, "remove_item", {"item_id": "d_gaseosa_personal"})
    assert "error interno" not in result
    payment = orders.get(order["id"])["payment"]
    assert payment["overpayment_amount"] == GASEOSA
    # The receipt nobody confirmed is never "paid", and its surplus is not money owed back.
    assert (
        f"«Su comprobante por {fmt(ESPECIAL + GASEOSA)} está pendiente de confirmación del "
        "local. Por ahora no necesita realizar otro pago. Con su comprobante pendiente, lo "
        "enviado supera el total del pedido; el local revisará la diferencia cuando lo "
        "confirme.»" in result
    )
    assert "No le prometas devolución" in result


@pytest.mark.parametrize("with_transfer", [False, True])
def test_edit_below_registered_cash_reports_the_surplus(with_transfer):
    """Cash registered while the order is still editable, then an edit that lowers the
    total: the surplus includes the cash, and the bot's line says how it was paid."""
    cart.add_item(PHONE, "b_especial", 1)
    cart.add_item(PHONE, "d_gaseosa_personal", 1)
    order = orders.create_from_cart(PHONE, "PICKUP")
    orders.apply_event(order["id"], "customer_confirms")
    if with_transfer:
        record = _receipt(order["id"], 10000, "EDITCASH-1")          # infers MIXED
        payments.confirm_receipt(order["id"], record["receipt"]["id"])
        payments.register_cash(order["id"], payments.current(order["id"])["outstanding"])
        paid_in = (f"Ya pagó {fmt(ESPECIAL + GASEOSA - 10000)} en efectivo. El local ya "
                   f"confirmó su transferencia de {fmt(10000)}.")
    else:
        payments.set_method(order["id"], "CASH")
        payments.register_cash(order["id"], ESPECIAL + GASEOSA)
        paid_in = f"Ya pagó {fmt(ESPECIAL + GASEOSA)} en efectivo."

    result = brain._run_tool(PHONE, "remove_item", {"item_id": "d_gaseosa_personal"})
    assert "error interno" not in result
    assert (
        f"«{paid_in} Su pedido ya está pagado completo. Pagó {fmt(GASEOSA)} más que el total "
        "del pedido; el local se comunicará con usted por esa diferencia.»" in result
    )
    assert "No le prometas devolución" in result
    assert orders.get(order["id"])["payment"]["overpayment_amount"] == GASEOSA


def test_edit_after_a_red_receipt_does_not_ask_to_pay_again():
    order = _transfer_order()
    payments.check_proof(order["id"], {
        "amount": ESPECIAL - 5000, "destination_account": "3009998877", "reference": "EDIT-3",
        "date_iso": _iso(),
    })
    result = brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    assert "error interno" not in result
    assert "revisando" in result.lower()
    assert "Faltan" not in result


def test_edit_after_a_confirmed_payment_shows_the_balance():
    order = _pickup_order()
    record = _receipt(order["id"], ESPECIAL, "EDIT-4")
    payments.confirm_receipt(order["id"], record["receipt"]["id"], ESPECIAL)
    orders.apply_event(order["id"], "customer_confirms")
    result = brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    assert "error interno" not in result
    payment = orders.get(order["id"])["payment"]
    assert payment["paid"] == ESPECIAL
    assert payment["outstanding"] == GASEOSA


# ---- one row per receipt (Task C) --------------------------------------

def _client(monkeypatch):
    """The real app through TestClient. WhatsApp is forced into FAKE mode, so no
    message can leave even if .env has a token."""
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(wa, "live", lambda: False)
    return TestClient(main.app)


def _outbound_count() -> int:
    return db.q1("SELECT COUNT(*) AS n FROM outbound_messages")["n"]


def _second_transfer_order():
    """Another confirmed TRANSFER order for the same customer."""
    cart.add_item(PHONE, "b_especial", 1)
    order = orders.create_from_cart(PHONE, "PICKUP")
    orders.apply_event(order["id"], "customer_confirms")
    payments.set_method(order["id"], "TRANSFER")
    return order


def _add_gaseosa() -> str:
    result = brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    assert "error interno" not in result
    return result


def test_every_receipt_is_its_own_row():
    order = _transfer_order()
    _receipt(order["id"], ESPECIAL, "FIRST-1")
    record = _receipt(order["id"], GASEOSA, "SECOND-1")
    rows = payments.receipts(order["id"])
    assert len(rows) == 2
    first, second = rows
    assert first["status"] == "PROOF_RECEIVED"       # the first receipt is untouched
    assert first["flags"] == []
    assert first["reference"] == "FIRST-1"
    assert second["status"] == "FLAGGED"
    assert "No se esperaba otro pago." in second["flags"]
    assert record["receipt"]["id"] == second["id"]
    assert not any("otro comprobante" in f for r in rows for f in r["flags"])


def test_difference_receipt_for_the_missing_amount_is_clean():
    order = _confirmed_order()                       # no method chosen yet
    first = _receipt(order["id"], ESPECIAL, "DIFF-1")
    assert first["method"] == "TRANSFER"             # inferred from the clean receipt
    assert fmt(GASEOSA) in _add_gaseosa()
    second = _receipt(order["id"], GASEOSA, "DIFF-2")
    assert second["receipt"]["status"] == "PROOF_RECEIVED"
    assert second["receipt"]["flags"] == []
    assert second["transfer_due"] == 0


def test_difference_receipt_with_the_wrong_amount_is_red_alone():
    order = _confirmed_order()
    _receipt(order["id"], ESPECIAL, "DIFFBAD-1")
    _add_gaseosa()
    second = _receipt(order["id"], GASEOSA - 500, "DIFFBAD-2")
    assert second["receipt"]["status"] == "FLAGGED"
    assert any(fmt(GASEOSA) in f for f in second["receipt"]["flags"])
    first = payments.receipts(order["id"])[0]
    assert first["status"] == "PROOF_RECEIVED"       # the first row does not change
    assert first["flags"] == []


def test_same_receipt_sent_again_is_a_resend():
    order = _transfer_order()
    good = {
        "amount": ESPECIAL, "destination_account": ACCOUNT, "reference": "RESEND-1",
        "date_iso": _iso(),
    }
    payments.check_proof(order["id"], good, photo=(b"img", "image/png"))
    again = payments.check_proof(order["id"], good, photo=(b"img", "image/png"))
    assert again["resent"] is True
    assert again["receipt"] is None
    rows = payments.receipts(order["id"])
    assert len(rows) == 1
    assert rows[0]["flags"] == []
    assert db.q1("SELECT COUNT(*) AS n FROM receipt_photos")["n"] == 1


def test_same_reference_with_another_amount_is_a_red_row(monkeypatch):
    """The manual Task C run: after an edit, the difference receipt repeats the
    first receipt's reference. Not a resend: a red row, with its photo."""
    from app import main
    monkeypatch.setattr(wa, "live", lambda: False)
    order = _transfer_order()
    _receipt(order["id"], ESPECIAL, "SAME-1")
    _add_gaseosa()
    told = []
    monkeypatch.setattr(brain, "extract_proof", lambda image: {
        "amount": GASEOSA, "destination_account": ACCOUNT, "reference": "SAME-1",
        "date_iso": _iso(),
    })
    monkeypatch.setattr(brain, "reply", lambda phone, message, **kw: told.append(message) or "ok")

    main.handle(PHONE, None, wa.image_block(b"img-2", "image/png"), db.now())

    first, red = payments.receipts(order["id"])
    assert first["status"] == "PROOF_RECEIVED"       # the first row does not change
    assert first["flags"] == []
    assert first["reference"] == "SAME-1"
    assert red["status"] == "FLAGGED"
    # The amount matched what was owed: the repeated reference is the only problem.
    assert red["flags"] == ["Este comprobante ya fue usado antes (referencia SAME-1)."]
    assert red["reference"] is None
    assert red["extracted"]["reference"] == "SAME-1"
    assert payments.photo(red["id"]) == (b"img-2", "image/png")
    assert "encontró un problema" in told[-1]
    assert "ya lo teníamos" not in told[-1]


@pytest.mark.parametrize("first_amount,again_amount", [
    (ESPECIAL, None), (ESPECIAL, "ilegible"), (None, None),
])
def test_same_reference_without_a_readable_amount_is_red(first_amount, again_amount):
    order = _transfer_order()
    _receipt(order["id"], first_amount, "SAME-2")
    again = _receipt(order["id"], again_amount, "SAME-2")
    assert again["resent"] is False
    assert again["receipt"]["status"] == "FLAGGED"
    assert "Este comprobante ya fue usado antes (referencia SAME-2)." in again["receipt"]["flags"]
    assert again["receipt"]["reference"] is None
    assert len(payments.receipts(order["id"])) == 2


def test_same_receipt_after_an_edit_is_still_a_resend():
    """A resend is compared with the receipt it repeats, not with what is owed
    now, and its amount is read by read_ocr_amount ("$29.900" == 29900)."""
    order = _transfer_order()
    _receipt(order["id"], ESPECIAL, "SAME-3")
    _add_gaseosa()
    again = _receipt(order["id"], fmt(ESPECIAL), "SAME-3")
    assert again["resent"] is True
    assert again["receipt"] is None
    assert len(payments.receipts(order["id"])) == 1


def test_second_receipt_reference_blocks_another_order():
    a = _transfer_order()
    assert _receipt(a["id"], ESPECIAL, "A-1")["receipt"]["status"] == "PROOF_RECEIVED"
    _add_gaseosa()
    assert _receipt(a["id"], GASEOSA, "A-2")["receipt"]["status"] == "PROOF_RECEIVED"

    b = _second_transfer_order()
    reused = _receipt(b["id"], GASEOSA, "A-2")       # the difference receipt of order A
    assert reused["receipt"]["status"] == "FLAGGED"
    assert any("ya fue usado" in f for f in reused["receipt"]["flags"])
    assert reused["receipt"]["reference"] is None    # the reference belongs to A's row
    assert reused["receipt"]["extracted"]["reference"] == "A-2"

    first_again = _receipt(b["id"], ESPECIAL, "A-1")
    assert first_again["receipt"]["status"] == "FLAGGED"
    assert any("ya fue usado" in f for f in first_again["receipt"]["flags"])


def test_wrong_screenshot_then_the_right_one():
    order = _confirmed_order()                       # no method chosen
    wrong = _receipt(order["id"], 1234, "WRONGFIRST-1", destination_account="3009998877")
    assert wrong["receipt"]["status"] == "FLAGGED"
    assert wrong["method"] == "UNSPECIFIED"          # a red receipt never sets the method
    right = _receipt(order["id"], ESPECIAL, "WRONGFIRST-2")
    assert right["receipt"]["status"] == "PROOF_RECEIVED"
    assert right["receipt"]["flags"] == []
    assert right["method"] == "TRANSFER"
    assert "Llegó mientras otro comprobante está en revisión" in right["receipt"]["notes"]
    assert payments.receipts(order["id"])[0]["status"] == "FLAGGED"
    assert right["status"] == "FLAGGED"
    assert right["under_review"] is True


def test_receipt_while_another_is_red_is_checked_normally():
    order = _transfer_order()
    _receipt(order["id"], ESPECIAL, "Q2-1")
    _add_gaseosa()
    red = _receipt(order["id"], GASEOSA - 500, "Q2-2")
    assert red["receipt"]["status"] == "FLAGGED"
    clean = _receipt(order["id"], GASEOSA, "Q2-3")
    assert clean["receipt"]["status"] == "PROOF_RECEIVED"
    assert clean["receipt"]["flags"] == []
    assert "Llegó mientras otro comprobante está en revisión" in clean["receipt"]["notes"]
    payments.confirm_receipt(order["id"], red["receipt"]["id"], GASEOSA - 500)
    done = payments.confirm_receipt(order["id"], clean["receipt"]["id"], GASEOSA)
    assert done["overpayment_amount"] == GASEOSA - 500


def test_mixed_customer_can_transfer_the_cash_part():
    order = _confirmed_order()
    payments.set_method(order["id"], "MIXED", transfer_amount=10000, cash_amount=ESPECIAL - 10000)
    first = _receipt(order["id"], 10000, "Q3-1")
    second = _receipt(order["id"], ESPECIAL - 10000, "Q3-2")
    assert first["receipt"]["flags"] == []
    assert second["receipt"]["status"] == "PROOF_RECEIVED"
    assert second["receipt"]["flags"] == []
    payments.confirm_receipt(order["id"], first["receipt"]["id"])
    done = payments.confirm_receipt(order["id"], second["receipt"]["id"])
    assert done["outstanding"] == 0
    assert done["cash_received"] == 0                # no cash was registered

    db.reset()
    order = _confirmed_order()
    payments.set_method(order["id"], "MIXED", transfer_amount=10000, cash_amount=ESPECIAL - 10000)
    _receipt(order["id"], 10000, "Q3-3")
    short = _receipt(order["id"], 5000, "Q3-4")      # not the cash part
    assert short["receipt"]["status"] == "FLAGGED"
    assert any(fmt(ESPECIAL - 10000) in f for f in short["receipt"]["flags"])


def test_receipt_when_nothing_is_owed_is_red():
    order = _transfer_order()
    first = _receipt(order["id"], ESPECIAL, "PAID-1")
    payments.confirm_receipt(order["id"], first["receipt"]["id"])
    extra = _receipt(order["id"], GASEOSA, "PAID-2")
    assert extra["receipt"]["status"] == "FLAGGED"
    assert "No se esperaba otro pago." in extra["receipt"]["flags"]
    assert payments.receipts(order["id"])[0]["status"] == "CONFIRMED"
    assert extra["paid"] == ESPECIAL


def test_miguel_mixed_example():
    """$50.000 = 30.000 transfer + 20.000 cash, with the transfer confirmed at
    $35.000: no surplus and 15.000 still to collect in cash. The agreed $20.000 is
    refused, because $5.000 of it is change. Registering the $15.000 still owed
    completes the payment with no surplus. Same shape at the menu's prices."""
    a = 20000
    order = _confirmed_order()
    payments.set_method(order["id"], "MIXED", transfer_amount=a, cash_amount=ESPECIAL - a)
    record = _receipt(order["id"], a, "MIGUEL-1")
    confirmed = payments.confirm_receipt(order["id"], record["receipt"]["id"], a + 5000)
    assert confirmed["overpayment_amount"] == 0
    assert confirmed["outstanding"] == confirmed["cash_due"] == ESPECIAL - a - 5000
    with pytest.raises(payments.PaymentError):
        payments.register_cash(order["id"], ESPECIAL - a)
    paid = payments.register_cash(order["id"], confirmed["outstanding"])
    assert paid["outstanding"] == 0
    assert paid["overpayment_amount"] == 0


def test_cash_to_collect_follows_the_confirmed_transfer():
    """The card shows cash_due, not the agreed cash part and not `outstanding`,
    which only counts confirmed receipts."""
    cart.add_item(PHONE, "b_caserita", 1)
    cart.add_item(PHONE, "d_gaseosa_personal", 1)
    order = orders.create_from_cart(PHONE, "PICKUP")
    total = CASERITA + GASEOSA                       # 22.400 at today's prices
    orders.apply_event(order["id"], "customer_confirms")
    payments.set_method(order["id"], "MIXED", transfer_amount=10000, cash_amount=total - 10000)
    record = _receipt(order["id"], 15000, "CASHDUE-1")          # the agreed part is 10000: red
    assert record["receipt"]["status"] == "FLAGGED"
    assert record["cash_due"] == total - 10000                  # 12.400
    confirmed = payments.confirm_receipt(order["id"], record["receipt"]["id"], 15000)
    assert confirmed["cash_due"] == total - 15000               # 7.400
    assert confirmed["outstanding"] == total - 15000
    assert confirmed["overpayment_amount"] == 0
    assert confirmed["transfer_due"] == 0

    # A clean receipt of exactly the agreed part that nobody has confirmed yet.
    cart.add_item(PHONE, "b_caserita", 1)
    cart.add_item(PHONE, "d_gaseosa_personal", 1)
    second = orders.create_from_cart(PHONE, "PICKUP")
    orders.apply_event(second["id"], "customer_confirms")
    payments.set_method(second["id"], "MIXED", transfer_amount=10000, cash_amount=total - 10000)
    waiting = _receipt(second["id"], 10000, "CASHDUE-2")
    assert waiting["receipt"]["status"] == "PROOF_RECEIVED"
    assert waiting["cash_due"] == total - 10000
    assert waiting["outstanding"] == total           # this is why the card shows cash_due


def test_confirming_one_of_two_receipts_mentions_the_other():
    from app import main
    order = _transfer_order()
    first = _receipt(order["id"], ESPECIAL, "Q4-1")
    _add_gaseosa()
    _receipt(order["id"], GASEOSA, "Q4-2")
    record = payments.confirm_receipt(order["id"], first["receipt"]["id"])
    message = main._payment_confirm_message(record)
    assert "sigue en revisión" in message
    assert fmt(ESPECIAL) in message
    assert "P-" not in message and order["code"] not in message
    assert "cocina" not in message.lower()


def test_order_status_is_the_worst_receipt():
    order = _transfer_order()
    assert payments.current(order["id"])["status"] == "PENDING"
    clean = _receipt(order["id"], ESPECIAL, "WORST-1")
    assert clean["status"] == "PROOF_RECEIVED"
    confirmed = payments.confirm_receipt(order["id"], clean["receipt"]["id"])
    assert confirmed["status"] == "CONFIRMED"
    red = _receipt(order["id"], GASEOSA, "WORST-2")             # nothing more is owed
    assert red["status"] == "FLAGGED"
    assert red["under_review"] is True
    after = payments.discard_receipt(order["id"], red["receipt"]["id"], "Llegó por error")
    assert after["status"] == "CONFIRMED"
    assert after["under_review"] is False

    second = _second_transfer_order()                # only discarded receipts: nothing counts
    only_red = _receipt(second["id"], 1, "WORST-3")
    assert payments.discard_receipt(second["id"], only_red["receipt"]["id"], "Llegó por error")["status"] == "PENDING"


def test_confirmed_receipt_keeps_its_flags_for_review():
    order = _transfer_order()
    red = _receipt(order["id"], ESPECIAL - 5000, "B6-1")["receipt"]
    assert red["flags"]
    confirmed = payments.confirm_receipt(order["id"], red["id"], ESPECIAL - 5000)
    assert confirmed["receipt"]["status"] == "CONFIRMED"
    assert confirmed["receipt"]["flags"] == red["flags"]


def test_confirm_receipt_refuses_wrong_order_and_double_confirm():
    first = _transfer_order()
    receipt = _receipt(first["id"], ESPECIAL, "REFUSE-1")["receipt"]
    other = _second_transfer_order()
    with pytest.raises(payments.PaymentError):
        payments.confirm_receipt(other["id"], receipt["id"])
    payments.confirm_receipt(first["id"], receipt["id"])
    with pytest.raises(payments.PaymentError):
        payments.confirm_receipt(first["id"], receipt["id"])


def test_confirm_refuses_zero(monkeypatch):
    client = _client(monkeypatch)
    order = _transfer_order()
    receipt = _receipt(order["id"], ESPECIAL, "ZERO-1")["receipt"]
    with pytest.raises(payments.PaymentError, match="descártelo con la X"):
        payments.confirm_receipt(order["id"], receipt["id"], 0)
    assert payments.receipts(order["id"])[0]["status"] == "PROOF_RECEIVED"

    before = _outbound_count()
    response = client.post(
        f"/api/orders/{order['id']}/receipts/{receipt['id']}/confirm", json={"amount": 0}
    )
    assert response.status_code == 400
    assert "descártelo con la X" in response.json()["error"]
    assert payments.receipts(order["id"])[0]["status"] == "PROOF_RECEIVED"
    assert _outbound_count() == before               # nothing reaches the customer


def test_confirmed_and_discarded_are_final(monkeypatch):
    client = _client(monkeypatch)
    order = _transfer_order()
    confirmed = _receipt(order["id"], ESPECIAL, "FINAL-1")["receipt"]
    payments.confirm_receipt(order["id"], confirmed["id"])
    discarded = _receipt(order["id"], GASEOSA, "FINAL-2")["receipt"]     # red: nothing more owed
    payments.discard_receipt(order["id"], discarded["id"], "Llegó por error")

    base = f"/api/orders/{order['id']}/receipts"
    # With a reason, so the 400 is about finality and not about the missing reason.
    refused = client.post(f"{base}/{confirmed['id']}/discard", json={"reason": "Llegó por error"})
    assert refused.status_code == 400
    assert refused.json()["error"] == "Un comprobante confirmado no se puede descartar."
    assert client.post(f"{base}/{discarded['id']}/confirm", json={}).status_code == 400
    states = [r["status"] for r in payments.receipts(order["id"])]
    assert states == ["CONFIRMED", "DISCARDED"]


def test_discarding_a_red_receipt_clears_the_review():
    order = _transfer_order()
    red = _receipt(order["id"], 1, "CLEAR-1")
    assert reports.snapshot(1)["receipts_flagged"] == 1
    assert red["status"] == "FLAGGED"
    assert red["under_review"] is True

    after = payments.discard_receipt(order["id"], red["receipt"]["id"], "Llegó por error")
    assert after["status"] == "PENDING"
    assert after["under_review"] is False
    assert reports.snapshot(1)["receipts_flagged"] == 0
    assert after["receipt"]["status"] == "DISCARDED"
    assert after["receipt"]["discarded_at"]
    assert after["receipt"]["counted"] == 0


def test_discarded_reference_still_blocks_reuse():
    a = _transfer_order()
    first = _receipt(a["id"], ESPECIAL, "D-1")
    payments.discard_receipt(a["id"], first["receipt"]["id"], "Llegó por error")

    b = _second_transfer_order()
    reused = _receipt(b["id"], ESPECIAL, "D-1")
    assert reused["receipt"]["status"] == "FLAGGED"
    assert any("ya fue usado" in f for f in reused["receipt"]["flags"])

    again = _receipt(a["id"], ESPECIAL, "D-1")
    assert again["resent"] is True


def test_discard_needs_a_reason(monkeypatch):
    client = _client(monkeypatch)
    order = _transfer_order()
    receipt = _receipt(order["id"], 1, "REASON-1")["receipt"]
    before = _outbound_count()
    url = f"/api/orders/{order['id']}/receipts/{receipt['id']}/discard"
    for response in (client.post(url), client.post(url, json={}),
                     client.post(url, json={"reason": "   "})):
        assert response.status_code == 400
        assert response.json()["error"] == "Hace falta el motivo para avisarle al cliente."
    with pytest.raises(payments.PaymentError):
        payments.discard_receipt(order["id"], receipt["id"], "")
    assert payments.receipts(order["id"])[0]["status"] == "FLAGGED"   # nothing changed
    assert _outbound_count() == before                                # and nothing was sent


DISCARD_REASON = "Este comprobante llegó por error; su pago ya estaba completo."


def _discard_through_route(monkeypatch, model_text):
    """P-0458: a confirmed receipt, then a second one by mistake (red), then staff
    discard it through the route. The model answers `model_text` ({code} is the
    order code), or fails when it is None. Returns (order, messages sent, history)."""
    client = _client(monkeypatch)
    monkeypatch.setattr(brain, "_model", "test-model")
    order = _transfer_order()
    confirmed = _receipt(order["id"], ESPECIAL, "DISC-1")["receipt"]
    payments.confirm_receipt(order["id"], confirmed["id"])
    extra = _receipt(order["id"], 30000, "DISC-2")["receipt"]     # red: nothing more owed
    if model_text is None:
        def down():
            raise RuntimeError("model down")
        monkeypatch.setattr(brain, "client", down)
    else:
        answer = types.SimpleNamespace(stop_reason="end_turn", content=[
            types.SimpleNamespace(type="text", text=model_text.format(code=order["code"])),
        ])
        fake = _capturing_client([answer], [])
        monkeypatch.setattr(brain, "client", lambda: fake)

    before = db.q1("SELECT COALESCE(MAX(id),0) AS m FROM outbound_messages")["m"]
    brain.forget(PHONE)
    try:
        response = client.post(
            f"/api/orders/{order['id']}/receipts/{extra['id']}/discard",
            json={"reason": DISCARD_REASON},
        )
        history = list(brain._history.get(PHONE, []))
    finally:
        brain.forget(PHONE)
    assert response.status_code == 200
    assert response.json()["receipt"]["status"] == "DISCARDED"
    sent = [r["body"] for r in db.q(
        "SELECT body FROM outbound_messages WHERE id>%s ORDER BY id", (before,)
    )]
    return order, sent, history


def _assert_safe(order, text):
    assert order["code"] not in text
    assert "cocina" not in text.lower()
    assert "confirmad" not in text.lower()
    assert "$" not in text


def test_discard_tells_the_customer_once_from_the_reason(monkeypatch):
    good = "Revisamos el comprobante que nos envió por error y no lo tomamos como pago."
    order, sent, history = _discard_through_route(monkeypatch, good)
    assert sent == [good]                                  # exactly one message
    _assert_safe(order, sent[0])
    assert DISCARD_REASON in history[-2]["content"]        # the bot learns about the discard


def test_discard_falls_back_to_the_template_when_the_model_fails(monkeypatch):
    from app import main
    order, sent, _ = _discard_through_route(monkeypatch, None)
    assert sent == [main.DISCARD_TEMPLATE.format(reason=DISCARD_REASON)]
    _assert_safe(order, sent[0])


@pytest.mark.parametrize("bad", [
    "Su pago ya quedó confirmado; este otro comprobante no cuenta.",
    "Descartamos el comprobante de $30.000 porque llegó por error.",
    "Su pedido {code} sigue igual; el comprobante no cuenta.",
    "La cocina ya tiene su pedido; el comprobante no cuenta.",
])
def test_discard_guard_sends_the_template(monkeypatch, bad):
    from app import main
    order, sent, history = _discard_through_route(monkeypatch, bad)
    template = main.DISCARD_TEMPLATE.format(reason=DISCARD_REASON)
    assert sent == [template]
    _assert_safe(order, sent[0])
    # The history says what the customer actually got, not the rejected text.
    assert history[-1] == {"role": "assistant", "content": [{"type": "text", "text": template}]}


def test_discard_reason_is_on_the_board(monkeypatch):
    client = _client(monkeypatch)
    order = _transfer_order()
    red = _receipt(order["id"], 1, "BOARD-1")["receipt"]
    payments.discard_receipt(order["id"], red["id"], f"  {DISCARD_REASON}  ")
    card = next(o for o in client.get("/api/board").json()["orders"] if o["id"] == order["id"])
    found = card["payment"]["receipts"][0]
    assert found["status"] == "DISCARDED"
    assert found["discard_reason"] == DISCARD_REASON              # stored trimmed


def test_real_state_shows_a_discarded_receipt_as_discarded():
    """P-0458: after the discard the line must name it, or the model keeps the
    "en revisión" from the chat history."""
    order = _transfer_order()
    confirmed = _receipt(order["id"], ESPECIAL, "GONE-1")["receipt"]
    payments.confirm_receipt(order["id"], confirmed["id"])
    extra = _receipt(order["id"], 30000, "GONE-2")["receipt"]
    payments.discard_receipt(order["id"], extra["id"], DISCARD_REASON)
    prompt = brain.system_prompt(PHONE)
    assert _payment_line(prompt) == (
        "- Pago del pedido más reciente: por transferencia; comprobantes: 1 confirmado por "
        "una persona del local, 1 descartado por el local; ya no falta nada por transferir; "
        "ya está pagado completo."
    )
    assert "nunca digas que sigue en revisión" in _real_state_block(prompt)
    assert DISCARD_REASON not in prompt                            # no staff text in the block

    only = _second_transfer_order()                                # its only receipt discarded
    red = _receipt(only["id"], 1, "GONE-3")["receipt"]
    payments.discard_receipt(only["id"], red["id"], DISCARD_REASON)
    line = _payment_line(brain.system_prompt(PHONE))
    assert line == (
        "- Pago del pedido más reciente: por transferencia; comprobantes: 1 descartado por "
        "el local; todavía falta por transferir; todavía falta por pagar."
    )


def test_discarded_wrong_screenshot_and_confirmed_right_one():
    order = _confirmed_order()
    wrong = _receipt(order["id"], 1234, "Q5-1", destination_account="3009998877")
    right = _receipt(order["id"], ESPECIAL, "Q5-2")
    payments.confirm_receipt(order["id"], right["receipt"]["id"])
    done = payments.discard_receipt(order["id"], wrong["receipt"]["id"], "Llegó por error")
    assert done["status"] == "CONFIRMED"
    assert done["under_review"] is False
    assert done["outstanding"] == 0


def test_receipt_photo_is_stored_and_served(monkeypatch):
    client = _client(monkeypatch)
    order = _transfer_order()
    record = payments.check_proof(order["id"], {
        "amount": ESPECIAL, "destination_account": ACCOUNT, "reference": "PHOTO-1",
        "date_iso": _iso(),
    }, photo=(b"\x89PNG-test", "image/png"))
    receipt = record["receipt"]
    assert receipt["has_photo"] is True
    assert not any(isinstance(v, bytes) for r in record["receipts"] for v in r.values())
    got = client.get(f"/api/receipts/{receipt['id']}/photo")
    assert got.status_code == 200
    assert got.content == b"\x89PNG-test"
    assert got.headers["content-type"] == "image/png"

    bare = _receipt(order["id"], GASEOSA, "PHOTO-2")["receipt"]        # no photo
    assert bare["has_photo"] is False
    missing = client.get(f"/api/receipts/{bare['id']}/photo")
    assert missing.status_code == 404
    assert missing.text == "Sin foto."


def test_confirm_route_sends_the_code_built_message(monkeypatch):
    client = _client(monkeypatch)
    order = _transfer_order()
    receipt = _receipt(order["id"], ESPECIAL, "ROUTE-1")["receipt"]
    response = client.post(
        f"/api/orders/{order['id']}/receipts/{receipt['id']}/confirm", json={"amount": ESPECIAL}
    )
    assert response.status_code == 200
    last = db.q1(
        "SELECT body FROM outbound_messages WHERE phone=%s ORDER BY id DESC LIMIT 1", (PHONE,)
    )
    assert last["body"].startswith("Confirmamos")
    bad = client.post(f"/api/orders/{order['id']}/receipts/999999/confirm", json={})
    assert bad.status_code == 400


def test_handle_stores_the_photo_and_answers_a_resend(monkeypatch):
    from app import main
    monkeypatch.setattr(wa, "live", lambda: False)
    order = _transfer_order()
    extracted = {
        "amount": ESPECIAL, "destination_account": ACCOUNT, "reference": "HANDLE-1",
        "date_iso": _iso(),
    }
    told = []
    monkeypatch.setattr(brain, "extract_proof", lambda image: extracted)
    monkeypatch.setattr(brain, "reply", lambda phone, message, **kw: told.append(message) or "ok")
    image = wa.image_block(b"img", "image/png")

    assert main.handle(PHONE, None, image, db.now()) == "ok"
    assert db.q1("SELECT COUNT(*) AS n FROM receipt_photos")["n"] == 1
    assert "recibió un comprobante" in told[-1]
    stored = payments.receipts(order["id"])[0]
    assert payments.photo(stored["id"]) == (b"img", "image/png")

    main.handle(PHONE, None, image, db.now())
    assert "ya lo teníamos" in told[-1]
    assert len(payments.receipts(order["id"])) == 1


def test_reset_removes_receipts_and_photos():
    order = _transfer_order()
    payments.check_proof(order["id"], {
        "amount": ESPECIAL, "destination_account": ACCOUNT, "reference": "RESET-1",
        "date_iso": _iso(),
    }, photo=(b"img", "image/png"))
    assert db.q1("SELECT COUNT(*) AS n FROM receipt_photos")["n"] == 1
    db.reset()
    assert db.q1("SELECT COUNT(*) AS n FROM payment_receipts")["n"] == 0
    assert db.q1("SELECT COUNT(*) AS n FROM receipt_photos")["n"] == 0


def test_reports_count_receipts():
    a = _transfer_order()
    _receipt(a["id"], 1, "REP-1")
    _receipt(a["id"], 2, "REP-2")                                         # two red on one order
    b = _second_transfer_order()
    _receipt(b["id"], ESPECIAL, "REP-3")                                  # one clean, waiting
    c = _second_transfer_order()
    gone = _receipt(c["id"], 3, "REP-4")["receipt"]                       # red, then discarded
    payments.discard_receipt(c["id"], gone["id"], "Llegó por error")
    snapshot = reports.snapshot(1)
    assert snapshot["receipts_flagged"] == 2
    assert snapshot["receipts_awaiting_confirmation"] == 1


# ---- the clean-receipt reply and the payment in ESTADO REAL (Task C fix) --

def _good(amount, reference) -> dict:
    return {
        "amount": amount, "destination_account": ACCOUNT, "reference": reference,
        "date_iso": _iso(),
    }


def _handle_receipt(monkeypatch, extracted) -> str:
    """main.handle on an image whose OCR is `extracted`; returns the instruction
    the bot was given."""
    from app import main
    monkeypatch.setattr(wa, "live", lambda: False)
    told = []
    monkeypatch.setattr(brain, "extract_proof", lambda image: extracted)
    monkeypatch.setattr(brain, "reply", lambda phone, message, **kw: told.append(message) or "ok")
    main.handle(PHONE, None, wa.image_block(b"img", "image/png"), db.now())
    return told[-1]


def _payment_line(prompt: str) -> str:
    return next(
        line for line in _real_state_block(prompt).splitlines()
        if line.startswith("- Pago del pedido más reciente:")
    )


def test_clean_receipt_for_the_total_says_the_method_it_settled(monkeypatch):
    order = _confirmed_order()                           # no method chosen yet
    told = _handle_receipt(monkeypatch, _good(ESPECIAL, "SETTLE-1"))
    assert payments.current(order["id"])["method"] == "TRANSFER"
    assert f"recibió un comprobante por {fmt(ESPECIAL)}" in told
    assert "Con este comprobante quedó definida la forma de pago" in told
    assert (f"«Su comprobante por {fmt(ESPECIAL)} está pendiente de confirmación del local. "
            "Por ahora no necesita realizar otro pago.»") in told
    assert "una persona del local lo va a revisar" in told
    assert "No le preguntes cómo va a pagar" in told
    assert "NO le digas que ya está confirmado" in told
    assert order["code"] not in told
    assert "cocina" not in told.lower()


def test_clean_partial_receipt_gives_the_payment_sentence(monkeypatch):
    """The R1 re-run (6 Oct): the instruction said "en parte y parte" and the bot made up
    the cash side. Now the code-built sentence says it."""
    order = _confirmed_order()
    part = 10000
    told = _handle_receipt(monkeypatch, _good(part, "SETTLE-2"))
    assert payments.current(order["id"])["method"] == "MIXED"
    assert "parte y parte" not in told
    assert (f"«Su comprobante por {fmt(part)} está pendiente de confirmación del local. "
            f"Quedan {fmt(ESPECIAL - part)} por pagar en efectivo al recibir.»") in told
    assert "No le preguntes cómo va a pagar" in told
    line = _payment_line(brain.system_prompt(PHONE))
    assert line.startswith("- Pago del pedido más reciente: parte por transferencia y parte en efectivo;")
    assert line.endswith(
        "ya no falta nada por transferir; queda por pagar en efectivo al recibir; "
        "todavía falta por pagar."
    )


def test_clean_receipt_on_a_chosen_method_claims_no_inference(monkeypatch):
    _transfer_order()
    told = _handle_receipt(monkeypatch, _good(ESPECIAL, "SETTLE-3"))
    assert f"recibió un comprobante por {fmt(ESPECIAL)}" in told
    assert "una persona del local lo va a revisar" in told
    assert "quedó definida la forma de pago" not in told


def test_inferred_is_true_only_when_the_receipt_set_the_method():
    a = _confirmed_order()
    red = _receipt(a["id"], ESPECIAL, "INF-1", destination_account="3009998877")
    assert red["inferred"] is False                       # a red receipt infers nothing
    assert _receipt(a["id"], ESPECIAL, "INF-2")["inferred"] is True
    b = _second_transfer_order()
    assert _receipt(b["id"], ESPECIAL, "INF-3")["inferred"] is False   # method already chosen


def test_real_state_shows_the_payment_in_words():
    order = _confirmed_order()
    head = "- Pago del pedido más reciente: "
    assert _payment_line(brain.system_prompt(PHONE)) == (
        head + "todavía no ha dicho cómo va a pagar; no ha enviado ningún comprobante; "
        "todavía falta por pagar."
    )

    clean = _receipt(order["id"], ESPECIAL, "STATE-1")["receipt"]
    assert _payment_line(brain.system_prompt(PHONE)) == (
        head + "por transferencia; comprobantes: 1 recibido que revisa una persona del local; "
        "ya no falta nada por transferir; todavía falta que una persona confirme el pago."
    )

    payments.confirm_receipt(order["id"], clean["id"])
    red = _receipt(order["id"], 1, "STATE-2")["receipt"]          # nothing owed: red
    assert _payment_line(brain.system_prompt(PHONE)) == (
        head + "por transferencia; comprobantes: 1 confirmado por una persona del local, "
        "1 con un problema que revisa una persona del local; hay un comprobante en revisión: "
        "no le pidas que pague otra vez; ya está pagado completo."
    )

    payments.discard_receipt(order["id"], red["id"], "Llegó por error")   # named, no longer red
    prompt = brain.system_prompt(PHONE)
    assert _payment_line(prompt) == (
        head + "por transferencia; comprobantes: 1 confirmado por una persona del local, "
        "1 descartado por el local; ya no falta nada por transferir; ya está pagado completo."
    )
    block = _real_state_block(prompt)
    assert "nunca digas que no te aparece ninguno" in block
    assert "no se lo vuelvas a preguntar" in block
    assert "$" not in block                                        # no amounts in the block
    assert order["code"] not in prompt


def test_real_state_lets_the_bot_report_a_confirmed_receipt():
    """The bot may tell the customer that the local already confirmed a receipt:
    that reports a person's confirmation, it is not the bot confirming. Only when
    the real state shows it."""
    order = _transfer_order()
    receipt = _receipt(order["id"], ESPECIAL, "CONF-STATE-1")["receipt"]
    prompt = brain.system_prompt(PHONE)
    assert "confirmado" not in _payment_line(prompt)
    assert ("nunca digas que está confirmado uno que aquí no aparece confirmado"
            in _real_state_block(prompt))

    payments.confirm_receipt(order["id"], receipt["id"])
    prompt = brain.system_prompt(PHONE)
    assert "1 confirmado por una persona del local" in _payment_line(prompt)
    assert "puedes decirle que el local ya lo confirmó" in _real_state_block(prompt)
    assert "muestra un comprobante confirmado puedes" in prompt   # SYSTEM agrees with the block


def test_receipt_turn_shows_the_receipt_to_the_model(monkeypatch):
    """The manual run: "parte y parte", then a receipt for the total instead of the
    split. In that turn the model must see the receipt and the settled method in
    the real state, and the instruction must say how the payment ended up."""
    from app import main
    monkeypatch.setattr(wa, "live", lambda: False)
    monkeypatch.setattr(brain, "_model", "test-model")

    def text(t):
        return types.SimpleNamespace(
            stop_reason="end_turn", content=[types.SimpleNamespace(type="text", text=t)]
        )
    seen = []
    fake = _capturing_client([text("¿Cuánto por transferencia y cuánto en efectivo?"),
                              text("Recibimos su comprobante.")], seen)
    monkeypatch.setattr(brain, "client", lambda: fake)
    monkeypatch.setattr(brain, "extract_proof", lambda image: _good(ESPECIAL, "SPLIT-1"))

    brain.forget(PHONE)
    try:
        _confirmed_order()
        brain.reply(PHONE, "Parte y parte")
        main.handle(PHONE, "Aquí está el comprobante", wa.image_block(b"img", "image/png"),
                    db.now())
        instruction = brain._history[PHONE][-2]["content"]
    finally:
        brain.forget(PHONE)

    assert "no ha enviado ningún comprobante" in _payment_line(seen[0])
    assert _payment_line(seen[1]).startswith(
        "- Pago del pedido más reciente: por transferencia; comprobantes: 1 recibido"
    )
    assert "no se lo vuelvas a preguntar" in _real_state_block(seen[1])
    assert "Con este comprobante quedó definida la forma de pago" in instruction
    assert (f"«Su comprobante por {fmt(ESPECIAL)} está pendiente de confirmación del local. "
            "Por ahora no necesita realizar otro pago.»") in instruction


def test_tool_calls_are_logged_with_the_last_digits_only(monkeypatch, capsys):
    monkeypatch.setattr(brain, "_model", "test-model")
    use = types.SimpleNamespace(stop_reason="tool_use", content=[
        types.SimpleNamespace(type="tool_use", id="t1", name="show_cart", input={}),
    ])
    done = types.SimpleNamespace(
        stop_reason="end_turn", content=[types.SimpleNamespace(type="text", text="Listo.")]
    )
    fake = _capturing_client([use, done], [])
    monkeypatch.setattr(brain, "client", lambda: fake)
    cart.add_item(PHONE, "b_especial", 1)
    brain.forget(PHONE)
    try:
        brain.reply(PHONE, "¿qué tengo?")
    finally:
        brain.forget(PHONE)
    brain._log_tool(PHONE, "order_status", {}, "x" * 500)

    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.startswith("[bot ")]
    assert len(lines) == 2                                        # one line per tool call
    assert lines[0].startswith(f"[bot ...{PHONE[-4:]}] show_cart {{}} -> ")
    assert lines[1] == (
        f"[bot ...{PHONE[-4:]}] order_status {{}} -> " + "x" * brain.TOOL_LOG_CHARS + "..."
    )
    assert PHONE not in out


# ---- payment method changes (Task B) -----------------------------------

def test_payment_change_after_a_receipt_goes_to_a_person():
    order = _transfer_order()
    payments.check_proof(order["id"], {
        "amount": ESPECIAL, "destination_account": ACCOUNT, "reference": "PAYCHG-1",
        "date_iso": _iso(),
    })
    result = brain._run_tool(PHONE, "choose_payment", {"method": "CASH"})
    assert "error interno" not in result
    assert "PASADO A UNA PERSONA" in result
    updated = orders.get(order["id"])
    assert len(updated["handoffs"]) == 1
    assert updated["payment"]["method"] == "TRANSFER"


def test_payment_can_still_be_chosen_after_acceptance():
    order = _accepted_order()
    result = brain._run_tool(PHONE, "choose_payment", {"method": "TRANSFER"})
    assert "error interno" not in result
    assert orders.get(order["id"])["payment"]["method"] == "TRANSFER"


def _delivery_order_out_for_delivery(method=None):
    cart.add_item(PHONE, "b_especial", 1)
    order = orders.create_from_cart(PHONE, "DELIVERY", address="calle 1", zone="Cabecera")
    orders.apply_event(order["id"], "customer_confirms")
    if method:
        payments.set_method(order["id"], method)
    orders.apply_event(order["id"], "staff_accepts")
    orders.apply_event(order["id"], "food_ready")
    return orders.apply_event(order["id"], "driver_leaves")


def test_payment_change_while_out_for_delivery_goes_to_a_person():
    order = _delivery_order_out_for_delivery(method="TRANSFER")
    result = brain._run_tool(PHONE, "choose_payment", {"method": "CASH"})
    assert "error interno" not in result
    assert "PASADO A UNA PERSONA" in result
    updated = orders.get(order["id"])
    assert len(updated["handoffs"]) == 1
    assert "efectivo" in updated["handoffs"][0]["summary"]
    assert updated["payment"]["method"] == "TRANSFER"

    db.reset()
    order = _delivery_order_out_for_delivery(method=None)
    result = brain._run_tool(PHONE, "choose_payment", {"method": "CASH"})
    assert "error interno" not in result
    assert "PASADO A UNA PERSONA" in result
    updated = orders.get(order["id"])
    assert updated["payment"]["method"] == "UNSPECIFIED"

    db.reset()
    order = _delivery_order_out_for_delivery(method="TRANSFER")
    result = brain._run_tool(PHONE, "choose_payment", {"method": "TRANSFER"})
    assert result == "La forma de pago ya estaba registrada así; no cambió nada."
    assert len(orders.get(order["id"])["handoffs"]) == 0


def test_changing_the_payment_method_marks_the_order_updated():
    order = _confirmed_order()
    result = brain._run_tool(PHONE, "choose_payment", {"method": "CASH"})
    assert "error interno" not in result
    assert orders.get(order["id"])["edited_at"] is None

    result = brain._run_tool(PHONE, "choose_payment", {"method": "TRANSFER"})
    assert "error interno" not in result
    assert orders.get(order["id"])["edited_at"] is not None


# ---- line merge and targeting (Task B fixes) -----------------------------

def test_identical_line_merges_instead_of_duplicating():
    cart.add_item(PHONE, "b_caserita", 1)
    cart.add_item(PHONE, "d_gaseosa_personal", 1)
    order = orders.create_from_cart(PHONE, "PICKUP")
    orders.apply_event(order["id"], "customer_confirms")
    result = brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal", "qty": 2})
    assert "error interno" not in result
    updated = orders.get(order["id"])
    assert updated["id"] == order["id"]
    gaseosa_lines = [i for i in updated["items"] if i["item_id"] == "d_gaseosa_personal"]
    assert len(gaseosa_lines) == 1
    assert gaseosa_lines[0]["qty"] == 3
    assert len(updated["items"]) == 2
    assert updated["total"] == CASERITA + 3 * GASEOSA


def test_different_modifiers_or_notes_stay_on_separate_lines():
    cart.add_item(PHONE, "b_caserita")
    cart.add_item(PHONE, "b_caserita", modifier_ids=["m_sin_cebolla"])
    cart.add_item(PHONE, "b_caserita", modifier_ids=["m_queso"])
    cart.add_item(PHONE, "b_caserita", note="bien asada")
    assert len(cart.summary(PHONE)["lines"]) == 4


def test_note_and_modifier_order_are_normalized_for_merge():
    cart.add_item(PHONE, "b_caserita", note="bien asada")
    cart.add_item(PHONE, "b_caserita", note=" Bien  Asada ")
    lines = cart.summary(PHONE)["lines"]
    assert len(lines) == 1
    assert lines[0]["qty"] == 2

    db.reset()
    cart.add_item(PHONE, "b_caserita", modifier_ids=["m_queso", "m_sin_cebolla"])
    cart.add_item(PHONE, "b_caserita", modifier_ids=["m_sin_cebolla", "m_queso"])
    assert len(cart.summary(PHONE)["lines"]) == 1


def test_merge_respects_the_50_limit():
    cart.add_item(PHONE, "d_gaseosa_personal", 30)
    with pytest.raises(cart.CartError):
        cart.add_item(PHONE, "d_gaseosa_personal", 30)
    assert cart.summary(PHONE)["lines"][0]["qty"] == 30


def test_remove_item_with_qty_reduces_the_single_line():
    cart.add_item(PHONE, "b_caserita", 1)
    cart.add_item(PHONE, "d_gaseosa_personal", 3)
    order = orders.create_from_cart(PHONE, "PICKUP")
    orders.apply_event(order["id"], "customer_confirms")
    result = brain._run_tool(PHONE, "remove_item", {"item_id": "d_gaseosa_personal", "qty": 1})
    assert "error interno" not in result
    updated = orders.get(order["id"])
    assert updated["id"] == order["id"]
    gaseosa_lines = [i for i in updated["items"] if i["item_id"] == "d_gaseosa_personal"]
    assert len(gaseosa_lines) == 1
    assert gaseosa_lines[0]["qty"] == 2
    assert len(updated["items"]) == 2
    assert updated["total"] == CASERITA + 2 * GASEOSA


def test_set_qty_refuses_when_lines_differ():
    cart.add_item(PHONE, "b_caserita", 1)
    cart.add_item(PHONE, "b_caserita", 1, modifier_ids=["m_sin_cebolla"])
    order = orders.create_from_cart(PHONE, "PICKUP")
    orders.apply_event(order["id"], "customer_confirms")
    result = brain._run_tool(PHONE, "set_qty", {"item_id": "b_caserita", "qty": 2})
    assert result.startswith("NO SE PUDO")
    assert "m_sin_cebolla" in result
    assert "set_qty" in result
    updated = orders.get(order["id"])
    caserita_lines = [i for i in updated["items"] if i["item_id"] == "b_caserita"]
    assert len(caserita_lines) == 2
    assert {i["qty"] for i in caserita_lines} == {1}
    assert cart.peek_open_cart(PHONE) is None


def test_set_qty_with_selector_targets_one_line():
    cart.add_item(PHONE, "b_caserita", 1)
    cart.add_item(PHONE, "b_caserita", 1, modifier_ids=["m_sin_cebolla"])
    order = orders.create_from_cart(PHONE, "PICKUP")
    orders.apply_event(order["id"], "customer_confirms")

    result = brain._run_tool(
        PHONE, "set_qty", {"item_id": "b_caserita", "qty": 2, "modifier_ids": ["m_sin_cebolla"]},
    )
    assert "error interno" not in result
    updated = orders.get(order["id"])
    caserita_lines = [i for i in updated["items"] if i["item_id"] == "b_caserita"]
    assert len(caserita_lines) == 2
    with_mod = next(i for i in caserita_lines if i["modifiers"])
    plain = next(i for i in caserita_lines if not i["modifiers"])
    assert with_mod["qty"] == 2
    assert plain["qty"] == 1
    assert updated["total"] == 3 * CASERITA

    result = brain._run_tool(
        PHONE, "set_qty", {"item_id": "b_caserita", "qty": 0, "modifier_ids": [], "note": ""},
    )
    assert "error interno" not in result
    updated = orders.get(order["id"])
    caserita_lines = [i for i in updated["items"] if i["item_id"] == "b_caserita"]
    assert len(caserita_lines) == 1
    assert caserita_lines[0]["qty"] == 2


def test_set_qty_selector_with_loose_note_match():
    cart.add_item(PHONE, "b_caserita", 1)
    cart.add_item(PHONE, "b_caserita", 1, note="bien asada")
    order = orders.create_from_cart(PHONE, "PICKUP")
    orders.apply_event(order["id"], "customer_confirms")

    result = brain._run_tool(
        PHONE, "set_qty", {"item_id": "b_caserita", "qty": 3, "note": " BIEN asada"},
    )
    assert "error interno" not in result
    updated = orders.get(order["id"])
    caserita_lines = [i for i in updated["items"] if i["item_id"] == "b_caserita"]
    noted = next(i for i in caserita_lines if i["note"])
    assert noted["qty"] == 3


def test_set_qty_selector_matching_nothing_is_refused():
    cart.add_item(PHONE, "b_caserita", 1)
    cart.add_item(PHONE, "b_caserita", 1, note="bien asada")
    order = orders.create_from_cart(PHONE, "PICKUP")
    orders.apply_event(order["id"], "customer_confirms")

    result = brain._run_tool(
        PHONE, "set_qty", {"item_id": "b_caserita", "qty": 1, "note": "sin tomate"},
    )
    assert result.startswith("NO SE PUDO")
    assert "no hay una línea" in result
    updated = orders.get(order["id"])
    caserita_lines = [i for i in updated["items"] if i["item_id"] == "b_caserita"]
    assert {i["qty"] for i in caserita_lines} == {1}


def test_remove_item_refuses_when_lines_differ():
    cart.add_item(PHONE, "b_caserita", 1)
    cart.add_item(PHONE, "b_caserita", 1, modifier_ids=["m_sin_cebolla"])
    order = orders.create_from_cart(PHONE, "PICKUP")
    orders.apply_event(order["id"], "customer_confirms")

    result = brain._run_tool(PHONE, "remove_item", {"item_id": "b_caserita"})
    assert result.startswith("NO SE PUDO")
    assert "set_qty" in result
    updated = orders.get(order["id"])
    caserita_lines = [i for i in updated["items"] if i["item_id"] == "b_caserita"]
    assert len(caserita_lines) == 2
    assert {i["qty"] for i in caserita_lines} == {1}


# ---- final response text only (Task B fixes) -----------------------------

def _scripted_client(responses):
    class FakeMessages:
        def create(self, **kwargs):
            return responses.pop(0)
    return types.SimpleNamespace(messages=FakeMessages())


def test_only_final_text_reaches_the_customer(monkeypatch):
    monkeypatch.setattr(brain, "_model", "test-model")
    response1 = types.SimpleNamespace(
        stop_reason="tool_use",
        content=[
            types.SimpleNamespace(type="text", text="Su pedido ya fue recibido por el local"),
            types.SimpleNamespace(type="tool_use", id="t1", name="show_cart", input={}),
        ],
    )
    response2 = types.SimpleNamespace(
        stop_reason="end_turn",
        content=[types.SimpleNamespace(type="text", text="Listo.")],
    )
    fake = _scripted_client([response1, response2])
    monkeypatch.setattr(brain, "client", lambda: fake)

    phone = "573000000001"
    brain.forget(phone)
    try:
        assert brain.reply(phone, "hola") == "Listo."
    finally:
        brain.forget(phone)


def test_final_response_with_no_text_falls_back_to_previous(monkeypatch):
    monkeypatch.setattr(brain, "_model", "test-model")
    response1 = types.SimpleNamespace(
        stop_reason="tool_use",
        content=[
            types.SimpleNamespace(type="text", text="Su pedido ya fue recibido por el local"),
            types.SimpleNamespace(type="tool_use", id="t1", name="show_cart", input={}),
        ],
    )
    response2 = types.SimpleNamespace(stop_reason="end_turn", content=[])
    fake = _scripted_client([response1, response2])
    monkeypatch.setattr(brain, "client", lambda: fake)

    phone = "573000000002"
    brain.forget(phone)
    try:
        assert brain.reply(phone, "hola") == "Su pedido ya fue recibido por el local"
    finally:
        brain.forget(phone)


# ---- who gets told what (notifications-and-followups.md §1) ----------

def _notified(order_id, event, reason=None):
    """Runs a transition through the real notify() and returns what was sent."""
    from app import main
    before = db.q1("SELECT COALESCE(MAX(id),0) AS m FROM outbound_messages")["m"]
    order = orders.apply_event(order_id, event, reason)
    main.notify(order, reason)
    return [r["body"] for r in db.q(
        "SELECT body FROM outbound_messages WHERE id>%s ORDER BY id", (before,)
    )]


def test_accepted_notifies_without_mentioning_the_code():
    order = _confirmed_order()
    sent = _notified(order["id"], "staff_accepts")
    assert len(sent) == 1
    assert order["code"] not in sent[0]
    assert "cocina" not in sent[0].lower()


def test_preparation_step_is_gone():
    order = _accepted_order()
    with pytest.raises(orders.IllegalTransition):
        orders.apply_event(order["id"], "start_cooking")


def test_completed_says_nothing():
    order = _accepted_order()
    orders.apply_event(order["id"], "food_ready")
    assert _notified(order["id"], "hand_over") == []


def test_ready_notifies_on_pickup_but_not_on_delivery():
    order = _accepted_order()
    assert len(_notified(order["id"], "food_ready")) == 1       # PICKUP

    db.reset()
    cart.add_item(PHONE, "b_especial", 1)
    delivery = orders.create_from_cart(PHONE, "DELIVERY", zone="Cabecera")
    for event in ("customer_confirms", "staff_accepts"):
        orders.apply_event(delivery["id"], event)
    assert _notified(delivery["id"], "food_ready") == []         # DELIVERY: silent
    assert len(_notified(delivery["id"], "driver_leaves")) == 1


def test_customer_cancellation_is_not_announced():
    order = _confirmed_order()
    assert _notified(order["id"], "customer_cancels") == []


def test_no_order_code_leaks_in_any_notification():
    order = _confirmed_order()
    code = order["code"]
    for event in ("staff_accepts", "food_ready", "hand_over"):
        for body in _notified(order["id"], event):
            assert code not in body


def test_notify_never_enters_the_model_history():
    brain.forget(PHONE)
    order = _confirmed_order()
    _notified(order["id"], "staff_accepts")
    assert PHONE not in brain._history


# ---- Task C2 Part 2: the "Pagos" block ---------------------------------------

MINI = CATALOG.price_of("d_gaseosa_mini")          # 2500
ACCOUNT_TEXT = " o ".join(
    f"{a['provider']} {a['number']} a nombre de {a['holder']}" for a in CATALOG.accounts()
)
TRANSFER_TAIL = f" Puede transferir a {ACCOUNT_TEXT}. Cuando transfiera, envíenos el comprobante."


def _order_of(*items, method=None, transfer=None):
    """A confirmed pickup order with these (item_id, qty) lines."""
    for item_id, qty in items:
        cart.add_item(PHONE, item_id, qty)
    order = orders.create_from_cart(PHONE, "PICKUP")
    orders.apply_event(order["id"], "customer_confirms")
    if method:
        payments.set_method(order["id"], method, transfer)
    return orders.get(order["id"])


def _cash_ids(order_id):
    return [c["id"] for c in payments.current(order_id)["cash"]]


@pytest.fixture
def chat():
    brain.forget(PHONE)
    yield
    brain.forget(PHONE)


def _say(text):
    return types.SimpleNamespace(
        stop_reason="end_turn", content=[types.SimpleNamespace(type="text", text=text)]
    )


def _use(name, args=None, block_id="t1"):
    return types.SimpleNamespace(stop_reason="tool_use", content=[
        types.SimpleNamespace(type="tool_use", id=block_id, name=name, input=args or {}),
    ])


def _scripted(monkeypatch, *responses):
    """The model answers `responses` in order; every call's keyword arguments are kept."""
    calls, queue = [], list(responses)

    class Messages:
        def create(self, **kwargs):
            calls.append(kwargs)
            return queue.pop(0)

    monkeypatch.setattr(brain, "_model", "test-model")
    monkeypatch.setattr(brain, "client", lambda: types.SimpleNamespace(messages=Messages()))
    return calls


def _customer_says(text):
    from app import main
    return main.handle(PHONE, text, None, db.now())


def _last_sent_in_history():
    return brain._history[PHONE][-1]["content"][0]["text"]


# -- cash rows and the X --------------------------------------------------------

def test_each_cash_registration_is_its_own_row(monkeypatch):
    client = _client(monkeypatch)
    order = _order_of(("b_caserita", 1), method="CASH")
    payments.register_cash(order["id"], 5000)
    record = payments.register_cash(order["id"], 2400)
    assert [(c["amount"], c["status"]) for c in record["cash"]] == [
        (5000, "RECEIVED"), (2400, "RECEIVED")]
    assert all(c["registered_at"] for c in record["cash"])
    assert record["cash_received"] == 7400
    assert record["outstanding"] == CASERITA - 7400            # "faltan $11.500"
    card = next(o for o in client.get("/api/board").json()["orders"] if o["id"] == order["id"])
    assert [c["amount"] for c in card["payment"]["cash"]] == [5000, 2400]


def test_cash_discard_needs_a_reason_and_tells_nobody(monkeypatch):
    client = _client(monkeypatch)
    order = _order_of(("d_gaseosa_mini", 1), method="CASH")
    payments.register_cash(order["id"], MINI)
    cash_id = _cash_ids(order["id"])[0]
    url = f"/api/orders/{order['id']}/cash/{cash_id}/discard"
    before = _outbound_count()
    for response in (client.post(url), client.post(url, json={}),
                     client.post(url, json={"reason": "   "})):
        assert response.status_code == 400
        assert response.json()["error"] == "Hace falta el motivo."
    assert payments.current(order["id"])["paid"] == MINI          # nothing changed

    wrong = client.post(f"/api/orders/{order['id'] + 99}/cash/{cash_id}/discard",
                        json={"reason": "Error"})
    assert wrong.json()["error"] == "Ese efectivo no es de este pedido."
    done = client.post(url, json={"reason": "  Se registró en el pedido equivocado  "})
    assert done.status_code == 200
    record = done.json()
    assert record["cash_row"]["status"] == "DISCARDED"
    assert record["cash_row"]["discard_reason"] == "Se registró en el pedido equivocado"
    assert (record["cash_received"], record["paid"], record["outstanding"]) == (0, 0, MINI)
    assert record["method"] == "CASH"                            # the customer chose it
    assert _outbound_count() == before                           # no message, ever
    again = client.post(url, json={"reason": "Otra vez"})
    assert again.json()["error"] == "Ese efectivo ya está descartado."


def test_discarded_cash_counts_nowhere():
    order = _transfer_order()
    receipt = _receipt(order["id"], ESPECIAL, "NOWHERE-1")["receipt"]   # clean, waiting
    payments.register_cash(order["id"], ESPECIAL)
    assert payments.current(order["id"])["overpayment_amount"] == ESPECIAL   # "Sobrepago"
    payments.discard_cash(order["id"], _cash_ids(order["id"])[0], "Era de otro pedido")
    after = payments.current(order["id"])
    assert (after["cash_received"], after["paid"], after["overpayment_amount"]) == (0, 0, 0)
    assert after["received"] == 0
    assert after["receipts"][0]["id"] == receipt["id"]
    assert {k["kind"]: k["orders"] for k in reports.snapshot(1)["how_paid"]}["NONE"] == 1


# -- Gap 1: discarding cash and the method ---------------------------------------

@pytest.mark.parametrize("first", ["5000", "2400"])
def test_the_method_cash_set_goes_back_with_the_last_cash_row(first):
    order = _order_of(("b_caserita", 1))                        # no method chosen
    payments.register_cash(order["id"], 5000)
    assert payments.current(order["id"])["method_from_cash"] == 1
    payments.register_cash(order["id"], 2400)
    five, two = _cash_ids(order["id"])
    one, last = (five, two) if first == "5000" else (two, five)
    after = payments.discard_cash(order["id"], one, "Error")
    assert after["method"] == "CASH"                             # cash is still registered
    after = payments.discard_cash(order["id"], last, "Error")
    assert (after["method"], after["transfer_amount"], after["cash_amount"],
            after["method_from_cash"]) == ("UNSPECIFIED", 0, 0, 0)
    assert after["paid"] == 0


@pytest.mark.parametrize("method", ["TRANSFER", "MIXED", "CASH"])
def test_discarded_cash_keeps_a_chosen_method(method):
    order = _order_of(("b_caserita", 1), method=method,
                      transfer=10000 if method == "MIXED" else None)
    before = payments.current(order["id"])
    payments.register_cash(order["id"], 5000)
    after = payments.discard_cash(order["id"], _cash_ids(order["id"])[0], "Error")
    assert (after["method"], after["transfer_amount"], after["cash_amount"]) == (
        before["method"], before["transfer_amount"], before["cash_amount"])


def test_a_method_a_receipt_set_later_is_never_reset():
    order = _confirmed_order()                                   # no method
    payments.register_cash(order["id"], 10000)                   # CASH, set by cash
    clean = _receipt(order["id"], ESPECIAL - 10000, "LATER-1")
    assert clean["inferred"] is True and clean["method"] == "MIXED"
    assert clean["method_from_cash"] == 0
    after = payments.discard_cash(order["id"], _cash_ids(order["id"])[0], "Error")
    assert after["method"] == "MIXED"


def test_a_red_receipt_stays_red_after_its_cash_is_discarded():
    order = _order_of(("d_gaseosa_mini", 1))                    # no method
    payments.register_cash(order["id"], MINI)
    red = _receipt(order["id"], MINI, "STAYRED-1")["receipt"]
    assert red["flags"] == ["No se esperaba otro pago."]
    after = payments.discard_cash(order["id"], _cash_ids(order["id"])[0], "Error")
    assert after["method"] == "UNSPECIFIED"
    assert after["receipts"][0]["status"] == "FLAGGED"          # no automatic re-check
    assert after["receipts"][0]["flags"] == ["No se esperaba otro pago."]
    confirmed = payments.confirm_receipt(order["id"], red["id"], MINI)
    assert (confirmed["method"], confirmed["paid"]) == ("TRANSFER", MINI)


# -- Gap 2: a discarded receipt sent again ---------------------------------------

def test_a_discarded_receipt_sent_again_gets_the_reason(monkeypatch):
    from app import main
    order = _transfer_order()
    first = _receipt(order["id"], ESPECIAL, "AGAIN-1")["receipt"]
    payments.discard_receipt(order["id"], first["id"], "No es de este pedido")
    told = []
    monkeypatch.setattr(brain, "extract_proof", lambda image: _good(ESPECIAL, "AGAIN-1"))
    monkeypatch.setattr(brain, "reply", lambda phone, message, **kw:
                        told.append(message) or "Ese comprobante ya lo habíamos revisado.")
    image = wa.image_block(b"img", "image/png")
    assert main.handle(PHONE, None, image, db.now()) == "Ese comprobante ya lo habíamos revisado."
    assert len(payments.receipts(order["id"])) == 1             # no new row
    assert '"No es de este pedido"' in told[-1]
    assert "No le digas que ya lo teníamos" in told[-1]
    assert "ya lo teníamos" not in told[-1].replace("No le digas que ya lo teníamos", "")

    # The guard of the discard message: an amount, "confirmad", the code or "cocina".
    monkeypatch.setattr(brain, "reply", lambda phone, message, **kw: "Su pago de $29.900 no cuenta.")
    monkeypatch.setattr(brain, "correct_last_reply", lambda phone, text: None)
    assert main.handle(PHONE, None, image, db.now()) == main.RESEND_DISCARDED_TEMPLATE.format(
        reason="No es de este pedido")

    other = _second_transfer_order()
    reused = _receipt(other["id"], ESPECIAL, "AGAIN-1")
    assert reused["receipt"]["status"] == "FLAGGED"
    assert "Este comprobante ya fue usado antes (referencia AGAIN-1)." in reused["receipt"]["flags"]


# -- the payment sentence --------------------------------------------------------

def _g29_order():
    order = _order_of(("b_caserita", 1), method="MIXED", transfer=10000)
    payments.register_cash(order["id"], 5000)
    return order


def _sentence_cases():
    def g29():
        _g29_order()
        brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
        return (f"Ya pagó {fmt(5000)} en efectivo. Quedan {fmt(10000)} por transferir y "
                f"{fmt(7400)} por pagar en efectivo al recibir." + TRANSFER_TAIL)

    def g30():
        order = _order_of(("b_caserita", 1), method="TRANSFER")
        payments.register_cash(order["id"], 5000)
        brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
        return f"Ya pagó {fmt(5000)} en efectivo. Quedan {fmt(17400)} por transferir." + TRANSFER_TAIL

    def r1():
        order = _order_of(("d_gaseosa_mini", 2))
        payments.register_cash(order["id"], MINI)
        _receipt(order["id"], MINI, "SENT-R1")
        return (f"Ya pagó {fmt(MINI)} en efectivo. Su comprobante por {fmt(MINI)} está pendiente "
                "de confirmación del local. Por ahora no necesita realizar otro pago.")

    def red_only():
        order = _transfer_order()
        _receipt(order["id"], ESPECIAL, "SENT-RED", destination_account="3009998877")
        return "El local está revisando un comprobante que nos envió y se comunicará con usted."

    def red_and_paid():
        order = _transfer_order()
        good = _receipt(order["id"], ESPECIAL, "SENT-PAID")["receipt"]
        payments.confirm_receipt(order["id"], good["id"])
        _receipt(order["id"], 1000, "SENT-EXTRA")                 # red: nothing more owed
        return (f"El local ya confirmó su transferencia de {fmt(ESPECIAL)}. El local está "
                "revisando un comprobante que nos envió y se comunicará con usted. Su pedido ya "
                "está pagado completo.")

    def all_discarded():
        order = _confirmed_order()
        payments.register_cash(order["id"], ESPECIAL)
        payments.discard_cash(order["id"], _cash_ids(order["id"])[0], "Error")
        return f"Falta por pagar {fmt(ESPECIAL)}."

    def cash_chosen():
        _order_of(("b_especial", 1), method="CASH")
        return f"Quedan {fmt(ESPECIAL)} por pagar en efectivo al recibir."

    return {f.__name__: f for f in (g29, g30, r1, red_only, red_and_paid, all_discarded, cash_chosen)}


@pytest.mark.parametrize("case", list(_sentence_cases()))
def test_payment_sentence(case):
    expected = _sentence_cases()[case]()
    sentence = brain.payment_sentence_for(PHONE)
    assert sentence == expected
    code = brain._payment_order(PHONE)["code"]
    assert code not in sentence and "cocina" not in sentence.lower()
    if "revisando" in expected:
        assert "Puede transferir" not in sentence                # no request while red


def test_two_live_orders_say_which_order_the_sentence_is_about():
    first = _accepted_order()
    second = _order_of(("b_caserita", 1))
    assert second["id"] > first["id"]
    assert brain.payment_sentence_for(PHONE) == (
        f"En su pedido más reciente: falta por pagar {fmt(CASERITA)}.")


# -- choose_payment carries the sentence (no unconditional receipt request) -------

def test_choose_payment_cash_never_asks_for_a_receipt():
    _confirmed_order()
    result = brain._run_tool(PHONE, "choose_payment", {"method": "CASH"})
    assert f"«Quedan {fmt(ESPECIAL)} por pagar en efectivo al recibir.»" in result
    assert "comprobante" not in result
    assert ACCOUNT not in result


def test_choose_payment_transfer_asks_for_the_receipt_once():
    _confirmed_order()
    result = brain._run_tool(PHONE, "choose_payment", {"method": "TRANSFER"})
    assert f"«Quedan {fmt(ESPECIAL)} por transferir.{TRANSFER_TAIL}»" in result
    assert result.count("comprobante") == 1
    assert result.count(ACCOUNT) == 1


def test_choose_payment_mixed_names_both_parts():
    _confirmed_order()
    result = brain._run_tool(
        PHONE, "choose_payment", {"method": "MIXED", "transfer_amount": 10000})
    assert (f"«Quedan {fmt(10000)} por transferir y {fmt(ESPECIAL - 10000)} por pagar en "
            f"efectivo al recibir.{TRANSFER_TAIL}»") in result
    assert result.count("comprobante") == 1


# -- the end-of-turn check -------------------------------------------------------

def _added_gaseosa_sentence():
    return f"Quedan {fmt(ESPECIAL + GASEOSA)} por transferir." + TRANSFER_TAIL


def test_a_missing_sentence_is_appended_without_another_call(monkeypatch, chat):
    _transfer_order()
    calls = _scripted(monkeypatch, _use("add_item", {"item_id": "d_gaseosa_personal"}),
                      _say("Listo, le agregué la gaseosa."))
    reply = _customer_says("Agrégueme una gaseosa personal")
    assert reply == f"Listo, le agregué la gaseosa.\n\n{_added_gaseosa_sentence()}"
    assert len(calls) == 2
    assert _last_sent_in_history() == reply


def test_a_contradictory_draft_is_corrected_once(monkeypatch, chat):
    _transfer_order()
    good = f"Listo, le agregué la gaseosa. {_added_gaseosa_sentence()}"
    calls = _scripted(
        monkeypatch, _use("add_item", {"item_id": "d_gaseosa_personal"}),
        _say(f"Listo. {_added_gaseosa_sentence()} Su pedido ya está pagado."), _say(good))
    assert _customer_says("Agrégueme una gaseosa personal") == good
    assert len(calls) == 3
    assert calls[2]["tool_choice"] == {"type": "none"}
    assert "Tu borrador anterior" in calls[2]["system"]
    assert _last_sent_in_history() == good


def test_still_wrong_after_the_correction_sends_the_safe_reply(monkeypatch, chat):
    order = _transfer_order()
    _scripted(monkeypatch, _use("add_item", {"item_id": "d_gaseosa_personal"}),
              _say("Su pedido ya está pagado."), _say("Ya quedó pagado, gracias."))
    reply = _customer_says("Agrégueme una gaseosa personal")
    updated = orders.get(order["id"])
    assert reply == ("Listo, su pedido quedó actualizado:\n" + brain._order_text(updated)
                     + "\n\n" + _added_gaseosa_sentence())          # the edit is still confirmed
    assert "ya está pagado" not in reply


def test_an_amount_is_checked_by_its_meaning(monkeypatch):
    """Regression: $5.000 from a discarded cash row never validates "you still owe $5.000"."""
    monkeypatch.setattr(brain, "_turn", brain._Turn())          # no sentence handed out yet
    order = _order_of(("b_caserita", 1))
    payments.register_cash(order["id"], 5000)
    payments.register_cash(order["id"], 2400)
    payments.discard_cash(order["id"], _cash_ids(order["id"])[0], "Error")
    pay = payments.current(order["id"])
    sentence = brain.payment_sentence(pay)
    assert sentence == (f"Ya pagó {fmt(2400)} en efectivo. Quedan {fmt(CASERITA - 2400)} por "
                        "pagar en efectivo al recibir.")
    assert brain._problems("Todavía le faltan $5.000.", sentence, pay, PHONE)[1] is True
    assert brain._problems(f"Todavía le faltan $5.000. {sentence}", sentence, pay, PHONE)[1]
    assert brain._problems(
        f"Los {fmt(5000)} que el local había registrado se anularon. {sentence}",
        sentence, pay, PHONE) == (False, False)
    assert brain._problems(f"Le faltan {fmt(CASERITA - 2400)}. {sentence}",
                           sentence, pay, PHONE) == (False, False)

    db.reset()
    g29 = _g29_order()
    brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    pay = payments.current(g29["id"])
    sentence = brain.payment_sentence(pay)
    assert brain._problems(f"Faltan {fmt(10000)}. {sentence}", sentence, pay, PHONE)[1] is True
    assert brain._problems(f"Le quedan {fmt(10000)} por transferir. {sentence}",
                           sentence, pay, PHONE) == (False, False)


def test_a_balance_question_after_a_partial_discard(monkeypatch, chat):
    order = _order_of(("b_caserita", 1))
    payments.register_cash(order["id"], 5000)
    payments.register_cash(order["id"], 2400)
    payments.discard_cash(order["id"], _cash_ids(order["id"])[0], "Error")
    sentence = brain.payment_sentence_for(PHONE)
    good = f"Los {fmt(5000)} que el local había registrado se anularon. {sentence}"
    calls = _scripted(monkeypatch, _use("order_status"), _say("Todavía le faltan $5.000."),
                      _say(good))
    assert _customer_says("¿Cuánto me falta?") == good
    assert calls[0]["tool_choice"] == {"type": "tool", "name": "order_status"}   # fresh read
    assert "Tu borrador anterior" in calls[2]["system"]


def test_an_amount_from_older_chat_is_corrected(monkeypatch, chat):
    _transfer_order()
    sentence = brain.payment_sentence_for(PHONE)
    _scripted(monkeypatch, _use("order_status"), _say(f"Debe $12.345. {sentence}"),
              _say(sentence))
    assert _customer_says("¿Cuánto debo?") == sentence


def test_the_backstop_replaces_an_unchecked_payment_claim(monkeypatch, chat):
    _transfer_order()
    sentence = brain.payment_sentence_for(PHONE)
    calls = _scripted(monkeypatch, _say("¡Con gusto! Su pedido ya está pagado."),
                      _use("order_status"), _say(sentence))
    reply = _customer_says("Muchas gracias")
    assert reply == sentence
    assert "tool_choice" not in calls[0]                         # not a payment question
    assert calls[1]["tool_choice"] == {"type": "tool", "name": "order_status"}
    assert calls[2]["tool_choice"] == {"type": "none"}


def test_a_claim_after_confirm_order_is_still_checked(monkeypatch, chat):
    _pickup_order()                                              # waiting for the "sí"
    final = f"Su pedido quedó confirmado. Falta por pagar {fmt(ESPECIAL)}."
    calls = _scripted(monkeypatch, _use("confirm_order"),
                      _say("¡Listo! Su pedido quedó confirmado y ya está pagado."),
                      _use("order_status", block_id="t2"), _say(final))
    assert _customer_says("Sí, confirmo") == final
    assert calls[2]["tool_choice"] == {"type": "tool", "name": "order_status"}


def test_choose_payment_in_a_payment_question_is_checked(monkeypatch, chat):
    _confirmed_order()
    before = brain.payment_sentence_for(PHONE)                   # "Falta por pagar $29.900."
    after = f"Quedan {fmt(ESPECIAL)} por transferir." + TRANSFER_TAIL
    calls = _scripted(monkeypatch, _use("order_status"),
                      _use("choose_payment", {"method": "TRANSFER"}, "t2"), _say(f"Listo. {after}"))
    reply = _customer_says("Lo pago por transferencia, ¿cuánto sería?")
    assert reply == f"Listo. {after}"
    assert reply.count("Cuando transfiera") == 1
    assert len(calls) == 3                                       # the tool's sentence is reused

    db.reset()
    brain.forget(PHONE)
    _confirmed_order()
    _scripted(monkeypatch, _use("order_status"),
              _use("choose_payment", {"method": "TRANSFER"}, "t2"),
              _say(f"{before} {after}"), _say(after))           # an earlier, stale sentence
    assert _customer_says("Lo pago por transferencia, ¿cuánto sería?") == after

    db.reset()
    brain.forget(PHONE)
    _confirmed_order()
    _scripted(monkeypatch, _use("order_status"),
              _use("choose_payment", {"method": "TRANSFER"}, "t2"),
              _say(f"{after} Ya quedó pagado."), _say(after))    # a contradiction
    assert _customer_says("Lo pago por transferencia, ¿cuánto sería?") == after


def test_an_edit_after_order_status_needs_the_newer_sentence(monkeypatch, chat):
    _transfer_order()
    earlier = brain.payment_sentence_for(PHONE)
    calls = _scripted(monkeypatch, _use("order_status"),
                      _use("add_item", {"item_id": "d_gaseosa_personal"}, "t2"),
                      _say(earlier), _say(_added_gaseosa_sentence()))
    reply = _customer_says("¿Cuánto me falta? Y agrégueme una gaseosa personal")
    assert reply == _added_gaseosa_sentence()
    assert "Tu borrador anterior" in calls[3]["system"]


def test_a_failed_payment_read_never_sends_an_amount(monkeypatch, chat):
    _transfer_order()

    def broken(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(brain, "_live_payment", lambda phone: (None, True))
    monkeypatch.setattr(brain, "payment_sentence", broken)
    _scripted(monkeypatch, _use("order_status"), _say(f"Le faltan {fmt(ESPECIAL)} por transferir."))
    assert _customer_says("¿Cuánto me falta?") == brain.PAYMENT_UNAVAILABLE

    brain.forget(PHONE)
    _scripted(monkeypatch, _use("order_status"), _say("Déjeme revisar."))
    assert _customer_says("¿Cuánto me falta?") == f"Déjeme revisar.\n\n{brain.PAYMENT_UNAVAILABLE}"


@pytest.mark.parametrize("stop", ["hand_over", "customer_cancels"])
def test_payment_of_a_delivered_or_cancelled_order_is_not_verified(monkeypatch, chat, stop):
    if stop == "hand_over":
        _completed_order()
    else:
        orders.apply_event(_confirmed_order()["id"], "customer_cancels")
    calls = _scripted(monkeypatch, _use("order_status"), _say("Sí, ya está pagado."),
                      _say(brain.PAYMENT_UNVERIFIED))
    assert _customer_says("¿Ya quedó pago mi pedido?") == brain.PAYMENT_UNVERIFIED
    assert calls[0]["tool_choice"] == {"type": "tool", "name": "order_status"}

    brain.forget(PHONE)
    _scripted(monkeypatch, _use("order_status"), _say("Sí, ya está pagado."), _say("Ya pagó todo."))
    assert _customer_says("¿Ya quedó pago mi pedido?") == brain.PAYMENT_UNVERIFIED   # safe reply

    brain.forget(PHONE)
    _scripted(monkeypatch, _use("order_status"), _say("Déjeme ver."))
    assert _customer_says("¿Ya quedó pago mi pedido?") == (
        f"Déjeme ver.\n\n{brain.PAYMENT_UNVERIFIED}")


def test_an_earlier_order_never_gets_the_live_orders_payment(monkeypatch, chat):
    _completed_order()
    _order_of(("b_caserita", 1))                                 # a new live order
    live_sentence = brain.payment_sentence_for(PHONE)
    _scripted(monkeypatch, _use("order_status"),
              _say(f"Su pedido anterior: {live_sentence}"), _say(brain.PAYMENT_UNVERIFIED))
    reply = _customer_says("¿Y el pedido anterior quedó pago?")
    assert reply == brain.PAYMENT_UNVERIFIED
    tool_result = brain._history[PHONE][2]["content"][0]["content"]
    assert "Frase de pago" not in tool_result
    assert "NO SE PUDO verificar el pago de ese pedido" in tool_result


# -- ESTADO REAL, reports, schema, panel -----------------------------------------

def test_real_state_names_discarded_cash_apart_from_the_cash_that_counts():
    order = _order_of(("b_caserita", 1), method="CASH")
    payments.register_cash(order["id"], 5000)
    payments.register_cash(order["id"], 2400)
    payments.discard_cash(order["id"], _cash_ids(order["id"])[0], "Error")
    prompt = brain.system_prompt(PHONE)
    assert _payment_line(prompt) == (
        "- Pago del pedido más reciente: en efectivo al recibir; el local ya recibió una parte en "
        "efectivo; el local anuló 1 registro de efectivo, que ya no cuenta como pago; no ha "
        "enviado ningún comprobante; queda por pagar en efectivo al recibir; todavía falta por "
        "pagar.")
    block = _real_state_block(prompt)
    assert "Nunca digas que se le devolvió dinero ni que no entregó el dinero" in block
    assert "Nunca uses montos de mensajes anteriores" in block
    assert "$" not in block


def test_report_counts_what_really_came_in():
    def kinds():
        return {k["kind"]: (k["orders"], k["received"]) for k in reports.snapshot(1)["how_paid"]}

    a = _transfer_order()
    payments.register_cash(a["id"], ESPECIAL)                    # agreed transfer, paid in cash
    b = _second_transfer_order()
    payments.confirm_receipt(b["id"], _receipt(b["id"], ESPECIAL, "REP2-B")["receipt"]["id"])
    c = _order_of(("b_especial", 1))                             # cash, then a confirmed transfer
    payments.register_cash(c["id"], ESPECIAL)
    red = _receipt(c["id"], ESPECIAL, "REP2-C")["receipt"]
    payments.confirm_receipt(c["id"], red["id"], ESPECIAL)
    d = _second_transfer_order()
    _receipt(d["id"], ESPECIAL, "REP2-D")                        # waiting: does not count yet
    e = _confirmed_order()
    payments.register_cash(e["id"], 5000)
    payments.discard_cash(e["id"], _cash_ids(e["id"])[0], "Error")

    assert kinds() == {
        "CASH": (1, ESPECIAL), "TRANSFER": (1, ESPECIAL),
        "MIXED": (1, 2 * ESPECIAL),                              # not capped at the total
        "NONE": (2, 0),
    }
    assert reports.snapshot(1)["confirmed_overpayments"] == [{
        "code": orders.get(c["id"])["code"], "total": ESPECIAL, "received": 2 * ESPECIAL,
        "surplus": ESPECIAL,
    }]


def test_report_never_turns_a_waiting_receipt_into_a_confirmed_overpayment():
    order = _transfer_order()
    _receipt(order["id"], ESPECIAL, "WAITOVER-1")                # clean, nobody confirmed it
    payments.register_cash(order["id"], ESPECIAL)
    assert payments.current(order["id"])["overpayment_amount"] == ESPECIAL   # card: "Sobrepago"
    snapshot = reports.snapshot(1)
    assert snapshot["confirmed_overpayments"] == []
    assert {k["kind"]: k["orders"] for k in snapshot["how_paid"]}["CASH"] == 1


def test_report_lists_rejected_orders_with_money_apart():
    order = _order_of(("d_gaseosa_mini", 1), method="CASH")
    payments.register_cash(order["id"], MINI)
    orders.apply_event(order["id"], "staff_rejects", "Se acabó")
    snapshot = reports.snapshot(1)
    assert all(k["orders"] == 0 for k in snapshot["how_paid"])
    assert snapshot["stopped_with_payments"] == [{
        "code": order["code"], "state": "REJECTED", "total": MINI, "received": MINI,
        "cash": MINI, "confirmed": 0,
    }]
    html = (Path(__file__).resolve().parent.parent / "static" / "reports.html").read_text(
        encoding="utf-8")
    for text in ("Dinero recibido", "Sin pago registrado", "Pagos de más confirmados",
                 "Pagos de pedidos cancelados/rechazados", "Pagos registrados",
                 "este reporte no registra devoluciones"):
        assert text in html


def test_reset_wipes_cash_rows():
    order = _confirmed_order()
    payments.register_cash(order["id"], 5000)
    db.reset()
    assert db.q1("SELECT COUNT(*) AS n FROM cash_payments")["n"] == 0


def test_kitchen_card_has_the_pagos_block():
    html = (Path(__file__).resolve().parent.parent / "static" / "kitchen.html").read_text(
        encoding="utf-8")
    assert "<b>Pagos</b>" in html
    assert "<b>Comprobantes</b>" not in html
    assert "Efectivo recibido" in html
    assert "/cash/${cashId}/discard" in html
    assert "¿Descartar este efectivo?" in html
    assert "El cliente no recibe ningún mensaje." in html
    assert ">Descartar</button>" not in html                   # the X replaced the text button
    assert "filter(r => r.status !== 'DISCARDED')" in html      # discarded rows leave the card


# ---- two models: conversation and receipts (D1-A) ----------------------------

PROOF_IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}}


def _model_recorder(seen):
    """A fake client that records which model each call asked for."""
    class FakeMessages:
        def create(self, **kwargs):
            seen.append(kwargs["model"])
            text = '{"amount": 47300, "reference": "M-1"}' \
                if kwargs.get("system") == brain.PROOF_SYSTEM else "Listo."
            return types.SimpleNamespace(
                stop_reason="end_turn", content=[types.SimpleNamespace(type="text", text=text)])
    return types.SimpleNamespace(messages=FakeMessages())


def _two_models(monkeypatch, chat, proof):
    monkeypatch.setenv("ANTHROPIC_MODEL", chat)
    monkeypatch.setenv("ANTHROPIC_PROOF_MODEL", proof)
    monkeypatch.setattr(brain, "_model", None)
    monkeypatch.setattr(brain, "_proof_model", None)
    seen = []
    fake = _model_recorder(seen)
    monkeypatch.setattr(brain, "client", lambda: fake)
    return seen


def test_conversation_and_receipts_use_their_own_models(monkeypatch):
    seen = _two_models(monkeypatch, "chat-model", "proof-model")
    brain.forget(PHONE)
    try:
        brain.reply(PHONE, "hola")
    finally:
        brain.forget(PHONE)
    assert brain.extract_proof(PROOF_IMAGE)["amount"] == 47300
    assert seen == ["chat-model", "proof-model"]


def test_receipts_use_the_conversation_model_when_no_proof_model_is_set(monkeypatch):
    seen = _two_models(monkeypatch, "chat-model", "")
    assert brain.extract_proof(PROOF_IMAGE)["amount"] == 47300
    assert seen == ["chat-model"]


def test_startup_banner_names_both_models(monkeypatch):
    _two_models(monkeypatch, "chat-model", "proof-model")
    assert brain.model_names() == ("chat-model", "proof-model")
    monkeypatch.setenv("ANTHROPIC_PROOF_MODEL", "")
    assert brain.model_names() == ("chat-model", "chat-model (igual que conversación)")
    monkeypatch.setenv("ANTHROPIC_MODEL", "")
    assert brain.model_names() == ("automático", "automático (igual que conversación)")


def test_an_empty_chat_model_picks_haiku_first(monkeypatch):
    """Sonnet 5.5 and Opus 5.5 refuse the forced order_status call (a 400 on every payment
    message), so with ANTHROPIC_MODEL empty the automatic choice starts with Haiku."""
    monkeypatch.setenv("ANTHROPIC_MODEL", "")
    monkeypatch.setattr(brain, "_model", None)
    ids = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
    listing = types.SimpleNamespace(data=[types.SimpleNamespace(id=i) for i in ids])
    fake = types.SimpleNamespace(models=types.SimpleNamespace(list=lambda limit: listing))
    monkeypatch.setattr(brain, "client", lambda: fake)
    assert brain.model() == "claude-haiku-5-5"
