"""x402 (HTTP 402 Payment Required) adapter — spec-compliant "exact"
EVM scheme (EIP-3009), built on the official `x402` SDK
(x402-foundation/x402 on PyPI) rather than hand-rolled signing, so wire
compatibility with a real x402 v2 resource server is guaranteed by the
same code the server-side ecosystem verifies against.

Verified against a real deployment in this codebase family
(weather-x402-api-ts, live at crypto.moramba.io/weather-api): its 402
challenge and payment retry already use this SDK's PAYMENT-REQUIRED /
PAYMENT-SIGNATURE / PAYMENT-RESPONSE headers — the real x402 v2 spec
names (X-PAYMENT / X-PAYMENT-RESPONSE are explicitly marked "V1 legacy"
in the SDK's own `x402.http.constants`, not a Moramba-specific rename).

Two-phase by design (`probe()` then `settle()`), unlike the MPP/ERC20
adapters: with x402 the amount, token and recipient aren't known until
the server's own 402 challenge arrives, so the caller's mandatory
spend-limit check (README section 2 — "the check happens in code, not
in a prompt") has to run *between* discovering the requirement and ever
signing anything. See engine.py's `pay_via_x402`.

Note: `x402ClientSync()` created with no config also applies the SDK's
own `spend_controls` — by default it only pays "default assets" (known
stablecoins) per network and caps the amount per payment. That's a
second, independent safety net on top of ours, not a replacement for
it; a resource demanding an unrecognized token gets rejected by the SDK
before our own limit check ever runs.
"""

from dataclasses import dataclass
from typing import Any

import httpx
from x402 import SchemeRegistration, x402ClientConfig, x402ClientSync
from x402.http import x402HTTPClientSync
from x402.mechanisms.evm.exact import ExactEvmScheme

from agent.adapters.base import PaymentResult
from agent.signing import Wallet


@dataclass(frozen=True)
class X402Requirement:
    network: str  # CAIP-2, e.g. "eip155:84532"
    asset: str  # token contract address
    pay_to: str
    amount_atomic: str  # smallest unit, as a string
    max_timeout_seconds: int
    extra: dict[str, str]


@dataclass(frozen=True)
class X402Probe:
    """Result of the unpaid first request. `requirement` is None when the
    resource didn't require payment at all, or required a scheme this
    adapter doesn't support (only "exact" is implemented)."""

    response: httpx.Response
    payment_required: Any = None
    requirement: X402Requirement | None = None


def _get_header_fn(response: httpx.Response):
    normalized = {k.upper(): v for k, v in response.headers.items()}
    return lambda name: normalized.get(name.upper())


def _safe_json(response: httpx.Response) -> dict | None:
    try:
        return response.json()
    except ValueError:
        return None


def probe(url: str, *, method: str = "GET", timeout: float = 20.0, **request_kwargs: Any) -> X402Probe:
    """Make the unpaid request. If the resource answers 402, parse — but
    do not pay — its requirement. Nothing is signed here."""
    with httpx.Client(timeout=timeout) as http:
        response = http.request(method, url, **request_kwargs)

    if response.status_code != 402:
        return X402Probe(response=response)

    http_client = x402HTTPClientSync(x402ClientSync())
    payment_required = http_client.get_payment_required_response(
        _get_header_fn(response), _safe_json(response)
    )

    accepted = next((r for r in payment_required.accepts if r.scheme == "exact"), None)
    if accepted is None:
        return X402Probe(response=response, payment_required=payment_required)

    requirement = X402Requirement(
        network=str(accepted.network),
        asset=accepted.asset,
        pay_to=accepted.pay_to,
        amount_atomic=accepted.amount,
        max_timeout_seconds=accepted.max_timeout_seconds,
        extra=dict(accepted.extra or {}),
    )
    return X402Probe(response=response, payment_required=payment_required, requirement=requirement)


def settle(
    url: str,
    *,
    wallet: Wallet,
    probe_result: X402Probe,
    method: str = "GET",
    timeout: float = 20.0,
    **request_kwargs: Any,
) -> PaymentResult:
    """Sign and pay a requirement already discovered by `probe()`, then
    retry the original request. Only call this after the caller's own
    limit check has approved `probe_result.requirement` — nothing here
    re-checks it."""
    if probe_result.requirement is None:
        return PaymentResult(success=False, error="no payable ('exact'-scheme) x402 requirement to settle")

    requirement = probe_result.requirement
    # The SDK's own default is a hard cap of $1 per payment, and only its
    # list of well-known tokens is payable — whatever the agent's own limits
    # say. That refused a $2 payment the agent had approved, and made any
    # other token (pathUSD, say) impossible to pay at all, both with an
    # opaque NoMatchingRequirementsError. The caller's limit check is what
    # decides, so the SDK is told exactly what was approved: this token, on
    # this network, up to this amount, and nothing else. It stays a second
    # net — it can never pay more than the approved amount.
    client = x402ClientSync.from_config(
        x402ClientConfig(
            schemes=[SchemeRegistration(network=requirement.network, client=ExactEvmScheme(signer=wallet._account))],
            spend_controls={
                "allowed_assets": [
                    {
                        "network": requirement.network,
                        "asset": requirement.asset,
                        "max_amount_per_payment": requirement.amount_atomic,
                    }
                ]
            },
        )
    )
    http_client = x402HTTPClientSync(client)

    try:
        payment_payload = http_client.create_payment_payload(probe_result.payment_required)
        payment_headers = http_client.encode_payment_signature_header(payment_payload)
    except Exception as exc:  # noqa: BLE001 - surfaced as a failed PaymentResult
        return PaymentResult(success=False, error=f"could not build x402 payment: {exc}")

    original_headers = dict(request_kwargs.pop("headers", {}) or {})
    merged_headers = {**original_headers, **payment_headers}

    with httpx.Client(timeout=timeout) as http:
        try:
            paid_response = http.request(method, url, headers=merged_headers, **request_kwargs)
        except httpx.HTTPError as exc:
            return PaymentResult(success=False, error=str(exc))

    tx_hash = None
    try:
        settle_response = http_client.get_payment_settle_response(_get_header_fn(paid_response))
        tx_hash = settle_response.transaction
    except ValueError:
        pass

    if paid_response.status_code >= 400:
        return PaymentResult(
            success=False, tx_hash=tx_hash, error=f"HTTP {paid_response.status_code}",
            raw_response=_safe_json(paid_response),
        )

    return PaymentResult(
        success=True,
        tx_hash=tx_hash,
        raw_request=payment_payload.model_dump(by_alias=True),
        raw_response=_safe_json(paid_response),
    )
