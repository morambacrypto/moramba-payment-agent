from decimal import Decimal

import httpx
import respx
from x402.http.utils import encode_payment_required_header, encode_payment_response_header
from x402.schemas import PaymentRequired, PaymentRequirements, SettleResponse

from agent.config import Settings
from agent.engine import Agent
from agent.ledger import STATUS_REJECTED, STATUS_SETTLED
from agent.signing import load_wallet

TEST_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
AGENT_ID = "22222222-2222-2222-2222-222222222222"
MORAMBA_BASE = "https://moramba.example"
RESOURCE_URL = "https://weather.example/api/weather"
NETWORK = "eip155:84532"
# Real Base Sepolia USDC — the x402 SDK's own spend_controls restricts
# unpaid-for-by-default clients to known default assets per network, so
# an arbitrary made-up address gets rejected before it ever reaches our
# own limit check. See x402.py's module docstring.
ASSET = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
PAY_TO = "0x" + "00" * 19 + "ab"


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


def make_payment_required() -> PaymentRequired:
    return PaymentRequired(
        error="Payment required",
        accepts=[
            PaymentRequirements(
                scheme="exact",
                network=NETWORK,
                amount="10000",  # 0.01 USDC at 6 decimals
                asset=ASSET,
                pay_to=PAY_TO,
                max_timeout_seconds=300,
                extra={"name": "USD Coin", "version": "2"},
            )
        ],
    )


@respx.mock
def test_pay_via_x402_settles_and_records_ledger_entry(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address, allowed_tokens=[{"token_name": "USD Coin"}])
    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent/{AGENT_ID}/payments/sync").mock(
        return_value=httpx.Response(404)
    )

    header_value = encode_payment_required_header(make_payment_required())
    settle_response = SettleResponse(success=True, transaction="0xtxhash", network=NETWORK, payer=wallet.address)
    response_header = encode_payment_response_header(settle_response)

    def handler(request: httpx.Request) -> httpx.Response:
        if "payment-signature" in request.headers:
            return httpx.Response(200, headers={"PAYMENT-RESPONSE": response_header}, json={"data": "sunny"})
        return httpx.Response(402, headers={"PAYMENT-REQUIRED": header_value}, json={"ok": False})

    respx.get(RESOURCE_URL).mock(side_effect=handler)

    agent = Agent(settings)
    try:
        record = agent.pay_via_x402(url=RESOURCE_URL)
    finally:
        agent.close()

    assert record is not None
    assert record.status == STATUS_SETTLED
    assert record.tx_hash == "0xtxhash"
    assert record.amount == Decimal("0.01")
    assert record.recipient == PAY_TO


@respx.mock
def test_pay_via_x402_returns_none_when_no_payment_required(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address)
    respx.get(RESOURCE_URL).mock(return_value=httpx.Response(200, json={"data": "free"}))

    agent = Agent(settings)
    try:
        record = agent.pay_via_x402(url=RESOURCE_URL)
        history = agent.history()
    finally:
        agent.close()

    assert record is None
    assert history == []


@respx.mock
def test_pay_via_x402_rejected_by_local_limit_never_signs_or_retries(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address, per_transaction_limit=0, allowed_tokens=[{"token_name": "USD Coin"}])  # any positive amount exceeds this

    header_value = encode_payment_required_header(make_payment_required())

    # Only mocked for the FIRST (unpaid) call. If the engine tried to
    # settle despite the rejection, the retry would carry a
    # payment-signature header this route doesn't distinguish, but more
    # importantly settle() would need a second call — assert call_count
    # stays at 1 to prove it never happened.
    route = respx.get(RESOURCE_URL).mock(
        return_value=httpx.Response(402, headers={"PAYMENT-REQUIRED": header_value}, json={"ok": False})
    )

    agent = Agent(settings)
    try:
        record = agent.pay_via_x402(url=RESOURCE_URL)
    finally:
        agent.close()

    assert record is not None
    assert record.status == STATUS_REJECTED
    assert "per_transaction_limit" in record.reason
    assert route.call_count == 1
