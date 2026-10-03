from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest
import respx

from agent import balance
from agent import engine as engine_module
from agent.adapters.base import PaymentResult
from agent.config import Settings
from agent.engine import Agent
from agent.ledger import STATUS_FAILED, STATUS_PENDING, STATUS_REJECTED, STATUS_SETTLED
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
        payment_agent_api_key="test-agent-api-key",
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


MPP_TOKEN = {
    "id": "t-1", "network": "Tempo", "chain": 42431, "network_type": "testnet",
    "rpc_url": "https://rpc.moderato.tempo.xyz", "token_name": "USDC",
    "token_address": "0x" + "20" * 20,
}


RECEIVER_WALLET = "0x6784f65225f7d567cf1535525b0dd720b1450d1b"


def mock_receiving_agent(receiver_id="receiver-agent-1", wallet=RECEIVER_WALLET, status="active", http_status=200):
    """The receiving agent's own Moramba record — where the MPP rail reads
    who a payment goes to, the same source the receiving server reads."""
    if http_status != 200:
        return respx.get(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent", params={"agent_id": receiver_id}).mock(
            return_value=httpx.Response(http_status, json={"message": "nope"})
        )
    return respx.get(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent", params={"agent_id": receiver_id}).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True, "message": "ok",
                "data": {
                    "id": receiver_id, "status": status, "is_receiving_agent": True,
                    "receiving_config": {"receive_wallet_address": wallet, "accepted_tokens": []},
                },
            },
        )
    )


def pay_mpp(agent, **overrides):
    kwargs = dict(
        receiver_base_url="https://receiver.example", receiver_agent_id="receiver-agent-1",
        amount=Decimal("2.5"), token="USDC", payout_agent_id="payer-agent-9",
    )
    kwargs.update(overrides)
    return agent.pay_via_mpp(**kwargs)


@respx.mock
def test_pay_via_mpp_settles_and_records_ledger_entry(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address, allowed_tokens=[MPP_TOKEN])
    mock_receiving_agent()
    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent/{AGENT_ID}/payments/sync").mock(
        return_value=httpx.Response(404)
    )
    respx.post("https://receiver.example/agent-api/payout/agent/receiver-agent-1/pay").mock(
        return_value=httpx.Response(200, json={"success": True, "tx_hash": "0xabc123"})
    )

    agent = Agent(settings)
    try:
        record = pay_mpp(agent)
    finally:
        agent.close()

    assert record.status == STATUS_SETTLED
    assert record.tx_hash == "0xabc123"
    assert record.chain_id == 42431


@respx.mock
def test_pay_via_mpp_pins_what_the_challenge_may_ask_for(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address, allowed_tokens=[MPP_TOKEN])
    mock_receiving_agent()
    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent/{AGENT_ID}/payments/sync").mock(
        return_value=httpx.Response(404)
    )

    agent = Agent(settings)
    try:
        with patch.object(engine_module.mpp, "pay", return_value=PaymentResult(success=True, tx_hash="0xok")) as mock_pay:
            pay_mpp(agent)
    finally:
        agent.close()

    expected = mock_pay.call_args.kwargs["expected"]
    assert expected.chain_id == 42431
    assert expected.token_address == MPP_TOKEN["token_address"]
    assert expected.recipient == RECEIVER_WALLET  # from the receiving agent's own record
    assert expected.max_units == 2_500_000  # 2.5 at the token's 6 on-chain decimals
    assert mock_pay.call_args.kwargs["rpc_url"] == MPP_TOKEN["rpc_url"]


@respx.mock
def test_pay_via_mpp_to_an_address_pins_that_address_and_never_looks_up_an_agent(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address, allowed_tokens=[MPP_TOKEN])
    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent/{AGENT_ID}/payments/sync").mock(
        return_value=httpx.Response(404)
    )
    destination = "0x" + "55" * 20

    agent = Agent(settings)
    try:
        with patch.object(engine_module.mpp, "pay", return_value=PaymentResult(success=True, tx_hash="0xok")) as mock_pay:
            pay_mpp(agent, payment_to="address", to_address=destination)
    finally:
        agent.close()

    assert mock_pay.call_args.kwargs["expected"].recipient == destination


@respx.mock
def test_pay_via_mpp_rejects_a_token_on_a_chain_that_is_not_tempo(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address, allowed_tokens=[{**MPP_TOKEN, "chain": 8453, "network_type": "mainnet"}])

    agent = Agent(settings)
    try:
        with patch.object(engine_module.mpp, "pay") as mock_pay:
            record = pay_mpp(agent)
    finally:
        agent.close()

    assert record.status == STATUS_REJECTED
    assert "MPP only runs on Tempo" in record.reason
    assert "8453" in record.reason
    mock_pay.assert_not_called()


@respx.mock
def test_pay_via_mpp_rejects_a_token_with_no_contract_address_on_record(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address, allowed_tokens=[{"token_name": "USDC"}])

    agent = Agent(settings)
    try:
        with patch.object(engine_module.mpp, "pay") as mock_pay:
            record = pay_mpp(agent)
    finally:
        agent.close()

    assert record.status == STATUS_REJECTED
    assert "contract address" in record.reason
    mock_pay.assert_not_called()


@respx.mock
def test_pay_via_mpp_rejects_when_the_receiving_agent_cannot_be_looked_up(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address, allowed_tokens=[MPP_TOKEN])
    mock_receiving_agent(http_status=404)

    agent = Agent(settings)
    try:
        with patch.object(engine_module.mpp, "pay") as mock_pay:
            record = pay_mpp(agent)
    finally:
        agent.close()

    assert record.status == STATUS_REJECTED
    assert "could not look up receiving agent" in record.reason
    mock_pay.assert_not_called()  # can't check who it pays, so nothing is paid


@respx.mock
def test_pay_via_mpp_rejects_an_inactive_receiving_agent(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address, allowed_tokens=[MPP_TOKEN])
    mock_receiving_agent(status="inactive")

    agent = Agent(settings)
    try:
        with patch.object(engine_module.mpp, "pay") as mock_pay:
            record = pay_mpp(agent)
    finally:
        agent.close()

    assert record.status == STATUS_REJECTED
    assert "is not active" in record.reason
    mock_pay.assert_not_called()


@respx.mock
def test_pay_via_mpp_fails_before_signing_when_the_wallet_lacks_the_token(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address, allowed_tokens=[MPP_TOKEN])
    mock_receiving_agent()
    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent/{AGENT_ID}/payments/sync").mock(
        return_value=httpx.Response(404)
    )
    reason = "insufficient token balance: wallet 0xabc has 0 USDC, needs 2.5 — fund it first"

    agent = Agent(settings)
    try:
        with patch.object(balance, "insufficient_balance_reason", return_value=reason) as mock_check, \
             patch.object(engine_module.mpp, "pay") as mock_pay:
            record = pay_mpp(agent)
    finally:
        agent.close()

    assert record.status == STATUS_FAILED
    assert record.reason == reason
    assert record.chain_id == 42431
    mock_pay.assert_not_called()  # nothing was signed or sent
    assert mock_check.call_args.kwargs["token_address"] == MPP_TOKEN["token_address"]
    assert mock_check.call_args.kwargs["rpc_url"] == MPP_TOKEN["rpc_url"]
    assert mock_check.call_args.kwargs["needed_amount"] == Decimal("2.5")
    assert mock_check.call_args.kwargs["wallet_address"] == wallet.address


@respx.mock
def test_pay_via_mpp_records_pending_when_the_outcome_is_unknown(tmp_path):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address, allowed_tokens=[MPP_TOKEN])
    mock_receiving_agent()
    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent/{AGENT_ID}/payments/sync").mock(
        return_value=httpx.Response(404)
    )
    unknown = PaymentResult(success=False, pending=True, error="outcome unknown — the payment was sent but not confirmed")

    agent = Agent(settings)
    try:
        with patch.object(engine_module.mpp, "pay", return_value=unknown):
            record = pay_mpp(agent)
        spent = agent.ledger.spent_since("1970-01-01T00:00:00+00:00")
    finally:
        agent.close()

    assert record.status == STATUS_PENDING
    assert spent == Decimal("2.5")  # may have gone through, so it still counts against limits


# ─── paying a URL directly (pay_via_mpp_url) ────────────────────────────────

from tests.mpp_helpers import (  # noqa: E402
    PAID_URL, PAYEE, challenge as url_challenge, install_fake_tempo, paid_content_server, www_authenticate,
)

URL_TOKEN_ADDRESS = MPP_TOKEN["token_address"]


def _agent_for_url(tmp_path, monkeypatch, **payout_overrides):
    settings = make_settings(tmp_path)
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address, **({"allowed_tokens": [MPP_TOKEN]} | payout_overrides))
    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent/{AGENT_ID}/payments/sync").mock(
        return_value=httpx.Response(404)
    )
    fake = install_fake_tempo(monkeypatch, wallet.address)
    return Agent(settings), fake


@respx.mock
def test_pay_via_mpp_url_pays_the_url_and_returns_what_it_serves(tmp_path, monkeypatch):
    agent, fake = _agent_for_url(tmp_path, monkeypatch)
    paid_content_server(challenges=[url_challenge(currency=URL_TOKEN_ADDRESS)], reference="0xurlsettled")
    try:
        outcome = agent.pay_via_mpp_url(url=PAID_URL)
    finally:
        agent.close()

    record = outcome.record
    assert record.status == STATUS_SETTLED
    assert record.tx_hash == "0xurlsettled"
    assert record.rail == "mpp"
    assert record.recipient == PAYEE  # the payee in the challenge, not the URL
    assert record.token == "USDC"  # the agent's own name for that contract
    assert record.amount == Decimal("1")  # 1000000 smallest units at 6 decimals
    assert record.chain_id == 42431
    assert "the paid content" in outcome.content["body"]
    assert fake.credentials_created == 1


@respx.mock
def test_pay_via_mpp_url_records_nothing_when_the_url_is_free(tmp_path, monkeypatch):
    agent, fake = _agent_for_url(tmp_path, monkeypatch)
    respx.get(PAID_URL).mock(return_value=httpx.Response(200, text="free stuff", headers={"content-type": "text/plain"}))
    try:
        outcome = agent.pay_via_mpp_url(url=PAID_URL)
        history = agent.history()
    finally:
        agent.close()

    assert outcome.record is None
    assert outcome.content["body"] == "free stuff"
    assert history == []
    assert fake.credentials_created == 0


@respx.mock
@pytest.mark.parametrize("url", ["http://paid.example/x", "https://127.0.0.1/x", "https://169.254.169.254/meta"])
def test_pay_via_mpp_url_rejects_an_unsafe_url_without_making_any_request(tmp_path, monkeypatch, url):
    agent, _ = _agent_for_url(tmp_path, monkeypatch)
    try:
        outcome = agent.pay_via_mpp_url(url=url)  # no route is mocked: any request would fail the test
    finally:
        agent.close()

    assert outcome.record.status == STATUS_REJECTED


@respx.mock
def test_pay_via_mpp_url_rejects_a_method_other_than_get_or_post(tmp_path, monkeypatch):
    agent, _ = _agent_for_url(tmp_path, monkeypatch)
    try:
        outcome = agent.pay_via_mpp_url(url=PAID_URL, method="DELETE")
    finally:
        agent.close()

    assert outcome.record.status == STATUS_REJECTED
    assert "GET or POST" in outcome.record.reason


@respx.mock
def test_pay_via_mpp_url_sends_an_x402_url_to_the_x402_tool(tmp_path, monkeypatch):
    agent, fake = _agent_for_url(tmp_path, monkeypatch)
    respx.get(PAID_URL).mock(return_value=httpx.Response(402, headers={"PAYMENT-REQUIRED": "eyJ4NDAyIjp0cnVlfQ=="}, json={}))
    try:
        outcome = agent.pay_via_mpp_url(url=PAID_URL)
    finally:
        agent.close()

    assert outcome.record.status == STATUS_REJECTED
    assert "pay_via_x402" in outcome.record.reason
    assert fake.credentials_created == 0


@respx.mock
def test_pay_via_mpp_url_rejects_a_challenge_on_a_chain_that_is_not_tempo(tmp_path, monkeypatch):
    agent, fake = _agent_for_url(tmp_path, monkeypatch)
    paid_content_server(challenges=[url_challenge(currency=URL_TOKEN_ADDRESS, chain_id=8453)])
    try:
        outcome = agent.pay_via_mpp_url(url=PAID_URL)
    finally:
        agent.close()

    assert outcome.record.status == STATUS_REJECTED
    assert "MPP only runs on Tempo" in outcome.record.reason
    assert fake.credentials_created == 0


@respx.mock
def test_pay_via_mpp_url_rejects_a_token_that_is_not_one_of_the_agents_payout_tokens(tmp_path, monkeypatch):
    agent, fake = _agent_for_url(tmp_path, monkeypatch)
    paid_content_server(challenges=[url_challenge(currency="0x" + "99" * 20)])
    try:
        outcome = agent.pay_via_mpp_url(url=PAID_URL)
    finally:
        agent.close()

    assert outcome.record.status == STATUS_REJECTED
    assert "not one of this agent's payout tokens" in outcome.record.reason
    assert "USDC" in outcome.record.reason  # tells the caller what the agent does accept
    assert fake.credentials_created == 0


@respx.mock
def test_pay_via_mpp_url_rejects_a_token_whose_configured_chain_differs(tmp_path, monkeypatch):
    agent, fake = _agent_for_url(tmp_path, monkeypatch, allowed_tokens=[{**MPP_TOKEN, "chain": 4217, "network_type": "mainnet"}])
    paid_content_server(challenges=[url_challenge(currency=URL_TOKEN_ADDRESS, chain_id=42431)])
    try:
        outcome = agent.pay_via_mpp_url(url=PAID_URL)
    finally:
        agent.close()

    assert outcome.record.status == STATUS_REJECTED
    assert "configured for chain 4217" in outcome.record.reason
    assert fake.credentials_created == 0


@respx.mock
def test_pay_via_mpp_url_is_stopped_by_the_spend_limits_before_anything_is_signed(tmp_path, monkeypatch):
    agent, fake = _agent_for_url(tmp_path, monkeypatch, allowed_tokens=[MPP_TOKEN], per_transaction_limit=0.5)
    paid_content_server(challenges=[url_challenge(currency=URL_TOKEN_ADDRESS)])  # asks for 1.0
    try:
        outcome = agent.pay_via_mpp_url(url=PAID_URL)
    finally:
        agent.close()

    assert outcome.record.status == STATUS_REJECTED
    assert "per_transaction_limit" in outcome.record.reason
    assert outcome.record.amount == Decimal("1")
    assert fake.credentials_created == 0


@respx.mock
def test_pay_via_mpp_url_fails_before_signing_when_the_wallet_lacks_the_token(tmp_path, monkeypatch):
    agent, fake = _agent_for_url(tmp_path, monkeypatch)
    paid_content_server(challenges=[url_challenge(currency=URL_TOKEN_ADDRESS)])
    reason = "insufficient token balance: wallet 0xabc has 0 USDC, needs 1.0 — fund it first"
    try:
        with patch.object(balance, "insufficient_balance_reason", return_value=reason) as mock_check:
            outcome = agent.pay_via_mpp_url(url=PAID_URL)
    finally:
        agent.close()

    assert outcome.record.status == STATUS_FAILED
    assert outcome.record.reason == reason
    assert mock_check.call_args.kwargs["needed_units"] == 1_000_000
    assert fake.credentials_created == 0


@respx.mock
def test_pay_via_mpp_url_records_an_unreachable_url_as_a_failure_after_retrying_the_probe(tmp_path, monkeypatch):
    agent, fake = _agent_for_url(tmp_path, monkeypatch)
    route = respx.get(PAID_URL).mock(side_effect=httpx.ConnectError("down"))
    try:
        outcome = agent.pay_via_mpp_url(url=PAID_URL)
    finally:
        agent.close()

    assert outcome.record.status == STATUS_FAILED
    assert "could not reach" in outcome.record.reason
    assert route.call_count == 3  # the unpaid probe is retried; nothing was ever paid
    assert fake.credentials_created == 0


@respx.mock
def test_pay_via_mpp_url_refuses_a_url_that_raises_its_price_on_the_real_request(tmp_path, monkeypatch):
    agent, fake = _agent_for_url(tmp_path, monkeypatch)
    paid_content_server(challenges=[
        url_challenge(currency=URL_TOKEN_ADDRESS, amount="1000000"),   # the probe: 1.0
        url_challenge(currency=URL_TOKEN_ADDRESS, amount="50000000"),  # the real request: 50.0
    ])
    try:
        outcome = agent.pay_via_mpp_url(url=PAID_URL)
        spent = agent.ledger.spent_since("1970-01-01T00:00:00+00:00")
    finally:
        agent.close()

    assert outcome.record.status == STATUS_FAILED
    assert "MPP challenge refused" in outcome.record.reason
    assert fake.credentials_created == 0
    assert spent == Decimal("0")


@respx.mock
def test_pay_via_mpp_url_records_pending_when_the_outcome_is_unknown_and_counts_it_as_spent(tmp_path, monkeypatch):
    agent, _ = _agent_for_url(tmp_path, monkeypatch)
    paid_content_server(challenges=[url_challenge(currency=URL_TOKEN_ADDRESS)])
    unknown = PaymentResult(success=False, pending=True, error="outcome unknown — sent but not confirmed")
    try:
        with patch.object(engine_module.mpp, "pay_url", return_value=unknown) as mock_pay_url:
            outcome = agent.pay_via_mpp_url(url=PAID_URL)
        spent = agent.ledger.spent_since("1970-01-01T00:00:00+00:00")
    finally:
        agent.close()

    assert outcome.record.status == STATUS_PENDING
    assert outcome.content is None
    assert spent == Decimal("1")
    expected = mock_pay_url.call_args.kwargs["expected"]
    assert (expected.chain_id, expected.token_address, expected.recipient, expected.max_units) == (
        42431, URL_TOKEN_ADDRESS, PAYEE, 1_000_000,
    )
