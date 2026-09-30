from decimal import Decimal

import httpx
import respx

from agent.ledger import STATUS_SETTLED, Ledger
from agent.signing import load_wallet
from agent.sync import SyncClient

TEST_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
BASE_URL = "https://moramba.example"
AGENT_ID = "agent-1"


@respx.mock
def test_sync_pending_marks_success_and_leaves_synced_at_set(tmp_path):
    ledger = Ledger(str(tmp_path / "t.db"))
    wallet = load_wallet(TEST_PRIVATE_KEY)
    record = ledger.record(rail="mpp", recipient="r1", token="USDC", amount=Decimal("5"), status=STATUS_SETTLED)

    route = respx.post(f"{BASE_URL}/api/v2/morambacrypto/public/agent/{AGENT_ID}/payments/sync").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    client = SyncClient(BASE_URL, AGENT_ID, wallet)
    succeeded, failed = client.sync_pending(ledger)

    assert succeeded == 1
    assert failed == 0
    assert route.called
    assert ledger.get(record.id).synced_at is not None
    assert ledger.pending_sync() == []


@respx.mock
def test_sync_pending_keeps_record_in_outbox_on_failure(tmp_path):
    ledger = Ledger(str(tmp_path / "t.db"))
    wallet = load_wallet(TEST_PRIVATE_KEY)
    record = ledger.record(rail="mpp", recipient="r1", token="USDC", amount=Decimal("5"), status=STATUS_SETTLED)

    respx.post(f"{BASE_URL}/api/v2/morambacrypto/public/agent/{AGENT_ID}/payments/sync").mock(
        return_value=httpx.Response(404)
    )

    client = SyncClient(BASE_URL, AGENT_ID, wallet)
    succeeded, failed = client.sync_pending(ledger)

    assert succeeded == 0
    assert failed == 1
    fetched = ledger.get(record.id)
    assert fetched.synced_at is None
    assert fetched.sync_attempts == 1
    assert len(ledger.pending_sync()) == 1


@respx.mock
def test_sync_payload_carries_a_wallet_signature_over_the_record():
    import json

    ledger = Ledger(":memory:")
    wallet = load_wallet(TEST_PRIVATE_KEY)
    ledger.record(rail="erc20", recipient="0xRecipient", token="USDC", amount=Decimal("1.5"), status=STATUS_SETTLED)

    route = respx.post(f"{BASE_URL}/api/v2/morambacrypto/public/agent/{AGENT_ID}/payments/sync").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    client = SyncClient(BASE_URL, AGENT_ID, wallet)
    client.sync_pending(ledger)

    body = json.loads(route.calls[0].request.content)
    assert body["wallet_address"] == wallet.address
    assert "attestation_signature" in body
    assert body["attestation_signature"].startswith("0x")
