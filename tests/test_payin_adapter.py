from decimal import Decimal
from unittest.mock import MagicMock, patch

import httpx
import respx

from agent.adapters import payin
from agent.adapters.ap2 import Ap2Error
from agent.signing import load_wallet

TEST_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
BASE_URL = "https://moramba.example"
PAYIN_ID = "payin-abc-1"


def _mock_init(wallet_address: str, **overrides):
    data = {
        "token_address": "0x20c0000000000000000000000000000000000000", "token_name": "pathusd",
        "amount": "1000000", "chain_id": 42431, "rpc": "https://rpc.moderato.tempo.xyz",
        "verify_sc_address": "0x" + "33" * 20, "to": "0x6784f65225f7d567cf1535525b0dd720b1450d1b",
        "transaction_id": "tx-1", "nonce": "0",
    }
    data.update(overrides)
    return respx.get(f"{BASE_URL}/api/v2/morambacrypto/public/payrequest/init/{PAYIN_ID}/address/{wallet_address}").mock(
        return_value=httpx.Response(200, json={"success": True, "message": "ok", "data": data})
    )


@respx.mock
def test_resolve_payin_converts_minor_units_to_human_units_using_onchain_decimals():
    wallet = load_wallet(TEST_PRIVATE_KEY)
    _mock_init(wallet.address, amount="2500000")

    with patch("agent.adapters.payin.Web3") as MockWeb3:
        MockWeb3.to_checksum_address.side_effect = lambda a: a
        MockWeb3.HTTPProvider.return_value = MagicMock()
        w3 = MockWeb3.return_value
        contract = w3.eth.contract.return_value
        contract.functions.decimals.return_value.call.return_value = 6

        plan = payin.resolve_payin(BASE_URL, PAYIN_ID, wallet)

    assert plan.decimals == 6
    assert plan.amount == Decimal("2.5")
    assert plan.init.to == "0x6784f65225f7d567cf1535525b0dd720b1450d1b"
    assert plan.init.token_name == "pathusd"


@respx.mock
def test_resolve_payin_raises_ap2error_when_payin_not_found():
    wallet = load_wallet(TEST_PRIVATE_KEY)
    respx.get(f"{BASE_URL}/api/v2/morambacrypto/public/payrequest/init/{PAYIN_ID}/address/{wallet.address}").mock(
        return_value=httpx.Response(404, json={"success": False, "message": "payin not found"})
    )

    try:
        payin.resolve_payin(BASE_URL, PAYIN_ID, wallet)
        assert False, "expected Ap2Error"
    except Ap2Error as exc:
        assert "payin not found" in str(exc)


@respx.mock
def test_resolve_payin_raises_ap2error_when_decimals_lookup_fails():
    wallet = load_wallet(TEST_PRIVATE_KEY)
    _mock_init(wallet.address)

    with patch("agent.adapters.payin.Web3") as MockWeb3:
        MockWeb3.to_checksum_address.side_effect = lambda a: a
        w3 = MockWeb3.return_value
        contract = w3.eth.contract.return_value
        contract.functions.decimals.return_value.call.side_effect = RuntimeError("no such contract")

        try:
            payin.resolve_payin(BASE_URL, PAYIN_ID, wallet)
            assert False, "expected Ap2Error"
        except Ap2Error as exc:
            assert "decimals" in str(exc)


@respx.mock
def test_pay_payin_full_flow_settles():
    wallet = load_wallet(TEST_PRIVATE_KEY)
    _mock_init(wallet.address)
    respx.post(f"{BASE_URL}/api/v2/morambacrypto/public/payrequest/pay/{PAYIN_ID}/tnxid/tx-1/relay").mock(
        return_value=httpx.Response(200, json={"success": True, "message": "ok", "data": {"accepted": True}})
    )
    respx.get(f"{BASE_URL}/api/v2/morambacrypto/public/payrequest/status/tnxid/tx-1").mock(
        return_value=httpx.Response(200, json={"success": True, "message": "ok", "data": {"status": "success", "tx_hash": "0xpayintx"}})
    )

    with patch("agent.adapters.payin.Web3") as MockWeb3:
        MockWeb3.to_checksum_address.side_effect = lambda a: a
        w3 = MockWeb3.return_value
        contract = w3.eth.contract.return_value
        contract.functions.decimals.return_value.call.return_value = 6
        plan = payin.resolve_payin(BASE_URL, PAYIN_ID, wallet)

    stub_body = {"from": wallet.address, "to": plan.init.to, "signature": "0xsig"}
    with patch.object(payin, "detect_flow", return_value="plain"), \
         patch.dict(payin._BUILDER_BY_FLOW, {"plain": lambda w3, account, init: stub_body}):
        result = payin.pay_payin(BASE_URL, PAYIN_ID, plan, wallet)

    assert result.success
    assert result.tx_hash == "0xpayintx"
    assert result.flow == "plain"


@respx.mock
def test_pay_payin_reports_failure_when_settlement_status_is_not_success():
    wallet = load_wallet(TEST_PRIVATE_KEY)
    _mock_init(wallet.address)
    respx.post(f"{BASE_URL}/api/v2/morambacrypto/public/payrequest/pay/{PAYIN_ID}/tnxid/tx-1/relay").mock(
        return_value=httpx.Response(200, json={"success": True, "message": "ok", "data": {"accepted": True}})
    )
    respx.get(f"{BASE_URL}/api/v2/morambacrypto/public/payrequest/status/tnxid/tx-1").mock(
        return_value=httpx.Response(
            200,
            json={"success": True, "message": "ok", "data": {"status": "failed", "tx_hash": None, "error_message": "reverted"}},
        )
    )

    with patch("agent.adapters.payin.Web3") as MockWeb3:
        MockWeb3.to_checksum_address.side_effect = lambda a: a
        w3 = MockWeb3.return_value
        contract = w3.eth.contract.return_value
        contract.functions.decimals.return_value.call.return_value = 6
        plan = payin.resolve_payin(BASE_URL, PAYIN_ID, wallet)

    stub_body = {"from": wallet.address, "to": plan.init.to, "signature": "0xsig"}
    with patch.object(payin, "detect_flow", return_value="plain"), \
         patch.dict(payin._BUILDER_BY_FLOW, {"plain": lambda w3, account, init: stub_body}):
        result = payin.pay_payin(BASE_URL, PAYIN_ID, plan, wallet)

    assert not result.success
    assert result.error == "reverted"
