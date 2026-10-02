"""Pay an existing payin directly by its `payin_id` — no button, no ACP
checkout session, no mandate. Reuses the exact same generic settlement
pipeline AP2/pay_button already implement (`fetch_payrequest_init`,
`detect_flow`, the capability-based signing builders,
`submit_relay_payment`, `poll_payment_status`) — the only difference
from those rails is where the payin_id comes from: here the caller
already has one (created by Moramba's own dashboard/backend, by a
different system entirely, or by a human) rather than this agent
creating it itself from a button or checkout session.
"""

from dataclasses import dataclass
from decimal import Decimal

from web3 import Web3

from agent.adapters.ap2 import (
    _BUILDER_BY_FLOW,
    Ap2Error,
    PayInit,
    check_wallet_token_balance,
    classify_settlement_exception,
    detect_flow,
    fetch_payrequest_init,
    poll_payment_status,
    submit_relay_payment,
)
from agent.signing import Wallet

_ERC20_DECIMALS_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
]


@dataclass(frozen=True)
class PayinPlan:
    """What paying this payin_id would cost, resolved BEFORE any limit
    check or signing — mirrors ButtonPaymentPlan's role for the pay-
    button rail."""

    init: PayInit
    amount: Decimal  # human units
    decimals: int


@dataclass(frozen=True)
class PayinSettlementResult:
    success: bool
    tx_hash: str | None
    flow: str | None
    error: str | None = None
    pending: bool = False  # submitted, but its outcome couldn't be confirmed


def resolve_payin(base_url: str, payin_id: str, wallet: Wallet) -> PayinPlan:
    """Read-only — `fetch_payrequest_init` has no side effect, so a
    rejection from the caller's own limit check afterward never follows
    an action that already changed anything server-side (this rail never
    creates the payin itself, so there's no earlier side-effecting step
    to guard against either)."""
    init = fetch_payrequest_init(base_url, wallet.address, payin_id)
    w3 = Web3(Web3.HTTPProvider(init.rpc))
    contract = w3.eth.contract(address=Web3.to_checksum_address(init.token_address), abi=_ERC20_DECIMALS_ABI)
    try:
        decimals = contract.functions.decimals().call()
    except Exception as exc:  # noqa: BLE001 - surfaced to caller as a failed plan
        raise Ap2Error(f"could not read token decimals for payin {payin_id}: {exc}") from exc
    amount = Decimal(init.amount) / (Decimal(10) ** decimals)
    return PayinPlan(init=init, amount=amount, decimals=decimals)


def pay_payin(base_url: str, payin_id: str, plan: PayinPlan, wallet: Wallet) -> PayinSettlementResult:
    """Everything after the caller's own limit check has approved `plan`:
    sign and settle through the exact pipeline AP2/pay_button already
    use."""
    flow: str | None = None
    submitted = False
    try:
        init = plan.init
        w3 = Web3(Web3.HTTPProvider(init.rpc))
        check_wallet_token_balance(w3, wallet.address, init)
        flow = detect_flow(w3, init.token_address)
        pay_body = _BUILDER_BY_FLOW[flow](w3, wallet._account, init)

        submit_relay_payment(base_url, payin_id, init.transaction_id, flow, pay_body)
        submitted = True
        final_status = poll_payment_status(base_url, init.transaction_id)
        submitted = False  # an outcome is known from here on, whatever it is
        if final_status.get("status") != "success":
            return PayinSettlementResult(
                success=False, tx_hash=final_status.get("tx_hash"), flow=flow,
                error=final_status.get("error_message") or "settlement failed on-chain",
            )
        return PayinSettlementResult(success=True, tx_hash=final_status.get("tx_hash"), flow=flow)
    except Exception as exc:  # noqa: BLE001 - any error here must become a recorded result, not a crashed tool call
        # Preserve the flow we already knew, same reasoning as
        # pay_button.py's equivalent: a real approve() may already be
        # on-chain for a permit2 flow that then failed to relay.
        pending, message = classify_settlement_exception(exc, submitted=submitted)
        return PayinSettlementResult(success=False, tx_hash=None, flow=flow, error=message, pending=pending)
