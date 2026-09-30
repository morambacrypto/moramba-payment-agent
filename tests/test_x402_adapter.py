import base64
import json
from decimal import Decimal

import httpx
import respx
from x402.http.utils import encode_payment_required_header, encode_payment_response_header
from x402.schemas import PaymentRequired, PaymentRequirements, SettleResponse

from agent.adapters import x402
from agent.signing import load_wallet

TEST_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
RESOURCE_URL = "https://weather.example/api/weather"
NETWORK = "eip155:84532"
# Real Base Sepolia USDC — the x402 SDK's own spend_controls restricts an
# unconfigured client to known default assets per network.
ASSET = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
# Built rather than hand-typed so its length can't be miscounted (all
# lowercase, so it doesn't need a valid EIP-55 checksum either).
PAY_TO = "0x" + "00" * 19 + "ab"


def make_payment_required() -> PaymentRequired:
    return PaymentRequired(
        error="Payment required",
        accepts=[
            PaymentRequirements(
                scheme="exact",
                network=NETWORK,
                amount="10000",
                asset=ASSET,
                pay_to=PAY_TO,
                max_timeout_seconds=300,
                extra={"name": "USD Coin", "version": "2"},
            )
        ],
    )


@respx.mock
def test_probe_parses_exact_requirement_from_402():
    header_value = encode_payment_required_header(make_payment_required())
    respx.get(RESOURCE_URL).mock(
        return_value=httpx.Response(402, headers={"PAYMENT-REQUIRED": header_value}, json={"ok": False})
    )

    result = x402.probe(RESOURCE_URL)

    assert result.response.status_code == 402
    assert result.requirement is not None
    assert result.requirement.network == NETWORK
    assert result.requirement.asset == ASSET
    assert result.requirement.pay_to == PAY_TO
    assert result.requirement.amount_atomic == "10000"
    assert result.requirement.extra["name"] == "USD Coin"


@respx.mock
def test_probe_returns_no_requirement_when_no_payment_needed():
    respx.get(RESOURCE_URL).mock(return_value=httpx.Response(200, json={"data": "free"}))

    result = x402.probe(RESOURCE_URL)

    assert result.response.status_code == 200
    assert result.requirement is None


@respx.mock
def test_settle_signs_authorization_and_reports_tx_hash():
    payment_required = make_payment_required()
    header_value = encode_payment_required_header(payment_required)
    settle_response = SettleResponse(success=True, transaction="0xsettledtxhash", network=NETWORK, payer="0xpayer")
    response_header = encode_payment_response_header(settle_response)

    def handler(request: httpx.Request) -> httpx.Response:
        if "payment-signature" in request.headers:
            return httpx.Response(200, headers={"PAYMENT-RESPONSE": response_header}, json={"data": "sunny"})
        return httpx.Response(402, headers={"PAYMENT-REQUIRED": header_value}, json={"ok": False})

    route = respx.get(RESOURCE_URL).mock(side_effect=handler)

    probe_result = x402.probe(RESOURCE_URL)
    assert route.call_count == 1

    wallet = load_wallet(TEST_PRIVATE_KEY)
    result = x402.settle(RESOURCE_URL, wallet=wallet, probe_result=probe_result)

    assert result.success
    assert result.tx_hash == "0xsettledtxhash"
    assert route.call_count == 2

    sent_headers = route.calls[1].request.headers
    assert "payment-signature" in sent_headers
    decoded = json.loads(base64.b64decode(sent_headers["payment-signature"]))
    assert decoded["accepted"]["payTo"] == PAY_TO
    assert decoded["payload"]["authorization"]["to"] == PAY_TO
    assert decoded["payload"]["authorization"]["value"] == "10000"
    assert decoded["payload"]["authorization"]["from"].lower() == wallet.address.lower()
    assert decoded["payload"]["signature"].startswith("0x")


def test_settle_without_a_requirement_fails_without_any_network_call():
    wallet = load_wallet(TEST_PRIVATE_KEY)
    probe_result = x402.X402Probe(response=httpx.Response(200))

    result = x402.settle(RESOURCE_URL, wallet=wallet, probe_result=probe_result)

    assert not result.success
    assert "no payable" in result.error
