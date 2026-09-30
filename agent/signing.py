"""Wallet loading and EIP-191 message signing.

The private key is loaded once, here, from Settings (which itself reads
only from the local .env). Nothing in this module ever logs the key or
returns it — callers get a `Wallet` with an `address` and a `sign()`
method, never the raw key material.
"""

from dataclasses import dataclass

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount


@dataclass(frozen=True)
class Wallet:
    address: str
    _account: LocalAccount

    def sign_message(self, message: str) -> str:
        """EIP-191 personal_sign — same scheme mppx-agent-endpoint-server-ts
        verifies with viem's `recoverMessageAddress` (≈ ethers'
        `verifyMessage`)."""
        signable = encode_defunct(text=message)
        signed = self._account.sign_message(signable)
        # HexBytes.hex() doesn't reliably include the "0x" prefix across
        # versions; the receiver (e.g. viem's recoverMessageAddress) needs
        # a properly-prefixed hex string.
        raw = signed.signature.hex()
        return raw if raw.startswith("0x") else f"0x{raw}"


def load_wallet(private_key: str) -> Wallet:
    account: LocalAccount = Account.from_key(private_key)
    return Wallet(address=account.address, _account=account)
