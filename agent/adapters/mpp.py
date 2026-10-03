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
import ipaddress
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlparse

import httpx
from mpp import Challenge, Credential, Receipt
from mpp.client import Client
from mpp.errors import PaymentOutcomeUnknownError
from mpp.methods.tempo import ChargeIntent, TempoAccount, tempo

from agent.adapters.base import PaymentResult
from agent.retry import retry_call
from agent.signing import Wallet

FIXED_SIGNING_MESSAGE = "I am doing transaction with this account"

TEMPO_CHAIN_IDS = frozenset({4217, 42431})

# pympp answers a 402 with a fresh credential, and by default keeps doing so
# up to 3 times: a server that rejected the first payment and re-challenged
# would be paid again, up to three times over. A payment is made at most
# once; anything that comes back after it is reported, never paid again.
_ONE_PAYMENT = 1


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


def _failure_result(exc: Exception, signature: str | None, raw_request: dict | None) -> PaymentResult:
    """Maps whatever went wrong while paying to a recorded result — never a
    raised error. A refused challenge sent nothing; a lost response after
    the credential went out is `pending`; anything else is a plain failure."""
    if isinstance(exc, MppChallengeRefused):
        return PaymentResult(success=False, signature=signature, raw_request=raw_request, error=f"MPP challenge refused: {exc}")
    if isinstance(exc, PaymentOutcomeUnknownError):
        return PaymentResult(
            success=False, signature=signature, raw_request=raw_request, pending=True,
            error=(
                "outcome unknown — the payment was sent but its result could not be confirmed "
                f"({exc.cause}); check the block explorer or the receiver before retrying"
            ),
        )
    return PaymentResult(success=False, signature=signature, raw_request=raw_request, error=f"{type(exc).__name__}: {exc}")


def _problem_detail(resp) -> str | None:
    """The reason in an error body, if it has one. MPP errors are RFC 9457
    "Problem Details" (`title`, `detail`), so a failed payment can say why
    instead of only "HTTP 402". Cut short, and only text from the body."""
    body = _safe_json(resp)
    if not body:
        return None
    title, detail = body.get("title"), body.get("detail")
    parts = [p for p in (title, detail) if isinstance(p, str) and p]
    return (" — ".join(parts))[:300] if parts else None


def _receipt_tx_hash(resp, raw_response: dict | None) -> str | None:
    """The tx hash: the `Payment-Receipt` reference, else a `tx_hash` in the
    JSON body (this project's own receivers put it there)."""
    tx_hash = None
    receipt_header = resp.headers.get("payment-receipt")
    if receipt_header:
        try:
            tx_hash = Receipt.from_payment_receipt(receipt_header).reference
        except Exception:  # noqa: BLE001 - a malformed receipt falls back to the body
            tx_hash = None
    return tx_hash or (raw_response or {}).get("tx_hash") or ((raw_response or {}).get("data") or {}).get("tx_hash")


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
        async with Client(methods=[method], max_payment_retries=_ONE_PAYMENT) as client:
            return await client.post(
                url, content=content, headers={"Content-Type": "application/json"}, timeout=timeout,
            )

    try:
        resp = _run_sync(send())
    except Exception as exc:  # noqa: BLE001 - any error becomes a recorded failure, never a crashed tool call
        return _failure_result(exc, signature, body)

    raw_response = _safe_json(resp)

    if resp.status_code >= 400:
        error = (raw_response or {}).get("message") or _problem_detail(resp) or f"HTTP {resp.status_code}"
        return PaymentResult(
            success=False, signature=signature, raw_request=body, raw_response=raw_response, error=error
        )

    return PaymentResult(
        success=True, tx_hash=_receipt_tx_hash(resp, raw_response), signature=signature, raw_request=body,
        raw_response=raw_response,
    )


# ─── paying a URL directly ──────────────────────────────────────────────────
#
# `pay()` above pays another Moramba agent through that receiver's own pay
# endpoint, where the recipient, token and amount are known before the first
# request. Here the URL is anything protected by MPP: its price, token and
# payee are only known once it answers 402, so — like the x402 rail — the
# caller's spend-limit check has to run *between* `probe_url()` and
# `pay_url()`. `pay_url` then refuses any challenge that strays from what
# the probe showed, so a server that raises its price on the second request
# gets nothing.

_MAX_CONTENT_CHARS = 20_000


def refuse_unsafe_url(url: str) -> str | None:
    """Why this URL must not be requested, or None. The URL comes from a
    caller that may be an LLM, and the agent runs on a partner's own
    machine and network, so it must not be pointed at that network or at
    the machine itself (cloud metadata endpoints included): https only, and
    no localhost, private, loopback, link-local or reserved addresses. Only
    the literal host is checked — no DNS lookup is made here."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return "only https:// URLs can be paid"
    host = (parsed.hostname or "").lower()
    if not host:
        return "the URL has no host"
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return f"{host} is a local address, not a public one"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return f"{host} is a private or reserved address, not a public one"
    return None


@dataclass(frozen=True)
class MppUrlChallenge:
    """The payable parts of a Tempo charge challenge, as the URL quoted them."""

    chain_id: int | None
    currency: str  # token contract address
    recipient: str
    amount_units: int  # smallest unit of the token


@dataclass(frozen=True)
class MppUrlProbe:
    status_code: int
    payment_required: bool  # the URL answered 402
    challenge: MppUrlChallenge | None  # None if it asked for payment we can't make
    note: str | None  # why `challenge` is None
    content: dict | None  # the response, when no payment was needed


_BUSY_STATUS = (429, 502, 503, 504)


class _Busy(Exception):
    """The server answered, but said it was busy — worth asking again."""

    def __init__(self, response):
        self.response = response


def _content_of(resp) -> dict:
    """The response in a form a caller can use: text and JSON as text (cut
    off at a sensible size), anything else described, not dumped."""
    content_type = resp.headers.get("content-type", "")
    base = {"status_code": resp.status_code, "content_type": content_type}
    if content_type.startswith("text/") or "json" in content_type or not content_type:
        text = resp.text
        return {**base, "body": text[:_MAX_CONTENT_CHARS], "truncated": len(text) > _MAX_CONTENT_CHARS}
    return {**base, "body": None, "truncated": False, "note": f"{len(resp.content)} bytes of {content_type}"}


def _parse_challenges(resp) -> list[Challenge]:
    challenges = []
    for header in resp.headers.get_list("www-authenticate"):
        for part in re.split(r",\s*(?=Payment\s)", header):
            try:
                challenges.append(Challenge.from_www_authenticate(part.strip()))
            except Exception:  # noqa: BLE001 - an unreadable challenge is simply not one we can pay
                continue
    return challenges


def probe_url(
    url: str, *, method: str = "GET", body: bytes | None = None, timeout: float = 20.0
) -> MppUrlProbe:
    """Make the unpaid request and read the price, without paying. Nothing
    is signed here, and redirects are not followed."""
    headers = {"Content-Type": "application/json"} if body is not None else None

    # The unpaid request can't spend anything, so it is retried when it
    # didn't get through. A GET is repeated on any dropped connection or a
    # busy server (429/502/503/504); a POST only when the connection was
    # never made, since a POST that timed out may already have run.
    retry_on = (_Busy, httpx.TransportError) if method == "GET" else (httpx.ConnectError, httpx.ConnectTimeout)

    def attempt():
        with httpx.Client(timeout=timeout, follow_redirects=False) as http:
            response = http.request(method, url, content=body, headers=headers)
        if method == "GET" and response.status_code in _BUSY_STATUS:
            raise _Busy(response)
        return response

    try:
        resp = retry_call(attempt, retry_on=retry_on)
    except _Busy as busy:  # still busy after every attempt: read the last answer as it is
        resp = busy.response

    if resp.status_code != 402:
        return MppUrlProbe(resp.status_code, False, None, None, _content_of(resp))

    challenges = _parse_challenges(resp)
    tempo_charge = next((c for c in challenges if c.method == "tempo" and c.intent == "charge"), None)
    if tempo_charge is None:
        offered = sorted({f"{c.method}/{c.intent}" for c in challenges})
        if offered:
            note = f"this URL asks for {', '.join(offered)}; only a Tempo charge can be paid here"
        elif "payment-required" in resp.headers:
            note = "this URL asks for an x402 payment, not MPP — use pay_via_x402"
        else:
            note = "this URL answered 402 but offered no payment challenge this agent understands"
        return MppUrlProbe(402, True, None, note, None)

    request = tempo_charge.request
    details = request.get("methodDetails") or {}
    try:
        chain_id = int(details["chainId"]) if details.get("chainId") is not None else None
        challenge = MppUrlChallenge(
            chain_id=chain_id,
            currency=str(request["currency"]),
            recipient=str(request["recipient"]),
            amount_units=int(request["amount"]),
        )
    except (KeyError, TypeError, ValueError):
        return MppUrlProbe(402, True, None, "the URL's payment challenge is missing or has a malformed amount, token or recipient", None)
    return MppUrlProbe(402, True, challenge, None, None)


def pay_url(
    *,
    url: str,
    wallet: Wallet,
    expected: MppExpectation,
    method: str = "GET",
    body: bytes | None = None,
    rpc_url: str | None = None,
    timeout: float = 60.0,
) -> PaymentResult:
    """Pay `url`'s Tempo charge challenge and return what it then serves.
    `expected` is what the probe showed and the caller approved; a challenge
    on this request that differs from it (a higher price, another payee) is
    refused before anything is signed. `raw_response` carries the content."""
    headers = {"Content-Type": "application/json"} if body is not None else None
    raw_request = {"url": url, "method": method}

    async def send():
        guarded = _GuardedMethod(_build_method(wallet, expected, rpc_url), expected, body)
        async with Client(methods=[guarded], max_payment_retries=_ONE_PAYMENT) as client:
            return await client.request(method, url, content=body, headers=headers, timeout=timeout)

    try:
        resp = _run_sync(send())
    except Exception as exc:  # noqa: BLE001 - any error becomes a recorded failure, never a crashed tool call
        return _failure_result(exc, None, raw_request)

    content = _content_of(resp)
    if resp.status_code >= 400:
        reason = _problem_detail(resp)
        return PaymentResult(
            success=False, raw_request=raw_request, raw_response=content,
            error=f"HTTP {resp.status_code}: {reason}" if reason else f"HTTP {resp.status_code}",
        )
    return PaymentResult(
        success=True, tx_hash=_receipt_tx_hash(resp, None), raw_request=raw_request, raw_response=content,
    )
