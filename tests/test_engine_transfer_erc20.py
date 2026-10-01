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
AGENT_ID = "22222222-2222-2222-2222-222222222222"
MORAMBA_BASE = "https://moramba.example"


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


@respx.mock
def test_transfer_erc20_resolves_contract_and_network_by_token_name(tmp_path):
    """Regression coverage: previously `token` only affected the ledger
    row/limit check, never which contract actually got called — a caller
    could ask to pay "pathUSD" and silently have USDC's contract used
    instead, whichever one happened to be DEFAULT_TOKEN_CONTRACT. Now the
    agent's own payout_tokens list resolves it, including a different
    chain/rpc than the wallet's static .env values."""
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(
        wallet.address,
        allowed_tokens=[
            {
                "token_name": "pathUSD", "token_address": "0x" + "aa" * 20,
                "network": "tempo_mainnet", "chain": 4217, "rpc_url": "https://rpc.tempo.xyz",
            }
        ],
    )
    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent/{AGENT_ID}/payments/sync").mock(
        return_value=httpx.Response(404)
    )

    agent = Agent(settings)
    try:
        with patch.object(engine_module.erc20, "pay", return_value=PaymentResult(success=True, tx_hash="0xerc20tx")) as mock_pay:
            record = agent.transfer_erc20(to_address="0x" + "11" * 20, amount=Decimal("1"), token="pathUSD")
    finally:
        agent.close()

    assert record.status == STATUS_SETTLED
    assert record.tx_hash == "0xerc20tx"
    assert record.chain_id == 4217
    mock_pay.assert_called_once()
    call_kwargs = mock_pay.call_args.kwargs
    assert call_kwargs["token_contract_address"] == "0x" + "aa" * 20
    assert call_kwargs["chain_id"] == 4217
    assert call_kwargs["rpc_url"] == "https://rpc.tempo.xyz"


@respx.mock
def test_transfer_erc20_rejects_every_token_when_agent_has_no_payout_tokens_configured(tmp_path):
    """An unconfigured/empty allowed_tokens means "nothing is payable",
    never "anything is" — even with a default_token_contract still set
    from an old setup, a token that isn't in the agent's own supported
    list is rejected outright, never silently sent through the default's
    contract."""
    settings = make_settings(tmp_path, default_token_contract="0x" + "bb" * 20)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address, allowed_tokens=[])

    agent = Agent(settings)
    try:
        with patch.object(engine_module.erc20, "pay") as mock_pay:
            record = agent.transfer_erc20(to_address="0x" + "11" * 20, amount=Decimal("1"), token="USDC")
    finally:
        agent.close()

    assert record.status == STATUS_REJECTED
    assert "not supported" in record.reason
    mock_pay.assert_not_called()


@respx.mock
def test_transfer_erc20_rejects_token_not_in_payout_tokens_with_helpful_message(tmp_path):
    settings = make_settings(tmp_path)  # no default_token_contract configured
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(
        wallet.address,
        allowed_tokens=[{"token_name": "pathUSD", "token_address": "0x" + "aa" * 20, "network": "tempo_mainnet", "chain": 4217, "rpc_url": "https://rpc.tempo.xyz"}],
    )

    agent = Agent(settings)
    try:
        with patch.object(engine_module.erc20, "pay") as mock_pay:
            record = agent.transfer_erc20(to_address="0x" + "11" * 20, amount=Decimal("1"), token="USDC")
    finally:
        agent.close()

    assert record.status == STATUS_REJECTED
    assert "not supported by this agent" in record.reason
    assert "pathUSD" in record.reason
    mock_pay.assert_not_called()


@respx.mock
def test_transfer_erc20_rejects_unsupported_token_even_when_default_token_contract_is_set(tmp_path):
    """The critical case: a default IS configured (it usually is, since
    the setup wizard auto-fills it from the agent's first payout token),
    but it belongs to a *different* token than the one requested here —
    falling back to it would silently pay the wrong token's contract."""
    settings = make_settings(tmp_path, default_token_contract="0x" + "aa" * 20)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(
        wallet.address,
        allowed_tokens=[{"token_name": "pathUSD", "token_address": "0x" + "aa" * 20, "network": "tempo_mainnet", "chain": 4217, "rpc_url": "https://rpc.tempo.xyz"}],
    )

    agent = Agent(settings)
    try:
        with patch.object(engine_module.erc20, "pay") as mock_pay:
            record = agent.transfer_erc20(to_address="0x" + "11" * 20, amount=Decimal("1"), token="USDC")
    finally:
        agent.close()

    assert record.status == STATUS_REJECTED
    assert "not supported by this agent" in record.reason
    mock_pay.assert_not_called()


@respx.mock
def test_transfer_erc20_explicit_contract_override_skips_resolution_and_uses_env_network(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(
        wallet.address,
        allowed_tokens=[{"token_name": "pathUSD", "token_address": "0x" + "aa" * 20, "network": "tempo_mainnet", "chain": 4217, "rpc_url": "https://rpc.tempo.xyz"}],
    )
    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent/{AGENT_ID}/payments/sync").mock(
        return_value=httpx.Response(404)
    )

    agent = Agent(settings)
    try:
        with patch.object(engine_module.erc20, "pay", return_value=PaymentResult(success=True, tx_hash="0xoverride")) as mock_pay:
            record = agent.transfer_erc20(
                to_address="0x" + "11" * 20, amount=Decimal("1"), token="pathUSD",
                token_contract_address="0x" + "cc" * 20,
            )
    finally:
        agent.close()

    assert record.status == STATUS_SETTLED
    call_kwargs = mock_pay.call_args.kwargs
    assert call_kwargs["token_contract_address"] == "0x" + "cc" * 20
    # Explicit override doesn't carry a network with it — the wallet's
    # own .env chain/rpc still apply, not the payout token's.
    assert call_kwargs["chain_id"] == 42431
    assert call_kwargs["rpc_url"] == "https://tempo-testnet.example"


@respx.mock
def test_transfer_erc20_forwards_explicit_gas_limit_to_adapter(tmp_path):
    """Regression coverage for a real live failure: gas estimation itself
    fell back to the fixed default (150k), which was still too low for a
    271,596-gas transfer — this is the escape hatch, an explicit override
    a human approves after seeing that exact error."""
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(
        wallet.address,
        allowed_tokens=[{"token_name": "pathUSD", "token_address": "0x" + "aa" * 20, "network": "tempo_mainnet", "chain": 4217, "rpc_url": "https://rpc.tempo.xyz"}],
    )
    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent/{AGENT_ID}/payments/sync").mock(
        return_value=httpx.Response(404)
    )

    agent = Agent(settings)
    try:
        with patch.object(engine_module.erc20, "pay", return_value=PaymentResult(success=True, tx_hash="0xhighgas")) as mock_pay:
            record = agent.transfer_erc20(
                to_address="0x" + "11" * 20, amount=Decimal("1.5"), token="pathUSD", gas_limit=350_000,
            )
    finally:
        agent.close()

    assert record.status == STATUS_SETTLED
    assert mock_pay.call_args.kwargs["gas_limit"] == 350_000
