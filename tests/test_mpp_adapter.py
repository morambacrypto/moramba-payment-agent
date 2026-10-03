import json
from decimal import Decimal

import httpx
import pytest
import respx
from eth_account import Account
from eth_account.messages import encode_defunct
from mpp import Challenge, Credential, Receipt
from mpp.runtime import PaymentRuntime

from agent.adapters import mpp
from agent.signing import load_wallet

TEST_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
RECEIVER = "https://receiver.example"
URL = f"{RECEIVER}/agent-api/payout/agent/receiver-agent-1/pay"
TOKEN = "0x20c0000000000000000000000000000000000000"
RECIPIENT = "0x6784f65225f7d567cf1535525b0dd720b1450d1b"


def expected(**overrides) -> mpp.MppExpectation:
    values = dict(chain_id=42431, token_address=TOKEN, max_units=5_250_000, recipient=RECIPIENT)
    values.update(overrides)
    return mpp.MppExpectation(**values)


def make_challenge(*, details=None, **request_overrides) -> Challenge:
    request = {
        "amount": "5250000", "currency": TOKEN, "recipient": RECIPIENT,
        "methodDetails": {"chainId": 42431} if details is None else details,
    }
    request.update(request_overrides)
    return Challenge.create(
        secret_key="test-secret", realm="receiver.example", method="tempo", intent="charge", request=request,
    )


class FakeTempo:
    """Stands in for pympp's real Tempo method — which would build and sign
    a real transaction against an RPC — so the flow around it (the
    challenge guard, the retry, the receipt) is what gets tested."""

    name = "tempo"
    intents = ("charge",)

    def __init__(self, address: str):
        self.address = address
        self.credentials_created = 0

    async def create_credential(self, challenge: Challenge) -> Credential:
        self.credentials_created += 1
        return Credential(
            challenge=challenge.to_echo(),
            payload={"type": "transaction", "signature": "0x" + "ab" * 65},
            source=f"did:pkh:eip155:{challenge.request['methodDetails']['chainId']}:{self.address}",
        )


@pytest.fixture
def fake_tempo(monkeypatch):
    wallet = load_wallet(TEST_PRIVATE_KEY)
    method = FakeTempo(wallet.address)
    monkeypatch.setattr(mpp, "_build_method", lambda wallet, expected, rpc_url: method)
    return method


def pay(wallet, **overrides):
    kwargs = dict(
        receiver_base_url=RECEIVER, receiver_agent_id="receiver-agent-1", wallet=wallet,
        amount=Decimal("5.25"), expected=expected(), payout_agent_id="payer-agent-9",
    )
    kwargs.update(overrides)
    return mpp.pay(**kwargs)


def mock_paid_server(challenge: Challenge, tx_reference: str = "0xdeadbeef"):
    """402 with a challenge until a credential arrives, then 200 + receipt."""
    receipt = Receipt.success(reference=tx_reference).to_payment_receipt()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization", "").startswith("Payment "):
            return httpx.Response(200, headers={"Payment-Receipt": receipt}, json={"success": True})
        return httpx.Response(402, headers={"WWW-Authenticate": challenge.to_www_authenticate("receiver.example")}, json={})

    return respx.post(URL).mock(side_effect=handler)


def test_missing_payout_agent_id_is_rejected_before_any_request():
    wallet = load_wallet(TEST_PRIVATE_KEY)
    result = pay(wallet, payment_to="agent", payout_agent_id=None)
    assert not result.success
    assert "payout_agent_id" in result.error


@respx.mock
def test_pays_the_402_challenge_and_returns_the_receipts_tx_hash(fake_tempo):
    wallet = load_wallet(TEST_PRIVATE_KEY)
    route = mock_paid_server(make_challenge())

    result = pay(wallet)

    assert result.success
    assert result.tx_hash == "0xdeadbeef"
    assert route.call_count == 2  # the unpaid request, then the retry with the credential
    assert fake_tempo.credentials_created == 1
    assert route.calls[1].request.headers["authorization"].startswith("Payment ")


@respx.mock
def test_request_body_sends_the_token_address_and_a_verifiable_signature(fake_tempo):
    wallet = load_wallet(TEST_PRIVATE_KEY)
    route = mock_paid_server(make_challenge())

    result = pay(wallet, payment_via="agent", payment_to="agent")

    body = json.loads(route.calls[0].request.content)
    assert body["payment_via"] == "agent"
    assert body["payment_to"] == "agent"
    assert body["payout_agent_id"] == "payer-agent-9"
    assert body["amount"] == "5.25"
    # The server matches this against its allow-list of contract addresses
    # and uses it as the challenge currency — a name like "USDC" is rejected.
    assert body["token"] == TOKEN
    assert body["signature"] == result.signature
    recovered = Account.recover_message(encode_defunct(text=mpp.FIXED_SIGNING_MESSAGE), signature=result.signature)
    assert recovered == wallet.address


@respx.mock
def test_falls_back_to_the_response_body_when_there_is_no_receipt_header(fake_tempo):
    wallet = load_wallet(TEST_PRIVATE_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization", "").startswith("Payment "):
            return httpx.Response(200, json={"success": True, "tx_hash": "0xfrombody"})
        return httpx.Response(402, headers={"WWW-Authenticate": make_challenge().to_www_authenticate("receiver.example")})

    respx.post(URL).mock(side_effect=handler)

    assert pay(wallet).tx_hash == "0xfrombody"


@pytest.mark.parametrize(
    "challenge, reason",
    [
        (make_challenge(details={"chainId": 4217}), "challenge is for chain 4217, expected 42431"),
        (make_challenge(details={}), "does not name a chain"),
        (make_challenge(currency="0x" + "99" * 20), "challenge asks for token"),
        (make_challenge(recipient="0x" + "88" * 20), "challenge pays"),
        (make_challenge(amount="5250001"), "more than the 5250000 approved"),
        (make_challenge(amount="5.25"), "whole number of smallest units"),
        (
            make_challenge(details={"chainId": 42431, "splits": [{"amount": "1", "recipient": "0x" + "77" * 20}]}),
            "splits the payment",
        ),
    ],
)
@respx.mock
def test_refuses_a_challenge_that_strays_from_what_was_approved_before_signing(fake_tempo, challenge, reason):
    wallet = load_wallet(TEST_PRIVATE_KEY)
    route = mock_paid_server(challenge)

    result = pay(wallet)

    assert not result.success
    assert not result.pending
    assert "MPP challenge refused" in result.error
    assert reason in result.error
    assert fake_tempo.credentials_created == 0  # nothing was signed
    assert route.call_count == 1  # and nothing was sent back


@respx.mock
def test_accepts_an_amount_below_the_approved_ceiling(fake_tempo):
    wallet = load_wallet(TEST_PRIVATE_KEY)
    mock_paid_server(make_challenge(amount="1000000"))

    assert pay(wallet).success


def test_validate_challenge_refuses_a_non_tempo_chain_outright():
    challenge = make_challenge(details={"chainId": 8453})
    with pytest.raises(mpp.MppChallengeRefused, match="MPP only runs on Tempo"):
        mpp.validate_challenge(challenge, expected(chain_id=8453))


def test_validate_challenge_refuses_a_non_charge_challenge():
    challenge = Challenge.create(
        secret_key="s", realm="r", method="tempo", intent="session",
        request={"amount": "1", "currency": TOKEN, "recipient": RECIPIENT, "methodDetails": {"chainId": 42431}},
    )
    with pytest.raises(mpp.MppChallengeRefused, match="Tempo charge only"):
        mpp.validate_challenge(challenge, expected())


@respx.mock
def test_receiver_rejection_is_reported_as_failure(fake_tempo):
    wallet = load_wallet(TEST_PRIVATE_KEY)
    respx.post(URL).mock(return_value=httpx.Response(402, json={"message": "daily spend limit exceeded"}))

    result = pay(wallet)

    assert not result.success
    assert result.error == "daily spend limit exceeded"
    assert fake_tempo.credentials_created == 0


@respx.mock
def test_a_lost_response_after_sending_the_credential_is_pending_not_failed(fake_tempo):
    wallet = load_wallet(TEST_PRIVATE_KEY)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(402, headers={"WWW-Authenticate": make_challenge().to_www_authenticate("receiver.example")})
        raise httpx.ReadTimeout("no answer")

    respx.post(URL).mock(side_effect=handler)

    result = pay(wallet)

    assert not result.success
    assert result.pending
    assert "outcome unknown" in result.error
    assert fake_tempo.credentials_created == 1


@respx.mock
def test_an_unexpected_error_becomes_a_failed_result_not_a_crash(fake_tempo):
    wallet = load_wallet(TEST_PRIVATE_KEY)
    respx.post(URL).mock(side_effect=httpx.ConnectError("receiver unreachable"))

    result = pay(wallet)

    assert not result.success
    assert not result.pending
    assert "receiver unreachable" in result.error


def digest_challenge_server(digest_for_body):
    """402 with a challenge whose `digest` is computed from the body of the
    request that came in, then 200 once a credential arrives."""
    receipt = Receipt.success(reference="0xdigested").to_payment_receipt()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization", "").startswith("Payment "):
            return httpx.Response(200, headers={"Payment-Receipt": receipt}, json={"success": True})
        challenge = Challenge.create(
            secret_key="test-secret", realm="receiver.example", method="tempo", intent="charge",
            request={
                "amount": "5250000", "currency": TOKEN, "recipient": RECIPIENT,
                "methodDetails": {"chainId": 42431},
            },
            digest=digest_for_body(request.content),
        )
        return httpx.Response(402, headers={"WWW-Authenticate": challenge.to_www_authenticate("receiver.example")})

    return respx.post(URL).mock(side_effect=handler)


@respx.mock
def test_pays_when_the_challenges_body_digest_matches_the_request(fake_tempo):
    wallet = load_wallet(TEST_PRIVATE_KEY)
    digest_challenge_server(mpp.body_digest)

    result = pay(wallet)

    assert result.success
    assert result.tx_hash == "0xdigested"
    assert fake_tempo.credentials_created == 1


@respx.mock
def test_refuses_a_challenge_whose_body_digest_does_not_match_the_request(fake_tempo):
    wallet = load_wallet(TEST_PRIVATE_KEY)
    route = digest_challenge_server(lambda content: mpp.body_digest(b"some other body"))

    result = pay(wallet)

    assert not result.success
    assert "body digest does not match" in result.error
    assert fake_tempo.credentials_created == 0
    assert route.call_count == 1


def test_body_digest_uses_the_sha256_base64_format_and_encode_body_is_compact_and_stable():
    assert mpp.body_digest(b"abc") == "sha-256=ungWv48Bz+pBQUDeXa4iI7ADYaOWF3qctBD/YfIAFa0="
    assert mpp.encode_body({"a": 1, "b": "x"}) == b'{"a":1,"b":"x"}'


def test_the_real_tempo_method_inside_the_guard_matches_charge_and_ignores_other_intents():
    """No network: only checks the real pympp method (not the fake) still
    matches a charge challenge once wrapped, and that a session or
    subscription challenge — which this rail can't pay — isn't matched, so
    nothing is signed for it."""
    wallet = load_wallet(TEST_PRIVATE_KEY)
    inner = mpp._build_method(wallet, expected(), None)
    guarded = mpp._GuardedMethod(inner, expected(), b"{}")
    runtime = PaymentRuntime([guarded])

    matched_challenge, matched_method = runtime.match_challenge([make_challenge()])
    assert matched_method is guarded
    assert inner.account.address == wallet.address

    session = Challenge.create(
        secret_key="s", realm="r", method="tempo", intent="session",
        request={"amount": "1", "currency": TOKEN, "recipient": RECIPIENT, "methodDetails": {"chainId": 42431}},
    )
    with pytest.raises(ValueError, match="No compatible payment method"):
        runtime.match_challenge([session])


# ─── paying a URL directly ──────────────────────────────────────────────────

from tests.mpp_helpers import (  # noqa: E402
    PAID_URL, PAYEE, TOKEN as URL_TOKEN, challenge as url_challenge, install_fake_tempo, paid_content_server,
    www_authenticate,
)


def url_expected(**overrides) -> mpp.MppExpectation:
    values = dict(chain_id=42431, token_address=URL_TOKEN, max_units=1_000_000, recipient=PAYEE)
    values.update(overrides)
    return mpp.MppExpectation(**values)


@pytest.mark.parametrize(
    "url",
    [
        "http://paid.example/x", "ftp://paid.example/x", "https://localhost/x", "https://api.localhost/x",
        "https://printer.local/x", "https://metadata.internal/x", "https://127.0.0.1/x", "https://10.0.0.5/x",
        "https://192.168.1.10/x", "https://169.254.169.254/latest/meta-data", "https://[::1]/x", "https:///nohost",
    ],
)
def test_unsafe_urls_are_refused(url):
    assert mpp.refuse_unsafe_url(url) is not None


@pytest.mark.parametrize("url", ["https://paid.example/x", "https://crypto.moramba.io/mppx/pay/api/paid-content", "https://8.8.8.8/x"])
def test_public_https_urls_are_allowed(url):
    assert mpp.refuse_unsafe_url(url) is None


@respx.mock
def test_probe_reads_the_price_without_paying():
    paid_content_server(challenges=[url_challenge(amount="1000000")])

    probe = mpp.probe_url(PAID_URL)

    assert probe.payment_required
    assert probe.challenge == mpp.MppUrlChallenge(chain_id=42431, currency=URL_TOKEN, recipient=PAYEE, amount_units=1_000_000)
    assert probe.note is None


@respx.mock
def test_probe_of_a_free_url_says_no_payment_is_needed_and_returns_its_content():
    respx.get(PAID_URL).mock(return_value=httpx.Response(200, json={"free": True}))

    probe = mpp.probe_url(PAID_URL)

    assert not probe.payment_required
    assert probe.challenge is None
    assert json.loads(probe.content["body"]) == {"free": True}


@respx.mock
def test_probe_points_an_x402_url_to_the_x402_tool():
    respx.get(PAID_URL).mock(return_value=httpx.Response(402, headers={"PAYMENT-REQUIRED": "eyJ4NDAyIjp0cnVlfQ=="}, json={}))

    probe = mpp.probe_url(PAID_URL)

    assert probe.payment_required and probe.challenge is None
    assert "pay_via_x402" in probe.note


@respx.mock
def test_probe_names_the_methods_it_cannot_pay():
    other = Challenge.create(
        secret_key="s", realm="paid.example", method="stripe", intent="charge",
        request={"amount": "100", "currency": "usd"},
    )
    respx.get(PAID_URL).mock(return_value=httpx.Response(402, headers={"WWW-Authenticate": other.to_www_authenticate("paid.example")}, json={}))

    probe = mpp.probe_url(PAID_URL)

    assert probe.challenge is None
    assert "stripe/charge" in probe.note


@respx.mock
def test_probe_does_not_follow_redirects():
    respx.get(PAID_URL).mock(return_value=httpx.Response(302, headers={"Location": "https://elsewhere.example/"}))

    probe = mpp.probe_url(PAID_URL)

    assert probe.status_code == 302 and not probe.payment_required


@respx.mock
def test_probe_retries_a_get_after_a_dropped_connection_then_reads_the_price():
    route = respx.get(PAID_URL).mock(side_effect=[
        httpx.ConnectError("dropped"),
        httpx.Response(402, headers={"WWW-Authenticate": www_authenticate(url_challenge())}, json={}),
    ])

    probe = mpp.probe_url(PAID_URL)

    assert probe.challenge is not None
    assert route.call_count == 2


@respx.mock
def test_probe_retries_a_get_when_the_server_is_busy():
    route = respx.get(PAID_URL).mock(side_effect=[
        httpx.Response(503),
        httpx.Response(429),
        httpx.Response(402, headers={"WWW-Authenticate": www_authenticate(url_challenge())}, json={}),
    ])

    assert mpp.probe_url(PAID_URL).challenge is not None
    assert route.call_count == 3


@respx.mock
def test_probe_gives_up_on_a_server_that_stays_busy_and_reports_what_it_said():
    route = respx.get(PAID_URL).mock(return_value=httpx.Response(503, text="busy"))

    probe = mpp.probe_url(PAID_URL)

    assert probe.status_code == 503 and not probe.payment_required
    assert route.call_count == 3


@respx.mock
def test_probe_raises_after_every_attempt_to_connect_fails():
    route = respx.get(PAID_URL).mock(side_effect=httpx.ConnectError("down"))

    with pytest.raises(httpx.ConnectError):
        mpp.probe_url(PAID_URL)
    assert route.call_count == 3


@respx.mock
def test_probe_does_not_repeat_a_post_that_timed_out_because_it_may_have_run():
    route = respx.post(PAID_URL).mock(side_effect=httpx.ReadTimeout("no answer"))

    with pytest.raises(httpx.ReadTimeout):
        mpp.probe_url(PAID_URL, method="POST", body=b"{}")
    assert route.call_count == 1


@respx.mock
def test_probe_does_repeat_a_post_whose_connection_was_never_made():
    route = respx.post(PAID_URL).mock(side_effect=[
        httpx.ConnectError("refused"),
        httpx.Response(402, headers={"WWW-Authenticate": www_authenticate(url_challenge())}, json={}),
    ])

    assert mpp.probe_url(PAID_URL, method="POST", body=b"{}").challenge is not None
    assert route.call_count == 2


@respx.mock
def test_pay_url_pays_the_challenge_and_returns_the_content_and_receipt(monkeypatch):
    wallet = load_wallet(TEST_PRIVATE_KEY)
    fake = install_fake_tempo(monkeypatch, wallet.address)
    route = paid_content_server(challenges=[url_challenge()], reference="0xurltx")

    result = mpp.pay_url(url=PAID_URL, wallet=wallet, expected=url_expected())

    assert result.success
    assert result.tx_hash == "0xurltx"
    assert json.loads(result.raw_response["body"]) == {"secret": "the paid content"}
    assert result.raw_response["status_code"] == 200
    assert route.call_count == 2  # the unpaid request, then the one carrying the credential
    assert fake.credentials_created == 1


@respx.mock
def test_pay_url_refuses_a_price_that_went_up_since_the_probe(monkeypatch):
    wallet = load_wallet(TEST_PRIVATE_KEY)
    fake = install_fake_tempo(monkeypatch, wallet.address)
    paid_content_server(challenges=[url_challenge(amount="9000000")])

    result = mpp.pay_url(url=PAID_URL, wallet=wallet, expected=url_expected(max_units=1_000_000))

    assert not result.success
    assert "more than the 1000000 approved" in result.error
    assert fake.credentials_created == 0


@respx.mock
def test_pay_url_refuses_another_payee_than_the_probe_showed(monkeypatch):
    wallet = load_wallet(TEST_PRIVATE_KEY)
    fake = install_fake_tempo(monkeypatch, wallet.address)
    paid_content_server(challenges=[url_challenge(recipient="0x" + "88" * 20)])

    result = mpp.pay_url(url=PAID_URL, wallet=wallet, expected=url_expected())

    assert not result.success and "challenge pays" in result.error
    assert fake.credentials_created == 0


@respx.mock
def test_a_url_that_keeps_asking_for_payment_is_paid_only_once(monkeypatch):
    """pympp answers a repeated 402 with a new credential, up to three
    times by default. A server that rejected the first payment and
    re-challenged would be paid again — the agent allows one payment."""
    wallet = load_wallet(TEST_PRIVATE_KEY)
    fake = install_fake_tempo(monkeypatch, wallet.address)
    route = respx.get(PAID_URL).mock(
        return_value=httpx.Response(402, headers={"WWW-Authenticate": www_authenticate(url_challenge())}, json={"detail": "no"})
    )

    result = mpp.pay_url(url=PAID_URL, wallet=wallet, expected=url_expected())

    assert not result.success
    assert fake.credentials_created == 1
    assert route.call_count == 2  # one unpaid request, one paid — never a second payment


@respx.mock
def test_pay_to_an_agent_is_also_paid_only_once(monkeypatch):
    wallet = load_wallet(TEST_PRIVATE_KEY)
    fake = install_fake_tempo(monkeypatch, wallet.address)
    respx.post(URL).mock(
        return_value=httpx.Response(402, headers={"WWW-Authenticate": make_challenge().to_www_authenticate("receiver.example")}, json={})
    )

    result = pay(wallet)

    assert not result.success
    assert fake.credentials_created == 1


@respx.mock
def test_a_lost_response_after_the_credential_is_pending_and_never_resent(monkeypatch):
    wallet = load_wallet(TEST_PRIVATE_KEY)
    fake = install_fake_tempo(monkeypatch, wallet.address)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(402, headers={"WWW-Authenticate": www_authenticate(url_challenge())}, json={})
        raise httpx.ReadTimeout("no answer")

    respx.get(PAID_URL).mock(side_effect=handler)

    result = mpp.pay_url(url=PAID_URL, wallet=wallet, expected=url_expected())

    assert not result.success and result.pending
    assert "outcome unknown" in result.error
    assert calls["n"] == 2 and fake.credentials_created == 1


@respx.mock
def test_pay_url_reports_an_error_status_after_payment_without_claiming_success(monkeypatch):
    wallet = load_wallet(TEST_PRIVATE_KEY)
    install_fake_tempo(monkeypatch, wallet.address)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization", "").startswith("Payment "):
            return httpx.Response(500, text="server error")
        return httpx.Response(402, headers={"WWW-Authenticate": www_authenticate(url_challenge())}, json={})

    respx.get(PAID_URL).mock(side_effect=handler)

    result = mpp.pay_url(url=PAID_URL, wallet=wallet, expected=url_expected())

    assert not result.success and result.error == "HTTP 500"


@respx.mock
def test_pay_url_describes_binary_content_instead_of_dumping_it(monkeypatch):
    wallet = load_wallet(TEST_PRIVATE_KEY)
    install_fake_tempo(monkeypatch, wallet.address)
    paid_content_server(body=b"\x89PNG\r\n", content_type="image/png")

    result = mpp.pay_url(url=PAID_URL, wallet=wallet, expected=url_expected())

    assert result.success
    assert result.raw_response["body"] is None
    assert "image/png" in result.raw_response["note"]


@respx.mock
def test_pay_url_cuts_very_long_content_and_says_so(monkeypatch):
    wallet = load_wallet(TEST_PRIVATE_KEY)
    install_fake_tempo(monkeypatch, wallet.address)
    paid_content_server(body="x" * 50_000, content_type="text/plain")

    result = mpp.pay_url(url=PAID_URL, wallet=wallet, expected=url_expected())

    assert len(result.raw_response["body"]) == 20_000
    assert result.raw_response["truncated"] is True
