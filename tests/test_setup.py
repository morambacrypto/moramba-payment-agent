import httpx
import respx

from agent import setup
from agent.signing import load_wallet

TEST_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
BASE_URL = "https://crypto.moramba.io"
AGENT_ID = "66666666-6666-6666-6666-666666666666"


def make_fake_io(answers: list[str]):
    it = iter(answers)

    def fn(prompt: str = "") -> str:
        return next(it)

    return fn


def mock_agent_lookup(wallet_address: str | None, with_payout_token: bool = True):
    wallets = [{"public_wallet_address": wallet_address}] if wallet_address else []
    payout_config = {"allowed_tokens": [], "wallets": wallets}
    if with_payout_token:
        payout_config["allowed_tokens"] = [
            {
                "id": "20c6d642-75d1-4fcc-8728-3cb78b6bb0ea", "network": "Tempo", "chain": 42431,
                "network_type": "testnet", "rpc_url": "https://rpc.moderato.tempo.xyz",
                "token_name": "pathUSD", "token_address": "0x20c0000000000000000000000000000000000000",
            }
        ]
    return respx.get(f"{BASE_URL}/api/v2/morambacrypto/public/agent", params={"agent_id": AGENT_ID}).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True, "message": "ok",
                "data": {
                    "id": AGENT_ID, "status": "active",
                    "payout_config": payout_config,
                    "rate_limit_max_transactions_count": None, "rate_limit_per_period": None,
                },
            },
        )
    )


def test_validate_private_key_accepts_with_and_without_0x_prefix():
    with_prefix = setup.validate_private_key(TEST_PRIVATE_KEY)
    without_prefix = setup.validate_private_key(TEST_PRIVATE_KEY.removeprefix("0x"))
    assert with_prefix == TEST_PRIVATE_KEY
    assert without_prefix == TEST_PRIVATE_KEY


def test_validate_private_key_rejects_garbage():
    assert setup.validate_private_key("not-a-key") is None
    assert setup.validate_private_key("") is None


def test_validate_agent_id_accepts_uuid_rejects_garbage():
    assert setup.validate_agent_id(AGENT_ID) == AGENT_ID
    assert setup.validate_agent_id("not-a-uuid") is None


def test_build_env_content_shape():
    content = setup.build_env_content(
        {
            "wallet_private_key": TEST_PRIVATE_KEY, "moramba_agent_id": AGENT_ID,
            "moramba_api_base_url": BASE_URL, "moramba_acp_api_key": "acp-key-123",
            "chain_id": 42431, "rpc_url": "https://rpc.example",
            "db_path": "a.db", "default_token_contract": "0x" + "11" * 20,
            "api_key": "test-api-key-xyz",
        }
    )
    assert f"WALLET_PRIVATE_KEY={TEST_PRIVATE_KEY}" in content
    assert f"MORAMBA_AGENT_ID={AGENT_ID}" in content
    assert "MORAMBA_ACP_API_KEY=acp-key-123" in content
    assert "RPC_URL=https://rpc.example" in content
    assert "AGENT_API_KEY=test-api-key-xyz" in content


@respx.mock
def test_fetch_agent_info_returns_limits_on_success_and_none_when_unreachable():
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address)
    assert setup.fetch_agent_info(BASE_URL, AGENT_ID) is not None

    respx.get(f"{BASE_URL}/api/v2/morambacrypto/public/agent", params={"agent_id": AGENT_ID}).mock(
        side_effect=httpx.ConnectError("unreachable")
    )
    assert setup.fetch_agent_info(BASE_URL, AGENT_ID) is None


@respx.mock
def test_resolve_chain_and_rpc_from_agents_own_payout_token():
    mock_agent_lookup("0xabc", with_payout_token=True)
    limits = setup.fetch_agent_info(BASE_URL, AGENT_ID)
    assert setup.resolve_chain_and_rpc(limits) == (42431, "https://rpc.moderato.tempo.xyz")


@respx.mock
def test_resolve_chain_and_rpc_none_when_agent_has_no_payout_tokens():
    mock_agent_lookup("0xabc", with_payout_token=False)
    limits = setup.fetch_agent_info(BASE_URL, AGENT_ID)
    assert setup.resolve_chain_and_rpc(limits) is None


def test_resolve_chain_and_rpc_none_when_lookup_failed():
    assert setup.resolve_chain_and_rpc(None) is None


@respx.mock
def test_run_wizard_happy_path_auto_detects_network(tmp_path):
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address, with_payout_token=True)
    env_path = tmp_path / ".env"

    fn = make_fake_io(
        [
            AGENT_ID,               # agent id
            TEST_PRIVATE_KEY,       # wallet private key — matches the registered wallet
            "acp-key-123",          # ACP api key
            # no db_path prompt — always moramba_payment_agent.db, and no
            # default_token_contract prompt — auto-derived from the
            # agent's own payout token, same as chain_id/rpc_url
        ]
    )

    setup.run_wizard(input_fn=fn, getpass_fn=fn, env_path=str(env_path))

    assert env_path.exists()
    content = env_path.read_text()
    assert f"WALLET_PRIVATE_KEY={TEST_PRIVATE_KEY}" in content
    assert f"MORAMBA_AGENT_ID={AGENT_ID}" in content
    assert "MORAMBA_ACP_API_KEY=acp-key-123" in content
    assert "CHAIN_ID=42431" in content
    assert "RPC_URL=https://rpc.moderato.tempo.xyz" in content
    assert "DB_PATH=moramba_payment_agent.db" in content
    assert "DEFAULT_TOKEN_CONTRACT=0x20c0000000000000000000000000000000000000" in content
    assert oct(env_path.stat().st_mode)[-3:] == "600"

    api_key_line = next(line for line in content.splitlines() if line.startswith("AGENT_API_KEY="))
    assert len(api_key_line.removeprefix("AGENT_API_KEY=")) > 20  # a real generated secret, not blank


@respx.mock
def test_run_wizard_falls_back_to_manual_network_when_lookup_fails(tmp_path):
    respx.get(f"{BASE_URL}/api/v2/morambacrypto/public/agent", params={"agent_id": AGENT_ID}).mock(
        side_effect=httpx.ConnectError("unreachable")
    )
    env_path = tmp_path / ".env"

    fn = make_fake_io(
        [
            AGENT_ID,
            TEST_PRIVATE_KEY,
            "acp-key-123",
            "42431",                          # chain id, manual
            "https://rpc.moderato.tempo.xyz",  # rpc url, manual
        ]
    )
    setup.run_wizard(input_fn=fn, getpass_fn=fn, env_path=str(env_path))

    assert env_path.exists()
    content = env_path.read_text()
    assert "CHAIN_ID=42431" in content
    assert "RPC_URL=https://rpc.moderato.tempo.xyz" in content
    assert "DB_PATH=moramba_payment_agent.db" in content
    # No payout tokens available (lookup failed) — left blank, not asked.
    assert "DEFAULT_TOKEN_CONTRACT=\n" in content


def test_run_wizard_aborts_without_overwriting_when_declined(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("PRESERVE_ME=1\n")

    fn = make_fake_io(["n"])
    setup.run_wizard(input_fn=fn, getpass_fn=fn, env_path=str(env_path))

    assert env_path.read_text() == "PRESERVE_ME=1\n"


@respx.mock
def test_run_wizard_reprompts_for_a_new_key_when_wallet_unregistered(tmp_path):
    other_wallet = load_wallet("0x" + "22" * 32)
    mock_agent_lookup(other_wallet.address, with_payout_token=True)  # some other wallet, not ours
    env_path = tmp_path / ".env"

    fn = make_fake_io(
        [
            AGENT_ID,
            TEST_PRIVATE_KEY,   # unregistered — will be asked to retry
            "y",                # yes, try a different key
            "0x" + "33" * 32,   # still not registered
            "n",                # no more retries — proceed anyway
            "acp-key-123",
        ]
    )
    setup.run_wizard(input_fn=fn, getpass_fn=fn, env_path=str(env_path))

    assert env_path.exists()
    assert "WALLET_PRIVATE_KEY=0x" + "33" * 32 in env_path.read_text()


def test_run_wizard_reprompts_on_invalid_private_key_then_accepts(tmp_path):
    env_path = tmp_path / ".env"
    fn = make_fake_io(
        [
            "not-a-uuid", AGENT_ID,             # invalid, then valid
            "garbage-key", TEST_PRIVATE_KEY,    # invalid, then valid
            "acp-key-123", "42431", "https://rpc.moderato.tempo.xyz",
        ]
    )
    with respx.mock:
        respx.get(f"{BASE_URL}/api/v2/morambacrypto/public/agent", params={"agent_id": AGENT_ID}).mock(
            side_effect=httpx.ConnectError("unreachable")
        )
        setup.run_wizard(input_fn=fn, getpass_fn=fn, env_path=str(env_path))

    assert env_path.exists()
    assert f"WALLET_PRIVATE_KEY={TEST_PRIVATE_KEY}" in env_path.read_text()
