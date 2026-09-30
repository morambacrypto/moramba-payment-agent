from decimal import Decimal

import httpx
import respx

from agent.adapters import mpp
from agent.signing import load_wallet

TEST_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"


def test_missing_payout_agent_id_is_rejected_before_any_request():
    wallet = load_wallet(TEST_PRIVATE_KEY)
    result = mpp.pay(
        receiver_base_url="https://receiver.example",
        receiver_agent_id="receiver-agent-1",
        wallet=wallet,
        amount=Decimal("5"),
        token="USDC",
        payment_to="agent",
        payout_agent_id=None,
    )
    assert not result.success
    assert "payout_agent_id" in result.error


@respx.mock
def test_successful_payment_signs_fixed_message_and_posts_expected_body():
    wallet = load_wallet(TEST_PRIVATE_KEY)
    route = respx.post("https://receiver.example/agent-api/payout/agent/receiver-agent-1/pay").mock(
        return_value=httpx.Response(200, json={"success": True, "tx_hash": "0xdeadbeef"})
    )

    result = mpp.pay(
        receiver_base_url="https://receiver.example",
        receiver_agent_id="receiver-agent-1",
        wallet=wallet,
        amount=Decimal("5.25"),
        token="USDC",
        payment_via="agent",
        payment_to="agent",
        payout_agent_id="payer-agent-9",
    )

    assert result.success
    assert result.tx_hash == "0xdeadbeef"
    assert route.called
    sent_body = route.calls[0].request.content
    import json

    body = json.loads(sent_body)
    assert body["payment_via"] == "agent"
    assert body["payment_to"] == "agent"
    assert body["payout_agent_id"] == "payer-agent-9"
    assert body["amount"] == "5.25"
    assert body["token"] == "USDC"
    assert body["signature"] == result.signature

    from eth_account import Account
    from eth_account.messages import encode_defunct

    recovered = Account.recover_message(
        encode_defunct(text=mpp.FIXED_SIGNING_MESSAGE), signature=result.signature
    )
    assert recovered == wallet.address


@respx.mock
def test_receiver_rejection_is_reported_as_failure():
    wallet = load_wallet(TEST_PRIVATE_KEY)
    respx.post("https://receiver.example/agent-api/payout/agent/receiver-agent-1/pay").mock(
        return_value=httpx.Response(402, json={"message": "daily spend limit exceeded"})
    )

    result = mpp.pay(
        receiver_base_url="https://receiver.example",
        receiver_agent_id="receiver-agent-1",
        wallet=wallet,
        amount=Decimal("5"),
        token="USDC",
        payout_agent_id="payer-agent-9",
    )

    assert not result.success
    assert result.error == "daily spend limit exceeded"
