from decimal import Decimal
from unittest.mock import patch

import httpx
import respx

from agent.adapters import ap2
from agent.config import Settings
from agent.engine import Agent
from agent.ledger import STATUS_REJECTED, STATUS_SETTLED
from agent.signing import load_wallet

TEST_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
AGENT_ID = "33333333-3333-3333-3333-333333333333"
MORAMBA_BASE = "https://moramba.example"
API_KEY = "acp-test-key"


def make_settings(tmp_path, **overrides) -> Settings:
    defaults = dict(
        wallet_private_key=TEST_PRIVATE_KEY,
        moramba_agent_id=AGENT_ID,
        moramba_api_base_url=MORAMBA_BASE,
        moramba_acp_api_key=API_KEY,
        chain_id=42431,
        rpc_url="https://tempo-testnet.example",
        db_path=str(tmp_path / "agent.db"),
        payment_agent_api_key="test-agent-api-key",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def mock_agent_lookup(wallet_address: str, **payout_overrides):
    payout_config = {
        "allowed_tokens": [],
        "wallets": [{"public_wallet_address": wallet_address}],
        "per_transaction_limit": None,
        "daily_transaction_limit": None,
        "monthly_transaction_limit": None,
        "vendor_wise_spending_limit": None,
        "aggregate_spending_limit": None,
        "maximum_payout_limit": None,
        "daily_transaction_count_limit": None,
        "hourly_transaction_limit": None,
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
def test_pay_via_ap2_rejected_by_local_limit_never_signs_a_mandate(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address, per_transaction_limit=0, allowed_tokens=[{"token_name": "USD"}])  # any positive amount exceeds this

    respx.post(f"{MORAMBA_BASE}/acp/checkout_sessions").mock(
        return_value=httpx.Response(200, json={"id": "sess-1", "currency": "USD", "totals": [{"amount": 500}]})
    )
    # Deliberately no mock for authorize_autonomous — if the engine tried
    # to sign and call it despite the rejection, respx would raise for the
    # unmocked route and this test would fail.

    agent = Agent(settings)
    try:
        record = agent.pay_via_ap2(items=[{"id": "button-1"}], buyer_email="buyer@example.com")
    finally:
        agent.close()

    assert record.status == STATUS_REJECTED
    assert "per_transaction_limit" in record.reason
    assert record.amount == Decimal("5.00")


@respx.mock
def test_pay_via_ap2_settles_end_to_end(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address, allowed_tokens=[{"token_name": "USD"}])

    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent/{AGENT_ID}/payments/sync").mock(
        return_value=httpx.Response(404)
    )
    respx.post(f"{MORAMBA_BASE}/acp/checkout_sessions").mock(
        return_value=httpx.Response(200, json={"id": "sess-1", "currency": "USD", "totals": [{"amount": 500}]})
    )
    respx.post(f"{MORAMBA_BASE}/acp/checkout_sessions/sess-1/authorize_autonomous").mock(
        return_value=httpx.Response(200, json={"id": "sess-1"})
    )
    respx.post(f"{MORAMBA_BASE}/acp/checkout_sessions/sess-1/start").mock(
        return_value=httpx.Response(200, json={"continue_url": "https://pay.example/x?payin_id=pin-1"})
    )
    respx.get(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/payrequest/init/pin-1/address/{wallet.address}").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True, "message": "ok",
                "data": {
                    "token_address": "0x" + "22" * 20, "token_name": "USDC", "amount": "5000000",
                    "chain_id": 42431, "rpc": "https://tempo-testnet.example", "verify_sc_address": "0x" + "33" * 20,
                    "to": "0x" + "44" * 20, "transaction_id": "tx-1", "nonce": "1",
                },
            },
        )
    )
    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/payrequest/pay/pin-1/tnxid/tx-1/relay").mock(
        return_value=httpx.Response(200, json={"success": True, "message": "ok", "data": {"accepted": True}})
    )
    respx.get(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/payrequest/status/tnxid/tx-1").mock(
        return_value=httpx.Response(200, json={"success": True, "message": "ok", "data": {"status": "success", "tx_hash": "0xfinaltx"}})
    )
    respx.post(f"{MORAMBA_BASE}/acp/checkout_sessions/sess-1/complete").mock(
        return_value=httpx.Response(200, json={"status": "completed"})
    )

    # The on-chain capability-detection/signing internals are already
    # covered by tests/test_ap2_adapter.py; here we only need the engine
    # to route to *some* flow so the HTTP orchestration can be exercised.
    # `_BUILDER_BY_FLOW` binds its values at module-definition time, so
    # patching `ap2.build_plain_pay_body` directly wouldn't redirect the
    # call made through that dict — patch the dict entry itself instead.
    stub_body = {"from": wallet.address, "to": "0x" + "44" * 20, "signature": "0xsig"}
    with patch.object(ap2, "detect_flow", return_value="plain"), \
         patch.dict(ap2._BUILDER_BY_FLOW, {"plain": lambda w3, account, init: stub_body}):
        agent = Agent(settings)
        try:
            record = agent.pay_via_ap2(items=[{"id": "button-1"}], buyer_email="buyer@example.com")
        finally:
            agent.close()

    assert record.status == STATUS_SETTLED
    assert record.tx_hash == "0xfinaltx"
    assert record.amount == Decimal("5.00")
    assert record.recipient == "button-1"


@respx.mock
def test_pay_via_ap2_converts_crypto_denominated_amount_using_sessions_own_decimals(tmp_path):
    """Regression test for a real live bug (2026-10-02): a 1.5 pathUSD
    coffee purchase (session.currency="PATHUSD", amount in 6-decimal
    minor units) was divided by a hardcoded 100 (fiat-cents convention),
    turning it into 15,000 and falsely rejecting it as over a $5
    per-transaction limit. `currency` here is the settlement token's own
    name, not fiat, and its decimals (6) come from the session's own
    payment_options, not an assumption."""
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address, per_transaction_limit=5, allowed_tokens=[{"token_name": "PATHUSD"}])

    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent/{AGENT_ID}/payments/sync").mock(
        return_value=httpx.Response(404)
    )
    option_id = "11111111-1111-1111-1111-111111111111"
    respx.post(f"{MORAMBA_BASE}/acp/checkout_sessions").mock(
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
    respx.post(f"{MORAMBA_BASE}/acp/checkout_sessions/sess-1/authorize_autonomous").mock(
        return_value=httpx.Response(200, json={"id": "sess-1"})
    )
    respx.post(f"{MORAMBA_BASE}/acp/checkout_sessions/sess-1/start").mock(
        return_value=httpx.Response(200, json={"continue_url": "https://pay.example/x?payin_id=pin-1"})
    )
    respx.get(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/payrequest/init/pin-1/address/{wallet.address}").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True, "message": "ok",
                "data": {
                    "token_address": "0x" + "20" * 20, "token_name": "pathusd", "amount": "1500000",
                    "chain_id": 42431, "rpc": "https://tempo-testnet.example", "verify_sc_address": "0x" + "33" * 20,
                    "to": "0x" + "44" * 20, "transaction_id": "tx-1", "nonce": "1",
                },
            },
        )
    )
    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/payrequest/pay/pin-1/tnxid/tx-1/relay").mock(
        return_value=httpx.Response(200, json={"success": True, "message": "ok", "data": {"accepted": True}})
    )
    respx.get(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/payrequest/status/tnxid/tx-1").mock(
        return_value=httpx.Response(200, json={"success": True, "message": "ok", "data": {"status": "success", "tx_hash": "0xcoffeetx"}})
    )
    respx.post(f"{MORAMBA_BASE}/acp/checkout_sessions/sess-1/complete").mock(
        return_value=httpx.Response(200, json={"status": "completed"})
    )

    stub_body = {"from": wallet.address, "to": "0x" + "44" * 20, "signature": "0xsig"}
    with patch.object(ap2, "detect_flow", return_value="plain"), \
         patch.dict(ap2._BUILDER_BY_FLOW, {"plain": lambda w3, account, init: stub_body}):
        agent = Agent(settings)
        try:
            record = agent.pay_via_ap2(items=[{"id": "coffee-1"}], buyer_email="buyer@example.com")
        finally:
            agent.close()

    assert record.status == STATUS_SETTLED
    assert record.tx_hash == "0xcoffeetx"
    assert record.amount == Decimal("1.5")
