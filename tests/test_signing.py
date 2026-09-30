from eth_account import Account
from eth_account.messages import encode_defunct

from agent.signing import load_wallet

TEST_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"


def test_load_wallet_derives_correct_address():
    wallet = load_wallet(TEST_PRIVATE_KEY)
    expected = Account.from_key(TEST_PRIVATE_KEY).address
    assert wallet.address == expected


def test_sign_message_is_independently_verifiable():
    wallet = load_wallet(TEST_PRIVATE_KEY)
    signature = wallet.sign_message("I am doing transaction with this account")

    recovered = Account.recover_message(
        encode_defunct(text="I am doing transaction with this account"), signature=signature
    )
    assert recovered == wallet.address


def test_different_messages_produce_different_signatures():
    wallet = load_wallet(TEST_PRIVATE_KEY)
    sig1 = wallet.sign_message("message one")
    sig2 = wallet.sign_message("message two")
    assert sig1 != sig2
