import hashlib
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx
from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data

from agent.adapters import ap2
from agent.signing import load_wallet

TEST_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
BASE_URL = "https://moramba.example"
API_KEY = "test-api-key"


def test_checkout_hash_matches_server_formula():
    result = ap2.checkout_hash("session-1", "USD", "1000")
    expected = hashlib.sha256(b"session-1|USD|1000").hexdigest()
    assert result == expected


def test_build_autonomous_message_matches_server_format_exactly():
    message = ap2.build_autonomous_message("session-1", "abc123", "1000")
    expected = (
        "Moramba AP2 Autonomous Checkout\n\n"
        "Session: session-1\n"
        "CheckoutHash: abc123\n"
        "Amount: 1000\n\n"
        "I authorize this exact checkout, within my pre-approved spend limits."
    )
    assert message == expected


@respx.mock
def test_create_checkout_session_parses_response():
    respx.post(f"{BASE_URL}/acp/checkout_sessions").mock(
        return_value=httpx.Response(
            200, json={"id": "sess-1", "currency": "USD", "totals": [{"amount": 500}]}
        )
    )
    session = ap2.create_checkout_session(BASE_URL, API_KEY, [{"id": "button-1"}])
    assert session.session_id == "sess-1"
    assert session.amount == "500"
    assert session.currency == "USD"
    assert session.decimals == 2  # no payment_options in this response — fiat-cents fallback


@respx.mock
def test_create_checkout_session_resolves_decimals_from_selected_payment_option():
    """Regression test for a real live bug (2026-10-02): a 1.5 pathUSD
    checkout (1,500,000 minor units at 6 decimals) was divided by a
    hardcoded 100 instead, turning it into 15,000 and triggering a false
    spend-limit rejection for an otherwise ordinary coffee purchase.
    `currency` is the settlement token's own name here, not fiat — see
    `currency_for` in acp_checkout_service.rs — so decimals must come
    from the session's own `payment_options`, not an assumption."""
    option_id = "11111111-1111-1111-1111-111111111111"
    respx.post(f"{BASE_URL}/acp/checkout_sessions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "sess-1", "currency": "PATHUSD", "totals": [{"amount": 1_500_000}],
                "selected_payout_destination_id": option_id,
                "payment_options": [
                    {
                        "payout_destination_id": option_id, "network": "tempo_testnet",
                        "token": "pathusd", "token_address": "0x" + "20" * 20,
                        "amount": 1_500_000, "decimals": 6, "is_default": True,
                    }
                ],
            },
        )
    )
    session = ap2.create_checkout_session(BASE_URL, API_KEY, [{"id": "button-1"}])
    assert session.amount == "1500000"
    assert session.decimals == 6


@respx.mock
def test_create_checkout_session_resolves_decimals_by_token_name_without_selected_id():
    respx.post(f"{BASE_URL}/acp/checkout_sessions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "sess-1", "currency": "USDC", "totals": [{"amount": 2_500_000}],
                "payment_options": [
                    {
                        "payout_destination_id": "22222222-2222-2222-2222-222222222222",
                        "network": "tempo_testnet", "token": "usdc", "token_address": "0x" + "aa" * 20,
                        "amount": 2_500_000, "decimals": 6, "is_default": True,
                    }
                ],
            },
        )
    )
    session = ap2.create_checkout_session(BASE_URL, API_KEY, [{"id": "button-1"}])
    assert session.decimals == 6


@respx.mock
def test_authorize_autonomous_sends_a_verifiable_closed_mandate():
    route = respx.post(f"{BASE_URL}/acp/checkout_sessions/sess-1/authorize_autonomous").mock(
        return_value=httpx.Response(200, json={"id": "sess-1"})
    )
    wallet = load_wallet(TEST_PRIVATE_KEY)
    session = ap2.CheckoutSession(session_id="sess-1", amount="1000", currency="USD", decimals=2, raw={})

    ap2.authorize_autonomous(BASE_URL, API_KEY, session, "agent-9", wallet)

    assert route.called
    body = json.loads(route.calls[0].request.content)
    expected_hash = hashlib.sha256(b"sess-1|USD|1000").hexdigest()
    assert body["checkout_hash"] == expected_hash
    assert body["agent_id"] == "agent-9"
    assert "I authorize this exact checkout" in body["message"]

    recovered = Account.recover_message(encode_defunct(text=body["message"]), signature=body["signature"])
    assert recovered == wallet.address
    assert route.calls[0].request.headers["Authorization"] == f"Bearer {API_KEY}"


@respx.mock
def test_authorize_autonomous_raises_on_rejection():
    respx.post(f"{BASE_URL}/acp/checkout_sessions/sess-1/authorize_autonomous").mock(
        return_value=httpx.Response(402, json={"message": "spend limit exceeded"})
    )
    wallet = load_wallet(TEST_PRIVATE_KEY)
    session = ap2.CheckoutSession(session_id="sess-1", amount="1000", currency="USD", decimals=2, raw={})

    try:
        ap2.authorize_autonomous(BASE_URL, API_KEY, session, "agent-9", wallet)
        assert False, "expected Ap2Error"
    except ap2.Ap2Error as exc:
        assert "spend limit exceeded" in str(exc)


@respx.mock
def test_start_checkout_payment_extracts_payin_id_from_continue_url():
    respx.post(f"{BASE_URL}/acp/checkout_sessions/sess-1/start").mock(
        return_value=httpx.Response(200, json={"continue_url": "https://pay.example/acp-pay.html?payin_id=pin-42"})
    )
    payin_id = ap2.start_checkout_payment(BASE_URL, API_KEY, "sess-1")
    assert payin_id == "pin-42"


@respx.mock
def test_complete_checkout_session_sends_expected_body():
    route = respx.post(f"{BASE_URL}/acp/checkout_sessions/sess-1/complete").mock(
        return_value=httpx.Response(200, json={"status": "completed"})
    )
    ap2.complete_checkout_session(BASE_URL, API_KEY, "sess-1", "buyer@example.com")
    body = json.loads(route.calls[0].request.content)
    assert body["buyer"]["email"] == "buyer@example.com"
    assert body["payment_data"]["handler_id"] == "moramba_ap2_mandate"
    assert body["payment_data"]["instrument"]["credential"]["token"] == "paid"


def _mock_w3_with_code(selector_present: bool, permit2_deployed: bool = False):
    w3 = MagicMock()
    real_code = ("6080604052" + ap2.TRANSFER_WITH_AUTHORIZATION_SELECTOR) if selector_present else "6080604052deadbeef"

    def get_code(address):
        if address == ap2.Web3.to_checksum_address(ap2.PERMIT2_ADDRESS):
            return bytes.fromhex("6080" if permit2_deployed else "")
        return bytes.fromhex(real_code)

    w3.eth.get_code.side_effect = get_code
    return w3


def test_detect_flow_prefers_authorization_when_selector_present():
    w3 = _mock_w3_with_code(selector_present=True)
    assert ap2.detect_flow(w3, "0x" + "11" * 20) == "authorization"


def test_detect_flow_falls_back_to_plain_when_nothing_else_matches():
    w3 = _mock_w3_with_code(selector_present=False, permit2_deployed=False)
    with patch.object(ap2, "check_supports_permit", return_value=False):
        assert ap2.detect_flow(w3, "0x" + "11" * 20) == "plain"


def test_detect_flow_uses_permit2_when_deployed_and_nothing_else_matches():
    w3 = _mock_w3_with_code(selector_present=False, permit2_deployed=True)
    with patch.object(ap2, "check_supports_permit", return_value=False):
        assert ap2.detect_flow(w3, "0x" + "11" * 20) == "permit2"


def test_build_authorization_pay_body_signature_is_independently_verifiable():
    wallet = load_wallet(TEST_PRIVATE_KEY)
    account = wallet._account

    w3 = MagicMock()
    token_contract = MagicMock()
    token_contract.functions.eip712Domain.side_effect = Exception("not implemented")
    token_contract.functions.name.return_value.call.return_value = "USD Coin"
    w3.eth.contract.return_value = token_contract

    init = ap2.PayInit(
        token_address="0x" + "22" * 20, token_name="USDC", amount="100000", chain_id=84532,
        rpc="https://rpc.example", verify_sc_address="0x" + "33" * 20, to="0x" + "44" * 20,
        transaction_id="tx-1", nonce=None, raw={},
    )

    body = ap2.build_authorization_pay_body(w3, account, init)

    domain = {"name": "USD Coin", "version": "1", "chainId": 84532, "verifyingContract": init.token_address}
    types = {
        "TransferWithAuthorization": [
            {"name": "from", "type": "address"}, {"name": "to", "type": "address"}, {"name": "value", "type": "uint256"},
            {"name": "validAfter", "type": "uint256"}, {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"},
        ]
    }
    value = {
        "from": account.address, "to": init.to, "value": int(init.amount),
        "validAfter": body["valid_after"], "validBefore": body["valid_before"], "nonce": body["nonce"],
    }
    signable = encode_typed_data(domain_data=domain, message_types=types, message_data=value)
    recovered = Account.recover_message(signable, vrs=(body["v"], int(body["r"], 16), int(body["s"], 16)))

    assert recovered == wallet.address
    assert body["token"] == init.token_address
    assert body["value"] == init.amount


@respx.mock
def test_settle_autonomous_checkout_preserves_payin_id_and_flow_when_relay_submission_fails():
    """Same real-world finding as pay_button.py's identical test: a relay
    submission can fail after a payin was already created and a flow
    already chosen (including, for permit2, after a real on-chain
    approve()) — discarding that on failure would hide exactly the
    detail needed to know a payin exists server-side."""
    wallet = load_wallet(TEST_PRIVATE_KEY)
    session = ap2.CheckoutSession(session_id="sess-1", amount="1000", currency="USD", decimals=2, raw={})

    respx.post(f"{BASE_URL}/acp/checkout_sessions/sess-1/authorize_autonomous").mock(
        return_value=httpx.Response(200, json={"id": "sess-1"})
    )
    respx.post(f"{BASE_URL}/acp/checkout_sessions/sess-1/start").mock(
        return_value=httpx.Response(200, json={"continue_url": "https://pay.example/x?payin_id=payin-partial-2"})
    )
    respx.get(f"{BASE_URL}/api/v2/morambacrypto/public/payrequest/init/payin-partial-2/address/{wallet.address}").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True, "message": "ok",
                "data": {
                    "token_address": "0x" + "22" * 20, "token_name": "USDC", "amount": "100000",
                    "chain_id": 84532, "rpc": "https://rpc.example", "verify_sc_address": "0x" + "33" * 20,
                    "to": "0x" + "44" * 20, "transaction_id": "tx-partial-2", "nonce": "0",
                },
            },
        )
    )
    respx.post(f"{BASE_URL}/api/v2/morambacrypto/public/payrequest/pay-permit2/payin-partial-2/tnxid/tx-partial-2/relay").mock(
        return_value=httpx.Response(500, json={"success": False, "message": "Requested application data is not configured correctly", "data": None})
    )

    with patch.object(ap2, "detect_flow", return_value="permit2"), \
         patch.dict(ap2._BUILDER_BY_FLOW, {"permit2": lambda w3, account, init: {}}):
        result = ap2.settle_autonomous_checkout(
            base_url=BASE_URL, api_key=API_KEY, agent_id="agent-9", wallet=wallet,
            session=session, buyer_email="buyer@example.com",
        )

    assert not result.success
    assert result.payin_id == "payin-partial-2"
    assert result.flow == "permit2"
    assert "not configured correctly" in result.error


def test_approve_if_needed_explains_an_unfunded_wallet_instead_of_crashing():
    """Found live: a wallet with no balance for network fees made
    eth_estimateGas fail with 'gas required exceeds allowance (0)', which
    escaped as a raw web3 error and crashed the whole pay_payin tool call
    with nothing recorded."""
    w3 = MagicMock()
    w3.eth.chain_id = 42431
    token = MagicMock()
    token.functions.allowance.return_value.call.return_value = 0
    token.functions.approve.return_value.build_transaction.side_effect = Exception(
        "{'code': -32000, 'message': 'gas required exceeds allowance (0)'}"
    )
    w3.eth.contract.return_value = token
    account = MagicMock()
    account.address = "0x" + "11" * 20

    try:
        ap2._approve_if_needed(w3, account, "0x" + "22" * 20, "0x" + "33" * 20, 1000)
        assert False, "expected Ap2Error"
    except ap2.Ap2Error as exc:
        assert "fund it first" in str(exc)
        assert account.address in str(exc)
        assert "42431" in str(exc)


def test_approve_if_needed_skips_the_transaction_when_allowance_is_already_enough():
    w3 = MagicMock()
    token = MagicMock()
    token.functions.allowance.return_value.call.return_value = 5000
    w3.eth.contract.return_value = token

    ap2._approve_if_needed(w3, MagicMock(), "0x" + "22" * 20, "0x" + "33" * 20, 1000)

    token.functions.approve.assert_not_called()


def _balance_w3(balance: int, decimals: int = 6):
    w3 = MagicMock()
    token = MagicMock()
    token.functions.balanceOf.return_value.call.return_value = balance
    token.functions.decimals.return_value.call.return_value = decimals
    w3.eth.contract.return_value = token
    return w3


def _balance_init(amount: str = "1500000"):
    return ap2.PayInit(
        token_address="0x" + "22" * 20, token_name="pathusd", amount=amount, chain_id=42431,
        rpc="https://rpc.example", verify_sc_address="0x" + "33" * 20, to="0x" + "44" * 20,
        transaction_id="tx-1", nonce=None, raw={},
    )


@pytest.mark.real_balance_check
def test_check_wallet_token_balance_passes_when_the_wallet_holds_enough():
    ap2.check_wallet_token_balance(_balance_w3(2_000_000), "0x" + "11" * 20, _balance_init("1500000"))


@pytest.mark.real_balance_check
def test_check_wallet_token_balance_names_the_wallet_token_and_shortfall():
    with pytest.raises(ap2.Ap2Error) as exc:
        ap2.check_wallet_token_balance(_balance_w3(0), "0x" + "11" * 20, _balance_init("1500000"))

    message = str(exc.value)
    assert "insufficient token balance" in message
    assert "0x" + "11" * 20 in message
    assert "pathusd" in message
    assert "needs 1.5" in message
    assert "fund it first" in message


@pytest.mark.real_balance_check
def test_check_wallet_token_balance_fails_clearly_when_the_balance_cannot_be_read():
    w3 = MagicMock()
    w3.eth.contract.return_value.functions.balanceOf.return_value.call.side_effect = Exception("rpc down")

    with pytest.raises(ap2.Ap2Error) as exc:
        ap2.check_wallet_token_balance(w3, "0x" + "11" * 20, _balance_init())

    assert "could not read token balance" in str(exc.value)


@pytest.mark.real_balance_check
@respx.mock
def test_settle_autonomous_checkout_stops_before_building_or_signing_when_balance_is_too_low():
    wallet = load_wallet(TEST_PRIVATE_KEY)
    session = ap2.CheckoutSession(session_id="sess-1", amount="1500000", currency="PATHUSD", decimals=6, raw={})
    respx.post(f"{BASE_URL}/acp/checkout_sessions/sess-1/authorize_autonomous").mock(
        return_value=httpx.Response(200, json={"id": "sess-1"})
    )
    respx.post(f"{BASE_URL}/acp/checkout_sessions/sess-1/start").mock(
        return_value=httpx.Response(200, json={"continue_url": "https://pay.example/x?payin_id=payin-1"})
    )
    respx.get(f"{BASE_URL}/api/v2/morambacrypto/public/payrequest/init/payin-1/address/{wallet.address}").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True, "message": "ok",
                "data": {
                    "token_address": "0x" + "22" * 20, "token_name": "pathusd", "amount": "1500000",
                    "chain_id": 42431, "rpc": "https://rpc.example", "verify_sc_address": "0x" + "33" * 20,
                    "to": "0x" + "44" * 20, "transaction_id": "tx-1", "nonce": "0",
                },
            },
        )
    )
    builder = MagicMock()

    with patch.object(ap2, "Web3") as MockWeb3, patch.dict(ap2._BUILDER_BY_FLOW, {"plain": builder, "permit2": builder, "authorization": builder}):
        MockWeb3.to_checksum_address.side_effect = lambda a: a
        MockWeb3.return_value = _balance_w3(0)
        result = ap2.settle_autonomous_checkout(
            base_url=BASE_URL, api_key=API_KEY, agent_id="agent-9", wallet=wallet,
            session=session, buyer_email="buyer@example.com",
        )

    assert not result.success
    assert "insufficient token balance" in result.error
    assert result.payin_id == "payin-1"
    assert result.chain_id == 42431
    builder.assert_not_called()
