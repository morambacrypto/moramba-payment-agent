import httpx
import respx

from agent import setup
from agent.signing import load_wallet

TEST_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
BASE_URL = "https://moramba.example"
AGENT_ID = "66666666-6666-6666-6666-666666666666"


def make_fake_io(answers: list[str]):
    it = iter(answers)

    def fn(prompt: str = "") -> str:
        return next(it)

    return fn


def mock_agent_lookup(wallet_address: str | None):
    wallets = [{"public_wallet_address": wallet_address}] if wallet_address else []
    return respx.get(f"{BASE_URL}/api/v2/morambacrypto/public/agent", params={"agent_id": AGENT_ID}).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True, "message": "ok",
                "data": {
                    "id": AGENT_ID, "status": "active",
                    "payout_config": {"allowed_tokens": [], "wallets": wallets},
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


def test_parse_rails_filters_unknown_and_preserves_canonical_order():
    assert setup.parse_rails("ap2, mpp, bogus, erc20") == ["mpp", "erc20", "ap2"]
    assert setup.parse_rails("") == []


def test_build_env_content_shape():
    content = setup.build_env_content(
        {
            "wallet_private_key": TEST_PRIVATE_KEY, "moramba_agent_id": AGENT_ID,
            "moramba_api_base_url": BASE_URL, "chain_id": 42431, "rpc_url": "https://rpc.example",
            "db_path": "a.db", "default_token_contract": "",
        }
    )
    assert f"WALLET_PRIVATE_KEY={TEST_PRIVATE_KEY}" in content
    assert f"MORAMBA_AGENT_ID={AGENT_ID}" in content
    assert "RPC_URL=https://rpc.example" in content
    assert "MORAMBA_ACP_API_KEY" not in content  # only present when the ap2 rail was selected


@respx.mock
def test_check_wallet_registered_true_false_and_unreachable():
    wallet = load_wallet(TEST_PRIVATE_KEY)

    mock_agent_lookup(wallet.address)
    assert setup.check_wallet_registered(BASE_URL, AGENT_ID, wallet.address) is True

    mock_agent_lookup("0x" + "99" * 20)
    assert setup.check_wallet_registered(BASE_URL, AGENT_ID, wallet.address) is False

    respx.get(f"{BASE_URL}/api/v2/morambacrypto/public/agent", params={"agent_id": AGENT_ID}).mock(
        side_effect=httpx.ConnectError("unreachable")
    )
    assert setup.check_wallet_registered(BASE_URL, AGENT_ID, wallet.address) is None


@respx.mock
def test_run_wizard_happy_path_writes_env_file(tmp_path):
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_agent_lookup(wallet.address)
    env_path = tmp_path / ".env"

    fn = make_fake_io(
        [
            TEST_PRIVATE_KEY,       # wallet private key
            AGENT_ID,               # agent id
            BASE_URL,               # api base url — matches the respx mock above
            "",                     # chain id -> default
            "https://rpc.example",  # rpc url
            "",                     # db path -> default
            "mpp,erc20",            # rails
            "",                     # default_token_contract (erc20 selected)
        ]
    )

    setup.run_wizard(input_fn=fn, getpass_fn=fn, env_path=str(env_path))

    assert env_path.exists()
    content = env_path.read_text()
    assert f"WALLET_PRIVATE_KEY={TEST_PRIVATE_KEY}" in content
    assert f"MORAMBA_AGENT_ID={AGENT_ID}" in content
    assert "RPC_URL=https://rpc.example" in content
    assert "MORAMBA_ACP_API_KEY" not in content  # ap2 rail wasn't selected
    assert oct(env_path.stat().st_mode)[-3:] == "600"


def test_run_wizard_aborts_without_overwriting_when_declined(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("PRESERVE_ME=1\n")

    fn = make_fake_io(["n"])
    setup.run_wizard(input_fn=fn, getpass_fn=fn, env_path=str(env_path))

    assert env_path.read_text() == "PRESERVE_ME=1\n"


@respx.mock
def test_run_wizard_warns_but_still_writes_env_when_wallet_unregistered(tmp_path):
    mock_agent_lookup("0x" + "99" * 20)  # some other wallet, not ours
    env_path = tmp_path / ".env"

    fn = make_fake_io(
        [
            TEST_PRIVATE_KEY, AGENT_ID, BASE_URL, "", "https://rpc.example", "", "mpp",  # mpp only -> no extra prompts
        ]
    )
    setup.run_wizard(input_fn=fn, getpass_fn=fn, env_path=str(env_path))

    assert env_path.exists()
    assert f"WALLET_PRIVATE_KEY={TEST_PRIVATE_KEY}" in env_path.read_text()


def test_run_wizard_reprompts_on_invalid_private_key_then_accepts(tmp_path):
    env_path = tmp_path / ".env"
    fn = make_fake_io(
        [
            "garbage-key", TEST_PRIVATE_KEY,  # invalid, then valid
            "not-a-uuid", AGENT_ID,           # invalid, then valid
            BASE_URL, "", "https://rpc.example", "", "mpp",  # base_url, chain_id, rpc_url, db_path, rails
        ]
    )
    with respx.mock:
        mock_agent_lookup(load_wallet(TEST_PRIVATE_KEY).address)
        setup.run_wizard(input_fn=fn, getpass_fn=fn, env_path=str(env_path))

    assert env_path.exists()
    assert f"WALLET_PRIVATE_KEY={TEST_PRIVATE_KEY}" in env_path.read_text()
