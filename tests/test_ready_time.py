"""Preparation time: staff pick the minutes on Aceptar, Cambiar tiempo moves them, the
customer is told «aproximadamente X minutos» (code-written, never a clock time), and
the bot only copies what is left.

No model calls. FAKE mode: messages are read back from outbound_messages."""

from datetime import datetime, timedelta, timezone

import pytest

from app import brain, cart, db, orders, wa
from app.catalog import CATALOG

PHONE = "573001112244"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(wa, "live", lambda: False)
    db.init()
    db.reset()
    brain.reset_all()
    yield
    brain.reset_all()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app import main
    return TestClient(main.app)


def _confirmed(fulfilment="PICKUP"):
    cart.add_item(PHONE, "b_caserita", 1)
    extra = {"address": "Calle 1 #2-3", "zone": "Cabecera"} if fulfilment == "DELIVERY" else {}
    order = orders.create_from_cart(PHONE, fulfilment, **extra)
    return orders.apply_event(order["id"], "customer_confirms")


def _sent() -> list[str]:
    return [r["body"] for r in db.q("SELECT body FROM outbound_messages ORDER BY id")]


def _accept(client, order, **body):
    return client.post(f"/api/orders/{order['id']}/event", json={"event": "staff_accepts", **body})


def _change(client, order, minutes):
    return client.post(f"/api/orders/{order['id']}/ready_by", json={"minutes": minutes})


def _ready_in(order, minutes: float):
    when = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    db.run("UPDATE orders SET ready_by=%s WHERE id=%s", (when.isoformat(), order["id"]))


@pytest.mark.parametrize("fulfilment", ["PICKUP", "DELIVERY"])
def test_accept_tells_the_customer_how_long_it_takes(client, fulfilment):
    order = _confirmed(fulfilment)
    assert _accept(client, order, minutes=20).status_code == 200
    assert _sent()[-1] == "¡Recibimos su pedido! Tardará aproximadamente 20 minutos."
    assert orders.get(order["id"])["ready_by"]


def test_accept_without_minutes_keeps_the_old_message(client):
    order = _confirmed()
    _accept(client, order)
    assert _sent()[-1] == "¡Recibimos su pedido! En un momento entra en preparación."
    assert orders.get(order["id"])["ready_by"] is None


@pytest.mark.parametrize("minutes", [0, -5, 241, "20", 2.5, True])
def test_bad_minutes_accept_nothing(client, minutes):
    order = _confirmed()
    assert _accept(client, order, minutes=minutes).status_code == 400
    assert orders.get(order["id"])["state"] == "CONFIRMED"
    assert _sent() == []


def test_change_time_later_apologises(client):
    order = _confirmed()
    _accept(client, order, minutes=15)
    assert _change(client, order, 45).status_code == 200
    assert _sent()[-1] == "Disculpe la demora: a su pedido le faltan aproximadamente 45 minutos."


def test_change_time_earlier_says_so(client):
    order = _confirmed()
    _accept(client, order, minutes=45)
    _change(client, order, 15)
    assert _sent()[-1] == "Su pedido va más rápido: le faltan aproximadamente 15 minutos."


def test_first_time_given_by_change_time(client):
    order = _confirmed()
    _accept(client, order)
    _change(client, order, 30)
    assert _sent()[-1] == "Su pedido tardará aproximadamente 30 minutos."


def test_a_change_under_five_minutes_sends_nothing(client):
    order = _confirmed()
    _accept(client, order, minutes=20)
    before = len(_sent())
    _change(client, order, 20)
    assert len(_sent()) == before


def test_change_time_only_while_it_is_being_made(client):
    order = _confirmed()
    assert _change(client, order, 20).status_code == 409
    assert client.post("/api/orders/99999/ready_by", json={"minutes": 20}).status_code == 404


def test_back_to_preparing_does_not_repeat_the_time(client):
    order = _confirmed()
    _accept(client, order, minutes=20)
    client.post(f"/api/orders/{order['id']}/event", json={"event": "food_ready"})
    client.post(f"/api/orders/{order['id']}/event", json={"event": "mark_in_error"})
    assert _sent()[-1] == "¡Recibimos su pedido! En un momento entra en preparación."


@pytest.mark.parametrize("left, said", [(20, 20), (12.5, 15), (0.5, 5)])
def test_the_bot_gets_what_is_left_rounded_up_to_five(client, left, said):
    order = _confirmed()
    _accept(client, order, minutes=20)
    _ready_in(order, left)
    state = brain._real_state(PHONE)
    assert f"le faltan aproximadamente {said} minutos" in state
    assert "nunca des una hora del reloj" in state


def test_the_bot_knows_when_the_time_has_passed(client):
    order = _confirmed()
    _accept(client, order, minutes=20)
    _ready_in(order, -10)
    state = brain._real_state(PHONE)
    assert "ya pasó" in state and "aproximadamente" not in state


def test_no_time_line_without_an_estimate_or_once_ready(client):
    order = _confirmed()
    _accept(client, order)
    assert "le faltan" not in brain._real_state(PHONE)
    _ready_in(order, 20)
    client.post(f"/api/orders/{order['id']}/event", json={"event": "food_ready"})
    assert "le faltan" not in brain._real_state(PHONE)


def test_board_and_menu_carry_the_time(client):
    order = _confirmed()
    _accept(client, order, minutes=20)
    card = next(o for o in client.get("/api/board").json()["orders"] if o["id"] == order["id"])
    assert card["ready_by"]
    menu = client.get("/api/menu").json()
    assert menu["prep_minutes"] == CATALOG.prep_minutes()
