"""Shared fixtures for tests of the MPP URL rail: a URL that answers 402
with a Tempo charge challenge until a credential arrives, and a stand-in for
pympp's real Tempo method (which would sign a real transaction against an
RPC)."""

import httpx
import respx
from mpp import Challenge, Credential, Receipt

from agent.adapters import mpp

PAID_URL = "https://paid.example/api/paid-content"
TOKEN = "0x20c0000000000000000000000000000000000000"
PAYEE = "0x6784f65225f7d567cf1535525b0dd720b1450d1b"


def challenge(*, amount="1000000", currency=TOKEN, recipient=PAYEE, chain_id=42431, details=None) -> Challenge:
    return Challenge.create(
        secret_key="test-secret", realm="paid.example", method="tempo", intent="charge",
        request={
            "amount": amount, "currency": currency, "recipient": recipient,
            "methodDetails": {"chainId": chain_id} if details is None else details,
        },
    )


def www_authenticate(ch: Challenge) -> str:
    return ch.to_www_authenticate("paid.example")


class FakeTempo:
    name = "tempo"
    intents = ("charge",)

    def __init__(self, address: str):
        self.address = address
        self.credentials_created = 0

    async def create_credential(self, ch: Challenge) -> Credential:
        self.credentials_created += 1
        return Credential(
            challenge=ch.to_echo(),
            payload={"type": "transaction", "signature": "0x" + "ab" * 65},
            source=f"did:pkh:eip155:{ch.request['methodDetails']['chainId']}:{self.address}",
        )


def install_fake_tempo(monkeypatch, address: str) -> FakeTempo:
    fake = FakeTempo(address)
    monkeypatch.setattr(mpp, "_build_method", lambda wallet, expected, rpc_url: fake)
    return fake


def paid_content_server(
    *, challenges=None, body='{"secret": "the paid content"}', content_type="application/json", reference="0xpaidtx",
    url=PAID_URL, method="get",
):
    """402 + a challenge until a credential arrives, then 200 + the content and
    a receipt. `challenges` is a list used in order (the last one repeats), so
    a test can make the server change its price on the second request."""
    sequence = list(challenges or [challenge()])
    receipt = Receipt.success(reference=reference).to_payment_receipt()
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization", "").startswith("Payment "):
            return httpx.Response(200, headers={"Payment-Receipt": receipt, "Content-Type": content_type}, content=body)
        ch = sequence[min(state["n"], len(sequence) - 1)]
        state["n"] += 1
        return httpx.Response(402, headers={"WWW-Authenticate": www_authenticate(ch)}, json={})

    return getattr(respx, method)(url).mock(side_effect=handler)
