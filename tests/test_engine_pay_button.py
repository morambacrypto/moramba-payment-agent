from decimal import Decimal
from unittest.mock import patch

import httpx
import respx

from agent.adapters import pay_button
from agent.config import Settings
from agent.engine import Agent
from agent.ledger import STATUS_REJECTED, STATUS_SETTLED
from agent.signing import load_wallet

TEST_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
AGENT_ID = "77777777-7777-7777-7777-777777777777"
MORAMBA_BASE = "https://moramba.example"
BUTTON_ID = "ee33ffd3-0816-4649-b979-63679351f82f"

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


def mock_button_methods(button_id: str, response: dict):
    return respx.get(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/pay-button/{button_id}/methods").mock(
        return_value=httpx.Response(200, json=response)
    )


def make_settings(tmp_path, **overrides) -> Settings:
    defaults = dict(
        wallet_private_key=TEST_PRIVATE_KEY,
        moramba_agent_id=AGENT_ID,
        moramba_api_base_url=MORAMBA_BASE,
        chain_id=42431,
        rpc_url="https://tempo-testnet.example",
        db_path=str(tmp_path / "agent.db"),
    )
    defaults.update(overrides)
    return Settings(**defaults)


def mock_agent_lookup(wallet_address: str, **payout_overrides):
    payout_config = {
        "allowed_tokens": [], "wallets": [{"public_wallet_address": wallet_address}],
        "per_transaction_limit": None, "daily_transaction_limit": None, "monthly_transaction_limit": None,
        "vendor_wise_spending_limit": None, "aggregate_spending_limit": None, "maximum_payout_limit": None,
        "daily_transaction_count_limit": None, "hourly_transaction_limit": None,
    }
    payout_config.update(payout_overrides)
    return respx.get(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent", params={"agent_id": AGENT_ID}).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True, "message": "ok",
                "data": {
                    "id": AGENT_ID, "status": "active", "payout_config": payout_config,
                    "rate_limit_max_transactions_count": None, "rate_limit_per_period": None,
                },
            },
        )
    )


@respx.mock
def test_pay_via_pay_button_rejected_by_local_limit_never_creates_a_payin(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address, per_transaction_limit=0, allowed_tokens=[{"token_name": "pathusd"}])  # any positive amount exceeds this

    mock_button_methods(BUTTON_ID, FIXED_BUTTON_METHODS_RESPONSE)
    # Deliberately no mock for payin/create/by/button_id — if the engine
    # tried to create a real payin despite the rejection, respx would
    # raise for the unmocked route and this test would fail.

    agent = Agent(settings)
    try:
        record = agent.pay_via_pay_button(button_id=BUTTON_ID)
    finally:
        agent.close()

    assert record.status == STATUS_REJECTED
    assert "per_transaction_limit" in record.reason
    assert record.amount == Decimal("1")


@respx.mock
def test_pay_via_pay_button_settles_end_to_end(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address, allowed_tokens=[{"token_name": "pathusd"}])
    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent/{AGENT_ID}/payments/sync").mock(
        return_value=httpx.Response(404)
    )

    mock_button_methods(BUTTON_ID, FIXED_BUTTON_METHODS_RESPONSE)
    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/payin/create/by/button_id/{BUTTON_ID}").mock(
        return_value=httpx.Response(200, json={"success": True, "message": "ok", "data": {"id": "payin-e2e-1"}})
    )
    respx.get(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/payrequest/init/payin-e2e-1/address/{wallet.address}").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True, "message": "ok",
                "data": {
                    "token_address": "0x20c0000000000000000000000000000000000000", "token_name": "pathusd",
                    "amount": "1000000", "chain_id": 42431, "rpc": "https://rpc.moderato.tempo.xyz",
                    "verify_sc_address": "0x" + "33" * 20, "to": "0x6784f65225f7d567cf1535525b0dd720b1450d1b",
                    "transaction_id": "tx-e2e-1", "nonce": "0",
                },
            },
        )
    )
    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/payrequest/pay/payin-e2e-1/tnxid/tx-e2e-1/relay").mock(
        return_value=httpx.Response(200, json={"success": True, "message": "ok", "data": {"accepted": True}})
    )
    respx.get(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/payrequest/status/tnxid/tx-e2e-1").mock(
        return_value=httpx.Response(200, json={"success": True, "message": "ok", "data": {"status": "success", "tx_hash": "0xe2etx"}})
    )

    stub_body = {"from": wallet.address, "to": "0x6784f65225f7d567cf1535525b0dd720b1450d1b", "signature": "0xsig"}
    with patch.object(pay_button, "detect_flow", return_value="plain"), \
         patch.dict(pay_button._BUILDER_BY_FLOW, {"plain": lambda w3, account, init: stub_body}):
        agent = Agent(settings)
        try:
            record = agent.pay_via_pay_button(button_id=BUTTON_ID)
        finally:
            agent.close()

    assert record.status == STATUS_SETTLED
    assert record.tx_hash == "0xe2etx"
    assert record.amount == Decimal("1")
    assert record.recipient == "0x6784f65225f7d567cf1535525b0dd720b1450d1b"
