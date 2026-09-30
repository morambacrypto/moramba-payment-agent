"""MPP adapter — matches mppx-agent-endpoint-server-ts's actual wire
protocol exactly, verified by reading that project's own source (its
README is out of date and documents a different route shape).

Real flow (src/routes/payRoutes.ts -> PayController.pay in that repo):

1. Sign the fixed string "I am doing transaction with this account" with
   the paying wallet (EIP-191 personal_sign).
2. POST to `/agent-api/payout/agent/:agent_id/pay` (or the equivalent
   `/agent-api/receiver/agent/:agent_id/pay` — both hit the same
   handler). `:agent_id` in the URL is always the RECEIVER's agent id,
   never the payer's.
3. Body carries `payment_via`/`payment_to` ("agent" or "wallet"),
   whichever of `payout_agent_id`/`to_address` applies, plus
   `amount`, `token`, and the `signature` from step 1.

The receiver resolves the recipient, checks its own limits, and — when
`payment_via == "agent"` — pulls the payer's own agent record from
Moramba's public agent API and re-checks those limits too. This
adapter's job ends at "send a validly-signed request"; the receiver-side
check is out of scope here (see README section 4).
"""

from decimal import Decimal

import httpx

from agent.adapters.base import PaymentResult
from agent.signing import Wallet

FIXED_SIGNING_MESSAGE = "I am doing transaction with this account"


def pay(
    *,
    receiver_base_url: str,
    receiver_agent_id: str,
    wallet: Wallet,
    amount: Decimal,
    token: str,
    payment_via: str = "agent",
    payment_to: str = "agent",
    payout_agent_id: str | None = None,
    to_address: str | None = None,
    timeout: float = 15.0,
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
        "token": token,
        "signature": signature,
    }
    url = f"{receiver_base_url.rstrip('/')}/agent-api/payout/agent/{receiver_agent_id}/pay"

    try:
        resp = httpx.post(url, json=body, timeout=timeout)
    except httpx.HTTPError as exc:
        return PaymentResult(success=False, signature=signature, raw_request=body, error=str(exc))

    raw_response = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else None

    if resp.status_code >= 400:
        error = (raw_response or {}).get("message", f"HTTP {resp.status_code}")
        return PaymentResult(
            success=False, signature=signature, raw_request=body, raw_response=raw_response, error=error
        )

    tx_hash = (raw_response or {}).get("tx_hash") or (raw_response or {}).get("data", {}).get("tx_hash")
    return PaymentResult(
        success=True, tx_hash=tx_hash, signature=signature, raw_request=body, raw_response=raw_response
    )
