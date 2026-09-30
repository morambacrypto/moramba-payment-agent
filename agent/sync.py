"""Outbox sync: pushes every ledger record to Moramba's own Postgres
(README section 5).

Backed by a real endpoint as of 2026-09-29:
`POST /api/v2/morambacrypto/public/agent/{agent_id}/payments/sync`
(moramba-crypto-api, migration 302 + `agent_payment_sync_service.rs`).
The backend independently reconstructs the same canonical JSON this
module signs and verifies `attestation_signature` against that agent's
registered payout wallet before storing anything — see
`services::agent_payment_sync_service::canonical_message` on the Rust
side, which must stay byte-identical with `_canonical_payload`/
`_attestation_message` below. It also validates the agent is active and
rejects a wallet that isn't one of the agent's registered payout
wallets, and de-dupes retried syncs so a record is never stored twice.

A companion read endpoint, `GET /morambacrypto/agent/{agent_id}/payments`
(partner-JWT authenticated), is what a partner's dashboard reads from —
this module only ever writes.
"""

import json
import logging

import httpx

from agent.ledger import Ledger, PaymentRecord
from agent.signing import Wallet

logger = logging.getLogger(__name__)

_SYNC_PATH = "/api/v2/morambacrypto/public/agent/{agent_id}/payments/sync"


def _canonical_payload(agent_id: str, wallet_address: str, record: PaymentRecord) -> dict:
    return {
        "agent_id": agent_id,
        "wallet_address": wallet_address,
        "rail": record.rail,
        "recipient": record.recipient,
        "token": record.token,
        "amount": str(record.amount),
        "chain_id": record.chain_id,
        "status": record.status,
        "tx_hash": record.tx_hash,
        "reason": record.reason,
        "receiving_agent_id": record.receiving_agent_id,
        "created_at": record.created_at,
    }


def _attestation_message(payload: dict) -> str:
    # Canonical, order-stable JSON so the same payload always signs to the
    # same message — a receiver re-serializing it the same way can verify.
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class SyncClient:
    def __init__(self, base_url: str, agent_id: str, wallet: Wallet, timeout: float = 10.0):
        self._base_url = base_url.rstrip("/")
        self._agent_id = agent_id
        self._wallet = wallet
        self._client = httpx.Client(timeout=timeout)

    def sync_pending(self, ledger: Ledger) -> tuple[int, int]:
        """Attempt to sync every unsynced record. Never raises — a failed
        sync just bumps that record's attempt count and stays in the
        outbox for next time. Returns (succeeded, failed)."""
        succeeded = 0
        failed = 0
        for record in ledger.pending_sync():
            if self._sync_one(record):
                ledger.mark_synced(record.id)
                succeeded += 1
            else:
                ledger.bump_sync_attempts(record.id)
                failed += 1
        return succeeded, failed

    def _sync_one(self, record: PaymentRecord) -> bool:
        payload = _canonical_payload(self._agent_id, self._wallet.address, record)
        attestation = self._wallet.sign_message(_attestation_message(payload))
        body = {**payload, "attestation_signature": attestation}

        url = f"{self._base_url}{_SYNC_PATH.format(agent_id=self._agent_id)}"
        try:
            resp = self._client.post(url, json=body)
            resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            logger.warning("payment history sync failed for record %s: %s", record.id, exc)
            return False

    def close(self) -> None:
        self._client.close()
