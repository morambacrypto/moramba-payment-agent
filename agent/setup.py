"""Interactive setup wizard — `python -m agent.setup` (README section 5,
"plug and play"). Network/RPC and the default ERC20 token contract are
both auto-detected from the agent's own Moramba config wherever possible
(chain/RPC fall back to a manual, still-required prompt if that lookup
fails; the token contract has no such fallback — it's just left blank,
since transfer_erc20 already requires a caller-supplied address in that
case anyway). Every rail is always enabled — there's no "which rails do
you want" step. Every other prompt requires a real answer: nothing is
skippable or silently defaulted.

The interactive flow (`run_wizard`) takes `input_fn`/`getpass_fn` so it
can be driven by canned answers in tests without touching real stdin;
everything it depends on beyond that (validation, the .env content
itself) is plain, separately-testable functions.
"""

import getpass
import os
import uuid
from pathlib import Path

from agent.limits_client import AgentLimits, MorambaAgentClient
from agent.signing import load_wallet

# Every rail this agent supports — always enabled. There's nothing to
# choose here: none of them cost anything to have "on" (a rail only ever
# runs when a caller explicitly invokes it), so asking "which rails do
# you want" was pure friction for no benefit.
ALL_RAILS = ("mpp", "erc20", "x402", "ap2", "pay_button", "agent_transfer")

_DEFAULT_API_BASE_URL = "https://crypto.moramba.io"

# Known-good public RPCs for Moramba's own supported chains — used when an
# agent's own token record has an empty rpc_url, or when Moramba couldn't
# be reached at all during setup (so a partner isn't stuck with no RPC).
KNOWN_CHAIN_RPCS: dict[int, str] = {
    42431: "https://rpc.moderato.tempo.xyz",  # Tempo testnet
    4217: "https://rpc.tempo.xyz",  # Tempo mainnet
}


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


def build_env_content(answers: dict) -> str:
    """Pure function: answers -> the .env file's exact text. Kept
    separate from any I/O so it's trivially unit-testable."""
    lines = [
        f"WALLET_PRIVATE_KEY={answers['wallet_private_key']}",
        f"MORAMBA_AGENT_ID={answers['moramba_agent_id']}",
        f"MORAMBA_API_BASE_URL={answers['moramba_api_base_url']}",
        f"MORAMBA_ACP_API_KEY={answers['moramba_acp_api_key']}",
        f"CHAIN_ID={answers['chain_id']}",
        f"RPC_URL={answers['rpc_url']}",
        f"DB_PATH={answers['db_path']}",
        f"DEFAULT_TOKEN_CONTRACT={answers.get('default_token_contract', '')}",
    ]
    return "\n".join(lines) + "\n"


def fetch_agent_info(base_url: str, agent_id: str) -> AgentLimits | None:
    """None if Moramba wasn't reachable or the agent wasn't found — the
    wizard treats that as "couldn't auto-detect, fall back to manual
    entry" rather than a hard failure. Used for BOTH the network
    auto-detection and the wallet-registration check, in one fetch."""
    client = MorambaAgentClient(base_url)
    try:
        return client.get_agent(agent_id)
    except Exception:
        return None
    finally:
        client.close()


def resolve_chain_and_rpc(agent_limits: AgentLimits | None) -> tuple[int, str] | None:
    """Derive (chain_id, rpc_url) from the agent's own configured payout
    token — Moramba already knows which network this agent's payouts run
    on, so the wizard doesn't need to ask. None if there's nothing to
    derive it from (Moramba unreachable, or this agent has no payout
    tokens configured yet)."""
    if not agent_limits or not agent_limits.payout_tokens:
        return None
    token = agent_limits.payout_tokens[0]
    rpc = token.rpc_url or KNOWN_CHAIN_RPCS.get(token.chain, "")
    return (token.chain, rpc) if rpc else None


def run_wizard(input_fn=input, getpass_fn=getpass.getpass, env_path: str = ".env") -> None:
    print("Moramba Payment Agent — setup wizard\n")

    if Path(env_path).exists():
        overwrite = input_fn(f"{env_path} already exists — overwrite it? [y/N] ").strip().lower()
        if overwrite != "y":
            print("Aborted — existing .env left untouched.")
            return

    moramba_api_base_url = _DEFAULT_API_BASE_URL

    moramba_agent_id = None
    while moramba_agent_id is None:
        raw = input_fn("Moramba agent id (UUID) — your payout agent: ")
        moramba_agent_id = validate_agent_id(raw)
        if moramba_agent_id is None:
            print("That doesn't look like a valid UUID — try again.")

    print("\nLooking up this agent in Moramba...")
    agent_limits = fetch_agent_info(moramba_api_base_url, moramba_agent_id)
    if agent_limits is None:
        print("Could not reach Moramba to look up this agent — you'll be asked for network details manually.\n")
    else:
        print(f"Found agent {moramba_agent_id} (status: {agent_limits.status}).\n")

    print(
        "Paste the private key for the wallet this agent will sign with.\n"
        "It is written straight to .env and never displayed, logged, or sent anywhere.\n"
        "This wallet must be dedicated to this agent alone (README section 2, "
        "\"one wallet, one job\") — never reuse a wallet you also use manually.\n"
    )
    wallet_private_key = None
    wallet_address = None
    while wallet_private_key is None:
        raw = getpass_fn("Wallet private key: ")
        candidate = validate_private_key(raw)
        if candidate is None:
            print("That doesn't look like a valid private key — try again.")
            continue
        candidate_address = load_wallet(candidate).address

        if agent_limits is not None:
            if candidate_address in agent_limits.wallet_addresses:
                print(f"\nWallet address: {candidate_address}")
                print("Confirmed — this wallet is one of that agent's registered payout wallets.\n")
            else:
                print(f"\nWallet address: {candidate_address}")
                print(
                    "This wallet is NOT among that agent's registered payout wallets — "
                    "every payment would be rejected until it's added in Moramba."
                )
                retry = input_fn("Try a different key? [Y/n] ").strip().lower()
                if retry != "n":
                    continue  # loop back for another key
                print("Proceeding with this unregistered wallet anyway.\n")
        else:
            print(f"\nWallet address: {candidate_address}")
            print("(Could not verify registration — Moramba wasn't reachable earlier.)\n")

        wallet_private_key = candidate
        wallet_address = candidate_address

    moramba_acp_api_key = ""
    while not moramba_acp_api_key:
        moramba_acp_api_key = input_fn("Moramba ACP API key (required): ").strip()
        if not moramba_acp_api_key:
            print("An ACP API key is required.")

    detected = resolve_chain_and_rpc(agent_limits)
    if detected is not None:
        chain_id, rpc_url = detected
        network_name = "Tempo mainnet" if chain_id == 4217 else "Tempo testnet" if chain_id == 42431 else f"chain {chain_id}"
        print(f"Detected network from this agent's own config: {network_name} ({rpc_url})\n")
    else:
        print("Couldn't auto-detect this agent's network — enter it manually.")
        chain_id = None
        while chain_id is None:
            chain_id_raw = input_fn("Chain id (required, e.g. 42431 for Tempo testnet): ").strip()
            if chain_id_raw.isdigit():
                chain_id = int(chain_id_raw)
            else:
                print("Chain id must be a number — try again.")
        rpc_url = ""
        while not rpc_url:
            rpc_url = input_fn("RPC URL (required): ").strip()

    # Not asked — same default Settings.db_path itself falls back to, so
    # a fresh setup just gets a ledger file next to wherever it's run
    # from, with nothing to type.
    db_path = "moramba_payment_agent.db"
    print(f"Local ledger: {db_path}\n")

    # Same source as the network auto-detection above — the agent's own
    # payout_config.allowed_tokens already carries each token's contract
    # address, so there's nothing to ask here either.
    default_token_contract = (
        agent_limits.payout_tokens[0].token_address if agent_limits and agent_limits.payout_tokens else ""
    )
    if default_token_contract:
        print(f"Detected default token contract from this agent's own config: {default_token_contract}\n")

    answers = {
        "wallet_private_key": wallet_private_key,
        "moramba_agent_id": moramba_agent_id,
        "moramba_api_base_url": moramba_api_base_url,
        "moramba_acp_api_key": moramba_acp_api_key,
        "chain_id": chain_id,
        "rpc_url": rpc_url,
        "db_path": db_path,
        "default_token_contract": default_token_contract,
    }

    Path(env_path).write_text(build_env_content(answers))
    os.chmod(env_path, 0o600)

    print(f"\nWrote {env_path} (permissions set to 600).")
    print(f"Rails enabled: {', '.join(ALL_RAILS)}")
    print(
        "\nNext: confirm this loads correctly —\n"
        "  python3 -c \"from agent.engine import Agent; from agent.config import Settings; "
        "print(Agent(Settings()).wallet.address)\"\n"
        f"and check that address matches {wallet_address} above.\n"
        "(If you're working from a source checkout rather than a pip install, "
        "`python -m pytest -q` still confirms the test suite passes.)"
    )


def main() -> None:
    run_wizard()


if __name__ == "__main__":
    main()
