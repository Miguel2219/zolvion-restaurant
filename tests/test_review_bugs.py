"""Task C2 Part 1: one test per defect of the 1 Oct review (docs/CURRENT-STATE.md §3).
Each starts as a strict xfail that shows the bug; the mark goes when the bug is fixed."""
import json
import os
import re
import tempfile
import types
import uuid
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("DEMO_DB", os.path.join(tempfile.mkdtemp(), "review.sqlite3"))

import pytest  # noqa: E402

from app import brain, cart, db, main, orders, payments, wa  # noqa: E402
from app.catalog import CATALOG  # noqa: E402
from app.money import MoneyError, fmt, pesos  # noqa: E402

REAL_DB = Path(__file__).resolve().parent.parent / "demo.sqlite3"
if Path(db.DB_PATH).resolve() == REAL_DB.resolve():
    pytest.exit("DEMO_DB points at the real demo.sqlite3; refusing to reset it", returncode=2)

PHONE = "573001112233"
CASERITA = CATALOG.price_of("b_caserita")          # 18900
ESPECIAL = CATALOG.price_of("b_especial")          # 29900
GASEOSA = CATALOG.price_of("d_gaseosa_personal")   # 3500
QUESO = CATALOG.modifier_price("m_queso")          # 2000
ACCOUNT = CATALOG.accounts()[0]["number"]
KITCHEN_HTML = Path(__file__).resolve().parent.parent / "static" / "kitchen.html"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(wa, "live", lambda: False)      # no real WhatsApp message, ever
    db.init()
    db.reset()
    brain.forget(PHONE)
    yield
    brain.forget(PHONE)


# ---- helpers (small copies of test_flow's, so the two files stay independent) ----

def _iso(minutes_ago: float = 0) -> str:
    now = datetime.now(CATALOG.timezone())
    return (now - timedelta(minutes=minutes_ago)).isoformat()


def _confirmed_order():
    cart.add_item(PHONE, "b_especial", 1)
    order = orders.create_from_cart(PHONE, "PICKUP")
    return orders.apply_event(order["id"], "customer_confirms")


def _transfer_order():
    order = _confirmed_order()                          # total = ESPECIAL
    payments.set_method(order["id"], "TRANSFER")
    return order


def _receipt(order_id, amount, reference, minutes_ago=0, **fields):
    """A receipt to the restaurant's account, dated now; the amount decides the rest."""
    return payments.check_proof(order_id, {
        "amount": amount, "destination_account": ACCOUNT, "reference": reference,
        "date_iso": _iso(minutes_ago), **fields,
    })


def _client(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(wa, "live", lambda: False)
    return TestClient(main.app)


def _last_outbound() -> str | None:
    row = db.q1(
        "SELECT body FROM outbound_messages WHERE phone=? ORDER BY id DESC LIMIT 1", (PHONE,)
    )
    return row["body"] if row else None


def _real_state_block(prompt: str) -> str:
    return prompt.split("ESTADO REAL AHORA", 1)[1]


def _payment_line(prompt: str) -> str:
    return next(
        line for line in _real_state_block(prompt).splitlines()
        if line.startswith("- Pago del pedido más reciente:")
    )


def _text(t):
    return types.SimpleNamespace(
        stop_reason="end_turn", content=[types.SimpleNamespace(type="text", text=t)]
    )


def _fake_model(monkeypatch, answer):
    """Replaces the Anthropic client. `answer(kw)` returns the response; `seen` records
    the last message's content and the tool_choice of every call. Like the real API, an
    empty user text is refused."""
    seen = types.SimpleNamespace(contents=[], choices=[])

    class Messages:
        def create(self, **kw):
            content = kw["messages"][-1]["content"]
            seen.contents.append(content)
            seen.choices.append(kw.get("tool_choice"))
            if content == "":
                raise RuntimeError("text content blocks must be non-empty")
            return answer(kw)

    monkeypatch.setattr(brain, "_model", "test-model")
    monkeypatch.setattr(brain, "client", lambda: types.SimpleNamespace(messages=Messages()))
    return seen


def _webhook(monkeypatch, message):
    """POST one WhatsApp message, in Meta's envelope, to /webhook."""
    client = _client(monkeypatch)
    monkeypatch.setattr(wa, "check_signature", lambda raw, header: True)
    payload = {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": PHONE, "profile": {"name": "Ana"}}],
        "messages": [{"from": PHONE, "id": f"wamid.{uuid.uuid4().hex}", **message}],
    }}]}]}
    return client.post("/webhook", json=payload)


def _count(table: str) -> int:
    return db.q1(f"SELECT COUNT(*) AS n FROM {table}")["n"]


def _confirm_url(order_id, receipt_id) -> str:
    return f"/api/orders/{order_id}/receipts/{receipt_id}/confirm"


# ---- R1, R2: cash counts toward what is still owed by transfer ----------------

def test_r1_transfer_after_full_cash_is_red():
    order = _confirmed_order()
    payments.register_cash(order["id"], ESPECIAL)       # method -> CASH
    record = _receipt(order["id"], ESPECIAL, "R1-1")
    assert record["status"] == "FLAGGED"
    assert "No se esperaba otro pago." in record["receipt"]["flags"]
    assert record["inferred"] is False
    assert record["method"] == "CASH"
    assert record["overpayment_amount"] == 0


def test_r1_full_transfer_after_partial_cash_is_red():
    order = _confirmed_order()
    payments.register_cash(order["id"], 10000)          # method -> CASH, 10.000 paid
    record = _receipt(order["id"], ESPECIAL, "R1-2")
    assert record["status"] == "FLAGGED"
    assert (
        f"El comprobante dice {fmt(ESPECIAL)} y faltaban {fmt(ESPECIAL - 10000)}."
        in record["receipt"]["flags"]
    )
    assert record["inferred"] is False
    assert record["method"] == "CASH"
    assert record["overpayment_amount"] == 0


def test_r2a_cash_on_a_transfer_order_leaves_nothing_to_transfer():
    order = _transfer_order()
    record = payments.register_cash(order["id"], ESPECIAL)
    assert (record["transfer_due"], record["cash_due"]) == (0, 0)
    prompt = brain.system_prompt(PHONE)
    assert _payment_line(prompt) == (
        "- Pago del pedido más reciente: había elegido transferencia, pero el local ya recibió "
        "el pago completo en efectivo; no hace falta que transfiera ni que envíe comprobante; "
        "ya está pagado completo.")
    assert "nunca le pidas que transfiera ni que envíe un comprobante" in _real_state_block(prompt)
    assert "(pay.transfer_due || 0) > 0" in KITCHEN_HTML.read_text(encoding="utf-8")


CASH_PART = "el local ya recibió una parte en efectivo; no ha enviado ningún comprobante; "
PARTIAL_CASH = {
    "CASH": "en efectivo al recibir; " + CASH_PART + "queda por pagar en efectivo al recibir; todavía falta por pagar.",
    "TRANSFER": "por transferencia; " + CASH_PART + "todavía falta por transferir; todavía falta por pagar.",
    "MIXED": "parte por transferencia y parte en efectivo; " + CASH_PART + "todavía falta por transferir; "
             "queda por pagar en efectivo al recibir; todavía falta por pagar.",
}


@pytest.mark.parametrize("method", list(PARTIAL_CASH))
def test_r2_r7_partial_cash_is_named_in_the_real_state(method):
    order = _confirmed_order()
    if method != "CASH":                                # with no method, cash settles it as CASH
        payments.set_method(order["id"], method, 10000 if method == "MIXED" else None)
    payments.register_cash(order["id"], 5000)
    assert _payment_line(brain.system_prompt(PHONE)) == (
        "- Pago del pedido más reciente: " + PARTIAL_CASH[method])


def test_r2a_cash_paid_all_still_shows_a_red_or_discarded_receipt():
    head = "- Pago del pedido más reciente: el local ya recibió el pago completo en efectivo; "
    order = _confirmed_order()
    payments.register_cash(order["id"], ESPECIAL)
    red = _receipt(order["id"], ESPECIAL, "R2A-RED")["receipt"]     # "No se esperaba otro pago."
    assert _payment_line(brain.system_prompt(PHONE)) == head + (
        "comprobantes: 1 con un problema que revisa una persona del local; hay un comprobante "
        "en revisión: no le pidas que pague otra vez; ya está pagado completo.")
    payments.discard_receipt(order["id"], red["id"], "Llegó por error")
    assert _payment_line(brain.system_prompt(PHONE)) == head + (
        "comprobantes: 1 descartado por el local; ya está pagado completo.")


def test_r2a_cash_paid_all_with_a_receipt_still_waiting():
    order = _transfer_order()
    _receipt(order["id"], ESPECIAL, "R2A-WAIT")                     # clean, nobody confirmed it
    payments.register_cash(order["id"], ESPECIAL)
    line = _payment_line(brain.system_prompt(PHONE))
    assert line == (
        "- Pago del pedido más reciente: había elegido transferencia, pero el local ya recibió el "
        "pago completo en efectivo; comprobantes: 1 recibido que revisa una persona del local; "
        "ya está pagado completo.")
    assert "confirmado" not in line


def test_r2b_transfer_for_the_rest_after_cash_is_clean():
    order = _transfer_order()
    payments.register_cash(order["id"], 10000)
    record = _receipt(order["id"], ESPECIAL - 10000, "R2B-1")
    assert record["status"] == "PROOF_RECEIVED"
    assert record["receipt"]["flags"] == []
    assert record["transfer_due"] == 0


# ---- R3, R3b: the "Confirmamos su transferencia" message ----------------------

def test_r3_partial_confirm_on_a_transfer_order(monkeypatch):
    client = _client(monkeypatch)
    order = _transfer_order()
    receipt = _receipt(order["id"], 10000, "R3-1")["receipt"]       # red: ESPECIAL was expected
    response = client.post(_confirm_url(order["id"], receipt["id"]), json={"amount": 10000})
    assert response.status_code == 200
    assert _last_outbound() == (
        f"Confirmamos su transferencia de {fmt(10000)}. Quedan {fmt(ESPECIAL - 10000)} por "
        "transferir. Cuando haga la transferencia, envíenos el comprobante."
    )


def test_r3b_confirm_message_ignores_unconfirmed_surplus(monkeypatch):
    client = _client(monkeypatch)
    order = _confirmed_order()
    first = _receipt(order["id"], 10000, "R3B-A")["receipt"]        # infers MIXED
    _receipt(order["id"], ESPECIAL - 10000, "R3B-B")                # clean (Q3)
    payments.register_cash(order["id"], 5000)
    response = client.post(_confirm_url(order["id"], first["id"]), json={"amount": 10000})
    assert response.status_code == 200
    assert _last_outbound() == (
        f"Confirmamos su transferencia de {fmt(10000)}. "
        "El otro comprobante que envió sigue en revisión."
    )
    assert "de más" not in _last_outbound()


# ---- R4: an edit must not lose a modifier added twice -------------------------

def test_r4_edit_keeps_a_modifier_added_twice():
    cart.add_item(PHONE, "b_caserita", 1, ["m_queso", "m_queso"])
    order = orders.create_from_cart(PHONE, "PICKUP")
    orders.apply_event(order["id"], "customer_confirms")
    brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    updated = orders.get(order["id"])
    line = next(i for i in updated["items"] if i["item_id"] == "b_caserita")
    assert line["modifiers"] == ["Queso", "Queso"]
    assert line["line_total"] == CASERITA + 2 * QUESO
    assert updated["total"] == CASERITA + 2 * QUESO + GASEOSA


# ---- R5: messages that are not text -------------------------------------------

R5_CASES = {
    "audio": (
        {"type": "audio", "audio": {"id": "a1", "voice": True}},
        ["nota de voz", "nunca hagas como si"],
    ),
    "sticker": (
        {"type": "sticker", "sticker": {"id": "s1"}},
        ["sticker", "nunca hagas como si"],
    ),
    "location_with_address": (
        {"type": "location", "location": {
            "latitude": 7.1193, "longitude": -73.1227, "name": "Edificio Torre 48",
            "address": "Calle 48 #34-20, Cabecera",
        }},
        ["Edificio Torre 48", "Calle 48 #34-20, Cabecera", "7.1193", "-73.1227", "barrio"],
    ),
    "location_without_address": (
        {"type": "location", "location": {"latitude": 7.1193, "longitude": -73.1227}},
        ["dirección escrita", "7.1193"],
    ),
    "image_failed": (
        {"type": "image", "image": {"id": "m1", "caption": "mi comprobante"}},
        ["otra vez", "mi comprobante"],
    ),
}


@pytest.mark.parametrize("message,fragments", list(R5_CASES.values()), ids=list(R5_CASES))
def test_r5_non_text_message_reaches_the_bot_as_a_note(monkeypatch, message, fragments):
    seen = _fake_model(monkeypatch, lambda kw: _text("Listo."))
    response = _webhook(monkeypatch, message)
    assert response.status_code == 200
    assert _last_outbound() == "Listo."                 # not "Perdón, se me cruzaron los cables…"
    for fragment in fragments:
        assert fragment in seen.contents[-1]


def test_r5_reaction_gets_no_reply(monkeypatch):
    seen = _fake_model(monkeypatch, lambda kw: _text("Listo."))
    response = _webhook(monkeypatch, {
        "type": "reaction", "reaction": {"message_id": "wamid.x", "emoji": "👍"},
    })
    assert response.status_code == 200
    assert seen.contents == []                          # the model was never called
    assert _count("outbound_messages") == 0
    assert _count("inbound_messages") == 1


# ---- R6: every error staff can read is Spanish --------------------------------

ENGLISH = re.compile(
    r"\b(not|is|are|allowed|money|legal|from|only|valid|exist|terminal)\b", re.IGNORECASE
)


@pytest.mark.parametrize("case", ["cash_float", "cash_text", "event_not_legal"])
def test_r6_errors_reach_staff_in_spanish(monkeypatch, case):
    client = _client(monkeypatch)
    order = _confirmed_order()
    cash = f"/api/orders/{order['id']}/payment/cash"
    if case == "cash_float":
        response = client.post(cash, json={"amount": 26000.5})
    elif case == "cash_text":
        response = client.post(cash, json={"amount": "abc"})
    else:
        response = client.post(f"/api/orders/{order['id']}/event", json={"event": "food_ready"})
        assert response.status_code == 409
    error = response.json()["error"]
    assert error.strip()
    assert not ENGLISH.search(error), error


# ---- R7: an edit after cash must not depend on the config listing CASH --------

def test_r7_edit_after_cash_without_cash_in_config(monkeypatch):
    monkeypatch.setitem(CATALOG.payments, "methods", ["TRANSFER", "MIXED"])
    order = _confirmed_order()
    payments.register_cash(order["id"], ESPECIAL)
    result = brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    assert not result.startswith("NO SE PUDO")
    payment = orders.get(order["id"])["payment"]
    assert payment["total"] == ESPECIAL + GASEOSA
    assert payment["method"] == "CASH"
    assert payment["cash_amount"] == ESPECIAL + GASEOSA
    assert payment["transfer_due"] == 0
    # The code-built sentence (C2 Part 2): both figures, no transfer, no receipt.
    assert (
        f"«Ya pagó {fmt(ESPECIAL)} en efectivo. Quedan {fmt(GASEOSA)} por pagar en efectivo "
        "al recibir.»" in result
    )
    assert "comprobante" not in result.split("«", 1)[1]
    assert "el local ya recibió una parte en efectivo" in _payment_line(brain.system_prompt(PHONE))


def test_r7_g29_edit_names_the_transfer_still_owed():
    """G29 (C2 Part 2): after an edit on a mixed order with cash registered, the sentence
    names the cash paid, the transfer still owed and the cash still owed."""
    order = _confirmed_order()
    payments.set_method(order["id"], "MIXED", transfer_amount=10000)
    payments.register_cash(order["id"], 5000)
    result = brain._run_tool(PHONE, "add_item", {"item_id": "d_gaseosa_personal"})
    pay = orders.get(order["id"])["payment"]
    due = ESPECIAL + GASEOSA - 15000
    assert (pay["total"], pay["cash_received"], pay["transfer_due"], pay["cash_due"]) == (
        ESPECIAL + GASEOSA, 5000, 10000, due)
    account = CATALOG.accounts()[0]
    assert (
        f"«Ya pagó {fmt(5000)} en efectivo. Quedan {fmt(10000)} por transferir y {fmt(due)} "
        f"por pagar en efectivo al recibir. Puede transferir a {account['provider']} "
        f"{account['number']} a nombre de {account['holder']}. Cuando transfiera, envíenos "
        "el comprobante.»" in result
    )


# ---- R8: an order is created whole or not at all ------------------------------

def _order_rows() -> tuple[int, int, int]:
    return _count("orders"), _count("order_items"), _count("order_transitions")


def test_r8_order_creation_is_all_or_nothing(monkeypatch):
    cart.add_item(PHONE, "b_especial", 1)
    real_run = db.run

    def failing_run(sql, args=()):
        if "INSERT INTO payments" in sql:
            raise RuntimeError("disk full")
        return real_run(sql, args)

    with monkeypatch.context() as patch:
        patch.setattr(db, "run", failing_run)
        with pytest.raises(RuntimeError):
            orders.create_from_cart(PHONE, "PICKUP")
    assert _order_rows() == (0, 0, 0)
    assert cart.peek_open_cart(PHONE) is not None       # the cart is still the customer's


def test_r8_total_is_checked_before_writing(monkeypatch):
    cart.add_item(PHONE, "b_especial", 1)
    monkeypatch.setattr(orders, "total_of", lambda lines, fee=0: -1)
    with pytest.raises(ValueError):
        orders.create_from_cart(PHONE, "PICKUP")
    assert _count("orders") == 0


# ---- R9: the customer never reads the order code or "cocina" ------------------

@pytest.mark.parametrize("case", ["chat", "reject"])
def test_r9_replies_never_carry_the_code_or_cocina(monkeypatch, case):
    order = _confirmed_order()
    if case == "chat":
        client = _client(monkeypatch)
        _fake_model(
            monkeypatch, lambda kw: _text(f"Su pedido {order['code']} ya está en la cocina.")
        )
        response = client.post("/sim/message", json={"phone": PHONE, "text": "¿cómo va?"})
        assert response.status_code == 200
        expected = "Su pedido ya está en el local."
        assert response.json()["reply"] == expected
        assert _last_outbound() == expected
        assert brain._history[PHONE][-1]["content"][0]["text"] == expected
    else:
        _fake_model(
            monkeypatch,
            lambda kw: _text(f"Lo sentimos, la cocina no pudo con su pedido {order['code']}."),
        )
        rejected = orders.apply_event(order["id"], "staff_rejects", "se acabó el pan")
        main.notify(rejected, "se acabó el pan")
        sent = [r["body"] for r in db.q("SELECT body FROM outbound_messages ORDER BY id")]
        assert sent == ["Lo sentimos, el local no pudo con su pedido."]


CLEANING_PAIRS = [
    ("Su pedido P-4438 ya está confirmado.", "Su pedido ya está confirmado."),
    ("Su pedido (P-4438) va en camino.", "Su pedido va en camino."),
    ("Ya está en la cocina.", "Ya está en el local."),
    ("Le paso la nota a la cocina.", "Le paso la nota al local."),
    ("Es una indicación de la cocina.", "Es una indicación del local."),
    ("Anoté la nota para la cocina.", "Anoté la nota para el local."),
    ("Cocina ya tiene su pedido.", "El local ya tiene su pedido."),
    ("La cocina ya lo está preparando.", "El local ya lo está preparando."),
    ("A la cocina le llegó su pedido.", "Al local le llegó su pedido."),
    ("Le aviso a cocina.", "Le aviso al local."),
    ("Desde nuestra cocina le pedimos disculpas.", "Desde nuestro local le pedimos disculpas."),
    ("Lo estamos cocinando.", "Lo estamos cocinando."),
    ("Su pedido #P-4438, listo.", "Su pedido, listo."),
    ("(su pedido P-4438)", "(su pedido)"),
    ("P-4438 ya está listo.", "Ya está listo."),
]


@pytest.mark.parametrize("before,after", CLEANING_PAIRS)
def test_r9_cleaning_is_grammatical(before, after):
    assert main._clean_for_customer(before) == after


def test_r9_tool_descriptions_never_say_cocina():
    assert "cocina" not in json.dumps(brain.TOOLS, ensure_ascii=False).lower()


# ---- R10: the instruction for a rejected or cancelled order -------------------

def test_r10_stop_instruction_says_who_did_what(monkeypatch):
    told = []
    monkeypatch.setattr(brain, "reply", lambda phone, message, **kw: told.append(message) or "ok")

    rejected = _confirmed_order()
    rejected = orders.apply_event(rejected["id"], "staff_rejects", "se acabó el pan")
    main._stop_message(rejected, "se acabó el pan")
    assert "El local rechazó el pedido del cliente antes de aceptarlo" in told[-1]
    assert "rechazado el pedido" not in told[-1]

    cart.add_item(PHONE, "b_especial", 1)
    other = orders.create_from_cart(PHONE, "PICKUP")
    for event in ("customer_confirms", "staff_accepts"):
        other = orders.apply_event(other["id"], event)
    cancelled = orders.apply_event(other["id"], "staff_cancels", "se dañó la plancha")
    main._stop_message(cancelled, "se dañó la plancha")
    assert "El local canceló el pedido del cliente después de haberlo aceptado" in told[-1]
    assert "cancelado por el local el pedido" not in told[-1]


# ---- R11: after the last tool round the customer gets only an informed answer --

def _tool_loop_model(monkeypatch, final):
    """A model that always wants another tool (with a guess written before it), until
    it is called with tool_choice none: then it answers `final`."""
    calls = []

    def answer(kw):
        calls.append(kw.get("tool_choice"))
        if kw.get("tool_choice") == {"type": "none"}:
            return final
        return types.SimpleNamespace(stop_reason="tool_use", content=[
            types.SimpleNamespace(type="text", text="GUESS"),
            types.SimpleNamespace(type="tool_use", id=f"t{len(calls)}", name="show_cart", input={}),
        ])

    return _fake_model(monkeypatch, answer)


def test_r11_text_before_tools_never_reaches_the_customer(monkeypatch):
    phone = "573000000011"
    seen = _tool_loop_model(monkeypatch, _text("FINAL"))
    brain.forget(phone)
    try:
        assert brain.reply(phone, "hola") == "FINAL"
        assert len(seen.choices) == 9
        assert seen.choices[-1] == {"type": "none"}
        assert brain._history[phone][-1]["role"] == "assistant"
    finally:
        brain.forget(phone)


def test_r11_empty_final_answer_falls_back(monkeypatch):
    phone = "573000000012"
    _tool_loop_model(monkeypatch, types.SimpleNamespace(stop_reason="end_turn", content=[]))
    brain.forget(phone)
    try:
        assert brain.reply(phone, "hola") == "Perdón, ¿me lo repite?"
    finally:
        brain.forget(phone)


# ---- R12: amounts typed by staff ----------------------------------------------

def test_r12_decimal_amounts_are_refused(monkeypatch):
    for value in ("26000.5", "26.0005", "2,5"):
        with pytest.raises(MoneyError):
            pesos(value)

    client = _client(monkeypatch)
    order = _transfer_order()
    receipt = _receipt(order["id"], ESPECIAL, "R12-1")["receipt"]   # clean
    response = client.post(_confirm_url(order["id"], receipt["id"]), json={"amount": "26000.5"})
    assert response.status_code == 400
    assert payments.receipts(order["id"])[0]["status"] == "PROOF_RECEIVED"

    response = client.post(f"/api/orders/{order['id']}/payment/cash", json={"amount": "26000.5"})
    assert response.status_code == 400
    assert "sin decimales" in response.json()["error"]
    assert payments.current(order["id"])["cash_received"] == 0


def test_r12_panel_amount_box_is_text():
    html = KITCHEN_HTML.read_text(encoding="utf-8")
    assert 'type="number"' not in html
    assert 'inputmode="numeric"' in html
    assert "parsePesos" in html


@pytest.mark.parametrize("raw", ["47300", "47.300", "$ 47.300 COP", "47.300,00"])
def test_r12_receipt_amounts_still_read(raw):
    assert payments.read_ocr_amount(raw) == 47300


def test_r12_staff_amounts_in_colombian_format():
    assert pesos("26.000") == 26000
    assert pesos("$26.000") == 26000
    assert pesos("1.250.000") == 1250000
    assert pesos("47,000") == 47000


# ---- G16: out of scope for Part 1 ---------------------------------------------

class _Stuck(Exception):
    pass


@pytest.mark.xfail(strict=True, reason="G16: the order-code loop has no bound (out of scope for C2 Part 1)")
def test_g16_order_code_loop_is_bounded(monkeypatch):
    first = _confirmed_order()
    calls = []

    def same_code():
        calls.append(1)
        if len(calls) > 50:
            raise _Stuck()
        return first["code"]

    monkeypatch.setattr(orders, "_code", same_code)
    cart.add_item(PHONE, "b_caserita", 1)
    try:
        orders.create_from_cart(PHONE, "PICKUP")
    except _Stuck:
        pytest.fail("G16: the code loop never gives up")
