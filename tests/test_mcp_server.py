import asyncio
import json

import httpx
import respx

from agent import mcp_server
from agent.config import Settings
from agent.engine import Agent
from agent.ledger import STATUS_SETTLED

TEST_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
AGENT_ID = "55555555-5555-5555-5555-555555555555"
MORAMBA_BASE = "https://moramba.example"

_KEY_LIKE_SUBSTRINGS = ("private_key", "privatekey", "secret", "wallet_key")

# The exact tool set README section 6's Surfaces table promises for the
# MCP server.
EXPECTED_TOOLS = {
    "pay_via_mpp", "transfer_erc20", "pay_via_x402", "pay_via_ap2", "pay_via_pay_button",
    "check_spend_limits", "list_payments",
}


def make_agent(tmp_path) -> Agent:
    settings = Settings(
        wallet_private_key=TEST_PRIVATE_KEY,
        moramba_agent_id=AGENT_ID,
        moramba_api_base_url=MORAMBA_BASE,
        chain_id=42431,
        rpc_url="https://tempo-testnet.example",
        db_path=str(tmp_path / "agent.db"),
    )
    return Agent(settings)


def mock_agent_lookup(wallet_address: str, **payout_overrides):
    payout_config = {
        "allowed_tokens": [], "wallets": [{"public_wallet_address": wallet_address}],
        "per_transaction_limit": None, "daily_transaction_limit": None, "monthly_transaction_limit": None,
        "vendor_wise_spending_limit": None, "aggregate_spending_limit": None, "maximum_payout_limit": None,
        "daily_transaction_count_limit": None, "hourly_transaction_limit": None,
    }
    payout_config.update(payout_overrides)
    return respx.get(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent", params={"agent_id": AGENT_ID}).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True, "message": "ok",
                "data": {
                    "id": AGENT_ID, "status": "active", "payout_config": payout_config,
                    "rate_limit_max_transactions_count": None, "rate_limit_per_period": None,
                },
            },
        )
    )


def test_expected_tools_are_registered():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {t.name for t in tools}
    assert EXPECTED_TOOLS.issubset(names)


def test_no_tool_input_schema_exposes_anything_key_shaped():
    """Security assertion for README section 7: no tool's parameters let
    a caller pass, or ask for, the private key."""
    tools = asyncio.run(mcp_server.mcp.list_tools())
    for tool in tools:
        properties = (tool.input_schema or {}).get("properties", {})
        for field_name in properties:
            lowered = field_name.lower()
            assert not any(s in lowered for s in _KEY_LIKE_SUBSTRINGS), (
                f"tool {tool.name!r} exposes a key-shaped parameter: {field_name!r}"
            )


def _call(name: str, arguments: dict):
    """Call a tool and parse its JSON text content — `structured_content`
    isn't populated for a plain `dict`/`list` return annotation (no fixed
    schema to build it from), so the text content is the reliable one."""
    result = asyncio.run(mcp_server.mcp.call_tool(name, arguments))
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


@respx.mock
def test_check_spend_limits_tool(tmp_path):
    agent = make_agent(tmp_path)
    mock_agent_lookup(agent.wallet.address, per_transaction_limit=0)
    mcp_server._agent = agent
    try:
        result = _call("check_spend_limits", {"recipient": "vendor-a", "token": "USDC", "amount": "5", "rail": "mpp"})
        assert result["allowed"] is False
        assert "per_transaction_limit" in result["reason"]
    finally:
        mcp_server._agent = None
        agent.close()


@respx.mock
def test_pay_via_mpp_tool_settles(tmp_path):
    agent = make_agent(tmp_path)
    mock_agent_lookup(agent.wallet.address)
    respx.post(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/agent/{AGENT_ID}/payments/sync").mock(
        return_value=httpx.Response(404)
    )
    respx.post("https://receiver.example/agent-api/payout/agent/receiver-agent-1/pay").mock(
        return_value=httpx.Response(200, json={"success": True, "tx_hash": "0xmcptx"})
    )
    mcp_server._agent = agent
    try:
        result = _call(
            "pay_via_mpp",
            {
                "receiver_base_url": "https://receiver.example",
                "receiver_agent_id": "receiver-agent-1",
                "amount": "2.5",
                "token": "USDC",
                "payout_agent_id": "payer-agent-9",
            },
        )
        assert result["status"] == STATUS_SETTLED
        assert result["tx_hash"] == "0xmcptx"
    finally:
        mcp_server._agent = None
        agent.close()


def test_list_payments_tool_reads_local_ledger(tmp_path):
    from decimal import Decimal

    agent = make_agent(tmp_path)
    agent.ledger.record(rail="mpp", recipient="r1", token="USDC", amount=Decimal("1"), status=STATUS_SETTLED)
    mcp_server._agent = agent
    try:
        # A list-returning tool serializes each element as its own content
        # block rather than one JSON array — parse them individually.
        raw = asyncio.run(mcp_server.mcp.call_tool("list_payments", {}))
        items = [json.loads(c.text) for c in raw.content]
        assert len(items) == 1
        assert items[0]["rail"] == "mpp"
    finally:
        mcp_server._agent = None
        agent.close()


def test_pay_via_x402_tool_reports_no_payment_needed(tmp_path):
    agent = make_agent(tmp_path)
    mcp_server._agent = agent
    try:
        with respx.mock:
            respx.get("https://weather.example/free").mock(return_value=httpx.Response(200, json={"data": "free"}))
            result = _call("pay_via_x402", {"url": "https://weather.example/free"})
        assert result["paid"] is False
    finally:
        mcp_server._agent = None
        agent.close()


def test_pay_via_pay_button_tool_rejects_by_local_limit(tmp_path):
    agent = make_agent(tmp_path)
    button_id = "ee33ffd3-0816-4649-b979-63679351f82f"
    methods_response = {
        "success": True, "message": "button payment methods fetched",
        "data": {
            "button_id": button_id, "fixed_amount": True,
            "methods": [
                {
                    "payout_destination_id": "po-1", "network": "tempo_testnet", "token_name": "pathusd",
                    "token_address": "0x20c0000000000000000000000000000000000000",
                    "to_wallet_address": "0x6784f65225f7d567cf1535525b0dd720b1450d1b",
                    "decimals": 6, "amount_with_decimal": "1000000", "is_default": False,
                }
            ],
        },
    }
    mcp_server._agent = agent
    try:
        with respx.mock:
            mock_agent_lookup(agent.wallet.address, per_transaction_limit=0)
            respx.get(f"{MORAMBA_BASE}/api/v2/morambacrypto/public/pay-button/{button_id}/methods").mock(
                return_value=httpx.Response(200, json=methods_response)
            )
            result = _call("pay_via_pay_button", {"button_id": button_id})
        assert result["status"] == "rejected"
        assert "per_transaction_limit" in result["reason"]
    finally:
        mcp_server._agent = None
        agent.close()
