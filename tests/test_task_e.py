"""Task E: changes after acceptance (automatic additions, requests, the staff's Editar),
durable notices, G43, the 6 s wait, photos, stickers, location and files.

No model calls: a scripted client stands in for the model. FAKE mode: messages are
read back from outbound_messages. Prices come from config/restaurant.json."""

import json
import types

import pytest

from app import brain, cart, changes, chatwoot, db, main, orders, payments, transcribe, wa, worker
from app.catalog import CATALOG
from app.money import fmt

PHONE = "573001112233"
CASERITA = CATALOG.price_of("b_caserita")            # 18900
MADURITA = CATALOG.price_of("b_madurita")            # 21900
GASEOSA = CATALOG.price_of("d_gaseosa_personal")     # 3500
QUESO = CATALOG.modifier_price("m_queso")            # 2000
CABECERA = 5000

PATH = {
    "PENDING_CONFIRMATION": [],
    "CONFIRMED": ["customer_confirms"],
    "ACCEPTED": ["customer_confirms", "staff_accepts"],
    "READY": ["customer_confirms", "staff_accepts", "food_ready"],
    "OUT_FOR_DELIVERY": ["customer_confirms", "staff_accepts", "food_ready", "driver_leaves"],
    "COMPLETED": ["customer_confirms", "staff_accepts", "food_ready", "hand_over"],
}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(brain, "_proof_model", "test-model")
    monkeypatch.setattr(wa, "live", lambda: False)
    db.init()
    db.reset()
    brain.reset_all()
    yield
    brain.reset_all()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    return TestClient(main.app)


def _order(state="ACCEPTED", fulfilment="PICKUP", items=(("b_caserita", 1),), split=None):
    for item_id, qty in items:
        cart.add_item(PHONE, item_id, qty)
    extra = {"address": "Calle 1 #2-3", "zone": "Cabecera"} if fulfilment == "DELIVERY" else {}
    order = orders.create_from_cart(PHONE, fulfilment, **extra)
    if split:
        payments.set_method(order["id"], *split)
    for event in PATH[state]:
        order = orders.apply_event(order["id"], event)
    return orders.get(order["id"])


def _outbound(where: str = "TRUE") -> list[dict]:
    return db.q(f"SELECT * FROM outbound_messages WHERE {where} ORDER BY id")


def _request() -> dict | None:
    row = db.q1("SELECT * FROM change_requests ORDER BY id DESC LIMIT 1")
    return changes.get(row["id"]) if row else None


def _clean_for_customer(text: str):
    assert "P-" not in text and "cocina" not in text.lower()


def _text(t):
    return types.SimpleNamespace(stop_reason="end_turn", content=[types.SimpleNamespace(type="text", text=t)])


def _tool(name, args=None):
    return types.SimpleNamespace(stop_reason="tool_use", content=[
        types.SimpleNamespace(type="tool_use", id=f"t-{name}", name=name, input=args or {})])


def _model(monkeypatch, answers=None):
    """A scripted client. `answers`: responses in order (then «Listo.»). Records every
    call's messages."""
    seen = types.SimpleNamespace(calls=[])
    queue = list(answers or [])

    class Messages:
        def create(self, **kw):
            seen.calls.append([dict(m) for m in kw["messages"]])
            return queue.pop(0) if queue else _text("Listo.")

    monkeypatch.setattr(brain, "_model", "test-model")
    monkeypatch.setattr(brain, "client", lambda: types.SimpleNamespace(messages=Messages()))
    return seen


def _post(client, url, body=None):
    return client.post(url, json=body or {})


# ---- Part 1: the wait ------------------------------------------------------------

def test_wait_default_is_six_seconds(monkeypatch):
    monkeypatch.delenv("MESSAGE_WAIT_SECONDS", raising=False)
    assert worker.wait_seconds() == 6.0


# ---- Part 2: G43 -----------------------------------------------------------------

def _seed_history():
    brain._history[PHONE] = [
        {"role": "user", "content": "Quiero una caserita"},
        {"role": "assistant", "content": [{"type": "text", "text": "Listo, una caserita."}]},
    ]


def test_a_finished_order_starts_a_fresh_conversation_once(monkeypatch):
    seen = _model(monkeypatch)
    _order("COMPLETED")
    _seed_history()
    brain.reply(PHONE, "Quiero una caserita", customer=True)
    assert seen.calls[-1] == [{"role": "user", "content": "Quiero una caserita"}]
    brain.reply(PHONE, "y una gaseosa", customer=True)
    assert len(seen.calls[-1]) == 3                  # the fresh turn is kept


def test_history_stays_while_another_order_is_live(monkeypatch):
    seen = _model(monkeypatch)
    _order("ACCEPTED")
    _order("COMPLETED", items=(("d_gaseosa_personal", 1),))
    _seed_history()
    brain.reply(PHONE, "hola", customer=True)
    assert len(seen.calls[-1]) == 3


def test_the_bots_own_cancel_keeps_the_history(monkeypatch):
    seen = _model(monkeypatch)
    _order("PENDING_CONFIRMATION")
    _seed_history()
    assert brain._run_tool(PHONE, "cancel_order", {}).startswith("Pedido cancelado")
    brain.reply(PHONE, "mejor sólo una gaseosa", customer=True)
    assert len(seen.calls[-1]) == 3


def test_a_rejection_message_stays_for_the_customers_reply(monkeypatch):
    seen = _model(monkeypatch, [_text("Lo sentimos, se acabó el pan.")])
    order = _order("CONFIRMED")
    _seed_history()
    order = orders.apply_event(order["id"], "staff_rejects", "se acabó el pan")
    main._stop_message(order, "se acabó el pan")
    assert len(seen.calls[-1]) == 1                  # cleared right before the stop message
    brain.reply(PHONE, "¿por qué?", customer=True)
    assert len(seen.calls[-1]) == 3                  # the stop message is still there


def test_the_catch_up_starts_after_the_finished_order(monkeypatch):
    monkeypatch.setattr(chatwoot, "enabled", lambda: True)
    db.run("INSERT INTO conversations(phone, created_at) VALUES(%s,%s)", (PHONE, db.now()))
    db.run("INSERT INTO inbound_messages(wa_message_id, phone, body, kind, received_at, status)"
           " VALUES('old', %s, 'quiero una caserita', 'text', %s, 'HUMAN')", (PHONE, db.now()))
    _order("COMPLETED")
    db.run("INSERT INTO inbound_messages(wa_message_id, phone, body, kind, received_at, status)"
           " VALUES('new', %s, 'gracias', 'text', %s, 'HUMAN')", (PHONE, db.now()))
    note = worker._catch_up(PHONE)
    assert "gracias" in note and "quiero una caserita" not in note


# ---- Part 3: what a change is ---------------------------------------------------------

def _target(order, add=(), remove=(), **delivery):
    target = orders.target_of(order)
    lines = target["lines"] + [{"item_id": i, "qty": q, "modifier_ids": m, "note": n}
                               for i, q, m, n in add]
    lines = [line for line in lines if line["item_id"] not in remove]
    return {**target, "lines": lines, **delivery}


def test_the_route_of_a_change(monkeypatch):
    accepted = _order("ACCEPTED")
    assert changes.route_for(accepted, _target(accepted, [("d_gaseosa_personal", 2, [], None)])) == "AUTO"
    assert changes.route_for(accepted, _target(accepted, [("b_madurita", 1, [], None)])) == "APPROVAL"
    assert changes.route_for(accepted, _target(accepted, [("d_gaseosa_personal", 1, [], "fría")])) == "APPROVAL"
    mixed = _target(accepted, [("d_gaseosa_personal", 1, [], None)])
    mixed["lines"][0]["qty"] = 2
    assert changes.route_for(accepted, mixed) == "APPROVAL"     # a burger more is not automatic
    moved = _target(accepted, [("d_gaseosa_personal", 1, [], None)], fulfilment="DELIVERY",
                    address="Calle 9", zone="Centro")
    assert changes.route_for(accepted, moved) == "APPROVAL"
    gaseosa = CATALOG.item("d_gaseosa_personal")
    monkeypatch.setitem(gaseosa, "available", False)
    assert changes.route_for(accepted, _target(accepted, [("d_gaseosa_personal", 1, [], None)])) == "APPROVAL"
    monkeypatch.setitem(gaseosa, "available", True)
    monkeypatch.setitem(gaseosa, "add_after_accept", False)
    assert changes.route_for(accepted, _target(accepted, [("d_gaseosa_personal", 1, [], None)])) == "APPROVAL"


def test_after_listo_even_a_drink_needs_approval():
    ready = _order("READY")
    assert changes.route_for(ready, _target(ready, [("d_gaseosa_personal", 1, [], None)])) == "APPROVAL"


# ---- Flow A: an automatic addition ------------------------------------------------------

def test_an_automatic_addition_is_proposed_then_applied_once():
    order = _order("ACCEPTED")
    result = brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    assert result.startswith("PROPUESTA")
    assert f"Agregar 1× Gaseosa personal ({fmt(GASEOSA)}). Su pedido quedaría en {fmt(CASERITA + GASEOSA)}" in result
    assert orders.get(order["id"])["total"] == CASERITA            # nothing changed yet

    result = brain._run_tool(PHONE, "confirm_change", {})
    assert result.startswith("CAMBIO HECHO")
    updated = orders.get(order["id"])
    assert (updated["id"], updated["code"], updated["total"]) == (order["id"], order["code"], CASERITA + GASEOSA)
    assert updated["edited_by"] == "customer"
    assert _request()["status"] == "APPLIED"
    [notice] = _outbound()
    assert notice["status"] == "QUEUED" and notice["notice_kind"] == "ORDER_STATE"
    assert notice["body"].startswith("Listo, agregamos 1× Gaseosa personal. Así quedó su pedido:")
    assert fmt(CASERITA + GASEOSA) in notice["body"]
    _clean_for_customer(notice["body"])
    assert brain._run_tool(PHONE, "confirm_change", {}).startswith("NO SE PUDO")

    added = changes.board(order["id"])["added"]
    assert [a["diff"] for a in added] == [["Agregar 1× Gaseosa personal"]]
    changes.seen(order["id"], added[0]["id"])
    assert changes.board(order["id"])["added"] == []


def test_a_stale_automatic_proposal_is_asked_again():
    order = _order("ACCEPTED")
    brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    orders.apply_event(order["id"], "food_ready")                 # no longer automatic
    result = brain._run_tool(PHONE, "confirm_change", {})
    assert result.startswith("NO SE PUDO") and "cambió" in result
    assert _request()["status"] == "DISCARDED"
    assert orders.get(order["id"])["total"] == CASERITA and _outbound() == []


def test_the_customer_can_say_no():
    order = _order("ACCEPTED")
    brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    assert "no cambiamos nada" in brain._run_tool(PHONE, "discard_change", {})
    assert _request()["status"] == "DISCARDED"
    assert orders.get(order["id"])["total"] == CASERITA


def test_a_proposal_goes_out_exactly_as_code_built_it(monkeypatch):
    # Hand test row 6: the model reworded an AUTO proposal as an APPROVAL one
    # («¿Se lo envío al local?»), and the customer's 👍 then added it at once.
    _order("ACCEPTED")
    _model(monkeypatch, [
        _tool("add_item", {"item_id": "d_gaseosa_personal"}),
        _text("Este cambio lo tiene que aprobar el local: agregar 1 Gaseosa personal. "
              "¿Se lo envío al local?"),
    ])
    sent = brain.reply(PHONE, "agrégueme una gaseosa personal", customer=True)
    want = (f"Agregar 1× Gaseosa personal ({fmt(GASEOSA)}). Su pedido quedaría en "
            f"{fmt(CASERITA + GASEOSA)}. ¿Lo agregamos?")
    assert sent == want
    assert brain._history[PHONE][-1]["content"] == [{"type": "text", "text": want}]


def test_a_swap_after_the_proposal_becomes_one_proposal(monkeypatch):
    # Hand test row 7: after add_item's proposal the model stopped and never removed
    # the caserita; the proposal now tells it to finish the swap.
    _order("ACCEPTED", items=(("b_caserita", 2),))
    seen = _model(monkeypatch, [
        _tool("add_item", {"item_id": "b_madurita"}),
        _tool("remove_item", {"item_id": "b_caserita", "qty": 1}),
        _text("Le cambiamos una caserita por una madurita."),
    ])
    sent = brain.reply(PHONE, "cámbieme una caserita por una madurita", customer=True)
    [first] = seen.calls[1][-1]["content"]                       # add_item's tool result
    assert "usa remove_item ahora" in first["content"]
    assert sent == ("Este cambio lo tiene que aprobar el local: Agregar 1× Caserita Madurita; "
                    f"Caserita: de 2 a 1. Si lo aprueban, su pedido quedaría en "
                    f"{fmt(CASERITA + MADURITA)}. ¿Se lo envío al local?")
    assert _request()["route"] == "APPROVAL" and _request()["status"] == "PROPOSED"


def test_a_proposal_answered_in_the_same_turn_keeps_the_models_reply(monkeypatch):
    _order("ACCEPTED")
    _model(monkeypatch, [
        _tool("add_item", {"item_id": "d_gaseosa_personal"}),
        _tool("discard_change"),
        _text("Listo, no cambiamos nada."),
    ])
    assert brain.reply(PHONE, "una gaseosa... no, mejor nada", customer=True) == \
        "Listo, no cambiamos nada."


def test_a_sticker_turn_confirms_nothing(monkeypatch):
    _order("ACCEPTED")
    brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    monkeypatch.setattr(brain, "_turn", brain._Turn(sticker=True))
    for tool in ("confirm_change", "confirm_order", "cancel_order", "choose_payment"):
        assert brain._run_tool(PHONE, tool, {"method": "CASH"}) == brain.STICKER_REFUSED
    assert _request()["status"] == "PROPOSED"


def test_a_mix_becomes_one_request_for_approval():
    _order("ACCEPTED", items=(("b_caserita", 1), ("b_madurita", 1)))
    brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    result = brain._run_tool(PHONE, "remove_item", {"item_id": "b_madurita"})
    assert "Este cambio lo tiene que aprobar el local" in result
    row = _request()
    assert (row["route"], row["revision"], row["status"]) == ("APPROVAL", 2, "PROPOSED")
    assert row["diff"] == ["Agregar 1× Gaseosa personal", "Quitar 1× Caserita Madurita"]
    assert db.q1("SELECT COUNT(*) AS n FROM change_requests")["n"] == 1


def test_emptying_an_accepted_order_is_refused():
    _order("ACCEPTED")
    assert brain._run_tool(PHONE, "remove_item", {"item_id": "b_caserita"}).startswith("NO SE PUDO")
    assert _request() is None


# ---- Flow B: a request the restaurant accepts or rejects --------------------------------

def _pending_request(state="ACCEPTED", fulfilment="PICKUP", item="b_madurita"):
    order = _order(state, fulfilment)
    brain._run_tool(PHONE, "add_item", {"item_id": item})
    assert brain._run_tool(PHONE, "confirm_change", {}).startswith("SOLICITUD ENVIADA")
    return order, _request()


def test_a_request_waits_on_the_card_and_is_accepted_once(client):
    order, row = _pending_request()
    assert row["status"] == "PENDING" and orders.get(order["id"])["total"] == CASERITA
    card = next(o for o in client.get("/api/board").json()["orders"] if o["id"] == order["id"])
    assert card["change"]["request"]["proposed_total"] == CASERITA + MADURITA

    url = f"/api/orders/{order['id']}/changes/{row['id']}/accept"
    preview = _post(client, url, {"revision": row["revision"], "preview": True}).json()
    assert preview["message"].startswith(main.ACCEPT_HEAD)
    assert orders.get(order["id"])["total"] == CASERITA and _outbound() == []
    assert _request()["status"] == "PENDING"

    saved = _post(client, url, {"revision": row["revision"], "token": preview["token"]})
    assert saved.status_code == 200
    updated = orders.get(order["id"])
    assert updated["total"] == CASERITA + MADURITA and updated["edited_by"] == "staff"
    assert _request()["status"] == "APPLIED"
    [notice] = _outbound()
    assert notice["body"] == preview["message"] and notice["status"] == "FAKE"
    _clean_for_customer(notice["body"])
    again = _post(client, url, {"revision": row["revision"], "token": preview["token"]})
    assert again.status_code == 409 and again.json()["error"] == changes.RESOLVED


def test_a_rejected_request_leaves_the_order_and_tells_the_reason(client):
    order, row = _pending_request()
    url = f"/api/orders/{order['id']}/changes/{row['id']}/reject"
    assert _post(client, url, {"revision": row["revision"], "reason": " "}).status_code == 400
    assert _post(client, url, {"revision": row["revision"], "reason": "se acabó el maduro"}).status_code == 200
    assert _request()["status"] == "REJECTED"
    assert orders.get(order["id"])["total"] == CASERITA
    assert _outbound()[-1]["body"] == (
        "El local no pudo hacer el cambio que pidió. Motivo: se acabó el maduro. Su pedido sigue igual.")
    assert "rechazó" in brain.system_prompt(PHONE)


def test_staff_act_only_on_the_revision_they_saw(client):
    order, row = _pending_request()
    url = f"/api/orders/{order['id']}/changes/{row['id']}"
    preview = _post(client, f"{url}/accept", {"revision": row["revision"], "preview": True}).json()
    brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})   # the customer changes it
    assert _request()["status"] == "PROPOSED"
    brain._run_tool(PHONE, "confirm_change", {})
    assert (_request()["status"], _request()["revision"]) == ("PENDING", row["revision"] + 1)
    old = _post(client, f"{url}/accept", {"revision": row["revision"], "token": preview["token"]})
    assert old.status_code == 409 and old.json()["error"] == changes.MODIFIED
    rejected = _post(client, f"{url}/reject", {"revision": row["revision"], "reason": "no"})
    assert rejected.status_code == 409 and rejected.json()["error"] == changes.MODIFIED
    assert orders.get(order["id"])["total"] == CASERITA and _outbound() == []


def test_a_request_built_on_an_older_order_is_refused(client):
    order, row = _pending_request()
    with db.transaction():
        orders.apply_target(order["id"], _target(order, [("d_gaseosa_mini", 1, [], None)]),
                            orders.ON_BOARD, "staff")
    answer = _post(client, f"/api/orders/{order['id']}/changes/{row['id']}/accept",
                   {"revision": row["revision"], "preview": True})
    assert answer.status_code == 409 and answer.json()["error"] == changes.BASE_CHANGED


def test_a_payment_change_after_the_preview_is_refused(client):
    order, row = _pending_request()
    url = f"/api/orders/{order['id']}/changes/{row['id']}/accept"
    preview = _post(client, url, {"revision": row["revision"], "preview": True}).json()
    payments.register_cash(order["id"], 5000)
    answer = _post(client, url, {"revision": row["revision"], "token": preview["token"]})
    assert answer.status_code == 409 and answer.json()["error"] == changes.STATE_CHANGED


def test_en_camino_needs_the_driver_and_never_becomes_pickup(client):
    order, row = _pending_request("OUT_FOR_DELIVERY", "DELIVERY", item="d_gaseosa_personal")
    url = f"/api/orders/{order['id']}/changes/{row['id']}/accept"
    preview = _post(client, url, {"revision": row["revision"], "preview": True}).json()
    refused = _post(client, url, {"revision": row["revision"], "token": preview["token"]})
    assert refused.status_code == 400 and refused.json()["error"] == changes.DRIVER_REQUIRED
    assert _post(client, url, {"revision": row["revision"], "token": preview["token"],
                               "driver_ok": True}).status_code == 200
    answer = brain._run_tool(PHONE, "create_order", {"fulfilment": "PICKUP"})
    assert answer.startswith("NO SE PUDO") and orders.ON_THE_WAY_PICKUP in answer


def test_a_request_closes_when_the_order_is_delivered(client):
    order, row = _pending_request("READY")
    assert _post(client, f"/api/orders/{order['id']}/event", {"event": "hand_over"}).status_code == 200
    assert _request()["status"] == "CLOSED"
    assert _outbound()[-1]["body"] == changes.NOT_DONE_DELIVERED


def test_a_request_survives_a_restart_and_is_one_per_order(client):
    order, row = _pending_request()
    brain.reset_all()                                 # the bot's memory, as on a restart
    assert changes.open_for(order["id"])["status"] == "PENDING"
    with pytest.raises(db.IntegrityError):
        db.run("INSERT INTO change_requests(order_id, status, route, target, diff, base_token,"
               " current_total, proposed_total, created_at, updated_at)"
               " VALUES(%s,'PROPOSED','AUTO','{}','[]','x',0,0,%s,%s)", (order["id"], db.now(), db.now()))


def test_a_pending_request_is_in_the_bots_real_state():
    order, _ = _pending_request()
    assert "todavía NO se aplicó" in brain.system_prompt(PHONE)
    assert "Caserita Madurita" in brain._run_tool(PHONE, "order_status", {})


# ---- the staff's Editar ------------------------------------------------------------------

def _edit(client, order, body):
    return _post(client, f"/api/orders/{order['id']}/edit", body)


def _lines(*items):
    return [{"item_id": i, "qty": q, "modifier_ids": list(m), "note": None} for i, q, m in items]


@pytest.mark.parametrize("state", ["CONFIRMED", "ACCEPTED", "READY", "OUT_FOR_DELIVERY"])
def test_staff_edit_any_card(client, state):
    order = _order(state, "DELIVERY")
    body = {"lines": _lines(("b_caserita", 1, []), ("d_gaseosa_personal", 1, [])),
            "fulfilment": "DELIVERY", "address": "Calle 1 #2-3", "zone": "Cabecera"}
    preview = _edit(client, order, {**body, "preview": True}).json()
    assert preview["changed"] and preview["message"].startswith(main.EDIT_HEAD)
    assert orders.get(order["id"])["total"] == order["total"] and _outbound() == []
    saved = _edit(client, order, {**body, "token": preview["token"], "driver_ok": True})
    assert saved.status_code == 200
    updated = orders.get(order["id"])
    assert (updated["id"], updated["code"], updated["state"]) == (order["id"], order["code"], state)
    assert updated["total"] == CASERITA + GASEOSA + CABECERA and updated["edited_by"] == "staff"
    [notice] = _outbound()
    assert notice["body"] == preview["message"]
    _clean_for_customer(notice["body"])
    assert "El local cambió este pedido desde el panel" in brain.system_prompt(PHONE)


def test_staff_edit_errors(client):
    order = _order("ACCEPTED")
    ok = {"lines": _lines(("b_caserita", 1, [])), "fulfilment": "PICKUP", "preview": True}
    cases = [
        ({**ok, "lines": []}, 400, main.EMPTY_EDIT),
        ({**ok, "lines": _lines(("pizza", 1, []))}, 400, "'pizza' no está en la carta."),
        ({**ok, "lines": _lines(("d_gaseosa_personal", 1, ["m_queso"]))}, 400, None),
        ({**ok, "lines": [{"item_id": "b_caserita", "qty": True}]}, 400, main.BAD_EDIT),
        ({**ok, "lines": [{"item_id": "b_caserita", "qty": 1, "note": "x" * 121}]}, 400, main.BAD_EDIT),
        ({**ok, "fulfilment": "DELIVERY"}, 400, main.ADDRESS_REQUIRED),
        ({**ok, "preview": False, "token": "old"}, 409, changes.STATE_CHANGED),
    ]
    for body, status, error in cases:
        answer = _edit(client, order, body)
        assert answer.status_code == status, body
        if error:
            assert answer.json()["error"] == error
    on_the_way = _order("OUT_FOR_DELIVERY", "DELIVERY")
    answer = _edit(client, on_the_way, {**ok, "lines": _lines(("b_caserita", 1, []))})
    assert answer.json()["error"] == orders.ON_THE_WAY_PICKUP
    done = _order("COMPLETED")
    assert _edit(client, done, ok).status_code == 409
    assert orders.get(order["id"])["total"] == CASERITA and _outbound() == []


def test_staff_edit_fees_follow_the_delivery(client):
    order = _order("ACCEPTED")
    body = {"lines": _lines(("b_caserita", 1, [])), "fulfilment": "DELIVERY",
            "address": "Calle 5", "zone": "cabecera"}
    preview = _edit(client, order, {**body, "preview": True}).json()
    _edit(client, order, {**body, "token": preview["token"]})
    assert orders.get(order["id"])["delivery_fee"] == CABECERA
    back = {"lines": _lines(("b_caserita", 1, [])), "fulfilment": "PICKUP"}
    preview = _edit(client, order, {**back, "preview": True}).json()
    _edit(client, order, {**back, "token": preview["token"]})
    updated = orders.get(order["id"])
    assert (updated["delivery_fee"], updated["address"], updated["total"]) == (0, None, CASERITA)


def test_staff_edit_with_no_change_says_so(client):
    order = _order("ACCEPTED")
    answer = _edit(client, order, {"lines": _lines(("b_caserita", 1, [])), "fulfilment": "PICKUP",
                                   "preview": True}).json()
    assert answer["changed"] is False and _outbound() == []


def test_staff_edit_refused_after_a_payment_change(client):
    order = _order("ACCEPTED")
    body = {"lines": _lines(("b_caserita", 2, [])), "fulfilment": "PICKUP"}
    preview = _edit(client, order, {**body, "preview": True}).json()
    payments.register_cash(order["id"], 5000)
    answer = _edit(client, order, {**body, "token": preview["token"]})
    assert answer.status_code == 409 and answer.json()["error"] == changes.STATE_CHANGED


def test_staff_edit_closes_an_open_request_and_leaves_the_customers_cart(client):
    order, row = _pending_request()
    assert brain._run_tool(PHONE, "start_separate_order", {}).startswith("Listo")
    cart.add_item(PHONE, "d_gaseosa_mini", 1)
    body = {"lines": _lines(("b_caserita", 1, ["m_queso", "m_queso"])), "fulfilment": "PICKUP"}
    preview = _edit(client, order, {**body, "preview": True}).json()
    _edit(client, order, {**body, "token": preview["token"]})
    assert changes.get(row["id"])["status"] == "CLOSED"
    assert orders.get(order["id"])["total"] == CASERITA + 2 * QUESO      # charged twice
    assert [line["item_id"] for line in cart.summary(PHONE)["lines"]] == ["d_gaseosa_mini"]
    assert len(_outbound()) == 1


def test_a_split_that_no_longer_fits_asks_only_when_the_bot_can_record_it(client):
    order = _order("ACCEPTED", items=(("b_caserita", 1), ("b_madurita", 1)),
                   split=("MIXED", 20000, CASERITA + MADURITA - 20000))
    body = {"lines": _lines(("b_caserita", 1, [])), "fulfilment": "PICKUP"}
    preview = _edit(client, order, {**body, "preview": True}).json()
    assert brain.METHOD_QUESTION in preview["message"]
    _edit(client, order, {**body, "token": preview["token"]})
    assert orders.get(order["id"])["payment"]["method"] == "UNSPECIFIED"
    card = client.get("/api/board").json()["orders"][0]
    assert card["payment"]["agree"] is False


def test_a_split_that_no_longer_fits_en_camino_is_agreed_by_a_person(client):
    total = CASERITA + MADURITA + CABECERA
    order = _order("OUT_FOR_DELIVERY", "DELIVERY", items=(("b_caserita", 1), ("b_madurita", 1)),
                   split=("MIXED", 30000, total - 30000))
    body = {"lines": _lines(("b_caserita", 1, [])), "fulfilment": "DELIVERY",
            "address": "Calle 1 #2-3", "zone": "Cabecera", "driver_ok": True}
    preview = _edit(client, order, {**body, "preview": True}).json()
    assert brain.AGREE_PAYMENT in preview["message"] and brain.METHOD_QUESTION not in preview["message"]
    _edit(client, order, {**body, "token": preview["token"]})
    assert client.get("/api/board").json()["orders"][0]["payment"]["agree"] is True
    payments.register_cash(order["id"], CASERITA + CABECERA)
    assert client.get("/api/board").json()["orders"][0]["payment"]["agree"] is False


def test_a_message_too_long_is_refused_before_saving(client):
    order = _order("ACCEPTED")
    lines = [{"item_id": "b_caserita", "qty": 1, "note": f"nota {n} " + "x" * 110} for n in range(30)]
    answer = _edit(client, order, {"lines": lines, "fulfilment": "PICKUP", "preview": True})
    assert answer.status_code == 400 and answer.json()["error"] == wa.TOO_LONG
    assert orders.get(order["id"])["total"] == CASERITA


def test_the_menu_for_the_editor(client):
    menu = client.get("/api/menu").json()
    burgers = menu["categories"][0]["items"]
    assert {m["id"] for m in burgers[0]["modifiers"]} >= {"m_queso"}
    assert menu["default_fee"] == CATALOG.fulfilment["default_delivery_fee"]


def test_the_panel_has_the_editor_and_the_request_buttons():
    page = (main.STATIC / "kitchen.html").read_text(encoding="utf-8")
    for needle in ("Editar", "/edit`", "preview: true", "Aceptar cambio", "Avisos sin enviar",
                   "Ya coordiné con el domiciliario", "Ver en el mapa", "Acordar pago"):
        assert needle in page, needle


# ---- durable notices ------------------------------------------------------------------

def test_a_notice_and_its_change_land_together():
    order = _order("ACCEPTED")
    with pytest.raises(RuntimeError):
        with db.transaction():
            orders.apply_target(order["id"], _target(order, [("d_gaseosa_mini", 1, [], None)]),
                                orders.ON_BOARD, "staff")
            wa.queue(PHONE, "aviso", order["id"], "ORDER_STATE")
            raise RuntimeError("after the write")
    assert orders.get(order["id"])["total"] == CASERITA and _outbound() == []


def test_a_notice_left_queued_by_a_crash_is_sent_by_the_worker():
    order = _order("ACCEPTED")
    old = wa.queue(PHONE, "viejo", order["id"], "FIXED")
    db.run("UPDATE outbound_messages SET sent_at=%s WHERE id=%s", (worker._at(-60), old))
    fresh = wa.queue(PHONE, "nuevo", order["id"], "FIXED")
    worker.run_once()
    status = {row["id"]: row["status"] for row in _outbound()}
    assert status == {old: "FAKE", fresh: "QUEUED"}
    assert wa.send_queued(fresh) is True and wa.send_queued(fresh) is False   # sent once


def test_a_newer_order_notice_supersedes_an_unsent_one():
    order = _order("ACCEPTED")
    first = wa.queue(PHONE, "uno", order["id"], "ORDER_STATE")
    wa.queue(PHONE, "dos", order["id"], "ORDER_STATE")
    assert db.q1("SELECT status FROM outbound_messages WHERE id=%s", (first,))["status"] == "SUPERSEDED"


def test_the_automatic_notice_goes_before_the_bots_reply(monkeypatch):
    monkeypatch.setenv("MESSAGE_WAIT_SECONDS", "0")
    _order("ACCEPTED")
    brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    _model(monkeypatch, [_tool("confirm_change"), _text("¡Listo! Ya quedó.")])
    db.run("INSERT INTO inbound_messages(wa_message_id, phone, body, kind, received_at, status)"
           " VALUES('w1', %s, 'sí', 'text', %s, 'RECEIVED')", (PHONE, worker._at(-1)))
    assert worker.run_once()
    bodies = [row["body"] for row in _outbound()]
    assert bodies[0].startswith("Listo, agregamos 1× Gaseosa personal") and bodies[1] == "¡Listo! Ya quedó."


def test_failed_notices_are_listed_and_resent_also_off_the_board(client):
    order = _order("ACCEPTED")
    state = wa.queue(PHONE, "viejo estado", order["id"], "ORDER_STATE")
    fixed = wa.queue(PHONE, "plantilla", order["id"], "FIXED")
    db.run("UPDATE outbound_messages SET status='FAILED' WHERE id IN (%s,%s)", (state, fixed))
    for event in ("food_ready", "hand_over"):
        orders.apply_event(order["id"], event)                   # off the board
    listed = client.get("/api/notices/failed").json()["notices"]
    assert [(n["id"], n["code"]) for n in listed] == [(state, order["code"]), (fixed, order["code"])]
    assert _post(client, f"/api/notices/{state}/resend").json()["sent"] is True
    assert _post(client, f"/api/notices/{fixed}/resend").json()["sent"] is True
    sent = _outbound("status='FAKE'")
    assert sent[0]["body"].startswith(main.RESEND_HEAD) and sent[1]["body"] == "plantilla"
    assert client.get("/api/notices/failed").json()["notices"] == []
    assert _post(client, f"/api/notices/{state}/resend").status_code == 409


# ---- photos ---------------------------------------------------------------------------

IMAGE = wa.image_block(b"\x89PNG fake", "image/png")


def test_a_photo_that_is_not_a_receipt_makes_no_row(monkeypatch):
    seen = _model(monkeypatch)
    monkeypatch.setattr(brain, "extract_proof", lambda image: {"is_receipt": False})
    _order("ACCEPTED")
    main.handle(PHONE, "¿tienen esta?", IMAGE)
    assert db.q("SELECT * FROM payment_receipts") == []
    note = seen.calls[-1][-1]["content"][1]["text"]
    assert "no es un comprobante" in note and "«¿tienen esta?»" in note


@pytest.mark.parametrize("reading", [{"is_receipt": True}, {"is_receipt": None}, {}])
def test_a_receipt_or_an_unsure_reading_is_checked(monkeypatch, reading):
    seen = _model(monkeypatch)
    monkeypatch.setattr(brain, "extract_proof", lambda image: {**reading, "amount": 18900})
    _order("ACCEPTED")
    main.handle(PHONE, "¿a qué hora llega?", IMAGE)
    assert len(db.q("SELECT * FROM payment_receipts")) == 1
    assert "también escribió con la foto: «¿a qué hora llega?»" in seen.calls[-1][-1]["content"]


def test_an_odd_reading_is_still_a_dict(monkeypatch):
    _model(monkeypatch, [_text("[1, 2]")])
    assert brain.extract_proof(IMAGE)["amount"] is None


def test_a_person_on_the_chat_gets_no_row_for_a_non_receipt(monkeypatch):
    _order("ACCEPTED")
    monkeypatch.setattr(worker, "_download", lambda row: (b"x", "image/png"))
    monkeypatch.setattr(brain, "extract_proof", lambda image: {"is_receipt": False})
    notes = []
    monkeypatch.setattr(worker, "_private_note", lambda *args: notes.append(args))
    worker._receipt_for_staff(PHONE, {"kind": "image", "media_url": "x", "received_at": db.now(),
                                      "conversation_id": None})
    assert db.q("SELECT * FROM payment_receipts") == [] and notes == []


# ---- stickers -------------------------------------------------------------------------

def test_a_sticker_is_a_step_of_its_own():
    rows = [{"kind": "text"}, {"kind": "sticker"}, {"kind": "text"}]
    assert [kind for kind, _ in worker._steps(rows)] == ["text", "image", "text"]


def test_a_sticker_with_no_order_is_seen_without_a_receipt_check(monkeypatch):
    seen = _model(monkeypatch)
    monkeypatch.setattr(brain, "extract_proof", lambda image: pytest.fail("no OCR for a sticker"))
    main.handle(PHONE, None, IMAGE, sticker=True)
    assert seen.calls[-1][-1]["content"][1]["text"] == main.STICKER_NOTE


def test_a_webp_receipt_is_checked(monkeypatch):
    _model(monkeypatch)
    monkeypatch.setattr(brain, "extract_proof", lambda image: {"is_receipt": True, "amount": 18900})
    _order("ACCEPTED")
    main.handle(PHONE, None, wa.image_block(b"RIFF", "image/webp"), sticker=True)
    assert len(db.q("SELECT * FROM payment_receipts")) == 1


def test_a_sticker_the_api_refuses_gets_the_old_answer(monkeypatch):
    seen = types.SimpleNamespace(calls=[])

    class Messages:
        def create(self, **kw):
            seen.calls.append(kw["messages"][-1]["content"])
            if isinstance(kw["messages"][-1]["content"], list):
                raise RuntimeError("animated webp")
            return _text("¿Qué quiso decir?")

    monkeypatch.setattr(brain, "_model", "test-model")
    monkeypatch.setattr(brain, "client", lambda: types.SimpleNamespace(messages=Messages()))
    assert main.handle(PHONE, None, IMAGE, sticker=True) == "¿Qué quiso decir?"
    assert "sticker" in seen.calls[-1] and "nunca hagas como si" in seen.calls[-1]


# ---- location -------------------------------------------------------------------------

PIN = {"latitude": 8.2345, "longitude": -73.35, "name": "Casa"}


def test_a_pin_goes_with_the_cart_and_never_to_a_later_order():
    assert orders.attach_location(PHONE, PIN) == "cart"           # no cart yet: a new one
    first = _order("COMPLETED", "DELIVERY")
    assert first["location"]["latitude"] == pytest.approx(8.2345)
    later = _order("ACCEPTED", "DELIVERY")
    assert later["location"] is None


def test_a_pin_after_confirming_goes_to_the_live_order():
    order = _order("CONFIRMED", "DELIVERY")
    assert orders.attach_location(PHONE, PIN) == "order"
    assert orders.get(order["id"])["location"]["name"] == "Casa"


def test_bad_coordinates_are_ignored():
    for pin in ({"latitude": 91, "longitude": 0}, {"latitude": "nan", "longitude": 1}, {"name": "x"}, None):
        assert orders.attach_location(PHONE, pin) is None


def test_a_pin_is_kept_while_a_person_has_the_chat(monkeypatch):
    monkeypatch.setenv("MESSAGE_WAIT_SECONDS", "0")
    monkeypatch.setattr(chatwoot, "bot_holds", lambda phone: False)
    order = _order("ACCEPTED", "DELIVERY")
    db.run("INSERT INTO inbound_messages(wa_message_id, phone, kind, received_at, status, location)"
           " VALUES('p1', %s, 'location', %s, 'RECEIVED', %s)", (PHONE, worker._at(-1), json.dumps(PIN)))
    assert worker.run_once()
    assert orders.get(order["id"])["location"]["latitude"] == pytest.approx(8.2345)


def test_the_map_link_is_for_delivery_only(client):
    orders.attach_location(PHONE, PIN)
    _order("ACCEPTED", "DELIVERY")
    orders.attach_location(PHONE, PIN)
    cart.add_item(PHONE, "b_caserita", 1)
    pickup = orders.create_from_cart(PHONE, "PICKUP")
    links = {o["id"]: o["map_url"] for o in client.get("/api/board").json()["orders"]}
    assert links[pickup["id"]] is None
    assert [url for url in links.values() if url] == [
        "https://www.google.com/maps/search/?api=1&query=8.2345,-73.35"]


def test_the_bot_never_gets_the_coordinates():
    note = main._note_for({"kind": "location", "location": {**PIN, "url": "https://maps"}}, False)
    assert "8.2345" not in note and "https" not in note and "barrio" in note


# ---- files ----------------------------------------------------------------------------

def test_a_file_with_a_live_order_asks_for_a_screenshot():
    for kind in ("document", "video"):
        assert "captura de pantalla" in main._note_for({"kind": kind}, False, has_order=True)
        assert "solo puedes leer mensajes de texto y fotos" in main._note_for({"kind": kind}, False)
    assert "captura" not in main._note_for({"kind": "contacts"}, False, has_order=True)


# ---- voice notes (Task E addendum) ------------------------------------------------------

def test_what_the_transcriber_heard_is_judged_by_code():
    word = {"type": "word", "text": "caserita"}
    assert transcribe.heard_from({"text": "una caserita", "words": [word],
                                  "language_probability": 0.97}).clear
    for answer in ({"text": "", "words": []},
                   {"text": "(música)", "words": [{"type": "audio_event", "text": "(música)"}]},
                   {"text": "una caserita", "words": [word], "language_probability": 0.2}):
        heard = transcribe.heard_from(answer)
        assert (heard.clear, heard.reason) == (False, "unclear")


def test_the_transcriber_gets_the_menu_and_spanish(monkeypatch):
    sent = {}

    def post(url, **kw):
        sent.update(kw, url=url)
        return types.SimpleNamespace(status_code=200, text="", json=lambda: {
            "text": "Quiero una caserita madurita", "language_probability": 0.99,
            "words": [{"type": "word", "text": "Quiero"}]})

    monkeypatch.setenv("ELEVENLABS_API_KEY", "clave-de-prueba")
    monkeypatch.setattr(transcribe.httpx, "post", post)
    heard = transcribe.transcribe(b"OggS...", "audio/ogg")
    assert heard.clear and heard.text == "Quiero una caserita madurita"
    assert sent["url"] == transcribe.URL and sent["headers"] == {"xi-api-key": "clave-de-prueba"}
    assert (sent["data"]["model_id"], sent["data"]["language_code"]) == ("scribe_v2", "spa")
    assert {"Caserita Madurita", "Queso", "Cabecera"} <= set(sent["data"]["keyterms"])
    assert sent["files"]["file"][2] == "audio/ogg"


def test_a_note_that_cannot_be_sent_is_never_a_guess(monkeypatch):
    monkeypatch.setattr(transcribe.httpx, "post", lambda *a, **k: pytest.fail("no call"))
    assert transcribe.transcribe(b"x", "audio/ogg").reason == "no_key"
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("VOICE_MAX_MB", "0.1")
    assert transcribe.transcribe(b"x" * 200_000, "audio/ogg").reason == "too_big"

    def broken(*a, **k):
        raise transcribe.httpx.ConnectError("caído")

    monkeypatch.setattr(transcribe.httpx, "post", broken)
    assert transcribe.transcribe(b"x", "audio/ogg").reason == "error"


def _voice_row(wa_id="v1", body=None, kind="audio", received=None):
    db.run("INSERT INTO inbound_messages(wa_message_id, phone, body, kind, received_at, status,"
           " media_url) VALUES(%s,%s,%s,%s,%s,'RECEIVED',%s)",
           (wa_id, PHONE, body, kind, received or worker._at(-1), "chatwoot://nota"))


def _hearing(monkeypatch, heard):
    monkeypatch.setenv("MESSAGE_WAIT_SECONDS", "0")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setattr(worker, "_download", lambda row: (b"OggS", "audio/ogg"))
    monkeypatch.setattr(transcribe, "transcribe", lambda raw, mime: heard)


def test_a_clear_voice_note_is_the_customers_words(monkeypatch):
    seen = _model(monkeypatch)
    _hearing(monkeypatch, transcribe.Heard("Quiero una caserita para recoger", True))
    _voice_row("t0", body="hola", kind="text", received=worker._at(-2))
    _voice_row("v1")
    assert worker.run_once()
    assert len(seen.calls) == 1                       # «hola» and the note: one turn
    content = seen.calls[0][-1]["content"]
    assert "hola" in content and "Nota de voz del cliente" in content
    assert "«Quiero una caserita para recoger»" in content
    assert db.q1("SELECT transcript FROM inbound_messages WHERE wa_message_id='v1'")["transcript"] \
        == "Quiero una caserita para recoger"


def test_an_unclear_voice_note_asks_to_repeat(monkeypatch):
    seen = _model(monkeypatch)
    _hearing(monkeypatch, transcribe.Heard("", False, "unclear"))
    _voice_row()
    assert worker.run_once()
    assert seen.calls[0][-1]["content"] == main.VOICE_NOTES["unclear"]


def test_without_a_key_the_bot_asks_to_write(monkeypatch):
    seen = _model(monkeypatch)
    monkeypatch.setenv("MESSAGE_WAIT_SECONDS", "0")
    monkeypatch.setattr(transcribe, "transcribe", lambda *a: pytest.fail("no transcription"))
    _voice_row()
    assert worker.run_once()
    content = seen.calls[0][-1]["content"]
    assert "nota de voz" in content and "escuchar" in content


def test_no_transcription_while_a_person_has_the_chat(monkeypatch):
    _hearing(monkeypatch, None)
    monkeypatch.setattr(transcribe, "transcribe", lambda *a: pytest.fail("no transcription"))
    monkeypatch.setattr(chatwoot, "bot_holds", lambda phone: False)
    _voice_row()
    assert worker.run_once()
    assert db.q1("SELECT status FROM inbound_messages WHERE wa_message_id='v1'")["status"] == "HUMAN"


def test_voice_notes_keep_their_file():
    from tests.test_chatwoot import fixture
    assert chatwoot.parse_customer(fixture("incoming_audio"))["media_url"].endswith(".ogg")
    [item] = wa.parse_webhook({"entry": [{"changes": [{"value": {"messages": [
        {"from": PHONE, "id": "wamid.a", "type": "audio", "audio": {"id": "m-audio"}}]}}]}]})
    assert item["media_id"] == "m-audio"


# ---- sticker intent (Task E addendum) -------------------------------------------------

def _shown(text="¿Me confirma el pedido así?"):
    wa.send_text(PHONE, text)                          # FAKE: the question went out


def _sticker_says(monkeypatch, meaning):
    _model(monkeypatch)
    monkeypatch.setattr(brain, "extract_proof", lambda image: {"is_receipt": False})
    read = []
    monkeypatch.setattr(brain, "read_sticker",
                        lambda image, question: read.append(question) or meaning)
    return read


def test_a_thumbs_up_confirms_the_summary_it_answers(monkeypatch):
    _sticker_says(monkeypatch, "AFFIRM")
    order = _order("PENDING_CONFIRMATION")
    _shown()
    main.handle(PHONE, None, IMAGE, received_at=db.now(), sticker=True)
    assert orders.get(order["id"])["state"] == "CONFIRMED"


def test_a_no_sticker_never_cancels_an_order(monkeypatch):
    _sticker_says(monkeypatch, "DENY")
    order = _order("PENDING_CONFIRMATION")
    _shown()
    main.handle(PHONE, None, IMAGE, received_at=db.now(), sticker=True)
    assert orders.get(order["id"])["state"] == "PENDING_CONFIRMATION"


def test_a_thumbs_up_adds_an_automatic_proposal_once(monkeypatch):
    _sticker_says(monkeypatch, "AFFIRM")
    order = _order("ACCEPTED")
    brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    _shown("¿Lo agregamos?")
    main.handle(PHONE, None, IMAGE, received_at=db.now(), sticker=True)
    assert orders.get(order["id"])["total"] == CASERITA + GASEOSA
    assert _request()["status"] == "APPLIED"
    assert _outbound("status='QUEUED'")[0]["body"].startswith("Listo, agregamos 1× Gaseosa personal")
    main.handle(PHONE, None, IMAGE, received_at=db.now(), sticker=True)       # nothing pending now
    assert orders.get(order["id"])["total"] == CASERITA + GASEOSA


def test_a_thumbs_up_sends_a_request_to_the_restaurant(monkeypatch):
    _sticker_says(monkeypatch, "AFFIRM")
    order = _order("ACCEPTED")
    brain._run_tool(PHONE, "add_item", {"item_id": "b_madurita"})
    _shown("¿Se lo envío al local?")
    main.handle(PHONE, None, IMAGE, received_at=db.now(), sticker=True)
    assert _request()["status"] == "PENDING" and orders.get(order["id"])["total"] == CASERITA


def test_a_no_sticker_discards_a_proposal(monkeypatch):
    _sticker_says(monkeypatch, "DENY")
    order = _order("ACCEPTED")
    brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    _shown("¿Lo agregamos?")
    main.handle(PHONE, None, IMAGE, received_at=db.now(), sticker=True)
    assert _request()["status"] == "DISCARDED" and orders.get(order["id"])["total"] == CASERITA


def test_a_sticker_acts_only_on_a_question_already_shown(monkeypatch):
    read = _sticker_says(monkeypatch, "AFFIRM")
    order = _order("PENDING_CONFIRMATION")
    early = db.now()
    _shown()
    main.handle(PHONE, None, IMAGE, received_at=early, sticker=True)         # before it went out
    assert orders.get(order["id"])["state"] == "PENDING_CONFIRMATION" and read == []


def test_two_pending_questions_mean_no_action(monkeypatch):
    read = _sticker_says(monkeypatch, "AFFIRM")
    _order("ACCEPTED")
    brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    assert brain._run_tool(PHONE, "start_separate_order", {}).startswith("Listo")
    cart.add_item(PHONE, "b_caserita", 1)
    second = orders.create_from_cart(PHONE, "PICKUP")
    _shown()
    main.handle(PHONE, None, IMAGE, received_at=db.now(), sticker=True)
    assert read == [] and orders.get(second["id"])["state"] == "PENDING_CONFIRMATION"
    assert _request()["status"] == "PROPOSED"


def test_a_thanks_sticker_changes_nothing(monkeypatch):
    _sticker_says(monkeypatch, "THANKS")
    order = _order("PENDING_CONFIRMATION")
    _shown()
    main.handle(PHONE, None, IMAGE, received_at=db.now(), sticker=True)
    assert orders.get(order["id"])["state"] == "PENDING_CONFIRMATION"


def test_an_odd_sticker_reading_is_unclear(monkeypatch):
    _model(monkeypatch, [_text('{"meaning": "MAYBE"}'), _text("no es json")])
    question = {"question": "¿Lo agregamos?"}
    assert brain.read_sticker(IMAGE, question) == "UNCLEAR"
    assert brain.read_sticker(IMAGE, question) == "UNCLEAR"
