import logging
from contextlib import contextmanager
from types import SimpleNamespace
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest
import respx
from anyio import to_thread
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
TEST_API_KEY = "test-payment-agent-api-key"


def make_agent(tmp_path) -> Agent:
    settings = Settings(
        wallet_private_key=TEST_PRIVATE_KEY,
        moramba_agent_id=AGENT_ID,
        moramba_api_base_url=MORAMBA_BASE,
        chain_id=42431,
        rpc_url="https://tempo-testnet.example",
        db_path=str(tmp_path / "agent.db"),
        payment_agent_api_key=TEST_API_KEY,
    )
    return Agent(settings)


@contextmanager
def authed_client(**kwargs):
    """A TestClient that satisfies `require_api_key` on every request it
    sends, for tests whose focus is the business logic behind a route,
    not the auth gate itself (see the dedicated auth tests below for
    that). `require_api_key` middleware reads the module-level `_api_key`
    set by `lifespan` on startup — the real Settings() call it makes
    almost always fails in a test run (no real .env with these exact
    fields in cwd), so it's set explicitly here, after startup, the same
    way `api._agent` is overridden elsewhere in this file. Extra kwargs
    (e.g. `base_url`) pass straight through to `TestClient`."""
    with TestClient(api.app, headers={"X-API-Key": TEST_API_KEY}, **kwargs) as client:
        api._api_key = TEST_API_KEY
        yield client


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
    with authed_client() as client:
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
        with authed_client() as client:
            response = client.get("/payment-agent-api/health")
        assert response.status_code == 200
        assert response.json()["wallet_address"] == agent.wallet.address
    finally:
        api.app.dependency_overrides.pop(api.get_agent, None)
        agent.close()


def test_request_rejected_without_a_matching_api_key_when_one_is_configured(tmp_path):
    """Regression test for a real gap: TUNNEL=1 exposes this service on a
    public URL with no auth of its own. Anyone who got that URL could
    otherwise call any pay_* route directly, bounded only by the agent's
    spend limits, not blocked outright."""
    agent = make_agent(tmp_path)
    api.app.dependency_overrides[api.get_agent] = lambda: agent
    try:
        with TestClient(api.app) as client:
            api._api_key = "secret-123"
            no_header = client.get("/payment-agent-api/health")
            wrong_header = client.get("/payment-agent-api/health", headers={"X-API-Key": "wrong"})
            right_header = client.get("/payment-agent-api/health", headers={"X-API-Key": "secret-123"})
        assert no_header.status_code == 401
        assert wrong_header.status_code == 401
        assert right_header.status_code == 200
    finally:
        api.app.dependency_overrides.pop(api.get_agent, None)
        api._api_key = None
        agent.close()


def test_request_rejected_without_any_header_when_no_api_key_is_configured(tmp_path):
    """Deny-by-default, not fail-open: a missing PAYMENT_AGENT_API_KEY
    (or a .env that failed to load at all) must mean nothing gets in —
    this was the real gap found live, where an agent with no key set in
    .env happily settled a payment with no Authorization header at all."""
    agent = make_agent(tmp_path)
    api.app.dependency_overrides[api.get_agent] = lambda: agent
    try:
        with TestClient(api.app) as client:
            api._api_key = None
            response = client.get("/payment-agent-api/health")
        assert response.status_code == 401
    finally:
        api.app.dependency_overrides.pop(api.get_agent, None)
        agent.close()


def _health(headers, key="secret-123"):
    """GET /health with the service's key set to `key` and exactly these headers."""
    api.app.dependency_overrides[api.get_agent] = lambda: SimpleNamespace(wallet=SimpleNamespace(address="0xabc"))
    try:
        with TestClient(api.app) as client:
            api._api_key = key
            return client.get("/payment-agent-api/health", headers=headers)
    finally:
        api.app.dependency_overrides.pop(api.get_agent, None)
        api._api_key = None


def test_the_key_is_accepted_in_the_x_api_key_header():
    assert _health({"X-API-Key": "secret-123"}).status_code == 200


def test_the_header_name_is_case_insensitive_and_surrounding_spaces_are_ignored():
    assert _health({"x-api-key": "secret-123"}).status_code == 200
    assert _health({"X-API-KEY": "  secret-123  "}).status_code == 200


def test_authorization_bearer_is_no_longer_accepted_even_with_the_right_key():
    assert _health({"Authorization": "Bearer secret-123"}).status_code == 401
    assert _health({"Authorization": "secret-123"}).status_code == 401


def test_a_wrong_x_api_key_is_refused():
    assert _health({"X-API-Key": "wrong"}).status_code == 401


def test_the_right_key_in_x_api_key_wins_even_with_a_stray_authorization_header():
    assert _health({"X-API-Key": "secret-123", "Authorization": "Bearer something-else"}).status_code == 200


def test_non_ascii_header_text_is_a_401_not_a_server_error():
    # Sent as raw bytes: httpx will not encode a non-ASCII str header, but
    # a real client on the wire can send exactly this.
    assert _health({"X-API-Key": b"caf\xe9-key"}).status_code == 401


@pytest.mark.parametrize(
    "headers, expected_in_log",
    [
        ({}, "no X-API-Key header was sent"),
        ({"Authorization": "Bearer secret-123"}, "the key is only read from X-API-Key"),
        ({"X-API-Key": "Bearer secret-123"}, "starts with 'Bearer ' — send the bare key"),
        ({"X-API-Key": "X-API-Key: secret-123"}, "header name must not be repeated"),
        ({"X-API-Key": "short"}, "the X-API-Key value is 5 characters, expected 10"),
        ({"X-API-Key": "secret-999"}, "X-API-Key value has the right length but does not match"),
    ],
)
def test_a_refusal_is_logged_with_its_reason_and_never_with_any_key(headers, expected_in_log, caplog):
    with caplog.at_level(logging.WARNING, logger="agent.api"):
        response = _health(headers)

    assert response.status_code == 401
    assert response.json() == {"detail": "missing or invalid API key"}  # the caller learns nothing more
    assert expected_in_log in caplog.text
    assert "GET /payment-agent-api/health" in caplog.text
    # No part of the real key, nor of what was typed, appears in the log.
    for secret in ("secret-123", "secret-999", "abc"):
        assert secret not in caplog.text


def test_an_unconfigured_service_says_so_in_the_log(caplog):
    with caplog.at_level(logging.WARNING, logger="agent.api"):
        response = _health({"X-API-Key": "anything"}, key=None)

    assert response.status_code == 401
    assert "no PAYMENT_AGENT_API_KEY configured" in caplog.text


def test_lifespan_raises_thread_pool_limit_above_anyios_default(tmp_path, monkeypatch):
    """Every route is sync (the rails make blocking httpx/web3 calls), so
    concurrent throughput is capped by anyio's thread pool, not by asyncio
    itself — this confirms startup actually raises that cap rather than
    leaving it at anyio's default of 40."""
    monkeypatch.delenv("AGENT_THREAD_POOL_SIZE", raising=False)
    agent = make_agent(tmp_path)
    api.app.dependency_overrides[api.get_agent] = lambda: agent
    try:
        with TestClient(api.app) as client:
            limit = client.portal.call(lambda: to_thread.current_default_thread_limiter().total_tokens)
        assert limit == api._DEFAULT_THREAD_POOL_SIZE
    finally:
        api.app.dependency_overrides.pop(api.get_agent, None)
        agent.close()


def test_lifespan_respects_thread_pool_size_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_THREAD_POOL_SIZE", "7")
    agent = make_agent(tmp_path)
    api.app.dependency_overrides[api.get_agent] = lambda: agent
    try:
        with TestClient(api.app) as client:
            limit = client.portal.call(lambda: to_thread.current_default_thread_limiter().total_tokens)
        assert limit == 7
    finally:
        api.app.dependency_overrides.pop(api.get_agent, None)
        agent.close()


@respx.mock
def test_pay_mpp_endpoint_settles_and_returns_record(tmp_path):
    agent = make_agent(tmp_path)
    mock_agent_lookup(agent.wallet.address, allowed_tokens=[{
        "id": "t-1", "network": "Tempo", "chain": 42431, "network_type": "testnet",
        "rpc_url": "https://rpc.moderato.tempo.xyz", "token_name": "USDC", "token_address": "0x" + "20" * 20,
    }])
    respx.get(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent", params={"agent_id": "receiver-agent-1"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True, "message": "ok",
                "data": {
                    "id": "receiver-agent-1", "status": "active", "is_receiving_agent": True,
                    "receiving_config": {
                        "receive_wallet_address": "0x6784f65225f7d567cf1535525b0dd720b1450d1b", "accepted_tokens": [],
                    },
                },
            },
        )
    )
    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent/{AGENT_ID}/payments/sync").mock(
        return_value=httpx.Response(404)
    )
    respx.post("https://receiver.example/agent-api/payout/agent/receiver-agent-1/pay").mock(
        return_value=httpx.Response(200, json={"success": True, "tx_hash": "0xapitx"})
    )

    api.app.dependency_overrides[api.get_agent] = lambda: agent
    try:
        with authed_client() as client:
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
def test_transfer_endpoint_rejects_when_agent_has_no_payout_tokens_configured(tmp_path):
    agent = make_agent(tmp_path)
    mock_agent_lookup(agent.wallet.address)  # allowed_tokens=[] — nothing is payable

    api.app.dependency_overrides[api.get_agent] = lambda: agent
    try:
        with authed_client() as client:
            response = client.post(
                "/payment-agent-api/transfer", json={"to_address": "0x" + "11" * 20, "amount": "1", "token": "USDC"}
            )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == STATUS_REJECTED
        assert "not supported" in body["reason"]
    finally:
        api.app.dependency_overrides.pop(api.get_agent, None)
        agent.close()


@respx.mock
def test_transfer_endpoint_forwards_explicit_gas_limit(tmp_path):
    agent = make_agent(tmp_path)
    mock_agent_lookup(agent.wallet.address, allowed_tokens=[{"token_name": "USDC"}])
    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent/{AGENT_ID}/payments/sync").mock(
        return_value=httpx.Response(404)
    )

    api.app.dependency_overrides[api.get_agent] = lambda: agent
    try:
        with patch.object(engine_module.erc20, "pay", return_value=PaymentResult(success=True, tx_hash="0xhighgas")) as mock_pay:
            with authed_client() as client:
                response = client.post(
                    "/payment-agent-api/transfer",
                    json={
                        "to_address": "0x" + "11" * 20, "amount": "1", "token": "USDC",
                        "token_contract_address": "0x" + "cc" * 20, "gas_limit": 350_000,
                    },
                )
        assert response.status_code == 200
        assert response.json()["status"] == STATUS_SETTLED
        assert mock_pay.call_args.kwargs["gas_limit"] == 350_000
    finally:
        api.app.dependency_overrides.pop(api.get_agent, None)
        agent.close()


def test_invalid_amount_returns_400(tmp_path):
    agent = make_agent(tmp_path)
    api.app.dependency_overrides[api.get_agent] = lambda: agent
    try:
        with authed_client() as client:
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
    mock_agent_lookup(agent.wallet.address, per_transaction_limit=0, allowed_tokens=[{"token_name": "pathusd"}])
    respx.get(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/pay-button/{button_id}/methods").mock(
        return_value=httpx.Response(200, json=methods_response)
    )

    api.app.dependency_overrides[api.get_agent] = lambda: agent
    try:
        with authed_client() as client:
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
            with authed_client() as client:
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
    mock_agent_lookup(agent.wallet.address, per_transaction_limit=0, allowed_tokens=[{"token_name": "USDC"}])

    api.app.dependency_overrides[api.get_agent] = lambda: agent
    try:
        with authed_client() as client:
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
        with authed_client() as client:
            response = client.get("/payment-agent-api/payments")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["rail"] == "mpp"
        assert body[0]["amount"] == "1"
    finally:
        api.app.dependency_overrides.pop(api.get_agent, None)
        agent.close()


@respx.mock
def test_pay_payin_endpoint_settles(tmp_path):
    agent = make_agent(tmp_path)
    payin_id = "payin-api-1"
    mock_agent_lookup(agent.wallet.address, allowed_tokens=[{"token_name": "pathusd"}])
    respx.get(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/payrequest/init/{payin_id}/address/{agent.wallet.address}").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True, "message": "ok",
                "data": {
                    "token_address": "0x20c0000000000000000000000000000000000000", "token_name": "pathusd",
                    "amount": "1000000", "chain_id": 42431, "rpc": "https://rpc.moderato.tempo.xyz",
                    "verify_sc_address": "0x" + "33" * 20, "to": "0x6784f65225f7d567cf1535525b0dd720b1450d1b",
                    "transaction_id": "tx-api-1", "nonce": "0",
                },
            },
        )
    )
    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent/{AGENT_ID}/payments/sync").mock(
        return_value=httpx.Response(404)
    )

    api.app.dependency_overrides[api.get_agent] = lambda: agent
    try:
        with patch("agent.adapters.payin.Web3") as MockWeb3:
            MockWeb3.to_checksum_address.side_effect = lambda a: a
            w3 = MockWeb3.return_value
            contract = w3.eth.contract.return_value
            contract.functions.decimals.return_value.call.return_value = 6

            with patch.object(
                engine_module.payin_adapter, "pay_payin",
                return_value=engine_module.payin_adapter.PayinSettlementResult(success=True, tx_hash="0xpayinapi", flow="plain"),
            ):
                with authed_client() as client:
                    response = client.post("/payment-agent-api/pay/payin", json={"payin_id": payin_id})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == STATUS_SETTLED
        assert body["tx_hash"] == "0xpayinapi"
        assert body["token"] == "pathusd"
    finally:
        api.app.dependency_overrides.pop(api.get_agent, None)
        agent.close()


def test_mcp_tool_server_is_reachable_on_the_same_app_at_slash_mcp(tmp_path):
    """One process, one port for both surfaces — this is what makes
    `/mcp` a real alternative to running `moramba-payment-agent-mcp` as a
    separate process."""
    agent = make_agent(tmp_path)
    api.app.dependency_overrides[api.get_agent] = lambda: agent
    try:
        # DNS-rebinding protection auto-enables for a localhost mount and
        # only allows `127.0.0.1`/`localhost`/`[::1]` Host headers —
        # TestClient's default base_url ("testserver") gets rejected
        # (421), so point it at a permitted host instead.
        with authed_client(base_url="http://127.0.0.1:8000") as client:
            response = client.post(
                "/mcp/",
                headers={"Accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0"}},
                },
            )
        assert response.status_code == 200
        assert "moramba-payment-agent" in response.text
    finally:
        api.app.dependency_overrides.pop(api.get_agent, None)
        agent.close()


def test_pay_mpp_url_endpoint_returns_the_payment_and_the_content(tmp_path):
    from decimal import Decimal

    agent = make_agent(tmp_path)
    record = agent.ledger.record(
        rail="mpp", recipient="0x" + "55" * 20, token="USDC", amount=Decimal("1"), status=STATUS_SETTLED, tx_hash="0xurltx",
    )
    content = {"status_code": 200, "content_type": "text/plain", "body": "the paid content", "truncated": False}
    api.app.dependency_overrides[api.get_agent] = lambda: agent
    try:
        with patch.object(agent, "pay_via_mpp_url", return_value=engine_module.UrlPayment(record, content)) as mock_pay:
            with authed_client() as client:
                response = client.post("/payment-agent-api/pay/mpp-url", json={"url": "https://paid.example/x", "method": "POST", "body": {"q": 1}})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == STATUS_SETTLED and body["tx_hash"] == "0xurltx"
        assert body["content"] == content
        assert mock_pay.call_args.kwargs == {"url": "https://paid.example/x", "method": "POST", "body": {"q": 1}}
    finally:
        api.app.dependency_overrides.pop(api.get_agent, None)
        agent.close()


def test_pay_mpp_url_endpoint_reports_a_free_url_as_not_paid(tmp_path):
    agent = make_agent(tmp_path)
    free = engine_module.UrlPayment(None, {"status_code": 200, "body": "hi"}, "resource did not require payment")
    api.app.dependency_overrides[api.get_agent] = lambda: agent
    try:
        with patch.object(agent, "pay_via_mpp_url", return_value=free):
            with authed_client() as client:
                response = client.post("/payment-agent-api/pay/mpp-url", json={"url": "https://free.example/x"})
        assert response.json() == {"paid": False, "detail": "resource did not require payment", "content": {"status_code": 200, "body": "hi"}}
    finally:
        api.app.dependency_overrides.pop(api.get_agent, None)
        agent.close()
