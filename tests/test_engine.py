from decimal import Decimal
from unittest.mock import patch

import httpx
import respx

from agent import engine as engine_module
from agent.adapters.base import PaymentResult
from agent.config import Settings
from agent.engine import Agent
from agent.ledger import STATUS_REJECTED, STATUS_SETTLED
from agent.signing import load_wallet

TEST_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
AGENT_ID = "11111111-1111-1111-1111-111111111111"
RECEIVING_AGENT_ID = "99999999-9999-9999-9999-999999999999"
MORAMBA_BASE = "https://moramba.example"


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
        # Real shape: a list of token objects, not plain strings — see
        # limits_client.py's module docstring.
        "allowed_tokens": [{"token_name": "USDC"}],
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
                "success": True,
                "message": "ok",
                "data": {
                    "id": AGENT_ID,
                    "status": "active",
                    "payout_config": payout_config,
                    "rate_limit_max_transactions_count": None,
                    "rate_limit_per_period": None,
                },
            },
        )
    )


def mock_receiving_agent_lookup(*, is_receiving_agent=True, status="active", accepted_tokens=None, wallet="0x6784f65225f7d567cf1535525b0dd720b1450d1b"):
    accepted_tokens = [{"token_name": "pathusd", "token_address": "0x20c0000000000000000000000000000000000000", "network": "tempo_testnet", "chain": 42431, "rpc_url": "https://rpc-testnet.example"}] if accepted_tokens is None else accepted_tokens
    data = {"id": RECEIVING_AGENT_ID, "status": status, "is_receiving_agent": is_receiving_agent}
    if is_receiving_agent:
        data["receiving_config"] = {"receive_wallet_address": wallet, "accepted_tokens": accepted_tokens}
    return respx.get(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent", params={"agent_id": RECEIVING_AGENT_ID}).mock(
        return_value=httpx.Response(200, json={"success": True, "message": "ok", "data": data})
    )


@respx.mock
def test_pay_agent_settles_and_records_receiving_agent_id(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address, allowed_tokens=[{"token_name": "pathusd"}])
    mock_receiving_agent_lookup()
    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent/{AGENT_ID}/payments/sync").mock(
        return_value=httpx.Response(404)
    )

    agent = Agent(settings)
    try:
        with patch.object(engine_module.erc20, "pay", return_value=PaymentResult(success=True, tx_hash="0xagentpay")):
            record = agent.pay_agent(receiving_agent_id=RECEIVING_AGENT_ID, amount=Decimal("1"))
    finally:
        agent.close()

    assert record.status == STATUS_SETTLED
    assert record.tx_hash == "0xagentpay"
    assert record.receiving_agent_id == RECEIVING_AGENT_ID
    assert record.recipient == "0x6784f65225f7d567cf1535525b0dd720b1450d1b"
    assert record.token == "pathusd"


@respx.mock
def test_pay_agent_rejects_a_payout_only_target_without_ever_calling_erc20(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address)
    mock_receiving_agent_lookup(is_receiving_agent=False)

    agent = Agent(settings)
    try:
        with patch.object(engine_module.erc20, "pay") as mock_pay:
            record = agent.pay_agent(receiving_agent_id=RECEIVING_AGENT_ID, amount=Decimal("1"))
    finally:
        agent.close()

    assert record.status == STATUS_REJECTED
    assert "not a receiving agent" in record.reason
    assert record.receiving_agent_id == RECEIVING_AGENT_ID
    mock_pay.assert_not_called()


@respx.mock
def test_pay_agent_rejected_by_local_limit_never_calls_erc20(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address, per_transaction_limit=1, allowed_tokens=[{"token_name": "pathusd"}])  # $1.00
    mock_receiving_agent_lookup()

    agent = Agent(settings)
    try:
        with patch.object(engine_module.erc20, "pay") as mock_pay:
            record = agent.pay_agent(receiving_agent_id=RECEIVING_AGENT_ID, amount=Decimal("5"))
    finally:
        agent.close()

    assert record.status == STATUS_REJECTED
    assert "per_transaction_limit" in record.reason
    assert record.receiving_agent_id == RECEIVING_AGENT_ID
    mock_pay.assert_not_called()


@respx.mock
def test_pay_agent_requires_explicit_token_when_multiple_are_accepted(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address)
    mock_receiving_agent_lookup(accepted_tokens=[
        {"token_name": "pathusd", "token_address": "0x" + "11" * 20, "network": "tempo_testnet", "chain": 42431, "rpc_url": "https://rpc-testnet.example"},
        {"token_name": "usdc", "token_address": "0x" + "22" * 20, "network": "tempo_testnet", "chain": 42431, "rpc_url": "https://rpc-testnet.example"},
    ])

    agent = Agent(settings)
    try:
        with patch.object(engine_module.erc20, "pay") as mock_pay:
            record = agent.pay_agent(receiving_agent_id=RECEIVING_AGENT_ID, amount=Decimal("1"))
    finally:
        agent.close()

    assert record.status == STATUS_REJECTED
    assert "token is required" in record.reason
    mock_pay.assert_not_called()


@respx.mock
def test_pay_via_mpp_settles_and_records_ledger_entry(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address)
    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent/{AGENT_ID}/payments/sync").mock(
        return_value=httpx.Response(404)
    )
    respx.post("https://receiver.example/agent-api/payout/agent/receiver-agent-1/pay").mock(
        return_value=httpx.Response(200, json={"success": True, "tx_hash": "0xabc123"})
    )

    agent = Agent(settings)
    try:
        record = agent.pay_via_mpp(
            receiver_base_url="https://receiver.example",
            receiver_agent_id="receiver-agent-1",
            amount=Decimal("2.5"),
            token="USDC",
            payout_agent_id="payer-agent-9",
        )
    finally:
        agent.close()

    assert record.status == STATUS_SETTLED
    assert record.tx_hash == "0xabc123"


@respx.mock
def test_pay_via_mpp_rejected_by_local_limit_never_hits_the_network(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address, per_transaction_limit=1)  # $1.00 — a whole-unit value, not cents

    # Deliberately no mock registered for the receiver's /pay endpoint —
    # if the engine tried to call it, respx would raise for the
    # unmocked route and this test would fail, proving the reject
    # happened before any signature or network call.
    agent = Agent(settings)
    try:
        record = agent.pay_via_mpp(
            receiver_base_url="https://receiver.example",
            receiver_agent_id="receiver-agent-1",
            amount=Decimal("5.00"),
            token="USDC",
            payout_agent_id="payer-agent-9",
        )
    finally:
        agent.close()

    assert record.status == STATUS_REJECTED
    assert "per_transaction_limit" in record.reason
    assert record.signature is None


@respx.mock
def test_pay_via_mpp_rejected_when_wallet_not_registered_to_agent(tmp_path):
    settings = make_settings(tmp_path)
    mock_agent_lookup("0xSomeoneElsesWallet")

    agent = Agent(settings)
    try:
        record = agent.pay_via_mpp(
            receiver_base_url="https://receiver.example",
            receiver_agent_id="receiver-agent-1",
            amount=Decimal("1"),
            token="USDC",
            payout_agent_id="payer-agent-9",
        )
    finally:
        agent.close()

    assert record.status == STATUS_REJECTED
    assert "registered payout wallets" in record.reason
