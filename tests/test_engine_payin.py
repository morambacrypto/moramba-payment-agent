from decimal import Decimal
from unittest.mock import patch

import httpx
import respx

from agent.adapters import payin as payin_adapter
from agent.config import Settings
from agent.engine import Agent
from agent.ledger import STATUS_REJECTED, STATUS_SETTLED
from agent.signing import load_wallet

TEST_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
AGENT_ID = "33333333-3333-3333-3333-333333333333"
MORAMBA_BASE = "https://moramba.example"
PAYIN_ID = "payin-engine-1"


def make_settings(tmp_path, **overrides) -> Settings:
    defaults = dict(
        wallet_private_key=TEST_PRIVATE_KEY,
        moramba_agent_id=AGENT_ID,
        moramba_api_base_url=MORAMBA_BASE,
        chain_id=42431,
        rpc_url="https://tempo-testnet.example",
        db_path=str(tmp_path / "agent.db"),
        payment_agent_api_key="test-agent-api-key",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def mock_agent_lookup(wallet_address: str, allowed_tokens: list[dict], **payout_overrides):
    payout_config = {
        "allowed_tokens": allowed_tokens,
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


def mock_payrequest_init(wallet_address: str, **overrides):
    data = {
        "token_address": "0x20c0000000000000000000000000000000000000", "token_name": "pathusd",
        "amount": "1000000", "chain_id": 42431, "rpc": "https://rpc.moderato.tempo.xyz",
        "verify_sc_address": "0x" + "33" * 20, "to": "0x6784f65225f7d567cf1535525b0dd720b1450d1b",
        "transaction_id": "tx-engine-1", "nonce": "0",
    }
    data.update(overrides)
    return respx.get(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/payrequest/init/{PAYIN_ID}/address/{wallet_address}").mock(
        return_value=httpx.Response(200, json={"success": True, "message": "ok", "data": data})
    )


@respx.mock
def test_pay_via_payin_id_settles(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address, allowed_tokens=[{"token_name": "pathusd"}])
    mock_payrequest_init(wallet.address)
    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent/{AGENT_ID}/payments/sync").mock(
        return_value=httpx.Response(404)
    )

    with patch("agent.adapters.payin.Web3") as MockWeb3:
        MockWeb3.to_checksum_address.side_effect = lambda a: a
        w3 = MockWeb3.return_value
        contract = w3.eth.contract.return_value
        contract.functions.decimals.return_value.call.return_value = 6

        agent = Agent(settings)
        try:
            with patch.object(payin_adapter, "pay_payin", return_value=payin_adapter.PayinSettlementResult(
                success=True, tx_hash="0xenginepayin", flow="plain",
            )) as mock_pay:
                record = agent.pay_via_payin_id(payin_id=PAYIN_ID)
        finally:
            agent.close()

    assert record.status == STATUS_SETTLED
    assert record.tx_hash == "0xenginepayin"
    assert record.token == "pathusd"
    assert record.recipient == "0x6784f65225f7d567cf1535525b0dd720b1450d1b"
    assert record.amount == Decimal("1")
    assert record.chain_id == 42431
    mock_pay.assert_called_once()


@respx.mock
def test_pay_via_payin_id_rejected_by_local_limit_never_settles(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address, allowed_tokens=[{"token_name": "pathusd"}], per_transaction_limit=0)
    mock_payrequest_init(wallet.address)

    with patch("agent.adapters.payin.Web3") as MockWeb3:
        MockWeb3.to_checksum_address.side_effect = lambda a: a
        w3 = MockWeb3.return_value
        contract = w3.eth.contract.return_value
        contract.functions.decimals.return_value.call.return_value = 6

        agent = Agent(settings)
        try:
            with patch.object(payin_adapter, "pay_payin") as mock_pay:
                record = agent.pay_via_payin_id(payin_id=PAYIN_ID)
        finally:
            agent.close()

    assert record.status == STATUS_REJECTED
    assert "per_transaction_limit" in record.reason
    mock_pay.assert_not_called()


@respx.mock
def test_pay_via_payin_id_rejects_unsupported_token_never_settles(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address, allowed_tokens=[{"token_name": "USDC"}])  # payin's token is pathusd
    mock_payrequest_init(wallet.address)

    with patch("agent.adapters.payin.Web3") as MockWeb3:
        MockWeb3.to_checksum_address.side_effect = lambda a: a
        w3 = MockWeb3.return_value
        contract = w3.eth.contract.return_value
        contract.functions.decimals.return_value.call.return_value = 6

        agent = Agent(settings)
        try:
            with patch.object(payin_adapter, "pay_payin") as mock_pay:
                record = agent.pay_via_payin_id(payin_id=PAYIN_ID)
        finally:
            agent.close()

    assert record.status == STATUS_REJECTED
    assert "not in this agent's allowed_tokens" in record.reason
    mock_pay.assert_not_called()


@respx.mock
def test_pay_via_payin_id_rejects_when_payin_does_not_exist(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    respx.get(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/payrequest/init/{PAYIN_ID}/address/{wallet.address}").mock(
        return_value=httpx.Response(404, json={"success": False, "message": "payin not found"})
    )

    agent = Agent(settings)
    try:
        with patch.object(payin_adapter, "pay_payin") as mock_pay:
            record = agent.pay_via_payin_id(payin_id=PAYIN_ID)
    finally:
        agent.close()

    assert record.status == STATUS_REJECTED
    assert "payin not found" in record.reason
    mock_pay.assert_not_called()
