"""Token balance pre-check for rails where the wallet's own tokens move
(MPP, x402). Run before anything is signed, so an underfunded wallet gets a
clear reason up front instead of a failure from the server or facilitator
after the fact. The AP2/pay-button/payin flows have their own equivalent,
`ap2.check_wallet_token_balance`, since they already hold a Web3 client."""

from decimal import Decimal

from web3 import Web3

_ERC20_BALANCE_ABI = [
    {"name": "balanceOf", "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"name": "decimals", "inputs": [], "outputs": [{"type": "uint8"}], "stateMutability": "view", "type": "function"},
]


def insufficient_balance_reason(
    *,
    rpc_url: str,
    token_address: str,
    wallet_address: str,
    label: str,
    needed_units: int | None = None,
    needed_amount: Decimal | None = None,
) -> str | None:
    """Returns why the wallet can't cover the payment, or `None` if it can.
    Give the amount either as `needed_units` (the token's smallest unit,
    e.g. x402's `amount_atomic`) or as `needed_amount` in human units
    (e.g. MPP), which is converted with the token's own on-chain decimals.
    A balance that can't be read counts as a reason (fail closed), the
    same way the ERC20 rail treats it."""
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    token = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=_ERC20_BALANCE_ABI)
    wallet = Web3.to_checksum_address(wallet_address)
    try:
        balance = token.functions.balanceOf(wallet).call()
    except Exception as exc:  # noqa: BLE001 - reported as the failure reason
        return f"could not read token balance for {wallet}: {exc}"
    try:
        decimals = token.functions.decimals().call()
    except Exception:  # noqa: BLE001 - only optional when the amount is already in smallest units
        decimals = None

    if needed_units is None:
        if needed_amount is None or decimals is None:
            return f"could not work out the amount needed in {label} (token decimals unreadable)"
        needed_units = int(needed_amount * (Decimal(10) ** decimals))
    if balance >= needed_units:
        return None

    def show(units: int) -> str:
        return str(Decimal(units) / (Decimal(10) ** decimals)) if decimals is not None else f"{units} (smallest units)"

    return f"insufficient token balance: wallet {wallet} has {show(balance)} {label}, needs {show(needed_units)} — fund it first"
