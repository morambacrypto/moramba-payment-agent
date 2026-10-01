# Moramba Payment Agent

**Status: All planned phases implemented, now six rails — MPP, ERC20,
x402, AP2 (Autonomous), Moramba Pay Button, and Agent Transfer (pay any
Moramba agent directly by id) — plus the local ledger, the live limit
check, a FastAPI service wrapper, the setup wizard, and the MCP tool
server, all with a passing test suite (156 tests). Packaged as a proper
pip-installable project (`pyproject.toml`) so a partner can
`pip install -e .` instead of running from source on `PYTHONPATH`; Docker
packaging was dropped — not needed for this project. The setup wizard now
auto-detects the agent's network (chain/RPC) from its own Moramba config,
always enables every rail (nothing to choose), and requires every
remaining answer explicitly — no field is silently defaulted or
skippable. Running the FastAPI service no longer requires a free port to
guess at either: `moramba-payment-agent-serve` tries a fixed preferred
port first and falls back to an auto-selected one if that's taken.
The Pay Button rail settled a real payment live, end-to-end, on Tempo
testnet against production (2026-09-29,
[tx 0x43d1d64f...](https://explore.testnet.tempo.xyz/tx/0x43d1d64fa09f1db9710b780d5382e4d10f5c4762718047285470c57e9aa34194)) —
see section 4 for the two real bugs that first blocked it (both fixed:
a whole-units-vs-cents mismatch in this project's own limit check, and
a NATS connectivity/firewall gap on Moramba's production server that
broke `RelayQueueClient` for every settlement flow, not just this one).
The other four rails are still only tested against mocks and the real
SDKs' own encode/decode/signing code, not a live deployment.**

A non-custodial Python agent that a partner runs on their own machine so
their AI agent can *actually pay* — via AP2, x402, MPP, a plain ERC20
transfer, or a Moramba Pay Button — without ever handing the private
key to Moramba or to the LLM itself.

## Getting started

```
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
moramba-payment-agent-setup   # interactive — writes .env for you; skip and copy .env.example by hand if you prefer
python -m pytest -q          # 156 tests, all passing
```

(`pip install -e ".[dev]"` installs this package itself in editable mode
plus the test dependencies, via `pyproject.toml` — the console script
`moramba-payment-agent-setup` and the plain `python -m agent.setup` both
still work, whichever you find easier to type. `PYTHONPATH=.` is no
longer needed once the package is installed.)

The wizard only ever asks for what's actually yours — everything else
comes from your agent's own Moramba config:

1. **Your Moramba agent id.** The wizard looks it up immediately.
2. **Your wallet's private key.** It checks the derived address against
   that agent's registered payout wallets right away — if it doesn't
   match, it offers to retry with a different key rather than silently
   writing a config that would reject every payment.
3. **Your Moramba ACP API key** (for the AP2 rail).

Network (`CHAIN_ID`/`RPC_URL`) and the ERC20 rail's default token
contract are auto-detected from the agent's own configured payout token
— you're only asked for the network manually if Moramba couldn't be
reached during setup. The local ledger isn't asked either — it's always
`moramba_payment_agent.db`, next to wherever the wizard runs from (the
same default `Settings.db_path` itself falls back to). Every rail is
always enabled; there's no "which rails do you want" step, and no prompt
in the whole wizard accepts a blank answer as "use the default" —
everything that's still asked is required.

As a library:

```python
from decimal import Decimal
from agent.config import Settings
from agent.engine import Agent

agent = Agent(Settings())
record = agent.pay_via_mpp(
    receiver_base_url="https://some-receiver.example",
    receiver_agent_id="<receiver's Moramba agent id>",
    payout_agent_id="<this agent's Moramba agent id>",
    amount=Decimal("2.50"),
    token="USDC",
)
print(record.status, record.tx_hash)
```

As an HTTP service, which also serves the MCP tool server on the same
port (section 6, "Surfaces"):

```
moramba-payment-agent-serve   # tries port 58417 first, falls back to a free one if that's taken
```

(tries `58417` first — an IANA dynamic/private-range port chosen
specifically because no well-known software defaults there, so it's
unlikely to already be in use; falls back to an OS-assigned free port
only if `58417` itself is taken by something else. Running the command
again is meant to *replace* an already-running instance, not start a
second one next to it: if `58417` turns out to be held by a previous
`moramba-payment-agent-serve` run (tracked via a `.moramba_payment_agent.pid`
file written on every start), that old process is stopped first so the
new one can take the port over. A pid that isn't this tool's own (or has
already exited) is never touched — the port is left alone and the usual
fallback applies instead. Prints something like
`Starting moramba-payment-agent on http://127.0.0.1:58417`; set
`PORT`/`HOST` env vars to pin a specific address instead, or use
`uvicorn agent.api:app --reload` directly if you want `--reload` for
local development. Set `AGENT_THREAD_POOL_SIZE` to raise how many
payments can be in flight at once — every route is sync, since each
rail makes blocking httpx/web3 calls, so concurrency comes from a
thread pool rather than asyncio; the default is 100, well above
anyio's own default of 40.)

```
curl -X POST localhost:PORT/payment-agent-api/pay/mpp -H "Content-Type: application/json" -d '{
  "receiver_base_url": "https://some-receiver.example",
  "receiver_agent_id": "<receiver agent id>",
  "payout_agent_id": "<this agent id>",
  "amount": "2.50",
  "token": "USDC"
}'
```

As an MCP tool server, e.g. for Claude Desktop — add to its config:

```json
{
  "mcpServers": {
    "moramba-payment-agent": {
      "url": "http://127.0.0.1:PORT/mcp/"
    }
  }
}
```

One process, one port, one wallet: `moramba-payment-agent-serve` mounts
the same MCP tool server at `/mcp` (they share a single `Agent`
instance, so there's one DB/ledger connection, not two) — a partner
doesn't run a second process just to expose the MCP surface. Prefer a
standalone MCP process instead (its own auto-picked port, via
`moramba-payment-agent-mcp`), or the original stdio-subprocess form
(`"command": "/absolute/path/to/.venv/bin/moramba-payment-agent-mcp"`,
with `MCP_TRANSPORT=stdio` set) if your MCP client can't connect over
HTTP.

No key or secret goes in that config — the `.env` lives in `cwd` and is
read once when the process starts (README section 7).

## 1. Why

Moramba already has AI agents that can *decide* things (the moramba-chat-agent
assistant, and third-party agents that talk to our ACP/UCP checkout).
None of them can currently *pay* on their own — a human always has to sign.
This project is the missing "hands": a small local service that holds a
signing key and executes a payment exactly once, exactly within the
spend limits the partner already configured in Moramba, and refuses
everything else.

## 2. Non-negotiable security model

- **The private key lives only in the user's own `.env` file, on their
  own machine.** It is never sent to Moramba, never logged, never part
  of any request body.
- **Moramba never becomes a custodian.** We only ever publish *limits*
  (via the existing `agents` table / public API) — we never hold or
  move the user's funds.
- **"Brain" vs "hands."** Any AI (moramba-chat-agent, Claude Desktop,
  ChatGPT, a partner's own bot) is the *brain* — it may ask for a
  payment. This agent is the *hands* — it independently re-checks the
  spend limit and the protocol rules before signing anything. The brain
  cannot talk the hands into exceeding a limit; the check happens in
  code, not in a prompt.
- **The limits are not invented for this project.** They are the same
  `per_transaction_limit` / `daily_transaction_limit` /
  `monthly_transaction_limit` / `vendor_wise_spending_limit` fields that
  already exist on Moramba's `agents` table today, read live from
  Moramba's public agent API (`GET /api/v2/morambacrypto/public/agent`,
  confirmed unauthenticated) on every payment attempt. A source read of
  moramba-crypto-api found that the backend itself only enforces
  `per_transaction_limit` and `maximum_payout_limit` live today — daily,
  monthly, hourly, vendor-wise, aggregate and rate limits all need a
  spend-history table the backend doesn't have yet (see `ap2_service.rs`'s
  own `check_agent_limits` comment). This agent's local ledger is exactly
  that spend-history table — it enforces every one of those fields
  itself, on the sending side, rather than waiting for the backend to
  grow one.
- **One wallet, one job.** The wallet this agent signs with must be
  dedicated to it alone — never reused for a manual, independent
  transfer outside the agent. On-chain data can only prove *which
  address* signed a transaction, not *which software* built it; a
  manual send from the same wallet looks identical on-chain to one the
  agent actually made under its own limit checks. A shared wallet
  quietly breaks the whole limits/audit story built on top of it, so the
  setup wizard treats this as a requirement, not a suggestion.
- **Only a supported token, on a supported network, is ever payable —
  never an arbitrary one.** Every rail's `token` argument is checked
  against this agent's own `payout_config.allowed_tokens`, read live on
  every attempt (same call as the limits above). An unconfigured/empty
  list means *nothing* is payable, not "no restriction" — an agent only
  ever spends in what Moramba explicitly configured it for. The Pay
  Button rail (section 4) extends this to the network as well: a
  multi-network button's method for a supported token is still rejected
  if that specific method isn't on the chain this agent is configured
  for on that token.

## 3. Architecture

```
                    ┌────────────────────────────┐
                    │   Any AI / chat client      │
                    │  (moramba-chat-agent,       │
                    │   Claude Desktop, ChatGPT)  │
                    └──────────────┬───────────────┘
                                   │ "pay $5 to shop X"
                                   │ (business params only —
                                   │  never sees the key)
                                   ▼
                    ┌────────────────────────────┐
                    │   moramba-payment-agent     │
                    │   (runs on the user's PC)   │
                    │                              │
                    │  1. loads key from .env once │
                    │  2. fetches live limits from │
                    │     Moramba's agents API     │
                    │  3. re-checks the limit       │
                    │  4. picks a protocol adapter  │
                    │  5. signs & sends              │
                    └───┬─────────┬─────────┬──────┘
                        │         │         │
                 AP2 mandate  x402 402   MPP payout
                  signing     challenge   signature
                        │         │         │
                        ▼         ▼         ▼
                 Merchant / Moramba ACP checkout
                 or MPP receiver's /pay endpoint
```

Three ways to run the same core engine (all share one Python package):

| Surface | Who uses it | Example |
|---|---|---|
| **Library** | a developer embedding payment logic in their own Python app | `from moramba_payment_agent import Agent` |
| **FastAPI service** | a partner's own backend calling it over HTTP, on their own network | all under `/payment-agent-api`: `POST pay/mpp`, `POST pay/x402`, `POST pay/ap2`, `POST pay/button`, `POST pay/agent`, `POST transfer`, `POST limits/check`, `GET payments`, `GET health` |
| **MCP tool server** | any MCP-compatible AI chat client (Claude Desktop, ChatGPT) | tools: `pay_via_ap2`, `pay_via_x402`, `pay_via_mpp`, `pay_via_pay_button`, `pay_agent`, `transfer_erc20`, `check_spend_limits`, `list_payments` — served at `/mcp` on the FastAPI service's own port (or standalone via `moramba-payment-agent-mcp`) |

## 4. Supported payment rails

### AP2 (Agent Payments Protocol) — Autonomous mode, against Moramba's own live implementation
Moramba's own ACP checkout already has a real, deployed AP2 Autonomous
flow (merged PRs `feat_ap2_autonomous_real_settlement`,
`fix_ap2_autonomous_unit_mismatch`) — no human, no browser, the agent
signs everything itself. Ported line-for-line from
`moramba-acp-mcp/test-ap2-autonomous-settle.mjs` in moramba-crypto-api
(the actual reference script from that PR), cross-checked against the
server's own `ap2_service.rs`.

Two signing schemes are involved, and they're not interchangeable:

1. **The Closed Mandate** — a plain EIP-191 `personal_sign` over a fixed
   string (`"Moramba AP2 Autonomous Checkout\n\nSession: ...\n..."`),
   POSTed to `authorize_autonomous`. This just authorizes the checkout
   in principle — the server checks the signature against the agent's
   registered wallets plus its live `per_transaction_limit`/
   `maximum_payout_limit`, but **no funds move here**.
2. **The actual settlement** — an EIP-712 typed-data signature, over one
   of four payloads depending on what the token contract actually
   supports, auto-detected on-chain: `authorization` (EIP-3009,
   preferred), `permit` (EIP-2612 + a relay meta-transfer), `permit2`
   (Uniswap's canonical Permit2 contract), or `plain` (an `approve()` +
   a relay meta-transfer) — submitted to the matching
   `payrequest/pay-{flow}/.../relay` endpoint, then polled until it
   settles on-chain.

Two-phase like x402: the checkout amount isn't known until
`create_checkout_session` returns, so the limit check runs **between**
session creation and the Closed Mandate signature — nothing is signed
before it passes. Known limitation: the limit check only has the
checkout's fiat currency (e.g. `"USD"`) to work with at that point, not
the crypto token actually settled later — an `allowed_tokens` list keyed
on crypto symbols won't match it (see `engine.py`'s `pay_via_ap2`
docstring). Also known (documented server-side, not something this
project can fix): Moramba's own `authorize_autonomous` only enforces
`per_transaction_limit`/`maximum_payout_limit` live — daily/monthly/
hourly/count limits for this endpoint are a stated v1 gap on the backend
itself, same shape as the MPP/ERC20/x402 gap this project's local ledger
already closes for those rails.

### x402 — real spec, official SDK, not hand-rolled
Two Moramba x402 deployments exist in this codebase family, and they
turned out to speak **different, incompatible wire protocols** on
**different chains** — neither on Tempo:
`moramba-crypto-x-402-monad-network` uses a custom tx-hash-reference
scheme on Monad testnet; `weather-x402-api-ts` (live at
`crypto.moramba.io/weather-api`) uses the real x402 v2 "exact" scheme
(EIP-3009, verified by a public facilitator) on Base Sepolia. This
project targets the latter — the spec-compliant one.

Built on the official `x402` PyPI package (`x402[evm,httpx]`,
x402-foundation/x402) rather than hand-rolled EIP-712 signing, so wire
compatibility is guaranteed by the same code the server ecosystem
verifies against — confirmed against `weather-x402-api-ts`'s own
`PAYMENT-REQUIRED`/`PAYMENT-SIGNATURE`/`PAYMENT-RESPONSE` headers (the
real v2 spec names; `X-PAYMENT` is explicitly "V1 legacy" in the SDK).

Two-phase by design (`probe()` then `settle()`), unlike MPP/ERC20:
x402's amount, token, and recipient aren't known until the server's own
402 challenge arrives, so the spend-limit check has to run **between**
discovering the requirement and ever signing anything — never before
(nothing to check yet) and never after (too late). The official SDK
also applies its own `spend_controls` (default-assets allowlist, a
per-payment cap) as a second, independent safety net before our check
ever runs.

### MPP — matches `mppx-agent-endpoint-server-ts` exactly
Verified by reading that sibling project's actual source (not its README,
which is out of date):

1. Sign the **fixed string** `"I am doing transaction with this account"`
   with the local wallet key (EIP-191 personal-sign).
2. `POST` to `/agent-api/payout/agent/:agent_id/pay` (or
   `/agent-api/receiver/agent/:agent_id/pay` — both routes hit the same
   handler; `:agent_id` in the URL is always the **receiver's** agent id):
   ```json
   {
     "payment_via": "agent",
     "payment_to": "agent",
     "payout_agent_id": "...",
     "to_address": "...",
     "amount": "...",
     "token": "...",
     "signature": "0x..."
   }
   ```
3. The receiving server resolves the recipient, checks its own limits,
   and — when `payment_via` is `"agent"` — pulls the payer agent's
   record straight from Moramba's existing
   `GET /api/v2/morambacrypto/public/agent` endpoint and re-checks
   *those* limits too. Our agent doesn't need to duplicate that check on
   the receiving side — only on the sending side, before it ever signs.

### Moramba Pay Button — pay an existing button by its `button_id` — done, live-tested
A fifth rail: paying one of the partner's own Moramba Pay Buttons
directly — the same thing a human pays by opening the button's link —
without going through an ACP checkout session or an AP2 mandate at all.
An initial pass at this misread the backend as having no clean JSON API
for it (see git history of this file if curious) — that was wrong. The
real flow below was verified live against a running local server
(2026-09-29), not just read from source:

1. **`POST /morambacrypto/public/payin/create/by/button_id/{button_id}`**
   with `{"network": "...", "token_address": "0x...", "amount": "..."}` —
   a clean, public, JSON endpoint, and one built specifically for this
   use case: internally it's tagged `user_type: "aiagent"`
   (`payin_user_controller.rs`). `amount` is only required when the
   button is variable-amount (`fixed_amount: false`); a fixed-amount
   button ignores any amount sent and uses its own configured one.
   Validates `max_uses`/`multi_use` server-side. The response's `id`
   field *is* the payin_id — confirmed live:
   ```json
   {"success":true,"data":{"id":"4a386e6f-...","payout_destination_address":"0x6784...","amount":"1000000","payin_status":"pending","button_id":"ee33ffd3-...", ...}}
   ```
2. **A button's supported network/token now has a clean JSON endpoint
   too**: `GET /morambacrypto/public/pay-button/{button_id}/methods`
   (added to moramba-crypto-api specifically to close this gap — new
   files: `pay_button_methods_dtos.rs`, `pay_button_methods_controller.rs`,
   `pay_button_methods_route.rs`, wired in with a one-line append each to
   `dtos/mod.rs`/`controllers/mod.rs`/`routes/mod.rs`). It composes the
   same two service calls the old HTML-page flow already made internally
   (`get_public_pay_button_details_service` +
   `find_payout_destination_merchant_by_req_id` — confirmed via source
   read that neither was ever exposed as its own public route before
   this), but as pure JSON with no side effects: unlike the HTML page
   (`GET /morambacrypto/public/pay/{button_id}`), it does **not** create
   a throwaway `payin_users` row on every call, and it is **not** gated
   by `max_uses`/`multi_use` — discovering what a button supports
   shouldn't require it still having uses left. `agent/adapters/pay_button.py`
   now calls this instead of scraping HTML; the old scraping code (regex
   over `<script>` content) is gone.
3. **Settlement is the exact same generic pipeline the AP2 rail already
   implements** — confirmed live, not just from source: the payin_id from
   step 1 was fed straight into `GET /morambacrypto/public/payrequest/init/{payin_id}/address/{wallet}`
   and it worked immediately, returning the same shape AP2's settlement
   already consumes (`transaction_id`, `to`, `amount`, `nonce`, `chain_id`,
   `rpc`, `token_address`, `verify_sc_address`, `decimal`). This rail can
   reuse essentially all of `ap2.py`'s settlement code as-is — only step
   1–2 above (getting to a `payin_id`) differs from AP2's
   create-session/authorize-autonomous steps.
4. **Bonus finding**: that live `payrequest/init` call returned
   `"chain_id":42431,"rpc":"https://rpc.moderato.tempo.xyz"` — a **real
   Tempo testnet RPC URL**, resolving the `RPC_URL=` blank left in
   `.env.example` since section 1. Worth confirming with the team before
   committing it anywhere, but this is the first real value seen for it.
5. **Public, unauthenticated** — same "the `payin_id` itself is the
   capability" model as every other `payrequest/*` endpoint; no API key
   needed for either the create-payin or the init call.
6. **Method selection prefers, then enforces, the agent's own
   token/network.** A button can list several payout methods (one per
   network/token); found live (2026-10-01): a button accepting both
   `usdc` and `pathUSD` always picked `usdc` (`payout_methods[0]`)
   regardless of which tokens the paying agent actually had in its own
   `allowed_tokens`, so an agent only configured for `pathUSD` got
   rejected even though the button also had a `pathUSD` method it could
   have used. `resolve_payment_plan` now takes `preferred_tokens` (the
   agent's own `allowed_tokens`) and picks a matching method when one
   exists, case-insensitively — and when the same preferred token exists
   on more than one network (e.g. `pathUSD` on both testnet and
   mainnet), `preferred_network_hint` (derived from the agent's own
   configured chain) breaks the tie, since `ButtonPayoutMethod` carries
   no chain id to match on directly, only a network slug string.
   Falls back to the first method only when nothing matches. This
   preference is then **enforced, not just hoped for**: after resolving
   a method, `pay_via_pay_button` checks it against this specific
   token's own configured chain (from the agent's `payout_tokens`) and
   rejects outright on a mismatch — it will never actually settle a
   payment on a network this agent isn't configured to use that token
   on, even if no better-matching method existed on the button at all.
   An explicit `network` is trusted as the caller's own deliberate
   choice and skips every part of this.
7. **A variable-amount button's `amount` must be sent in minor units,
   not human units.** Found live (2026-10-01): paying "1 pathUSD" on a
   variable-amount button settled `0.000001` pathUSD on-chain instead.
   Root cause confirmed from the backend
   (`create_ai_payin_user_by_button_id_controller` in
   `payin_user_controller.rs`): for a variable button, the request's
   `amount` is stored as the payin's `base_amount` completely unscaled —
   the exact same way a *fixed* button's own `amount_with_decimal` field
   (already minor units, e.g. `"1000000"`) is used. `pay_button()` now
   multiplies the human-unit amount by `10 ** decimals` before sending
   it, the inverse of the division `resolve_payment_plan` already does
   to convert a fixed button's `amount_with_decimal` the other way.

Implemented (`agent/adapters/pay_button.py`), reusing AP2's settlement
primitives directly (import, not duplication — see section 8, item 8).
Two-phase like x402/AP2, for a different reason: resolving the button's
amount/token/recipient (step 2 above) is read-only, but actually
creating a payin (step 1 above) is a real, one-shot server-side side
effect — the limit check runs between those two steps, so a rejection
never triggers it. 7 tests passing, including one asserting a rejected
payment never calls the create-payin endpoint at all, and one asserting
a partial settlement failure still preserves the `payin_id`/`flow` it
already knew (a real bug found live — see below).

**Live-tested end to end, 2026-09-29** — settled a real payment on Tempo
testnet against production:
[`0x43d1d64f...`](https://explore.testnet.tempo.xyz/tx/0x43d1d64fa09f1db9710b780d5382e4d10f5c4762718047285470c57e9aa34194).
Getting there surfaced three real bugs, none of them hypothetical:

1. **Whole units, not cents.** `AgentLimits`'s limit fields were assumed
   to be USD cents (matching the Rust field's own doc comment) — real
   production data showed partners actually set these as whole USD
   units. A `per_transaction_limit` of `5` meant $5, not $0.05; a $1
   payment was being computed as $1.00 > $0.05 and rejected ~100x too
   aggressively. Fixed by dropping the `_cents` suffix and comparing
   `Decimal` amounts directly, no unit conversion (`limits_client.py`,
   `limit_check.py`).
2. **`allowed_tokens` is a list of objects, not strings.** The real API
   returns full token records (`{token_name, network, chain, ...}`);
   this client stored the raw list as-is, silently turning every
   `allowed_tokens` check into an always-reject once a real agent had
   any tokens configured — every test fixture until then used an empty
   list, which is why it went unnoticed. Fixed with proper extraction
   plus case-insensitive matching (a button's `payment_token_name` came
   through as `"pathusd"`, Moramba's own registry spells it `"pathUSD"`).
3. **A real backend gap, not ours**: the settlement relay returned
   `500 "Requested application data is not configured correctly"` —
   Actix's own error for a `web::Data<T>` extractor that was never
   registered. Traced to `main.rs`: `RelayQueueClient` is only
   registered when `RelayQueueClient::connect(&config.nats_url)`
   succeeds at boot; on this deployment NATS was firewalled off
   (different droplet, same private subnet, blocked at the NATS host's
   firewall), so `relay_queue_client` was `None` and every
   `payrequest/pay-*` flow — not just this one — was broken. Fixed by
   opening the firewall between the two hosts and restarting the
   service. While diagnosing this, also found and fixed a real bug in
   *this* project: `pay_button.py` and `ap2.py` both discarded the
   `payin_id`/`flow` they already knew when a settlement failed
   partway through — exactly the detail needed to know a payin exists
   server-side (and, for the `permit2`/`plain` flows, that a real
   on-chain `approve()` may have already gone out) when the final relay
   call is what actually fails.

### Plain ERC20 transfer
The fallback rail: no counterparty protocol, just a direct on-chain
transfer, still gated by the same local limit check.

`token` resolves by name against the agent's own configured payout
tokens (`GET .../public/agent`'s `payout_config.allowed_tokens`) — every
token the agent supports is payable this way, each on its own
chain/RPC, not only the one contract address written to
`DEFAULT_TOKEN_CONTRACT` at setup time. A `token` this agent isn't
configured for is rejected outright ("not supported by this agent"), an
unconfigured/empty token list rejects every token rather than allowing
any of them through (README section 2) — there is no fallback contract
for an unrecognized token. An explicit `token_contract_address` still
overrides which contract address gets called, but `token`'s name is
still checked against `allowed_tokens` regardless, and it still uses the
wallet's own `.env` chain/RPC, since there's no other network to infer
it from. Gas is estimated live per-transaction (node's own
`eth_estimateGas` + 20% margin), not a fixed guess — found live
(2026-09-30): a token needing ~271k gas was rejected against a
hardcoded 100k limit. If the RPC node's own estimate itself isn't
obtainable, a fixed fallback (150k) is used and a warning is logged
naming which — this fallback can still undershoot a specific token's
real cost, as happened live even after the fix above (needed ~271k,
got the 150k fallback because estimation itself failed on that node).
For that case, `transfer_erc20` also takes an explicit `gas_limit`
override — meant to be set only after a failed attempt's error names
the actual gas needed, and only with a human's go-ahead (an AI agent
calling this as a tool should ask before raising it, not decide alone).

Before building or signing anything, both balances this transfer
actually needs are checked: the token balance (`balanceOf`, against the
amount being sent) and the native balance (against the resolved gas
limit × gas price). Either coming up short rejects the payment with a
clear reason (`"insufficient token balance: have X, need Y"` /
`"insufficient native balance for gas: ..."`) instead of broadcasting a
transaction that would revert on-chain — a revert still costs the gas
already spent getting it mined, so checking first is strictly cheaper,
not just a nicer error message. This rail shares its on-chain sending
code (`agent/adapters/erc20.py`) with `pay_agent`, so the same checks
apply there too.

### Network
**Tempo testnet (chain id 42431) first.** Mainnet and other chains are a
later phase, once the testnet flow is proven end-to-end.

## 5. Payment history — local ledger, synced live to Moramba

Every attempt — settled, rejected, or failed — is written to a local
SQLite ledger **first** (timestamp, rail, recipient, amount + token, tx
hash/signature, outcome), then synced to **Moramba's own Postgres** on
every write. SQLite is the fast, always-available copy the agent reads
from for its own limit checks; Moramba's Postgres is the durable,
central copy a partner's dashboard — and the boss — reads from. "How
many payments has this agent made, and to whom" is answered from there,
not by opening a file on someone's laptop.

> **Backend: done (2026-09-29).** `POST /api/v2/morambacrypto/public/agent/:agent_id/payments/sync`
> is live in moramba-crypto-api (migration 302,
> `agent_payment_sync_service.rs`). It reconstructs the same canonical
> JSON this project signs and verifies `attestation_signature` against
> the agent's registered payout wallet, checks the agent is active,
> rejects an unregistered wallet, and de-dupes retried syncs so a record
> is never stored twice. A companion `GET /morambacrypto/agent/:agent_id/payments`
> (partner-JWT authenticated) is what a partner's dashboard — and the
> boss — reads from. Verified with `cargo check` + a passing Rust test
> suite (3 new tests, including one that locks the exact canonical-JSON
> format against this project's `sync.py` so the two sides can't drift
> apart silently); not yet exercised as a real HTTP round trip against a
> running server.

This is load-bearing, not just visibility. MPP already reports into
Moramba's own transaction tables, so its receiver-side limit check
trusts Moramba's data (section 4). AP2, x402, and plain ERC20 don't —
without this sync, their limit math would rest entirely on a local file
nobody else could verify. Syncing every record closes that gap: Moramba
can see, and in a later phase independently re-check, spend on those
three rails the same way it already does for MPP.

If Moramba is briefly unreachable, the write still lands in SQLite
immediately and queues for sync (simple retry/outbox, not a blocking
call) — a network hiccup to Moramba never blocks or drops a payment, and
the local copy stays authoritative for the agent's own limit checks in
the meantime. **Only transaction metadata syncs — never the private
key.** The key never leaves the `.env` it started in.

**Attribution — why Moramba can trust a synced record instead of just
believing it.** The sync payload isn't a bare claim from the agent; it
carries the same signature that authorized the payment itself — the
wallet signature for MPP/ERC20, the mandate signature for AP2/x402 —
matched against that agent's own `public_wallet_address` already on file
in Moramba's `agents` table. Moramba verifies that signature before
accepting the record, the same check it would already do for the
payment itself, so it's never trusting the agent's word for who made a
payment.

**If a record is delayed, or never arrives at all.** The retry/outbox
queue keeps trying as long as the agent process is running, so a
temporary outage only delays the sync — it doesn't lose it. If the
agent's machine disappears before it ever syncs, the transaction itself
isn't gone: every rail here settles on-chain, so it's checkable on Tempo
straight from the agent's wallet address as a last-resort backstop.

That backstop only works cleanly because of the one-wallet-one-job rule
above. A manual transfer sent independently from the same wallet,
outside the agent entirely, looks identical on-chain to one the agent
made under its own limit checks — chain data can't tell those apart.
Plain ERC20 transfers are the most exposed rail here, since a manual
send is just a value transfer with no protocol wrapper to distinguish
it. MPP, AP2 and x402 at least require a specific signed artifact the
agent's own code produces, which a plain wallet UI wouldn't create by
accident — but a deliberately reused key can still forge that too. The
dedicated-wallet rule, not the presence of a protocol wrapper, is what
actually keeps this guarantee intact.

Exposed the same three ways as everything else:

| Surface | How |
|---|---|
| Library | `agent.history(since=..., recipient=...)` |
| FastAPI service | `GET /payment-agent-api/payments` |
| MCP tool server | `list_payments` — reads the fast local copy, so it still works if Moramba is briefly unreachable |

## 6. Setup — "plug and play"

One command:
```
python -m agent.setup
```
An interactive wizard that asks for: the partner's wallet private key
(written straight to a local `.env`, never echoed back or transmitted),
the Moramba agent id whose limits to enforce, and which rails to enable.
No Docker — not needed for this project; the venv + wizard is the whole
onboarding story.

## 7. MCP security boundary

This is the part that needs to be explicit, since it's the one place an
LLM and a private key are in the same room:

- The key is loaded **once**, at MCP server startup, from the server's
  own local `.env` — never from a tool call argument.
- **No tool's input schema ever includes the key.** The LLM only ever
  passes business parameters (`merchant_url`, `amount`, `token`).
- The key is **not** placed in the MCP client's config (e.g. Claude
  Desktop's `claude_desktop_config.json` only needs the server's URL, or
  the command to launch it in stdio mode — not the key itself).
- The spend-limit check runs unconditionally inside the tool handler, in
  code — the LLM cannot skip it by phrasing a request differently.

## 8. Phased delivery

1. **Core engine — done, backend sync included.** Local SQLite ledger,
   the outbox sync client (now backed by a real, verified endpoint — see
   section 5), the full local limit check against Moramba's public agent
   API, the MPP adapter (verified against
   `mppx-agent-endpoint-server-ts`'s source), and plain ERC20 transfer,
   targeting Tempo testnet. 31 Python tests + 3 Rust tests passing.
   Outstanding before this phase is really "done": get a real Tempo
   testnet RPC URL and run one live payment end-to-end against a running
   server (everything so far is tested against mocks/a local dev DB, not
   a live HTTP round trip).
2. **x402 adapter — done.** Built on the official `x402` SDK, targeting
   the real v2 "exact" scheme (Base Sepolia USDC, matching
   `weather-x402-api-ts`). Two-phase `probe()`/`settle()` so the limit
   check runs after the amount is known but before anything is signed.
   7 tests passing. Not yet run against the live `weather-x402-api-ts`
   deployment end-to-end (tested against mocks + the real SDK's own
   encode/decode functions).
3. **AP2 adapter — done.** Autonomous mode, ported from Moramba's own
   real settlement script; both signing schemes (EIP-191 mandate,
   EIP-712 settlement across 4 auto-detected flows). 13 tests passing.
   Not yet run against a live checkout end-to-end (tested against mocks
   + the reference script's exact message/domain formulas).
4. **FastAPI service wrapper — done.** Thin translation layer over the
   core engine (`agent/api.py`), all routes under an `/payment-agent-api`
   prefix (an `APIRouter`, not string-prefixed by hand): `POST pay/mpp`,
   `POST pay/x402`, `POST pay/ap2`, `POST transfer`, `POST limits/check`,
   `GET payments`, `GET health`. Single-tenant by design — one process,
   one wallet, meant to run on the partner's own machine, not a shared
   multi-tenant API. 7 tests passing. Caught and fixed one real bug along
   the way: SQLite connections default to single-thread-only, but FastAPI
   runs sync routes in a worker thread pool — `Ledger` now opens with
   `check_same_thread=False`.
5. **Setup wizard — done, then simplified further (2026-09-30).**
   `python -m agent.setup`: validates the private key and Moramba agent
   id as you type them (re-prompts rather than accepting garbage), and
   re-prompts for a different wallet key (with an explicit override) if
   it isn't registered to that agent in Moramba — rather than just
   warning and moving on. Network (`CHAIN_ID`/`RPC_URL`) and the ERC20
   rail's default token contract are auto-detected from the agent's own
   `payout_config.allowed_tokens` instead of being asked, with a
   hardcoded known-RPC fallback per chain if that lookup fails or the
   agent's own token record has no `rpc_url`. Every rail is always
   enabled — no "which rails" step. Every remaining prompt requires a
   real answer; nothing is skippable or silently defaulted. Writes `.env`
   at `0600` permissions, never echoes or logs the key. Interactive flow
   is injectable (`input_fn`/`getpass_fn`) so it's fully unit-tested
   without touching real stdin. 13 tests passing.
6. **MCP tool server — done.** `agent/mcp_server.py`, built on the
   official `mcp` SDK: `pay_via_ap2`, `pay_via_x402`, `pay_via_mpp`,
   `transfer_erc20`, `check_spend_limits`, `list_payments` — the exact
   set section 6 promised. 6 tests passing, including one that asserts
   no tool's input schema contains anything key-shaped (the MCP security
   boundary in section 7, checked in code, not just written down).
7. ~~Docker packaging~~ — dropped. Not needed for this project.
8. **Pay Button rail — done, discovery gap closed on the backend too.**
   Pay an existing Moramba Pay Button directly by `button_id` (section
   4), live-tested against a running server. Discovering a button's
   supported network/token used to require HTML-scraping; a new public
   endpoint (`GET .../pay-button/{button_id}/methods`, added to
   moramba-crypto-api as 3 new files) closed that gap, and
   `agent/adapters/pay_button.py` now calls it directly — no scraping
   code left. Reuses AP2's settlement primitives (`detect_flow`, the
   four `build_*_pay_body` functions, `submit_relay_payment`,
   `poll_payment_status`) by importing them directly from `ap2.py`
   rather than duplicating them — wired into the FastAPI service
   (`POST /payment-agent-api/pay/button`) and the MCP server
   (`pay_via_pay_button`) too.
   8 tests passing. Still outstanding: extracting that shared settlement
   code into its own module instead of importing it from `ap2.py` (a
   plain refactor, deferred — not needed for correctness, just tidiness
   now that two rails depend on it).

## 9. Explicitly out of scope for v1

- Custody of any kind — the key never leaves the user's machine.
- Mainnet — Tempo testnet only, until the flow is proven.
- Docker — not needed for this project; the setup wizard covers onboarding.
- A UI — v1 is library + API + MCP tools only, no dashboard.
