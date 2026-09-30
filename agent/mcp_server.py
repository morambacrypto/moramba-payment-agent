"""MCP tool server — the same core engine (agent/engine.py) exposed as
tools for any MCP-compatible client (Claude Desktop, ChatGPT).

Security boundary (README section 7 — the one place an LLM and a private
key are in the same room):

- The key loads once, here, from this process's own local `.env` — via
  `Settings()` inside `_get_agent()`, on first tool call, never from a
  tool argument.
- No tool below takes the key, or anything key-shaped, as a parameter —
  every signature is business parameters only (amount, token, recipient,
  url).
- The MCP client's own config (e.g. Claude Desktop's
  `claude_desktop_config.json`) only needs the command to launch this
  process — the key lives in the process's `.env`, not the client config.
- The spend-limit check is not optional or promptable-around: every
  `pay_via_*` tool calls the exact same `Agent` method the library and
  FastAPI surfaces use, which runs the check in code before anything is
  signed (README section 2).
"""

import os
from decimal import Decimal, InvalidOperation
from typing import Any

from mcp.server.mcpserver import MCPServer

from agent.config import Settings
from agent.engine import Agent
from agent.ledger import PaymentRecord
from agent.serve import find_free_port

mcp = MCPServer("moramba-payment-agent")

_agent: Agent | None = None


def _get_agent() -> Agent:
    global _agent
    if _agent is None:
        _agent = Agent(Settings())
    return _agent


def _parse_amount(raw: str) -> Decimal:
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"invalid amount: {raw!r}") from exc


def _record_to_dict(record: PaymentRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "created_at": record.created_at,
        "rail": record.rail,
        "recipient": record.recipient,
        "token": record.token,
        "amount": str(record.amount),
        "chain_id": record.chain_id,
        "status": record.status,
        "tx_hash": record.tx_hash,
        "reason": record.reason,
        "receiving_agent_id": record.receiving_agent_id,
    }


@mcp.tool()
def pay_via_mpp(
    receiver_base_url: str,
    receiver_agent_id: str,
    amount: str,
    token: str,
    payout_agent_id: str | None = None,
    to_address: str | None = None,
    payment_via: str = "agent",
    payment_to: str = "agent",
) -> dict:
    """Pay another Moramba agent (or a raw wallet address) via MPP.

    Rejected attempts are still returned (status "rejected"), not raised
    as errors, with `reason` explaining which limit stopped it.
    """
    record = _get_agent().pay_via_mpp(
        receiver_base_url=receiver_base_url,
        receiver_agent_id=receiver_agent_id,
        amount=_parse_amount(amount),
        token=token,
        payment_via=payment_via,
        payment_to=payment_to,
        payout_agent_id=payout_agent_id,
        to_address=to_address,
    )
    return _record_to_dict(record)


@mcp.tool()
def transfer_erc20(to_address: str, amount: str, token: str, token_contract_address: str | None = None) -> dict:
    """Send a plain ERC20 transfer — subject to the same spend-limit check as every other rail."""
    record = _get_agent().transfer_erc20(
        to_address=to_address, amount=_parse_amount(amount), token=token, token_contract_address=token_contract_address
    )
    return _record_to_dict(record)


@mcp.tool()
def pay_via_x402(url: str, method: str = "GET", token_decimals: int = 6) -> dict:
    """Pay for an x402-protected resource. If the resource doesn't
    actually require payment, returns {"paid": false} rather than a
    payment record — nothing was spent."""
    record = _get_agent().pay_via_x402(url=url, method=method, token_decimals=token_decimals)
    if record is None:
        return {"paid": False, "detail": "resource did not require payment"}
    return _record_to_dict(record)


@mcp.tool()
def pay_via_ap2(items: list[dict], buyer_email: str, api_key: str | None = None) -> dict:
    """Autonomously complete a Moramba ACP checkout via AP2 — no human
    present. `items` is the same shape `create_checkout_session` takes,
    e.g. [{"id": "<pay_button_id>"}]."""
    record = _get_agent().pay_via_ap2(items=items, buyer_email=buyer_email, api_key=api_key)
    return _record_to_dict(record)


@mcp.tool()
def pay_via_pay_button(button_id: str, network: str | None = None, amount: str | None = None) -> dict:
    """Pay a Moramba Pay Button directly by its button_id — no ACP
    checkout session or AP2 mandate involved. `amount` is only needed for
    a variable-amount button; a fixed-amount button ignores it."""
    record = _get_agent().pay_via_pay_button(
        button_id=button_id, network=network, amount=_parse_amount(amount) if amount is not None else None
    )
    return _record_to_dict(record)


@mcp.tool()
def pay_agent(receiving_agent_id: str, amount: str, token: str | None = None) -> dict:
    """Pay another Moramba agent directly by its agent_id — a payout
    agent paying a receiving agent. The destination wallet and token
    contract come from that agent's own Moramba config, never a caller-
    supplied address; `token` only needs to be given when that agent
    accepts more than one."""
    record = _get_agent().pay_agent(
        receiving_agent_id=receiving_agent_id, amount=_parse_amount(amount), token=token
    )
    return _record_to_dict(record)


@mcp.tool()
def check_spend_limits(recipient: str, token: str, amount: str, rail: str) -> dict:
    """Dry run a spend-limit check without paying anything — the same
    check a real payment on `rail` would go through."""
    result = _get_agent().check_spend_limits(recipient=recipient, token=token, amount=_parse_amount(amount), rail=rail)
    return {"allowed": result.allowed, "reason": result.reason}


@mcp.tool()
def list_payments(recipient: str | None = None, rail: str | None = None, limit: int = 20) -> list[dict]:
    """List this agent's own payment history — reads the fast local
    ledger, so it still works if Moramba is briefly unreachable."""
    records = _get_agent().history(recipient=recipient, rail=rail, limit=limit)
    return [_record_to_dict(r) for r in records]


def main() -> None:
    """Serves over streamable-HTTP on localhost by default, so any
    MCP client can point at a URL instead of spawning this as a stdio
    subprocess — still single-partner, still non-custodial: the key
    only ever loads from this process's own local `.env`, unaffected by
    which transport carries the tool calls.

    Set MCP_TRANSPORT=stdio to fall back to the original subprocess
    style instead. MCP_HOST/MCP_PORT override the localhost/auto-port
    defaults (auto-port picked the same way `moramba-payment-agent-serve`
    does, via `find_free_port`)."""
    if os.environ.get("MCP_TRANSPORT", "streamable-http") == "stdio":
        mcp.run(transport="stdio")
        return

    host = os.environ.get("MCP_HOST", "127.0.0.1")
    pinned_port = os.environ.get("MCP_PORT")
    port = int(pinned_port) if pinned_port else find_free_port(host)

    print(f"moramba-payment-agent MCP server: http://{host}:{port}/mcp")
    if not pinned_port:
        print("(auto-selected a free port — set MCP_PORT to pin a specific one instead)")

    mcp.run(transport="streamable-http", host=host, port=port)


if __name__ == "__main__":
    main()
