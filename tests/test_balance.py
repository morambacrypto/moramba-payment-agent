from decimal import Decimal
from unittest.mock import patch

import pytest

from agent import balance

WALLET = "0x" + "11" * 20
TOKEN = "0x" + "22" * 20


def _patched_web3(balance_units, decimals=6):
    patcher = patch("agent.balance.Web3")
    mock_web3 = patcher.start()
    mock_web3.to_checksum_address.side_effect = lambda a: a
    token = mock_web3.return_value.eth.contract.return_value
    if isinstance(balance_units, Exception):
        token.functions.balanceOf.return_value.call.side_effect = balance_units
    else:
        token.functions.balanceOf.return_value.call.return_value = balance_units
    token.functions.decimals.return_value.call.return_value = decimals
    return patcher


@pytest.mark.real_balance_check
def test_none_when_the_wallet_holds_enough_for_a_human_unit_amount():
    patcher = _patched_web3(2_000_000)
    try:
        reason = balance.insufficient_balance_reason(
            rpc_url="https://rpc.example", token_address=TOKEN, wallet_address=WALLET,
            label="USDC", needed_amount=Decimal("1.5"),
        )
    finally:
        patcher.stop()
    assert reason is None


@pytest.mark.real_balance_check
def test_none_when_the_wallet_holds_exactly_the_amount_needed_in_smallest_units():
    patcher = _patched_web3(10_000)
    try:
        reason = balance.insufficient_balance_reason(
            rpc_url="https://rpc.example", token_address=TOKEN, wallet_address=WALLET,
            label="USDC", needed_units=10_000,
        )
    finally:
        patcher.stop()
    assert reason is None


@pytest.mark.real_balance_check
def test_reason_names_the_wallet_token_and_both_amounts_when_short():
    patcher = _patched_web3(500_000)
    try:
        reason = balance.insufficient_balance_reason(
            rpc_url="https://rpc.example", token_address=TOKEN, wallet_address=WALLET,
            label="USDC", needed_amount=Decimal("1.5"),
        )
    finally:
        patcher.stop()
    assert "insufficient token balance" in reason
    assert WALLET in reason
    assert "has 0.5 USDC" in reason
    assert "needs 1.5" in reason
    assert "fund it first" in reason


@pytest.mark.real_balance_check
def test_reason_when_the_balance_cannot_be_read_fails_closed():
    patcher = _patched_web3(Exception("rpc down"))
    try:
        reason = balance.insufficient_balance_reason(
            rpc_url="https://rpc.example", token_address=TOKEN, wallet_address=WALLET,
            label="USDC", needed_units=1,
        )
    finally:
        patcher.stop()
    assert "could not read token balance" in reason
    assert "rpc down" in reason
