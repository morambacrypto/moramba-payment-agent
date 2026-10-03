"""MPP (Machine Payments Protocol) adapter — built on the official `pympp`
SDK (github.com/tempoxyz/pympp), not hand-rolled, so the wire format is
exactly what a real MPP server verifies. MPP here runs on **Tempo only**
(mainnet 4217, testnet 42431).

Real flow, against `mppx-agent-endpoint-server-ts`'s
`POST /agent-api/payout/agent/:receiver_agent_id/pay` (`:agent_id` in the
URL is always the RECEIVER's id, never the payer's):

1. POST the body below with no credential. The server runs its own checks
   and answers `402` with a Challenge in `WWW-Authenticate` — amount,
   currency (the token's contract address), recipient, chain id.
2. The client signs a TIP-20 `transfer` transaction for exactly that
   challenge and retries the same POST with it as the `Authorization`
   credential. pympp does steps 1-2 and the retry itself.
3. The server broadcasts the transaction (settling in about half a
   second), verifies it, and returns a Receipt (`Payment-Receipt`).

pympp pays whatever a challenge demands, and its event hooks can't veto a
payment, so every challenge is checked against what the caller already
approved — chain, token contract, recipient, a ceiling on the amount, and
the body digest when the server sets one — *before* anything is signed
(`_GuardedMethod` below). A refused challenge signs and sends nothing.

The request body carries `payment_via`/`payment_to` ("agent" or "wallet"),
whichever of `payout_agent_id`/`to_address` applies, `amount` in human
units, `token` as the token's contract ADDRESS (the server matches it
against its allow-list of addresses and uses it as the challenge currency
— a token *name* is rejected), and the EIP-191 `signature` of a fixed
message that identifies the paying wallet.
"""

import asyncio
import base64
import concurrent.futures
import hashlib
import hmac
import json
from dataclasses import dataclass
from decimal import Decimal

from mpp import Challenge, Credential, Receipt
from mpp.client import Client
from mpp.errors import PaymentOutcomeUnknownError
from mpp.methods.tempo import ChargeIntent, TempoAccount, tempo

from agent.adapters.base import PaymentResult
from agent.signing import Wallet

FIXED_SIGNING_MESSAGE = "I am doing transaction with this account"

TEMPO_CHAIN_IDS = frozenset({4217, 42431})


@dataclass(frozen=True)
class MppExpectation:
    """What the caller already approved — the only thing a challenge may
    ask this wallet to pay."""

    chain_id: int
    token_address: str
    max_units: int  # ceiling, in the token's smallest unit
    recipient: str


class MppChallengeRefused(Exception):
    """A challenge didn't match what was approved. Deliberately not an
    `InvalidChallengeError`: pympp quietly returns the 402 for those, and
    this one has to reach the caller with its reason."""


def encode_body(body: dict) -> bytes:
    """The exact request bytes that get sent — fixed here, not left to the
    HTTP library, so a challenge's body digest can be checked against them."""
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def body_digest(content: bytes) -> str:
    """`sha-256=<base64>`, the format a Challenge's `digest` uses."""
    return "sha-256=" + base64.b64encode(hashlib.sha256(content).digest()).decode("ascii")


def validate_challenge(challenge: Challenge, expected: MppExpectation, body: bytes | None = None) -> None:
    """Raises `MppChallengeRefused` unless `challenge` asks for exactly
    what was approved. Run before anything is signed. A server may bind a
    challenge to the request body with a `digest` (so the body can't be
    swapped after the price is quoted); when it does, it has to match the
    body actually being sent."""
    if challenge.method != "tempo" or challenge.intent != "charge":
        raise MppChallengeRefused(f"unsupported challenge {challenge.method}/{challenge.intent} — MPP here is Tempo charge only")
    if expected.chain_id not in TEMPO_CHAIN_IDS:
        raise MppChallengeRefused(f"MPP only runs on Tempo (chains {sorted(TEMPO_CHAIN_IDS)}), not chain {expected.chain_id}")

    request = challenge.request
    details = request.get("methodDetails") or {}
    try:
        challenge_chain = int(details.get("chainId"))
    except (TypeError, ValueError):
        raise MppChallengeRefused("the challenge does not name a chain") from None
    if challenge_chain != expected.chain_id:
        raise MppChallengeRefused(f"challenge is for chain {challenge_chain}, expected {expected.chain_id}")

    if details.get("splits"):
        raise MppChallengeRefused("the challenge splits the payment across several recipients")

    if challenge.digest is not None and body is not None:
        if not hmac.compare_digest(body_digest(body), challenge.digest):
            raise MppChallengeRefused("the challenge's body digest does not match the request that was sent")

    if str(request.get("currency", "")).lower() != expected.token_address.lower():
        raise MppChallengeRefused(f"challenge asks for token {request.get('currency')}, expected {expected.token_address}")
    if str(request.get("recipient", "")).lower() != expected.recipient.lower():
        raise MppChallengeRefused(f"challenge pays {request.get('recipient')}, expected {expected.recipient}")

    try:
        asked = int(request["amount"])
    except (KeyError, TypeError, ValueError):
        raise MppChallengeRefused(f"challenge amount {request.get('amount')!r} is not a whole number of smallest units") from None
    if asked > expected.max_units:
        raise MppChallengeRefused(f"challenge asks for {asked} (smallest units), more than the {expected.max_units} approved")


class _GuardedMethod:
    """Wraps pympp's Tempo method so `validate_challenge` runs right before
    a credential — i.e. a signed transfer — is created."""

    def __init__(self, inner, expected: MppExpectation, body: bytes | None = None):
        self._inner = inner
        self._expected = expected
        self._body = body
        self.name = inner.name
        self.intents = getattr(inner, "intents", ("charge",))

    def __getattr__(self, item):
        return getattr(self._inner, item)

    async def create_credential(self, challenge: Challenge) -> Credential:
        validate_challenge(challenge, self._expected, self._body)
        return await self._inner.create_credential(challenge)


def _build_method(wallet: Wallet, expected: MppExpectation, rpc_url: str | None):
    account = TempoAccount.from_key("0x" + wallet._account.key.hex().removeprefix("0x"))
    return tempo(account=account, chain_id=expected.chain_id, rpc_url=rpc_url, intents={"charge": ChargeIntent()})


def _run_sync(coro):
    """Run a coroutine to completion from sync code. The engine is sync
    (FastAPI sync routes and MCP tools run in worker threads with no event
    loop); if a loop is somehow already running here, use a fresh thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _safe_json(response) -> dict | None:
    try:
        parsed = response.json()
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def pay(
    *,
    receiver_base_url: str,
    receiver_agent_id: str,
    wallet: Wallet,
    amount: Decimal,
    expected: MppExpectation,
    payment_via: str = "agent",
    payment_to: str = "agent",
    payout_agent_id: str | None = None,
    to_address: str | None = None,
    rpc_url: str | None = None,
    timeout: float = 60.0,
) -> PaymentResult:
    if payment_to == "agent" and not payout_agent_id:
        return PaymentResult(success=False, error="payout_agent_id is required when payment_to == 'agent'")
    if payment_to == "address" and not to_address:
        return PaymentResult(success=False, error="to_address is required when payment_to == 'address'")

    signature = wallet.sign_message(FIXED_SIGNING_MESSAGE)

    body = {
        "payment_via": payment_via,
        "payment_to": payment_to,
        "payout_agent_id": payout_agent_id,
        "to_address": to_address,
        "amount": str(amount),
        "token": expected.token_address,
        "signature": signature,
    }
    url = f"{receiver_base_url.rstrip('/')}/agent-api/payout/agent/{receiver_agent_id}/pay"

    content = encode_body(body)

    async def send():
        method = _GuardedMethod(_build_method(wallet, expected, rpc_url), expected, content)
        async with Client(methods=[method]) as client:
            return await client.post(
                url, content=content, headers={"Content-Type": "application/json"}, timeout=timeout,
            )

    try:
        resp = _run_sync(send())
    except MppChallengeRefused as exc:
        return PaymentResult(success=False, signature=signature, raw_request=body, error=f"MPP challenge refused: {exc}")
    except PaymentOutcomeUnknownError as exc:
        return PaymentResult(
            success=False, signature=signature, raw_request=body, pending=True,
            error=(
                "outcome unknown — the payment was sent but its result could not be confirmed "
                f"({exc.cause}); check the block explorer or the receiver before retrying"
            ),
        )
    except Exception as exc:  # noqa: BLE001 - any other error becomes a recorded failure, never a crashed tool call
        return PaymentResult(success=False, signature=signature, raw_request=body, error=f"{type(exc).__name__}: {exc}")

    raw_response = _safe_json(resp)

    if resp.status_code >= 400:
        error = (raw_response or {}).get("message", f"HTTP {resp.status_code}")
        return PaymentResult(
            success=False, signature=signature, raw_request=body, raw_response=raw_response, error=error
        )

    tx_hash = None
    receipt_header = resp.headers.get("payment-receipt")
    if receipt_header:
        try:
            tx_hash = Receipt.from_payment_receipt(receipt_header).reference
        except Exception:  # noqa: BLE001 - a malformed receipt falls back to the body below
            tx_hash = None
    tx_hash = tx_hash or (raw_response or {}).get("tx_hash") or ((raw_response or {}).get("data") or {}).get("tx_hash")
    return PaymentResult(
        success=True, tx_hash=tx_hash, signature=signature, raw_request=body, raw_response=raw_response
    )
