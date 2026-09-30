import httpx
import respx

from agent.adapters.ap2 import Ap2Error
from agent.limits_client import MorambaAgentClient

BASE_URL = "https://moramba.example"
AGENT_ID = "88888888-8888-8888-8888-888888888888"
RECEIVING_AGENT_ID = "99999999-9999-9999-9999-999999999999"


@respx.mock
def test_get_agent_extracts_token_names_from_real_object_shaped_allowed_tokens():
    """Real production data (2026-09-29): `allowed_tokens` is a list of
    full token objects, not plain strings. A prior version of this client
    stored the raw list as-is, silently turning every `allowed_tokens`
    check into an always-reject once a real agent had any tokens
    configured — every test fixture until now used an empty list, which
    is why that went unnoticed."""
    respx.get(f"{BASE_URL}/api/v2/morambacrypto/public/agent", params={"agent_id": AGENT_ID}).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True, "message": "ok",
                "data": {
                    "id": AGENT_ID, "status": "active",
                    "payout_config": {
                        "allowed_tokens": [
                            {
                                "id": "20c6d642-75d1-4fcc-8728-3cb78b6bb0ea", "network": "Tempo", "chain": 4217,
                                "network_type": "mainnet", "rpc_url": "https://rpc.tempo.xyz",
                                "token_name": "pathUSD", "token_address": "0x20c0000000000000000000000000000000000000",
                                "is_active": True, "created_at": "2026-06-04T11:49:28.347100Z",
                                "updated_at": "2026-06-09T11:11:28.424945Z",
                            }
                        ],
                        "wallets": [{"public_wallet_address": "0xabc"}],
                        "per_transaction_limit": 5,
                    },
                    "rate_limit_max_transactions_count": None, "rate_limit_per_period": None,
                },
            },
        )
    )

    client = MorambaAgentClient(BASE_URL)
    limits = client.get_agent(AGENT_ID)
    client.close()

    assert limits.allowed_tokens == ["pathUSD"]


@respx.mock
def test_get_receiving_agent_resolves_wallet_and_accepted_tokens_from_receiving_config():
    respx.get(f"{BASE_URL}/api/v2/morambacrypto/public/agent", params={"agent_id": RECEIVING_AGENT_ID}).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True, "message": "ok",
                "data": {
                    "id": RECEIVING_AGENT_ID, "status": "active", "is_receiving_agent": True,
                    "receiving_config": {
                        "receive_wallet_address": "0x6784f65225f7d567cf1535525b0dd720b1450d1b",
                        "accepted_tokens": [
                            {
                                "id": "20c6d642-75d1-4fcc-8728-3cb78b6bb0ea", "network": "tempo_testnet",
                                "chain": 42431, "network_type": "testnet", "rpc_url": "https://rpc.moderato.tempo.xyz",
                                "token_name": "pathusd", "token_address": "0x20c0000000000000000000000000000000000000",
                                "is_active": True, "created_at": "2026-06-04T11:49:28.347100Z",
                                "updated_at": "2026-06-09T11:11:28.424945Z",
                            }
                        ],
                    },
                },
            },
        )
    )

    client = MorambaAgentClient(BASE_URL)
    receiving_agent = client.get_receiving_agent(RECEIVING_AGENT_ID)
    client.close()

    assert receiving_agent.is_active
    assert receiving_agent.receive_wallet_address == "0x6784f65225f7d567cf1535525b0dd720b1450d1b"
    token = receiving_agent.resolve_token("pathusd")
    assert token is not None
    assert token.token_address == "0x20c0000000000000000000000000000000000000"
    assert token.rpc_url == "https://rpc.moderato.tempo.xyz"
    assert token.chain == 42431


@respx.mock
def test_get_receiving_agent_rejects_a_payout_only_agent():
    """A payout-only agent (is_receiving_agent False) has no
    receiving_config at all — it can never be a valid pay_agent target."""
    respx.get(f"{BASE_URL}/api/v2/morambacrypto/public/agent", params={"agent_id": AGENT_ID}).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True, "message": "ok",
                "data": {"id": AGENT_ID, "status": "active", "is_receiving_agent": False},
            },
        )
    )

    client = MorambaAgentClient(BASE_URL)
    try:
        client.get_receiving_agent(AGENT_ID)
        assert False, "expected Ap2Error"
    except Ap2Error as exc:
        assert "not a receiving agent" in str(exc)
    finally:
        client.close()
