from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Non-custodial signing key — read once at startup, never logged, never
    # sent anywhere. See README section 2 ("one wallet, one job"): this
    # wallet must never be used for anything other than this agent.
    wallet_private_key: str

    # The Moramba `agents` row whose limits this agent enforces.
    moramba_agent_id: str
    moramba_api_base_url: str = "https://crypto.moramba.io"

    # Bearer token for the /acp checkout endpoints (AP2 rail only) — a
    # partner API key, not a JWT; /acp/* is otherwise unauthenticated.
    moramba_acp_api_key: str | None = None

    # Tempo testnet first (see README section 3).
    chain_id: int = 42431
    rpc_url: str

    db_path: str = "moramba_payment_agent.db"

    # Shared secret required on every request to this process's HTTP/MCP
    # server (`X-API-Key: <payment_agent_api_key>`) — generated
    # once by the setup wizard (32 random bytes, url-safe encoded).
    # Prefixed PAYMENT_AGENT_, not AGENT_: this is THIS running service's
    # own transport key, unrelated to a Moramba `agents` row's id or to
    # MORAMBA_ACP_API_KEY (which authenticates this agent *to* Moramba,
    # the opposite direction). Required, not optional: without it, every
    # request is rejected (see `require_api_key` in agent/api.py) rather
    # than the server silently running open — a partner must never be
    # able to combine TUNNEL=1's public URL with no auth at all.
    payment_agent_api_key: str

    # ERC20 token contract used by the `transfer_erc20` rail when a token
    # symbol isn't resolvable through Moramba's own token list. Optional —
    # only required if that rail is used with a raw contract address.
    default_token_contract: str | None = None
