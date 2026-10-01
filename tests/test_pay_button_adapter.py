import json
from decimal import Decimal
from unittest.mock import patch

import httpx
import respx

from agent.adapters import pay_button
from agent.signing import load_wallet

TEST_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
BASE_URL = "https://moramba.example"
BUTTON_ID = "ee33ffd3-0816-4649-b979-63679351f82f"

# Shape mirrors the real GET .../public/pay-button/{button_id}/methods
# response (pay_button_methods_controller.rs) — see README section 4's
# "Moramba Pay Button" entry.
FIXED_BUTTON_METHODS_RESPONSE = {
    "success": True, "message": "button payment methods fetched",
    "data": {
        "button_id": BUTTON_ID, "fixed_amount": True,
        "methods": [
            {
                "payout_destination_id": "b47e3390-2208-43f4-abf3-7b60f4f2ddc2",
                "network": "tempo_testnet", "token_name": "pathusd",
                "token_address": "0x20c0000000000000000000000000000000000000",
                "to_wallet_address": "0x6784f65225f7d567cf1535525b0dd720b1450d1b",
                "decimals": 6, "amount_with_decimal": "1000000", "is_default": False,
            }
        ],
    },
}

VARIABLE_BUTTON_METHODS_RESPONSE = {
    "success": True, "message": "button payment methods fetched",
    "data": {
        "button_id": "variable-button-id", "fixed_amount": False,
        "methods": [
            {
                "payout_destination_id": "payout-1",
                "network": "tempo_testnet", "token_name": "pathusd",
                "token_address": "0x20c0000000000000000000000000000000000000",
                "to_wallet_address": "0x6784f65225f7d567cf1535525b0dd720b1450d1b",
                "decimals": 6, "amount_with_decimal": None, "is_default": True,
            }
        ],
    },
}


def _mock_methods(button_id: str, response: dict, status_code: int = 200):
    return respx.get(f"{BASE_URL}/api/v2/morambacrypto/public/pay-button/{button_id}/methods").mock(
        return_value=httpx.Response(status_code, json=response)
    )


@respx.mock
def test_fetch_button_details_parses_fixed_amount_button():
    _mock_methods(BUTTON_ID, FIXED_BUTTON_METHODS_RESPONSE)
    details = pay_button.fetch_button_details(BASE_URL, BUTTON_ID)

    assert details.fixed_amount is True
    assert len(details.payout_methods) == 1
    method = details.payout_methods[0]
    assert method.network == "tempo_testnet"
    assert method.token_name == "pathusd"
    assert method.token_address == "0x20c0000000000000000000000000000000000000"
    assert method.to_wallet_address == "0x6784f65225f7d567cf1535525b0dd720b1450d1b"
    assert method.decimals == 6
    assert method.amount_with_decimal == "1000000"


@respx.mock
def test_fetch_button_details_surfaces_the_servers_actual_error_message():
    _mock_methods(BUTTON_ID, {"success": False, "message": "Pay button is inactive", "data": None}, status_code=400)
    try:
        pay_button.fetch_button_details(BASE_URL, BUTTON_ID)
        assert False, "expected Ap2Error"
    except pay_button.Ap2Error as exc:
        assert "Pay button is inactive" in str(exc)


@respx.mock
def test_resolve_payment_plan_fixed_amount_button_converts_minor_units():
    _mock_methods(BUTTON_ID, FIXED_BUTTON_METHODS_RESPONSE)
    plan = pay_button.resolve_payment_plan(BASE_URL, BUTTON_ID)

    assert plan.fixed_amount is True
    assert plan.amount == Decimal("1")  # 1_000_000 / 10**6
    assert plan.method.to_wallet_address == "0x6784f65225f7d567cf1535525b0dd720b1450d1b"


@respx.mock
def test_resolve_payment_plan_variable_amount_requires_caller_amount():
    _mock_methods("variable-button-id", VARIABLE_BUTTON_METHODS_RESPONSE)

    try:
        pay_button.resolve_payment_plan(BASE_URL, "variable-button-id")
        assert False, "expected Ap2Error"
    except pay_button.Ap2Error as exc:
        assert "amount is required" in str(exc)

    plan = pay_button.resolve_payment_plan(BASE_URL, "variable-button-id", amount=Decimal("2.5"))
    assert plan.fixed_amount is False
    assert plan.amount == Decimal("2.5")


MULTI_TOKEN_BUTTON_METHODS_RESPONSE = {
    "success": True, "message": "button payment methods fetched",
    "data": {
        "button_id": BUTTON_ID, "fixed_amount": True,
        "methods": [
            {
                "payout_destination_id": "usdc-method", "network": "tempo_testnet", "token_name": "usdc",
                "token_address": "0x" + "aa" * 20, "to_wallet_address": "0x" + "11" * 20,
                "decimals": 6, "amount_with_decimal": "1000000", "is_default": True,
            },
            {
                "payout_destination_id": "pathusd-method", "network": "tempo_testnet", "token_name": "pathUSD",
                "token_address": "0x20c0000000000000000000000000000000000000",
                "to_wallet_address": "0x6784f65225f7d567cf1535525b0dd720b1450d1b",
                "decimals": 6, "amount_with_decimal": "1000000", "is_default": False,
            },
        ],
    },
}


@respx.mock
def test_resolve_payment_plan_defaults_to_first_method_without_preferred_tokens():
    _mock_methods(BUTTON_ID, MULTI_TOKEN_BUTTON_METHODS_RESPONSE)
    plan = pay_button.resolve_payment_plan(BASE_URL, BUTTON_ID)
    assert plan.method.token_name == "usdc"


@respx.mock
def test_resolve_payment_plan_prefers_a_method_matching_preferred_tokens():
    """Regression coverage: a button accepting both usdc and pathUSD
    previously always picked usdc (index 0) regardless of which tokens
    the paying agent actually supports."""
    _mock_methods(BUTTON_ID, MULTI_TOKEN_BUTTON_METHODS_RESPONSE)
    plan = pay_button.resolve_payment_plan(BASE_URL, BUTTON_ID, preferred_tokens=["pathUSD"])
    assert plan.method.token_name == "pathUSD"
    assert plan.method.to_wallet_address == "0x6784f65225f7d567cf1535525b0dd720b1450d1b"


@respx.mock
def test_resolve_payment_plan_preferred_tokens_is_case_insensitive():
    _mock_methods(BUTTON_ID, MULTI_TOKEN_BUTTON_METHODS_RESPONSE)
    plan = pay_button.resolve_payment_plan(BASE_URL, BUTTON_ID, preferred_tokens=["pathusd"])
    assert plan.method.token_name == "pathUSD"


@respx.mock
def test_resolve_payment_plan_falls_back_to_first_method_when_no_preferred_token_matches():
    _mock_methods(BUTTON_ID, MULTI_TOKEN_BUTTON_METHODS_RESPONSE)
    plan = pay_button.resolve_payment_plan(BASE_URL, BUTTON_ID, preferred_tokens=["DAI"])
    assert plan.method.token_name == "usdc"


@respx.mock
def test_resolve_payment_plan_network_wins_over_preferred_tokens():
    _mock_methods(BUTTON_ID, MULTI_TOKEN_BUTTON_METHODS_RESPONSE)
    # Both methods are on the same network here, but this confirms an
    # explicit network selection is never overridden by token preference.
    plan = pay_button.resolve_payment_plan(
        BASE_URL, BUTTON_ID, network="tempo_testnet", preferred_tokens=["pathUSD"]
    )
    assert plan.method.token_name == "usdc"  # first match for that network


MULTI_NETWORK_SAME_TOKEN_BUTTON_METHODS_RESPONSE = {
    "success": True, "message": "button payment methods fetched",
    "data": {
        "button_id": BUTTON_ID, "fixed_amount": True,
        "methods": [
            {
                "payout_destination_id": "pathusd-mainnet", "network": "tempo_mainnet", "token_name": "pathUSD",
                "token_address": "0x" + "bb" * 20, "to_wallet_address": "0x" + "22" * 20,
                "decimals": 6, "amount_with_decimal": "1000000", "is_default": True,
            },
            {
                "payout_destination_id": "pathusd-testnet", "network": "tempo_testnet", "token_name": "pathUSD",
                "token_address": "0x20c0000000000000000000000000000000000000",
                "to_wallet_address": "0x6784f65225f7d567cf1535525b0dd720b1450d1b",
                "decimals": 6, "amount_with_decimal": "1000000", "is_default": False,
            },
        ],
    },
}


@respx.mock
def test_resolve_payment_plan_prefers_network_hint_when_token_matches_multiple_methods():
    """The same preferred token can exist on more than one network (e.g.
    pathUSD on both testnet and mainnet) — preferred_network_hint breaks
    that tie using the agent's own configured chain, since
    ButtonPayoutMethod carries no chain id to match on directly."""
    _mock_methods(BUTTON_ID, MULTI_NETWORK_SAME_TOKEN_BUTTON_METHODS_RESPONSE)
    plan = pay_button.resolve_payment_plan(
        BASE_URL, BUTTON_ID, preferred_tokens=["pathUSD"], preferred_network_hint="testnet"
    )
    assert plan.method.network == "tempo_testnet"
    assert plan.method.to_wallet_address == "0x6784f65225f7d567cf1535525b0dd720b1450d1b"


@respx.mock
def test_resolve_payment_plan_without_network_hint_takes_first_matching_token():
    _mock_methods(BUTTON_ID, MULTI_NETWORK_SAME_TOKEN_BUTTON_METHODS_RESPONSE)
    plan = pay_button.resolve_payment_plan(BASE_URL, BUTTON_ID, preferred_tokens=["pathUSD"])
    assert plan.method.network == "tempo_mainnet"  # first configured match, no tiebreaker given


@respx.mock
def test_resolve_payment_plan_falls_back_when_network_hint_matches_nothing():
    _mock_methods(BUTTON_ID, MULTI_NETWORK_SAME_TOKEN_BUTTON_METHODS_RESPONSE)
    plan = pay_button.resolve_payment_plan(
        BASE_URL, BUTTON_ID, preferred_tokens=["pathUSD"], preferred_network_hint="devnet"
    )
    assert plan.method.network == "tempo_mainnet"  # no method's network contains "devnet" — first match stands


VARIABLE_MULTI_TOKEN_BUTTON_METHODS_RESPONSE = {
    "success": True, "message": "button payment methods fetched",
    "data": {
        "button_id": "variable-multi-button-id", "fixed_amount": False,
        "methods": [
            {
                "payout_destination_id": "usdc-method", "network": "tempo_testnet", "token_name": "usdc",
                "token_address": "0x" + "aa" * 20, "to_wallet_address": "0x" + "11" * 20,
                "decimals": 6, "amount_with_decimal": None, "is_default": True,
            },
            {
                "payout_destination_id": "pathusd-method", "network": "tempo_testnet", "token_name": "pathUSD",
                "token_address": "0x20c0000000000000000000000000000000000000",
                "to_wallet_address": "0x6784f65225f7d567cf1535525b0dd720b1450d1b",
                "decimals": 6, "amount_with_decimal": None, "is_default": False,
            },
        ],
    },
}


@respx.mock
def test_resolve_payment_plan_token_preference_also_applies_to_variable_amount_buttons():
    """Method selection runs before the fixed/variable-amount branch, so
    a variable-amount multi-token button gets the same preferred-token
    treatment as a fixed-amount one — this pins that down explicitly."""
    _mock_methods("variable-multi-button-id", VARIABLE_MULTI_TOKEN_BUTTON_METHODS_RESPONSE)
    plan = pay_button.resolve_payment_plan(
        BASE_URL, "variable-multi-button-id", amount=Decimal("3"), preferred_tokens=["pathUSD"]
    )
    assert plan.method.token_name == "pathUSD"
    assert plan.fixed_amount is False
    assert plan.amount == Decimal("3")


@respx.mock
def test_create_payin_by_button_id_sends_expected_body_and_omits_amount_when_none():
    route = respx.post(f"{BASE_URL}/api/v2/morambacrypto/public/payin/create/by/button_id/{BUTTON_ID}").mock(
        return_value=httpx.Response(
            200, json={"success": True, "message": "payin created successfully", "data": {"id": "payin-1"}}
        )
    )
    result = pay_button.create_payin_by_button_id(BASE_URL, BUTTON_ID, "tempo_testnet", "0x20c0000000000000000000000000000000000000")

    assert result["id"] == "payin-1"
    body = json.loads(route.calls[0].request.content)
    assert body == {"network": "tempo_testnet", "token_address": "0x20c0000000000000000000000000000000000000"}


@respx.mock
def test_pay_button_converts_variable_amount_to_minor_units_for_create_payin():
    """Regression test for a real live bug: sending "1" (human units) for
    a 1-pathUSD variable-amount payment settled 0.000001 pathUSD on-chain.
    The backend treats a variable button's `amount` the same way it
    treats a fixed button's own `amount_with_decimal` — already in minor
    units, not human units — so the client must scale it before sending,
    the same way `amount_with_decimal` is scaled back on the way in."""
    wallet = load_wallet(TEST_PRIVATE_KEY)
    route = respx.post(f"{BASE_URL}/api/v2/morambacrypto/public/payin/create/by/button_id/{BUTTON_ID}").mock(
        return_value=httpx.Response(200, json={"success": True, "message": "ok", "data": {"id": "payin-var-amt-1"}})
    )
    respx.get(f"{BASE_URL}/api/v2/morambacrypto/public/payrequest/init/payin-var-amt-1/address/{wallet.address}").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True, "message": "ok",
                "data": {
                    "token_address": "0x20c0000000000000000000000000000000000000", "token_name": "pathusd",
                    "amount": "1000000", "chain_id": 42431, "rpc": "https://rpc.moderato.tempo.xyz",
                    "verify_sc_address": "0x" + "33" * 20, "to": "0x6784f65225f7d567cf1535525b0dd720b1450d1b",
                    "transaction_id": "tx-var-amt-1", "nonce": "0",
                },
            },
        )
    )
    respx.post(f"{BASE_URL}/api/v2/morambacrypto/public/payrequest/pay/payin-var-amt-1/tnxid/tx-var-amt-1/relay").mock(
        return_value=httpx.Response(200, json={"success": True, "message": "ok", "data": {"accepted": True}})
    )
    respx.get(f"{BASE_URL}/api/v2/morambacrypto/public/payrequest/status/tnxid/tx-var-amt-1").mock(
        return_value=httpx.Response(200, json={"success": True, "message": "ok", "data": {"status": "success", "tx_hash": "0xvaramttx"}})
    )

    plan = pay_button.ButtonPaymentPlan(
        method=pay_button.ButtonPayoutMethod(
            payout_destination_id="payout-1", network="tempo_testnet", token_name="pathusd",
            token_address="0x20c0000000000000000000000000000000000000",
            to_wallet_address="0x6784f65225f7d567cf1535525b0dd720b1450d1b", decimals=6, amount_with_decimal=None,
        ),
        amount=Decimal("1"), fixed_amount=False,
    )

    stub_body = {"from": wallet.address, "to": plan.method.to_wallet_address, "signature": "0xsig"}
    with patch.object(pay_button, "detect_flow", return_value="plain"), \
         patch.dict(pay_button._BUILDER_BY_FLOW, {"plain": lambda w3, account, init: stub_body}):
        result = pay_button.pay_button(BASE_URL, BUTTON_ID, plan, wallet)

    assert result.success
    body = json.loads(route.calls[0].request.content)
    assert body["amount"] == "1000000"  # 1 pathUSD at 6 decimals, in minor units


@respx.mock
def test_pay_button_full_flow_settles(tmp_path):
    wallet = load_wallet(TEST_PRIVATE_KEY)

    respx.post(f"{BASE_URL}/api/v2/morambacrypto/public/payin/create/by/button_id/{BUTTON_ID}").mock(
        return_value=httpx.Response(200, json={"success": True, "message": "ok", "data": {"id": "payin-live-1"}})
    )
    respx.get(f"{BASE_URL}/api/v2/morambacrypto/public/payrequest/init/payin-live-1/address/{wallet.address}").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True, "message": "ok",
                "data": {
                    "token_address": "0x20c0000000000000000000000000000000000000", "token_name": "pathusd",
                    "amount": "1000000", "chain_id": 42431, "rpc": "https://rpc.moderato.tempo.xyz",
                    "verify_sc_address": "0x" + "33" * 20, "to": "0x6784f65225f7d567cf1535525b0dd720b1450d1b",
                    "transaction_id": "tx-live-1", "nonce": "0",
                },
            },
        )
    )
    respx.post(f"{BASE_URL}/api/v2/morambacrypto/public/payrequest/pay/payin-live-1/tnxid/tx-live-1/relay").mock(
        return_value=httpx.Response(200, json={"success": True, "message": "ok", "data": {"accepted": True}})
    )
    respx.get(f"{BASE_URL}/api/v2/morambacrypto/public/payrequest/status/tnxid/tx-live-1").mock(
        return_value=httpx.Response(200, json={"success": True, "message": "ok", "data": {"status": "success", "tx_hash": "0xbuttontx"}})
    )

    plan = pay_button.ButtonPaymentPlan(
        method=pay_button.ButtonPayoutMethod(
            payout_destination_id="b47e3390-2208-43f4-abf3-7b60f4f2ddc2", network="tempo_testnet",
            token_name="pathusd", token_address="0x20c0000000000000000000000000000000000000",
            to_wallet_address="0x6784f65225f7d567cf1535525b0dd720b1450d1b", decimals=6, amount_with_decimal="1000000",
        ),
        amount=Decimal("1"), fixed_amount=True,
    )

    # The on-chain capability-detection/signing internals are already
    # covered by tests/test_ap2_adapter.py; patch this module's own
    # imported references (not ap2's) since `from ... import` binds a
    # separate name in this module's namespace.
    stub_body = {"from": wallet.address, "to": plan.method.to_wallet_address, "signature": "0xsig"}
    with patch.object(pay_button, "detect_flow", return_value="plain"), \
         patch.dict(pay_button._BUILDER_BY_FLOW, {"plain": lambda w3, account, init: stub_body}):
        result = pay_button.pay_button(BASE_URL, BUTTON_ID, plan, wallet)

    assert result.success
    assert result.tx_hash == "0xbuttontx"
    assert result.payin_id == "payin-live-1"
    assert result.flow == "plain"


@respx.mock
def test_pay_button_preserves_payin_id_and_flow_when_relay_submission_fails():
    """Real finding (2026-09-29, production): the relay call can fail
    (e.g. a 500 from a misconfigured server-side flow) after a payin was
    already created and a flow already chosen — including, for permit2,
    after a real on-chain approve() already went through. Discarding
    payin_id/flow in that case would hide exactly the detail needed to
    know a payin exists server-side and what was attempted against it."""
    wallet = load_wallet(TEST_PRIVATE_KEY)

    respx.post(f"{BASE_URL}/api/v2/morambacrypto/public/payin/create/by/button_id/{BUTTON_ID}").mock(
        return_value=httpx.Response(200, json={"success": True, "message": "ok", "data": {"id": "payin-partial-1"}})
    )
    respx.get(f"{BASE_URL}/api/v2/morambacrypto/public/payrequest/init/payin-partial-1/address/{wallet.address}").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True, "message": "ok",
                "data": {
                    "token_address": "0x20c0000000000000000000000000000000000000", "token_name": "pathusd",
                    "amount": "1000000", "chain_id": 42431, "rpc": "https://rpc.moderato.tempo.xyz",
                    "verify_sc_address": "0x" + "33" * 20, "to": "0x6784f65225f7d567cf1535525b0dd720b1450d1b",
                    "transaction_id": "tx-partial-1", "nonce": "0",
                },
            },
        )
    )
    respx.post(f"{BASE_URL}/api/v2/morambacrypto/public/payrequest/pay-permit2/payin-partial-1/tnxid/tx-partial-1/relay").mock(
        return_value=httpx.Response(500, json={"success": False, "message": "Requested application data is not configured correctly", "data": None})
    )

    plan = pay_button.ButtonPaymentPlan(
        method=pay_button.ButtonPayoutMethod(
            payout_destination_id="po-1", network="tempo_testnet", token_name="pathusd",
            token_address="0x20c0000000000000000000000000000000000000",
            to_wallet_address="0x6784f65225f7d567cf1535525b0dd720b1450d1b", decimals=6, amount_with_decimal="1000000",
        ),
        amount=Decimal("1"), fixed_amount=True,
    )

    stub_body = {"token": "0x20c0000000000000000000000000000000000000"}
    with patch.object(pay_button, "detect_flow", return_value="permit2"), \
         patch.dict(pay_button._BUILDER_BY_FLOW, {"permit2": lambda w3, account, init: stub_body}):
        result = pay_button.pay_button(BASE_URL, BUTTON_ID, plan, wallet)

    assert not result.success
    assert result.payin_id == "payin-partial-1"
    assert result.flow == "permit2"
    assert "not configured correctly" in result.error
