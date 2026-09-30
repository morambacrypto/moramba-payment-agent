from decimal import Decimal
from unittest.mock import MagicMock, patch

from agent.adapters import erc20
from agent.signing import load_wallet

TEST_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"


def test_pay_converts_decimal_amount_using_token_decimals_and_signs():
    wallet = load_wallet(TEST_PRIVATE_KEY)

    fake_tx = {"to": "0xTokenContract"}
    fake_signed = MagicMock(raw_transaction=b"\x01\x02")
    account = MagicMock(wraps=wallet._account)
    account.address = wallet.address
    account.sign_transaction.return_value = fake_signed

    with patch("agent.adapters.erc20.Web3") as MockWeb3:
        MockWeb3.to_checksum_address.side_effect = lambda a: a
        w3 = MockWeb3.return_value
        w3.eth.get_transaction_count.return_value = 7
        w3.eth.gas_price = 1_000_000_000
        w3.eth.send_raw_transaction.return_value.hex.return_value = "0xsenttxhash"

        contract = w3.eth.contract.return_value
        contract.functions.decimals.return_value.call.return_value = 6
        contract.functions.transfer.return_value.build_transaction.return_value = fake_tx

        result = erc20.pay(
            rpc_url="https://tempo-testnet.example",
            chain_id=42431,
            account=account,
            token_contract_address="0xTokenContract",
            to_address="0xRecipient",
            amount=Decimal("2.5"),
        )

    assert result.success
    assert result.tx_hash == "0xsenttxhash"
    # 2.5 tokens at 6 decimals == 2_500_000 base units
    contract.functions.transfer.assert_called_once_with("0xRecipient", 2_500_000)
    account.sign_transaction.assert_called_once_with(fake_tx)


def test_pay_uses_estimated_gas_with_margin_when_no_gas_limit_given():
    """Regression test for a real failure: a fixed 100k default rejected a
    transfer that actually needed ~271k gas — gas must come from the
    node's own estimate, not a guess."""
    wallet = load_wallet(TEST_PRIVATE_KEY)
    fake_signed = MagicMock(raw_transaction=b"\x01\x02")
    account = MagicMock(wraps=wallet._account)
    account.address = wallet.address
    account.sign_transaction.return_value = fake_signed

    with patch("agent.adapters.erc20.Web3") as MockWeb3:
        MockWeb3.to_checksum_address.side_effect = lambda a: a
        w3 = MockWeb3.return_value
        w3.eth.get_transaction_count.return_value = 7
        w3.eth.gas_price = 1_000_000_000
        w3.eth.send_raw_transaction.return_value.hex.return_value = "0xsenttxhash"

        contract = w3.eth.contract.return_value
        contract.functions.decimals.return_value.call.return_value = 6
        transfer_call = contract.functions.transfer.return_value
        transfer_call.estimate_gas.return_value = 271_596

        erc20.pay(
            rpc_url="https://tempo-testnet.example",
            chain_id=42431,
            account=account,
            token_contract_address="0xTokenContract",
            to_address="0xRecipient",
            amount=Decimal("1"),
        )

    transfer_call.estimate_gas.assert_called_once_with({"from": wallet.address})
    sent_tx = transfer_call.build_transaction.call_args[0][0]
    assert sent_tx["gas"] == int(271_596 * erc20._GAS_ESTIMATE_MARGIN)


def test_pay_falls_back_to_fixed_gas_when_estimation_fails():
    wallet = load_wallet(TEST_PRIVATE_KEY)
    fake_signed = MagicMock(raw_transaction=b"\x01\x02")
    account = MagicMock(wraps=wallet._account)
    account.address = wallet.address
    account.sign_transaction.return_value = fake_signed

    with patch("agent.adapters.erc20.Web3") as MockWeb3:
        MockWeb3.to_checksum_address.side_effect = lambda a: a
        w3 = MockWeb3.return_value
        w3.eth.get_transaction_count.return_value = 7
        w3.eth.gas_price = 1_000_000_000
        w3.eth.send_raw_transaction.return_value.hex.return_value = "0xsenttxhash"

        contract = w3.eth.contract.return_value
        contract.functions.decimals.return_value.call.return_value = 6
        transfer_call = contract.functions.transfer.return_value
        transfer_call.estimate_gas.side_effect = RuntimeError("eth_estimateGas not supported")

        erc20.pay(
            rpc_url="https://tempo-testnet.example",
            chain_id=42431,
            account=account,
            token_contract_address="0xTokenContract",
            to_address="0xRecipient",
            amount=Decimal("1"),
        )

    sent_tx = transfer_call.build_transaction.call_args[0][0]
    assert sent_tx["gas"] == erc20._FALLBACK_GAS_LIMIT


def test_pay_respects_explicit_gas_limit_and_skips_estimation():
    wallet = load_wallet(TEST_PRIVATE_KEY)
    fake_signed = MagicMock(raw_transaction=b"\x01\x02")
    account = MagicMock(wraps=wallet._account)
    account.address = wallet.address
    account.sign_transaction.return_value = fake_signed

    with patch("agent.adapters.erc20.Web3") as MockWeb3:
        MockWeb3.to_checksum_address.side_effect = lambda a: a
        w3 = MockWeb3.return_value
        w3.eth.get_transaction_count.return_value = 7
        w3.eth.gas_price = 1_000_000_000
        w3.eth.send_raw_transaction.return_value.hex.return_value = "0xsenttxhash"

        contract = w3.eth.contract.return_value
        contract.functions.decimals.return_value.call.return_value = 6
        transfer_call = contract.functions.transfer.return_value

        erc20.pay(
            rpc_url="https://tempo-testnet.example",
            chain_id=42431,
            account=account,
            token_contract_address="0xTokenContract",
            to_address="0xRecipient",
            amount=Decimal("1"),
            gas_limit=42_000,
        )

    transfer_call.estimate_gas.assert_not_called()
    sent_tx = transfer_call.build_transaction.call_args[0][0]
    assert sent_tx["gas"] == 42_000


def test_pay_reports_failure_when_decimals_lookup_fails():
    with patch("agent.adapters.erc20.Web3") as MockWeb3:
        MockWeb3.to_checksum_address.side_effect = lambda a: a
        w3 = MockWeb3.return_value
        contract = w3.eth.contract.return_value
        contract.functions.decimals.return_value.call.side_effect = RuntimeError("no such contract")

        result = erc20.pay(
            rpc_url="https://tempo-testnet.example",
            chain_id=42431,
            account=MagicMock(address="0xFrom"),
            token_contract_address="0xTokenContract",
            to_address="0xRecipient",
            amount=Decimal("1"),
        )

    assert not result.success
    assert "decimals" in result.error
