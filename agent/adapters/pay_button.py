"""Moramba Pay Button rail — pay an existing button directly by its
`button_id`, with no ACP checkout session or AP2 mandate involved.

Verified live against a running server (2026-09-29), not just read from
source:

    POST /morambacrypto/public/payin/create/by/button_id/{button_id}
    {"network": "tempo_testnet", "token_address": "0x..."}
    -> {"success": true, "data": {"id": "<payin_id>", "payout_destination_address": "0x...", "amount": "1000000", ...}}

is a clean, public, JSON endpoint built specifically for this — tagged
`user_type: "aiagent"` server-side (`payin_user_controller.rs`). The
resulting payin_id was then fed straight into
`GET payrequest/init/{payin_id}/address/{wallet}` and worked immediately
— confirming live that this is the exact same generic settlement
pipeline the AP2 rail already implements. This module reuses that
pipeline directly from `adapters.ap2` rather than duplicating it (worth
factoring into its own shared module if a third rail ever needs it too
— see README section 8, item 8).

Discovering "what network/token does this button support" used to
require scraping the button's HTML payment page — that gap is now
closed on the backend: `GET /morambacrypto/public/pay-button/{button_id}/methods`
(new endpoint, `pay_button_methods_controller.rs`) returns the same data
as clean JSON, with none of the scraped page's two side effects (it
doesn't create a throwaway payin row on every call, and isn't gated by
`max_uses`/`multi_use` — discovering what a button supports shouldn't
require it still having uses left).
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from web3 import Web3

from agent.adapters.ap2 import (
    Ap2Error,
    _BUILDER_BY_FLOW,
    _public_api_request,
    detect_flow,
    fetch_payrequest_init,
    poll_payment_status,
    submit_relay_payment,
)
from agent.signing import Wallet


@dataclass(frozen=True)
class ButtonPayoutMethod:
    payout_destination_id: str
    network: str
    token_name: str
    token_address: str
    to_wallet_address: str
    decimals: int
    amount_with_decimal: str | None  # only meaningful for a fixed-amount button


@dataclass(frozen=True)
class ButtonDetails:
    button_id: str
    fixed_amount: bool
    payout_methods: list[ButtonPayoutMethod]


def fetch_button_details(base_url: str, button_id: str, timeout: float = 20.0) -> ButtonDetails:
    """`GET .../public/pay-button/{button_id}/methods` — a clean, public,
    read-only JSON endpoint. `_public_api_request` already surfaces the
    server's actual error message (e.g. "Pay button max uses reached")
    on a non-2xx response, matching the standard {success, message,
    data} envelope every other endpoint in this API uses."""
    data = _public_api_request(base_url, f"/morambacrypto/public/pay-button/{button_id}/methods", "GET", None, timeout)
    methods = [
        ButtonPayoutMethod(
            payout_destination_id=m["payout_destination_id"],
            network=m["network"],
            token_name=m["token_name"],
            token_address=m["token_address"],
            to_wallet_address=m["to_wallet_address"],
            decimals=m["decimals"],
            amount_with_decimal=m.get("amount_with_decimal"),
        )
        for m in data["methods"]
    ]
    return ButtonDetails(button_id=button_id, fixed_amount=bool(data.get("fixed_amount")), payout_methods=methods)


def create_payin_by_button_id(
    base_url: str,
    button_id: str,
    network: str,
    token_address: str,
    amount: Decimal | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """`POST .../payin/create/by/button_id/{button_id}`. `amount` is only
    sent for a variable-amount button — a fixed-amount button ignores
    any amount sent and uses its own configured one."""
    body: dict[str, Any] = {"network": network, "token_address": token_address}
    if amount is not None:
        body["amount"] = str(amount)
    return _public_api_request(
        base_url, f"/morambacrypto/public/payin/create/by/button_id/{button_id}", "POST", body, timeout
    )


@dataclass(frozen=True)
class ButtonPaymentPlan:
    """What paying this button would cost, resolved BEFORE creating any
    payin — so a limit-check rejection never causes the side effect of
    actually creating one."""

    method: ButtonPayoutMethod
    amount: Decimal  # human units (already divided by decimals)
    fixed_amount: bool


def resolve_payment_plan(
    base_url: str,
    button_id: str,
    *,
    network: str | None = None,
    amount: Decimal | None = None,
    preferred_tokens: list[str] | None = None,
    preferred_network_hint: str | None = None,
    timeout: float = 20.0,
) -> ButtonPaymentPlan:
    """Scrapes the button's details and picks a payout method, resolving
    the amount to charge without ever creating a payin.

    Method selection: `network` wins outright if given (unambiguous — a
    button has at most one method per network). Otherwise, prefer a
    method whose token is in `preferred_tokens` (case-insensitive) —
    normally the paying agent's own `allowed_tokens`, so a multi-token
    button doesn't get paid in whichever token happens to be listed
    first if that one isn't actually one this agent can spend (found
    live, 2026-10-01: a button accepting both USDC and pathUSD picked
    USDC — index 0 — for an agent only configured for pathUSD, and was
    rejected, even though the button also had a pathUSD method).

    When more than one method matches `preferred_tokens` (the same token
    offered on more than one network — e.g. pathUSD on both testnet and
    mainnet), `preferred_network_hint` (a substring like `"testnet"` or
    `"mainnet"`, derived from the agent's own configured chain) breaks
    the tie by matching it against each candidate's `network` string —
    `ButtonPayoutMethod` doesn't carry a chain id to match on directly,
    only this network slug, so substring matching is what's available.
    Without a match on either, falls back to the first configured
    method, same as before any of this existed."""
    details = fetch_button_details(base_url, button_id, timeout=timeout)
    if not details.payout_methods:
        raise Ap2Error(f"button {button_id} has no payout methods configured")

    method = details.payout_methods[0]
    if network is not None:
        matching = next((m for m in details.payout_methods if m.network == network), None)
        if matching is None:
            raise Ap2Error(f"button {button_id} has no payout method for network {network!r}")
        method = matching
    elif preferred_tokens:
        preferred_lower = {t.lower() for t in preferred_tokens}
        candidates = [m for m in details.payout_methods if m.token_name.lower() in preferred_lower]
        if candidates:
            method = candidates[0]
            if preferred_network_hint and len(candidates) > 1:
                network_matched = next(
                    (m for m in candidates if preferred_network_hint.lower() in m.network.lower()), None
                )
                if network_matched is not None:
                    method = network_matched

    if details.fixed_amount:
        if not method.amount_with_decimal:
            raise Ap2Error(f"button {button_id} is fixed-amount but its payout method has no amount configured")
        resolved_amount = Decimal(method.amount_with_decimal) / (Decimal(10) ** method.decimals)
    else:
        if amount is None:
            raise Ap2Error(f"button {button_id} is variable-amount — an amount is required")
        resolved_amount = amount

    return ButtonPaymentPlan(method=method, amount=resolved_amount, fixed_amount=details.fixed_amount)


@dataclass(frozen=True)
class ButtonSettlementResult:
    success: bool
    tx_hash: str | None
    flow: str | None
    payin_id: str | None
    error: str | None = None


def pay_button(base_url: str, button_id: str, plan: ButtonPaymentPlan, wallet: Wallet) -> ButtonSettlementResult:
    """Everything after the caller's own limit check has approved `plan`:
    create the real payin, then settle it through the exact pipeline the
    AP2 rail already implements."""
    payin_id: str | None = None
    flow: str | None = None
    try:
        payin = create_payin_by_button_id(
            base_url,
            button_id,
            plan.method.network,
            plan.method.token_address,
            amount=None if plan.fixed_amount else plan.amount,
        )
        payin_id = payin["id"]

        init = fetch_payrequest_init(base_url, wallet.address, payin_id)
        w3 = Web3(Web3.HTTPProvider(init.rpc))
        flow = detect_flow(w3, init.token_address)
        pay_body = _BUILDER_BY_FLOW[flow](w3, wallet._account, init)

        submit_relay_payment(base_url, payin_id, init.transaction_id, flow, pay_body)
        final_status = poll_payment_status(base_url, init.transaction_id)
        if final_status.get("status") != "success":
            return ButtonSettlementResult(
                success=False, tx_hash=final_status.get("tx_hash"), flow=flow, payin_id=payin_id,
                error=final_status.get("error_message") or "settlement failed on-chain",
            )
        return ButtonSettlementResult(success=True, tx_hash=final_status.get("tx_hash"), flow=flow, payin_id=payin_id)
    except Ap2Error as exc:
        # Preserve whatever we already knew (a payin may well have been
        # created, and a flow chosen, before this failed) — discarding
        # them here would hide exactly the detail needed to debug a
        # partial failure, e.g. a real approve() already sent on-chain
        # for a payin that never got to settle.
        return ButtonSettlementResult(success=False, tx_hash=None, flow=flow, payin_id=payin_id, error=str(exc))
