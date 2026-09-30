from decimal import Decimal
from unittest.mock import patch

import httpx
import respx
from fastapi.testclient import TestClient

from agent import api
from agent import engine as engine_module
from agent.adapters.base import PaymentResult
from agent.config import Settings
from agent.engine import Agent
from agent.ledger import STATUS_REJECTED, STATUS_SETTLED

TEST_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
AGENT_ID = "44444444-4444-4444-4444-444444444444"
MORAMBA_BASE = "https://moramba.example"


def make_agent(tmp_path) -> Agent:
    settings = Settings(
        wallet_private_key=TEST_PRIVATE_KEY,
        moramba_agent_id=AGENT_ID,
        moramba_api_base_url=MORAMBA_BASE,
        chain_id=42431,
        rpc_url="https://tempo-testnet.example",
        db_path=str(tmp_path / "agent.db"),
    )
    return Agent(settings)


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


def test_health_returns_503_when_agent_not_initialized():
    api.app.dependency_overrides.pop(api.get_agent, None)
    with TestClient(api.app) as client:
        # Force "not initialized" explicitly rather than relying on the
        # lifespan's real Settings() call failing — it would succeed (and
        # this assumption silently break) in any working directory that
        # happens to have a real .env, e.g. one set up for live testing.
        api._agent = None
        response = client.get("/payment-agent-api/health")
    assert response.status_code == 503


def test_health_returns_wallet_address_when_agent_injected(tmp_path):
    agent = make_agent(tmp_path)
    api.app.dependency_overrides[api.get_agent] = lambda: agent
    try:
        with TestClient(api.app) as client:
            response = client.get("/payment-agent-api/health")
        assert response.status_code == 200
        assert response.json()["wallet_address"] == agent.wallet.address
    finally:
        api.app.dependency_overrides.pop(api.get_agent, None)
        agent.close()


@respx.mock
def test_pay_mpp_endpoint_settles_and_returns_record(tmp_path):
    agent = make_agent(tmp_path)
    mock_agent_lookup(agent.wallet.address)
    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent/{AGENT_ID}/payments/sync").mock(
        return_value=httpx.Response(404)
    )
    respx.post("https://receiver.example/agent-api/payout/agent/receiver-agent-1/pay").mock(
        return_value=httpx.Response(200, json={"success": True, "tx_hash": "0xapitx"})
    )

    api.app.dependency_overrides[api.get_agent] = lambda: agent
    try:
        with TestClient(api.app) as client:
            response = client.post(
                "/payment-agent-api/pay/mpp",
                json={
                    "receiver_base_url": "https://receiver.example",
                    "receiver_agent_id": "receiver-agent-1",
                    "amount": "2.5",
                    "token": "USDC",
                    "payout_agent_id": "payer-agent-9",
                },
            )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == STATUS_SETTLED
        assert body["tx_hash"] == "0xapitx"
    finally:
        api.app.dependency_overrides.pop(api.get_agent, None)
        agent.close()


@respx.mock
def test_transfer_endpoint_rejects_without_token_contract_configured(tmp_path):
    agent = make_agent(tmp_path)
    mock_agent_lookup(agent.wallet.address)

    api.app.dependency_overrides[api.get_agent] = lambda: agent
    try:
        with TestClient(api.app) as client:
            response = client.post(
                "/payment-agent-api/transfer", json={"to_address": "0x" + "11" * 20, "amount": "1", "token": "USDC"}
            )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == STATUS_REJECTED
        assert "default_token_contract" in body["reason"]
    finally:
        api.app.dependency_overrides.pop(api.get_agent, None)
        agent.close()


def test_invalid_amount_returns_400(tmp_path):
    agent = make_agent(tmp_path)
    api.app.dependency_overrides[api.get_agent] = lambda: agent
    try:
        with TestClient(api.app) as client:
            response = client.post(
                "/payment-agent-api/transfer", json={"to_address": "0x" + "11" * 20, "amount": "not-a-number", "token": "USDC"}
            )
        assert response.status_code == 400
    finally:
        api.app.dependency_overrides.pop(api.get_agent, None)
        agent.close()


@respx.mock
def test_pay_button_endpoint_rejects_by_local_limit(tmp_path):
    agent = make_agent(tmp_path)
    button_id = "ee33ffd3-0816-4649-b979-63679351f82f"
    methods_response = {
        "success": True, "message": "button payment methods fetched",
        "data": {
            "button_id": button_id, "fixed_amount": True,
            "methods": [
                {
                    "payout_destination_id": "po-1", "network": "tempo_testnet", "token_name": "pathusd",
                    "token_address": "0x20c0000000000000000000000000000000000000",
                    "to_wallet_address": "0x6784f65225f7d567cf1535525b0dd720b1450d1b",
                    "decimals": 6, "amount_with_decimal": "1000000", "is_default": False,
                }
            ],
        },
    }
    mock_agent_lookup(agent.wallet.address, per_transaction_limit=0)
    respx.get(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/pay-button/{button_id}/methods").mock(
        return_value=httpx.Response(200, json=methods_response)
    )

    api.app.dependency_overrides[api.get_agent] = lambda: agent
    try:
        with TestClient(api.app) as client:
            response = client.post("/payment-agent-api/pay/button", json={"button_id": button_id})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == STATUS_REJECTED
        assert "per_transaction_limit" in body["reason"]
    finally:
        api.app.dependency_overrides.pop(api.get_agent, None)
        agent.close()


@respx.mock
def test_pay_agent_endpoint_settles_and_returns_receiving_agent_id(tmp_path):
    agent = make_agent(tmp_path)
    receiving_agent_id = "88888888-8888-8888-8888-888888888888"
    mock_agent_lookup(agent.wallet.address, allowed_tokens=[{"token_name": "pathusd"}])
    respx.get(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent", params={"agent_id": receiving_agent_id}).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True, "message": "ok",
                "data": {
                    "id": receiving_agent_id, "status": "active", "is_receiving_agent": True,
                    "receiving_config": {
                        "receive_wallet_address": "0x6784f65225f7d567cf1535525b0dd720b1450d1b",
                        "accepted_tokens": [
                            {
                                "token_name": "pathusd", "token_address": "0x" + "20" * 20,
                                "network": "tempo_testnet", "chain": 42431, "rpc_url": "https://tempo-testnet.example",
                            }
                        ],
                    },
                },
            },
        )
    )
    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent/{AGENT_ID}/payments/sync").mock(
        return_value=httpx.Response(404)
    )

    api.app.dependency_overrides[api.get_agent] = lambda: agent
    try:
        with patch.object(engine_module.erc20, "pay", return_value=PaymentResult(success=True, tx_hash="0xagentapi")):
            with TestClient(api.app) as client:
                response = client.post(
                    "/payment-agent-api/pay/agent",
                    json={"receiving_agent_id": receiving_agent_id, "amount": "1"},
                )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == STATUS_SETTLED
        assert body["tx_hash"] == "0xagentapi"
        assert body["receiving_agent_id"] == receiving_agent_id
    finally:
        api.app.dependency_overrides.pop(api.get_agent, None)
        agent.close()


@respx.mock
def test_check_limits_endpoint(tmp_path):
    agent = make_agent(tmp_path)
    mock_agent_lookup(agent.wallet.address, per_transaction_limit=0)

    api.app.dependency_overrides[api.get_agent] = lambda: agent
    try:
        with TestClient(api.app) as client:
            response = client.post(
                "/payment-agent-api/limits/check", json={"recipient": "vendor-a", "token": "USDC", "amount": "5", "rail": "mpp"}
            )
        assert response.status_code == 200
        body = response.json()
        assert body["allowed"] is False
        assert "per_transaction_limit" in body["reason"]
    finally:
        api.app.dependency_overrides.pop(api.get_agent, None)
        agent.close()


def test_list_payments_endpoint_returns_ledger_history(tmp_path):
    agent = make_agent(tmp_path)
    agent.ledger.record(rail="mpp", recipient="r1", token="USDC", amount=Decimal("1"), status=STATUS_SETTLED)

    api.app.dependency_overrides[api.get_agent] = lambda: agent
    try:
        with TestClient(api.app) as client:
            response = client.get("/payment-agent-api/payments")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["rail"] == "mpp"
        assert body[0]["amount"] == "1"
    finally:
        api.app.dependency_overrides.pop(api.get_agent, None)
        agent.close()
