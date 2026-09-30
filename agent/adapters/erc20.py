"""Plain ERC20 transfer — the fallback rail with no counterparty
protocol. Still goes through the same signing wallet and is still
subject to the same limit check as every other rail (see engine.py);
this module only builds, signs and broadcasts the transaction."""

from decimal import Decimal

from eth_account.signers.local import LocalAccount
from web3 import Web3

from agent.adapters.base import PaymentResult

_ERC20_ABI = [
    {
        "constant": False,
        "inputs": [{"name": "to", "type": "address"}, {"name": "value", "type": "uint256"}],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
]


def pay(
    *,
    rpc_url: str,
    chain_id: int,
    account: LocalAccount,
    token_contract_address: str,
    to_address: str,
    amount: Decimal,
    gas_limit: int = 100_000,
) -> PaymentResult:
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    contract = w3.eth.contract(address=Web3.to_checksum_address(token_contract_address), abi=_ERC20_ABI)

    try:
        decimals = contract.functions.decimals().call()
    except Exception as exc:  # noqa: BLE001 - surfaced to caller as a failed PaymentResult
        return PaymentResult(success=False, error=f"could not read token decimals: {exc}")

    amount_units = int(amount * (10**decimals))

    try:
        nonce = w3.eth.get_transaction_count(account.address)
        tx = contract.functions.transfer(Web3.to_checksum_address(to_address), amount_units).build_transaction(
            {
                "chainId": chain_id,
                "from": account.address,
                "nonce": nonce,
                "gas": gas_limit,
                "gasPrice": w3.eth.gas_price,
            }
        )
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    except Exception as exc:  # noqa: BLE001
        return PaymentResult(success=False, raw_request={"to": to_address, "amount": str(amount)}, error=str(exc))

    return PaymentResult(
        success=True,
        tx_hash=tx_hash.hex(),
        raw_request={"to": to_address, "amount": str(amount), "token_contract": token_contract_address},
    )
