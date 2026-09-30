"""Interactive setup wizard — `python -m agent.setup` (README section 5,
"plug and play"). Asks for the partner's wallet private key (written
straight to a local `.env`, never echoed back or transmitted anywhere),
the Moramba agent id whose limits to enforce, and which rails to enable.

The interactive flow (`run_wizard`) takes `input_fn`/`getpass_fn` so it
can be driven by canned answers in tests without touching real stdin;
everything it depends on beyond that (validation, the .env content
itself) is plain, separately-testable functions.
"""

import getpass
import os
import uuid
from pathlib import Path

from agent.limits_client import MorambaAgentClient
from agent.signing import load_wallet

_ALL_RAILS = ("mpp", "erc20", "x402", "ap2")
_DEFAULT_API_BASE_URL = "https://crypto.moramba.io"
_DEFAULT_CHAIN_ID = 42431
_DEFAULT_DB_PATH = "moramba_payment_agent.db"


def validate_private_key(raw: str) -> str | None:
    """Returns a normalized `0x...`-prefixed key on success, or None if
    it isn't a loadable key — never raises, so the wizard can just loop."""
    candidate = raw.strip()
    if not candidate:
        return None
    if not candidate.startswith("0x"):
        candidate = f"0x{candidate}"
    try:
        load_wallet(candidate)
    except Exception:
        return None
    return candidate


def validate_agent_id(raw: str) -> str | None:
    candidate = raw.strip()
    try:
        uuid.UUID(candidate)
    except ValueError:
        return None
    return candidate


def parse_rails(raw: str) -> list[str]:
    """Comma-separated rail names -> the subset of _ALL_RAILS actually
    requested, preserving _ALL_RAILS's order. Unrecognized entries are
    dropped rather than rejected, so a typo doesn't abort the wizard."""
    requested = {r.strip().lower() for r in raw.split(",") if r.strip()}
    return [r for r in _ALL_RAILS if r in requested]


def build_env_content(answers: dict) -> str:
    """Pure function: answers -> the .env file's exact text. Kept
    separate from any I/O so it's trivially unit-testable."""
    lines = [
        f"WALLET_PRIVATE_KEY={answers['wallet_private_key']}",
        f"MORAMBA_AGENT_ID={answers['moramba_agent_id']}",
        f"MORAMBA_API_BASE_URL={answers['moramba_api_base_url']}",
        f"CHAIN_ID={answers['chain_id']}",
        f"RPC_URL={answers['rpc_url']}",
        f"DB_PATH={answers['db_path']}",
        f"DEFAULT_TOKEN_CONTRACT={answers.get('default_token_contract', '')}",
    ]
    if "moramba_acp_api_key" in answers:
        lines.append(f"MORAMBA_ACP_API_KEY={answers['moramba_acp_api_key']}")
    return "\n".join(lines) + "\n"


def check_wallet_registered(base_url: str, agent_id: str, wallet_address: str) -> bool | None:
    """True/False if the check could run, None if Moramba wasn't
    reachable — the wizard treats None as "couldn't verify, proceed
    anyway" rather than a hard failure."""
    client = MorambaAgentClient(base_url)
    try:
        limits = client.get_agent(agent_id)
    except Exception:
        return None
    finally:
        client.close()
    return wallet_address in limits.wallet_addresses


def run_wizard(input_fn=input, getpass_fn=getpass.getpass, env_path: str = ".env") -> None:
    print("Moramba Payment Agent — setup wizard\n")

    if Path(env_path).exists():
        overwrite = input_fn(f"{env_path} already exists — overwrite it? [y/N] ").strip().lower()
        if overwrite != "y":
            print("Aborted — existing .env left untouched.")
            return

    print(
        "\nPaste the private key for the wallet this agent will sign with.\n"
        "It is written straight to .env and never displayed, logged, or sent anywhere.\n"
        "This wallet must be dedicated to this agent alone (README section 2, "
        "\"one wallet, one job\") — never reuse a wallet you also use manually.\n"
    )
    wallet_private_key = None
    while wallet_private_key is None:
        raw = getpass_fn("Wallet private key: ")
        wallet_private_key = validate_private_key(raw)
        if wallet_private_key is None:
            print("That doesn't look like a valid private key — try again.")
    wallet_address = load_wallet(wallet_private_key).address
    print(f"\nWallet address: {wallet_address}")
    print("(This address is public — safe to share. Register it as this agent's payout wallet in Moramba.)\n")

    moramba_agent_id = None
    while moramba_agent_id is None:
        raw = input_fn("Moramba agent id (UUID) whose limits this instance enforces: ")
        moramba_agent_id = validate_agent_id(raw)
        if moramba_agent_id is None:
            print("That doesn't look like a valid UUID — try again.")

    base_url_raw = input_fn(f"Moramba API base URL [{_DEFAULT_API_BASE_URL}]: ").strip()
    moramba_api_base_url = base_url_raw or _DEFAULT_API_BASE_URL

    chain_id_raw = input_fn(f"Chain id [{_DEFAULT_CHAIN_ID}]: ").strip()
    chain_id = int(chain_id_raw) if chain_id_raw else _DEFAULT_CHAIN_ID

    rpc_url = ""
    while not rpc_url:
        rpc_url = input_fn("Tempo RPC URL (required — not guessed for you): ").strip()
        if not rpc_url:
            print("An RPC URL is required.")

    db_path_raw = input_fn(f"Local ledger path [{_DEFAULT_DB_PATH}]: ").strip()
    db_path = db_path_raw or _DEFAULT_DB_PATH

    rails_raw = input_fn(f"Rails to enable, comma-separated [{','.join(_ALL_RAILS)}]: ").strip()
    rails = parse_rails(rails_raw) if rails_raw else list(_ALL_RAILS)

    answers = {
        "wallet_private_key": wallet_private_key,
        "moramba_agent_id": moramba_agent_id,
        "moramba_api_base_url": moramba_api_base_url,
        "chain_id": chain_id,
        "rpc_url": rpc_url,
        "db_path": db_path,
    }

    if "erc20" in rails:
        answers["default_token_contract"] = input_fn(
            "Default ERC20 token contract for transfer_erc20 (optional, blank to skip): "
        ).strip()

    if "ap2" in rails:
        answers["moramba_acp_api_key"] = input_fn(
            "Moramba ACP API key for the AP2 rail (optional, blank to skip for now): "
        ).strip()

    print("\nChecking whether this wallet is registered to that agent in Moramba...")
    registered = check_wallet_registered(moramba_api_base_url, moramba_agent_id, wallet_address)
    if registered is True:
        print("Confirmed — this wallet is one of that agent's registered payout wallets.\n")
    elif registered is False:
        print(
            "Warning: this wallet is NOT among that agent's registered payout wallets yet.\n"
            "Every payment will be rejected until you add it in Moramba. Proceeding anyway —\n"
            "you can register the wallet before the agent's first real payment.\n"
        )
    else:
        print("Could not reach Moramba to verify — proceeding without that check.\n")

    Path(env_path).write_text(build_env_content(answers))
    os.chmod(env_path, 0o600)

    print(f"Wrote {env_path} (permissions set to 600).")
    print(f"Rails enabled: {', '.join(rails) or '(none)'}")
    print("\nNext: PYTHONPATH=. python -m pytest -q   # confirm everything still passes")
    print("Then: from agent.config import Settings; from agent.engine import Agent")


def main() -> None:
    run_wizard()


if __name__ == "__main__":
    main()
