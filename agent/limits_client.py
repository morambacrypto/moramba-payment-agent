"""Client for Moramba's public agent-lookup API.

Verified against the real backend (moramba-crypto-api,
src/controllers/agent_controller.rs + src/dtos/agent_response_dtos.rs):

    GET /api/v2/morambacrypto/public/agent?agent_id=<uuid>

is unauthenticated (bypassed by the JwtApplicationAuth middleware's
"/api/v2/morambacrypto/public/" prefix rule) and returns
`{success, message, data: AgentResponse}` where the limits this project
cares about live under `data.payout_config`.

Limit fields are **whole USD units** (`Option<i64>` in Rust — e.g. `5`
means $5, not 5 cents). Confirmed against real production data
(2026-09-29): a `per_transaction_limit` of `5` on a live agent was meant
to allow up to $5 per transaction, not $0.05 — an earlier version of
this client wrongly assumed cents (the Rust field's own doc comment says
"USD cents", which turned out not to match how partners actually set
these values), which meant every real agent with a nonzero limit would
reject payments ~100x smaller than intended. This client assumes the
token being paid is a USD-pegged stablecoin (1 token == 1 USD); a
non-stablecoin token needs a real price oracle, out of scope for v1
(README section 4 only commits to stablecoin-shaped rails on Tempo
testnet).
"""

from dataclasses import dataclass, field

import httpx

from agent.adapters.ap2 import Ap2Error


@dataclass(frozen=True)
class AcceptedToken:
    token_name: str
    token_address: str
    network: str
    chain: int
    # A receiving agent's accepted token can be on a different chain than
    # this payer's own `.env` `rpc_url`/`chain_id` (found 2026-09-30: a
    # real receiving agent's token was on Tempo mainnet, chain 4217, while
    # the payer's configured rpc_url pointed at Tempo testnet, 42431) — so
    # `pay_agent` must use the destination's own RPC, never the payer's
    # single configured one.
    rpc_url: str


@dataclass(frozen=True)
class ReceivingAgentInfo:
    """The subset of `GET .../public/agent`'s `receiving_config` a payer
    needs to pay this agent directly — see `MorambaAgentClient.get_receiving_agent`.
    """

    id: str
    status: str
    receive_wallet_address: str
    accepted_tokens: list[AcceptedToken] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.status.lower() == "active"

    def resolve_token(self, token_name: str) -> AcceptedToken | None:
        return next(
            (t for t in self.accepted_tokens if t.token_name.lower() == token_name.lower()), None
        )


@dataclass(frozen=True)
class AgentLimits:
    id: str
    status: str  # "active" | "inactive" | "suspended"
    allowed_tokens: list[str]
    wallet_addresses: list[str]
    per_transaction_limit: int | None
    daily_transaction_limit: int | None
    monthly_transaction_limit: int | None
    vendor_wise_spending_limit: int | None
    aggregate_spending_limit: int | None
    maximum_payout_limit: int | None
    daily_transaction_count_limit: int | None
    hourly_transaction_limit: int | None
    rate_limit_max_transactions_count: int | None
    rate_limit_per_period: str | None  # "per_minute" | "per_hour" | "per_day"

    @property
    def is_active(self) -> bool:
        return self.status.lower() == "active"


class MorambaAgentClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    def get_agent(self, agent_id: str) -> AgentLimits:
        resp = self._client.get(
            f"{self._base_url}/api/v2/morambacrypto/public/agent",
            params={"agent_id": agent_id},
        )
        resp.raise_for_status()
        body = resp.json()
        data = body["data"]
        payout = data.get("payout_config") or {}
        wallets = payout.get("wallets") or []
        return AgentLimits(
            id=data["id"],
            status=data["status"],
            # Real production data (2026-09-29): `allowed_tokens` is a list
            # of full token objects ({id, network, chain, token_name,
            # token_address, ...}), not plain strings — extract the symbol
            # a rail actually compares against. Every test fixture so far
            # used an empty list, which is why this went unnoticed.
            allowed_tokens=[t["token_name"] for t in (payout.get("allowed_tokens") or [])],
            wallet_addresses=[w["public_wallet_address"] for w in wallets],
            per_transaction_limit=payout.get("per_transaction_limit"),
            daily_transaction_limit=payout.get("daily_transaction_limit"),
            monthly_transaction_limit=payout.get("monthly_transaction_limit"),
            vendor_wise_spending_limit=payout.get("vendor_wise_spending_limit"),
            aggregate_spending_limit=payout.get("aggregate_spending_limit"),
            maximum_payout_limit=payout.get("maximum_payout_limit"),
            daily_transaction_count_limit=payout.get("daily_transaction_count_limit"),
            hourly_transaction_limit=payout.get("hourly_transaction_limit"),
            rate_limit_max_transactions_count=data.get("rate_limit_max_transactions_count"),
            rate_limit_per_period=data.get("rate_limit_per_period"),
        )

    def get_receiving_agent(self, agent_id: str) -> ReceivingAgentInfo:
        """Same endpoint as `get_agent`, but reads `receiving_config`
        instead of `payout_config` — this is how the `pay_agent` rail
        (engine.py) resolves who it's actually paying: the destination
        wallet and accepted tokens come from Moramba's own DB record for
        that agent, not from anything the caller supplies directly."""
        resp = self._client.get(
            f"{self._base_url}/api/v2/morambacrypto/public/agent",
            params={"agent_id": agent_id},
        )
        resp.raise_for_status()
        body = resp.json()
        data = body["data"]

        if not data.get("is_receiving_agent"):
            raise Ap2Error(f"agent {agent_id} is not a receiving agent")

        receiving = data.get("receiving_config")
        if not receiving:
            raise Ap2Error(f"agent {agent_id} has no receiving wallet configured yet")

        return ReceivingAgentInfo(
            id=data["id"],
            status=data["status"],
            receive_wallet_address=receiving["receive_wallet_address"],
            accepted_tokens=[
                AcceptedToken(
                    token_name=t["token_name"],
                    token_address=t["token_address"],
                    network=t["network"],
                    chain=t["chain"],
                    rpc_url=t["rpc_url"],
                )
                for t in (receiving.get("accepted_tokens") or [])
            ],
        )

    def close(self) -> None:
        self._client.close()
