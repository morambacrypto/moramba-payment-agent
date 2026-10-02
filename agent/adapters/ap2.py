"""AP2 (Agent Payments Protocol) — Autonomous mode, matching Moramba's
own real, live implementation exactly.

Ported line-for-line from `moramba-acp-mcp/test-ap2-autonomous-settle.mjs`
in the moramba-crypto-api repo (the actual PR that added "a real on-chain
settlement script for the Autonomous flow") — verified against the
server's own `ap2_service.rs` for the mandate message format and against
`moramba-relay-pay/src/lib/{tokenCapabilities,signing}.ts` (the browser
version of the same on-chain signing this script replicates without a
browser). Two different signing schemes are involved and must not be
confused:

1. The AP2 "closed mandate" itself — a plain EIP-191 personal_sign over
   a fixed-format string, authorizing the checkout in principle. This is
   NOT proof of payment; the server checks it (signature + the agent's
   own per_transaction_limit/maximum_payout_limit) before any funds move.
2. The actual on-chain settlement — an EIP-712 typed-data signature over
   one of four possible payloads (authorization / permit / permit2 /
   plain), chosen by which capability the token contract actually
   supports. This is the real payment.

Two-phase like x402, for the same reason: the checkout amount isn't
known until `create_checkout_session()` returns, so the caller's own
spend-limit check has to run between session creation and ever calling
`authorize_autonomous` (nothing is signed before that point).
"""

import hashlib
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
from eth_account.messages import encode_typed_data
from eth_account.signers.local import LocalAccount
from web3 import Web3

from agent.signing import Wallet

DEADLINE_WINDOW_SECONDS = 30 * 60
PERMIT2_ADDRESS = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
# EIP-1967 proxy implementation storage slot — bytes32(uint256(keccak256(
# "eip1967.proxy.implementation")) - 1), copied verbatim from the
# reference script rather than recomputed, to avoid a transcription error.
EIP1967_IMPLEMENTATION_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"

_ERC20_MINI_ABI = [
    {"name": "name", "inputs": [], "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"},
    {"name": "decimals", "inputs": [], "outputs": [{"type": "uint8"}], "stateMutability": "view", "type": "function"},
    {"name": "balanceOf", "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"name": "nonces", "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"name": "allowance", "inputs": [{"type": "address"}, {"type": "address"}], "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"name": "approve", "inputs": [{"type": "address"}, {"type": "uint256"}], "outputs": [{"type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {
        "name": "eip712Domain", "inputs": [],
        "outputs": [
            {"type": "bytes1"}, {"type": "string"}, {"type": "string"}, {"type": "uint256"},
            {"type": "address"}, {"type": "bytes32"}, {"type": "uint256[]"},
        ],
        "stateMutability": "view", "type": "function",
    },
    {"name": "DOMAIN_SEPARATOR", "inputs": [], "outputs": [{"type": "bytes32"}], "stateMutability": "view", "type": "function"},
]


def _hex0x(value: bytes | Any) -> str:
    raw = bytes(value).hex()
    return raw if raw.startswith("0x") else f"0x{raw}"


def _uint_to_bytes32_hex(value: int) -> str:
    return "0x" + format(value, "064x")


def _selector(signature: str) -> str:
    """4-byte function selector, as a bare lowercase hex string (no 0x) —
    matches ethers' `id(sig).slice(2, 10)`."""
    return Web3.keccak(text=signature).hex().removeprefix("0x")[:8].lower()


TRANSFER_WITH_AUTHORIZATION_SELECTOR = _selector(
    "transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)"
)
PERMIT_SELECTOR = _selector("permit(address,address,uint256,uint256,uint8,bytes32,bytes32)")


# ─── ACP / public API HTTP clients ──────────────────────────────────────────


@dataclass(frozen=True)
class CheckoutSession:
    session_id: str
    amount: str  # minor units, at `decimals` decimal places — NOT always 2
    currency: str  # the settlement token's own name (e.g. "PATHUSD"), uppercased — not an ISO fiat code
    decimals: int
    raw: dict[str, Any]


class Ap2Error(Exception):
    pass


def _acp_request(base_url: str, api_key: str, path: str, method: str = "POST", body: dict | None = None, timeout: float = 20.0) -> dict:
    resp = httpx.request(
        method,
        f"{base_url.rstrip('/')}/acp{path}",
        json=body,
        headers={"Authorization": f"Bearer {api_key}", "Idempotency-Key": secrets.token_hex(16)},
        timeout=timeout,
    )
    data = {}
    try:
        data = resp.json()
    except ValueError:
        pass
    if resp.status_code >= 300:
        raise Ap2Error(f"{method} /acp{path} -> {resp.status_code}: {data}")
    return data


def _public_api_request(base_url: str, path: str, method: str = "GET", body: dict | None = None, timeout: float = 20.0) -> dict:
    """The payrequest/* endpoints live under /api/v2 (not /acp), are
    unauthenticated, and wrap responses as {success, message, data}."""
    resp = httpx.request(method, f"{base_url.rstrip('/')}/api/v2{path}", json=body, timeout=timeout)
    try:
        parsed = resp.json()
    except ValueError:
        parsed = None
    if resp.status_code >= 400:
        message = (parsed or {}).get("message") if parsed else resp.text[:300]
        raise Ap2Error(f"{method} /api/v2{path} -> {resp.status_code}: {message}")
    if not parsed or "data" not in parsed:
        raise Ap2Error(f"{method} /api/v2{path} -> 200 but no 'data' field")
    return parsed["data"]


def _resolve_session_decimals(data: dict[str, Any], currency: str) -> int:
    """`currency` is the settlement token's own name (`currency_for` in
    acp_checkout_service.rs, Moramba's side), not an ISO fiat code — a
    stablecoin's own decimals (6 for pathUSD, matching `erc20.py` and
    every other rail's on-chain reads) govern its minor units, not a
    fixed 2 the way real currency cents would. `payment_options[]` is
    where the session reports each accepted token's own `decimals`;
    matched by `selected_payout_destination_id` when present (multiple
    options can share a token name across networks), else by name.

    Found live (2026-10-02): hardcoding 2 here turned a real 1.5 pathUSD
    checkout (1,500,000 minor units at 6 decimals) into 15,000 — a
    10,000x inflation that then looked like a spend-limit violation for
    an otherwise completely ordinary coffee purchase."""
    options = data.get("payment_options") or []
    selected_id = data.get("selected_payout_destination_id")
    if selected_id is not None:
        matching = next((o for o in options if o.get("payout_destination_id") == selected_id), None)
        if matching is not None:
            return int(matching["decimals"])
    matching = next((o for o in options if str(o.get("token", "")).upper() == currency), None)
    if matching is not None:
        return int(matching["decimals"])
    # No payment_options at all (e.g. a non-Moramba-aware ACP backend) —
    # fall back to the one convention the ACP spec itself actually
    # documents: minor units as real currency cents.
    return 2


def create_checkout_session(
    base_url: str,
    api_key: str,
    items: list[dict[str, Any]],
    *,
    buyer: dict[str, Any] | None = None,
    delivery_address: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> CheckoutSession:
    body: dict[str, Any] = {"items": items}
    if buyer is not None:
        body["buyer"] = buyer
    if delivery_address is not None:
        body["delivery_address"] = delivery_address
    data = _acp_request(base_url, api_key, "/checkout_sessions", "POST", body, timeout)
    totals = data.get("totals") or [{}]
    currency = data.get("currency", "USD")
    return CheckoutSession(
        session_id=data["id"],
        amount=str(totals[0].get("amount", 0)),
        currency=currency,
        decimals=_resolve_session_decimals(data, currency),
        raw=data,
    )


def checkout_hash(session_id: str, currency: str, amount: str) -> str:
    """Same formula as ap2_service.rs's `checkout_hash`: sha256 hex of
    "{session_id}|{currency}|{amount}"."""
    return hashlib.sha256(f"{session_id}|{currency}|{amount}".encode()).hexdigest()


def build_autonomous_message(session_id: str, hash_: str, amount: str) -> str:
    """Byte-identical to ap2_service.rs's `build_closed_mandate_message`."""
    return (
        "Moramba AP2 Autonomous Checkout\n\n"
        f"Session: {session_id}\n"
        f"CheckoutHash: {hash_}\n"
        f"Amount: {amount}\n\n"
        "I authorize this exact checkout, within my pre-approved spend limits."
    )


def authorize_autonomous(
    base_url: str, api_key: str, session: CheckoutSession, agent_id: str, wallet: Wallet, timeout: float = 20.0
) -> None:
    """Signs and sends the Closed Mandate. Raises Ap2Error on rejection
    (bad signature, inactive agent, or over per_transaction_limit /
    maximum_payout_limit). No funds move here."""
    hash_ = checkout_hash(session.session_id, session.currency, session.amount)
    message = build_autonomous_message(session.session_id, hash_, session.amount)
    signature = wallet.sign_message(message)
    _acp_request(
        base_url, api_key, f"/checkout_sessions/{session.session_id}/authorize_autonomous", "POST",
        {"session_id": session.session_id, "agent_id": agent_id, "checkout_hash": hash_, "message": message, "signature": signature},
        timeout,
    )


def start_checkout_payment(base_url: str, api_key: str, session_id: str, timeout: float = 20.0) -> str:
    """Creates the payin and returns its `payin_id`, parsed out of the
    returned `continue_url` (there's no human to open it in Autonomous mode)."""
    data = _acp_request(base_url, api_key, f"/checkout_sessions/{session_id}/start", "POST", {}, timeout)
    continue_url = httpx.URL(data["continue_url"])
    payin_id = continue_url.params.get("payin_id")
    if not payin_id:
        raise Ap2Error(f"start_checkout_payment response had no payin_id in continue_url: {data.get('continue_url')}")
    return payin_id


def complete_checkout_session(
    base_url: str, api_key: str, session_id: str, buyer_email: str, timeout: float = 20.0
) -> dict:
    return _acp_request(
        base_url, api_key, f"/checkout_sessions/{session_id}/complete", "POST",
        {
            "buyer": {"email": buyer_email},
            "payment_data": {
                "handler_id": "moramba_ap2_mandate",
                "instrument": {"type": "moramba_ap2_mandate", "credential": {"type": "receipt_token", "token": "paid"}},
            },
        },
        timeout,
    )


# ─── payrequest / on-chain settlement ───────────────────────────────────────


@dataclass(frozen=True)
class PayInit:
    token_address: str
    token_name: str
    amount: str  # minor token units, as a decimal string
    chain_id: int
    rpc: str
    verify_sc_address: str
    to: str
    transaction_id: str
    nonce: str | None
    raw: dict[str, Any]


def fetch_payrequest_init(base_url: str, wallet_address: str, payin_id: str, timeout: float = 20.0) -> PayInit:
    data = _public_api_request(
        base_url, f"/morambacrypto/public/payrequest/init/{payin_id}/address/{wallet_address}", "GET", None, timeout
    )
    return PayInit(
        token_address=data["token_address"],
        token_name=data.get("token_name", ""),
        amount=str(data["amount"]),
        chain_id=int(data["chain_id"]),
        rpc=data["rpc"],
        verify_sc_address=data["verify_sc_address"],
        to=data["to"],
        transaction_id=data["transaction_id"],
        nonce=data.get("nonce"),
        raw=data,
    )


def _token_code_includes_selector(w3: Web3, token_address: str, selector: str) -> bool:
    code = w3.eth.get_code(Web3.to_checksum_address(token_address))
    if not code:
        raise Ap2Error(f"no contract code at token address {token_address}")
    code_hex = code.hex().lower()
    if selector in code_hex:
        return True

    implementation_slot = w3.eth.get_storage_at(Web3.to_checksum_address(token_address), EIP1967_IMPLEMENTATION_SLOT)
    implementation_address = "0x" + implementation_slot.hex()[-40:]
    if int(implementation_address, 16) == 0:
        return False

    implementation_code = w3.eth.get_code(Web3.to_checksum_address(implementation_address))
    return selector in implementation_code.hex().lower()


def check_supports_permit(w3: Web3, token_address: str) -> bool:
    token = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=_ERC20_MINI_ABI)
    try:
        token.functions.DOMAIN_SEPARATOR().call()
    except Exception:
        return False
    try:
        token.functions.nonces(Web3.to_checksum_address(token_address)).call()
    except Exception:
        return False
    return _token_code_includes_selector(w3, token_address, PERMIT_SELECTOR)


def check_supports_authorization(w3: Web3, token_address: str) -> bool:
    return _token_code_includes_selector(w3, token_address, TRANSFER_WITH_AUTHORIZATION_SELECTOR)


def check_permit2_deployed(w3: Web3) -> bool:
    return bool(w3.eth.get_code(Web3.to_checksum_address(PERMIT2_ADDRESS)))


def detect_flow(w3: Web3, token_address: str) -> str:
    try:
        supports_authorization = check_supports_authorization(w3, token_address)
    except Exception:
        supports_authorization = False
    if supports_authorization:
        return "authorization"
    try:
        supports_permit = check_supports_permit(w3, token_address)
    except Exception:
        supports_permit = False
    if supports_permit:
        return "permit"
    try:
        if check_permit2_deployed(w3):
            return "permit2"
    except Exception:
        pass
    return "plain"


def _resolve_token_domain(w3: Web3, token_address: str) -> dict[str, str]:
    token = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=_ERC20_MINI_ABI)
    try:
        _, name, version, *_ = token.functions.eip712Domain().call()
        return {"name": name, "version": version}
    except Exception:
        return {"name": token.functions.name().call(), "version": "1"}


def check_wallet_token_balance(w3: Web3, wallet_address: str, init: "PayInit") -> None:
    """Raises `Ap2Error` if the wallet holds less of the payin's token than
    the payin needs. Run before anything is built, signed or approved —
    found live: an empty wallet only failed deep inside the Permit2
    approve() step, with a raw RPC error. Shared by every flow that settles
    a payin through the relay (AP2, pay button, payin)."""
    token = w3.eth.contract(address=Web3.to_checksum_address(init.token_address), abi=_ERC20_MINI_ABI)
    try:
        balance = token.functions.balanceOf(Web3.to_checksum_address(wallet_address)).call()
    except Exception as exc:  # noqa: BLE001 - surfaced as a failed payment, same as every other read here
        raise Ap2Error(f"could not read token balance for {wallet_address}: {exc}") from exc
    needed = int(init.amount)
    if balance >= needed:
        return
    try:
        decimals = token.functions.decimals().call()
    except Exception:  # noqa: BLE001 - only used to make the message readable
        decimals = None

    def show(units: int) -> str:
        return str(Decimal(units) / (Decimal(10) ** decimals)) if decimals is not None else f"{units} (minor units)"

    raise Ap2Error(
        f"insufficient token balance: wallet {wallet_address} has {show(balance)} {init.token_name}, "
        f"needs {show(needed)} — fund it first"
    )


def _approve_if_needed(w3: Web3, account: LocalAccount, token_address: str, spender: str, amount: int) -> None:
    token = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=_ERC20_MINI_ABI)
    current = token.functions.allowance(account.address, Web3.to_checksum_address(spender)).call()
    if current >= amount:
        return
    chain_id = w3.eth.chain_id
    try:
        tx = token.functions.approve(Web3.to_checksum_address(spender), amount).build_transaction(
            {"chainId": chain_id, "from": account.address, "nonce": w3.eth.get_transaction_count(account.address), "gasPrice": w3.eth.gas_price}
        )
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    except Exception as exc:  # noqa: BLE001 - an RPC/web3 error here must become a failed payment, not a crashed tool call
        # Found live: a wallet with no balance for network fees made
        # eth_estimateGas fail with "gas required exceeds allowance (0)",
        # which escaped as a raw web3 error and crashed the whole tool
        # call with nothing recorded. This first-time Permit2 approve()
        # is the one step that needs the wallet itself to hold fee funds.
        hint = ""
        text = str(exc).lower()
        if "exceeds allowance" in text or "insufficient funds" in text:
            hint = f" — wallet {account.address} has no balance to pay network fees on chain {chain_id}; fund it first"
        raise Ap2Error(f"approve() for Permit2 could not be sent: {exc}{hint}") from exc
    if receipt.status == 0:
        raise Ap2Error(f"approve() tx {_hex0x(tx_hash)} reverted on-chain")


RELAY_DOMAIN_NAME = "MorambaERC20Transfer"
RELAY_DOMAIN_VERSION = "1"


def build_authorization_pay_body(w3: Web3, account: LocalAccount, init: PayInit) -> dict:
    token_domain = _resolve_token_domain(w3, init.token_address)
    domain = {"name": token_domain["name"], "version": token_domain["version"], "chainId": init.chain_id, "verifyingContract": init.token_address}
    types = {
        "TransferWithAuthorization": [
            {"name": "from", "type": "address"}, {"name": "to", "type": "address"}, {"name": "value", "type": "uint256"},
            {"name": "validAfter", "type": "uint256"}, {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"},
        ]
    }
    now = int(time.time())
    valid_after, valid_before = 0, now + DEADLINE_WINDOW_SECONDS
    nonce = _hex0x(secrets.token_bytes(32))
    value = {"from": account.address, "to": init.to, "value": int(init.amount), "validAfter": valid_after, "validBefore": valid_before, "nonce": nonce}

    signable = encode_typed_data(domain_data=domain, message_types=types, message_data=value)
    signed = account.sign_message(signable)

    return {
        "token": init.token_address, "from": account.address, "to": init.to, "value": init.amount,
        "valid_after": valid_after, "valid_before": valid_before, "nonce": nonce,
        "v": signed.v, "r": _uint_to_bytes32_hex(signed.r), "s": _uint_to_bytes32_hex(signed.s),
    }


def build_permit_pay_body(w3: Web3, account: LocalAccount, init: PayInit) -> dict:
    if not init.nonce:
        raise Ap2Error("init response had no relay nonce — cannot build a permit signature")
    relay_nonce = int(init.nonce)

    token_domain = _resolve_token_domain(w3, init.token_address)
    token = w3.eth.contract(address=Web3.to_checksum_address(init.token_address), abi=_ERC20_MINI_ABI)
    token_permit_nonce = token.functions.nonces(account.address).call()

    now = int(time.time())
    deadline = now + DEADLINE_WINDOW_SECONDS
    permit_value = int(init.amount)

    permit_domain = {"name": token_domain["name"], "version": token_domain["version"], "chainId": init.chain_id, "verifyingContract": init.token_address}
    permit_types = {
        "Permit": [
            {"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}, {"name": "value", "type": "uint256"},
            {"name": "nonce", "type": "uint256"}, {"name": "deadline", "type": "uint256"},
        ]
    }
    permit_value_obj = {"owner": account.address, "spender": init.verify_sc_address, "value": permit_value, "nonce": token_permit_nonce, "deadline": deadline}
    permit_signable = encode_typed_data(domain_data=permit_domain, message_types=permit_types, message_data=permit_value_obj)
    permit_signed = account.sign_message(permit_signable)

    relay_domain = {"name": RELAY_DOMAIN_NAME, "version": RELAY_DOMAIN_VERSION, "chainId": init.chain_id, "verifyingContract": init.verify_sc_address}
    outer_types = {
        "MetaTransferWithPermit": [
            {"name": "from", "type": "address"}, {"name": "to", "type": "address"}, {"name": "token", "type": "address"},
            {"name": "amount", "type": "uint256"}, {"name": "nonce", "type": "uint256"}, {"name": "deadline", "type": "uint256"},
            {"name": "permitValue", "type": "uint256"}, {"name": "permitDeadline", "type": "uint256"},
            {"name": "permitV", "type": "uint8"}, {"name": "permitR", "type": "bytes32"}, {"name": "permitS", "type": "bytes32"},
        ]
    }
    outer_value = {
        "from": account.address, "to": init.to, "token": init.token_address, "amount": int(init.amount),
        "nonce": relay_nonce, "deadline": deadline, "permitValue": permit_value, "permitDeadline": deadline,
        "permitV": permit_signed.v, "permitR": _uint_to_bytes32_hex(permit_signed.r), "permitS": _uint_to_bytes32_hex(permit_signed.s),
    }
    outer_signable = encode_typed_data(domain_data=relay_domain, message_types=outer_types, message_data=outer_value)
    outer_signed = account.sign_message(outer_signable)

    return {
        "from": account.address, "to": init.to, "token": init.token_address, "amount": init.amount,
        "nonce": relay_nonce, "deadline": deadline,
        "permit_value": permit_value, "permit_deadline": deadline,
        "permit_v": permit_signed.v, "permit_r": _uint_to_bytes32_hex(permit_signed.r), "permit_s": _uint_to_bytes32_hex(permit_signed.s),
        "signature": _hex0x(outer_signed.signature),
    }


def build_plain_pay_body(w3: Web3, account: LocalAccount, init: PayInit) -> dict:
    if not init.nonce:
        raise Ap2Error("init response had no relay nonce — cannot build a meta-transfer signature")
    relay_nonce = int(init.nonce)

    _approve_if_needed(w3, account, init.token_address, init.verify_sc_address, int(init.amount))

    domain = {"name": RELAY_DOMAIN_NAME, "version": RELAY_DOMAIN_VERSION, "chainId": init.chain_id, "verifyingContract": init.verify_sc_address}
    types = {
        "MetaTransfer": [
            {"name": "from", "type": "address"}, {"name": "to", "type": "address"}, {"name": "token", "type": "address"},
            {"name": "amount", "type": "uint256"}, {"name": "nonce", "type": "uint256"}, {"name": "deadline", "type": "uint256"},
        ]
    }
    now = int(time.time())
    deadline = now + DEADLINE_WINDOW_SECONDS
    value = {"from": account.address, "to": init.to, "token": init.token_address, "amount": int(init.amount), "nonce": relay_nonce, "deadline": deadline}
    signable = encode_typed_data(domain_data=domain, message_types=types, message_data=value)
    signed = account.sign_message(signable)

    return {"from": account.address, "to": init.to, "token": init.token_address, "amount": init.amount, "nonce": relay_nonce, "deadline": deadline, "signature": _hex0x(signed.signature)}


_PERMIT2_WITNESS_TYPES = {
    "PermitWitnessTransferFrom": [
        {"name": "permitted", "type": "TokenPermissions"}, {"name": "spender", "type": "address"},
        {"name": "nonce", "type": "uint256"}, {"name": "deadline", "type": "uint256"}, {"name": "witness", "type": "Witness"},
    ],
    "TokenPermissions": [{"name": "token", "type": "address"}, {"name": "amount", "type": "uint256"}],
    "Witness": [{"name": "to", "type": "address"}],
}


def build_permit2_pay_body(w3: Web3, account: LocalAccount, init: PayInit) -> dict:
    _approve_if_needed(w3, account, init.token_address, PERMIT2_ADDRESS, int(init.amount))

    domain = {"name": "Permit2", "chainId": init.chain_id, "verifyingContract": PERMIT2_ADDRESS}
    now = int(time.time())
    deadline = now + DEADLINE_WINDOW_SECONDS
    nonce = str(int.from_bytes(secrets.token_bytes(31), "big"))
    value = {
        "permitted": {"token": init.token_address, "amount": int(init.amount)},
        "spender": init.verify_sc_address, "nonce": nonce, "deadline": deadline, "witness": {"to": init.to},
    }
    signable = encode_typed_data(domain_data=domain, message_types=_PERMIT2_WITNESS_TYPES, message_data=value)
    signed = account.sign_message(signable)
    packed = signed.r.to_bytes(32, "big") + signed.s.to_bytes(32, "big") + signed.v.to_bytes(1, "big")

    return {
        "token": init.token_address, "from": account.address, "to": init.to, "amount": init.amount,
        "permit2_nonce": nonce, "permit2_deadline": deadline, "permit2_signature": _hex0x(packed),
    }


_PAY_PATH_BY_FLOW = {
    "authorization": "pay-authorization",
    "permit": "pay-permit",
    "permit2": "pay-permit2",
    "plain": "pay",
}
_BUILDER_BY_FLOW = {
    "authorization": build_authorization_pay_body,
    "permit": build_permit_pay_body,
    "permit2": build_permit2_pay_body,
    "plain": build_plain_pay_body,
}


def submit_relay_payment(base_url: str, payin_id: str, transaction_id: str, flow: str, body: dict, timeout: float = 20.0) -> dict:
    path = f"/morambacrypto/public/payrequest/{_PAY_PATH_BY_FLOW[flow]}/{payin_id}/tnxid/{transaction_id}/relay"
    return _public_api_request(base_url, path, "POST", body, timeout)


def poll_payment_status(
    base_url: str, transaction_id: str, *, poll_interval_seconds: float = 4.0, timeout_seconds: float = 120.0
) -> dict:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        status = _public_api_request(base_url, f"/morambacrypto/public/payrequest/status/tnxid/{transaction_id}", "GET")
        if status.get("status") in ("success", "failed"):
            return status
        time.sleep(poll_interval_seconds)
    raise Ap2Error("timed out waiting for on-chain settlement")


@dataclass(frozen=True)
class Ap2SettlementResult:
    success: bool
    tx_hash: str | None
    flow: str | None
    payin_id: str | None = None
    error: str | None = None
    # From the payin's own init response — only known once that call
    # succeeded, so None for a failure before that point.
    chain_id: int | None = None


def settle_autonomous_checkout(
    *,
    base_url: str,
    api_key: str,
    agent_id: str,
    wallet: Wallet,
    session: CheckoutSession,
    buyer_email: str,
    poll_interval_seconds: float = 4.0,
    poll_timeout_seconds: float = 120.0,
) -> Ap2SettlementResult:
    """Everything after the caller's own limit check has approved
    `session` — authorize, start, detect the token's settlement
    capability, sign and submit the on-chain payment, poll to
    settlement, then complete the session."""
    payin_id: str | None = None
    flow: str | None = None
    chain_id: int | None = None
    try:
        authorize_autonomous(base_url, api_key, session, agent_id, wallet)
        payin_id = start_checkout_payment(base_url, api_key, session.session_id)
        init = fetch_payrequest_init(base_url, wallet.address, payin_id)
        chain_id = init.chain_id

        w3 = Web3(Web3.HTTPProvider(init.rpc))
        check_wallet_token_balance(w3, wallet.address, init)
        flow = detect_flow(w3, init.token_address)
        pay_body = _BUILDER_BY_FLOW[flow](w3, wallet._account, init)

        submit_relay_payment(base_url, payin_id, init.transaction_id, flow, pay_body)
        final_status = poll_payment_status(
            base_url, init.transaction_id, poll_interval_seconds=poll_interval_seconds, timeout_seconds=poll_timeout_seconds
        )
        if final_status.get("status") != "success":
            return Ap2SettlementResult(
                success=False, tx_hash=final_status.get("tx_hash"), flow=flow, payin_id=payin_id,
                error=final_status.get("error_message") or "settlement failed on-chain", chain_id=chain_id,
            )

        try:
            complete_checkout_session(base_url, api_key, session.session_id, buyer_email)
        except Ap2Error as exc:
            # Found live (2026-10-01): by the time the on-chain payment is
            # confirmed, Moramba has already completed the session itself
            # (the order was recorded), so this call is a no-op conflict —
            # not a failure. Recording it as one hid a real, settled
            # payment as "Failed" in the ledger and Moramba's history.
            if "already completed" not in str(exc):
                raise
        return Ap2SettlementResult(
            success=True, tx_hash=final_status.get("tx_hash"), flow=flow, payin_id=payin_id, chain_id=chain_id
        )
    except Ap2Error as exc:
        # Preserve whatever we already knew — a payin may well have been
        # created, and a flow chosen, before this failed; discarding them
        # here would hide exactly the detail needed to debug a partial
        # failure (e.g. a real approve() already sent on-chain for a
        # payin that never got to settle — see pay_button.py's identical
        # fix for the same pattern).
        return Ap2SettlementResult(
            success=False, tx_hash=None, flow=flow, payin_id=payin_id, error=str(exc), chain_id=chain_id
        )
