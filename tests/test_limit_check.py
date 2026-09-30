from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from agent.ledger import STATUS_SETTLED, Ledger
from agent.limit_check import LimitChecker
from agent.limits_client import AgentLimits

WALLET = "0xWallet"
BASE_LIMITS = AgentLimits(
    id="agent-1",
    status="active",
    allowed_tokens=["USDC"],
    wallet_addresses=[WALLET],
    per_transaction_limit=None,
    daily_transaction_limit=None,
    monthly_transaction_limit=None,
    vendor_wise_spending_limit=None,
    aggregate_spending_limit=None,
    maximum_payout_limit=None,
    daily_transaction_count_limit=None,
    hourly_transaction_limit=None,
    rate_limit_max_transactions_count=None,
    rate_limit_per_period=None,
)


class FakeAgentsClient:
    def __init__(self, limits: AgentLimits):
        self.limits = limits

    def get_agent(self, agent_id: str) -> AgentLimits:
        return self.limits


def make_checker(tmp_path, limits: AgentLimits):
    ledger = Ledger(str(tmp_path / "test.db"))
    checker = LimitChecker(ledger, FakeAgentsClient(limits), limits.id)
    return ledger, checker


def test_allows_when_no_limits_configured(tmp_path):
    _, checker = make_checker(tmp_path, BASE_LIMITS)
    result = checker.check(wallet_address=WALLET, recipient="vendor-a", token="USDC", amount=Decimal("50"), rail="mpp")
    assert result.allowed


def test_rejects_inactive_agent(tmp_path):
    limits = replace(BASE_LIMITS, status="suspended")
    _, checker = make_checker(tmp_path, limits)
    result = checker.check(wallet_address=WALLET, recipient="v", token="USDC", amount=Decimal("1"), rail="mpp")
    assert not result.allowed
    assert "not active" in result.reason


def test_rejects_wallet_not_registered_to_agent(tmp_path):
    _, checker = make_checker(tmp_path, BASE_LIMITS)
    result = checker.check(wallet_address="0xSomeoneElse", recipient="v", token="USDC", amount=Decimal("1"), rail="mpp")
    assert not result.allowed
    assert "registered payout wallets" in result.reason


def test_rejects_disallowed_token(tmp_path):
    _, checker = make_checker(tmp_path, BASE_LIMITS)
    result = checker.check(wallet_address=WALLET, recipient="v", token="DOGE", amount=Decimal("1"), rail="mpp")
    assert not result.allowed
    assert "allowed_tokens" in result.reason


def test_allows_token_matching_case_insensitively(tmp_path):
    """Moramba's own registry spells this "pathUSD"; a button's own
    payment_token_name can come through as "pathusd" (real production
    data, 2026-09-29) — an exact-case match would wrongly reject an
    actually-allowed token."""
    limits = replace(BASE_LIMITS, allowed_tokens=["pathUSD"])
    _, checker = make_checker(tmp_path, limits)
    result = checker.check(wallet_address=WALLET, recipient="v", token="pathusd", amount=Decimal("1"), rail="mpp")
    assert result.allowed


def test_per_transaction_limit_is_whole_units_not_cents(tmp_path):
    """Real production data (2026-09-29): a `per_transaction_limit` of 5
    on a live agent means $5, not $0.05. An earlier version of this
    checker multiplied the amount by 100 before comparing, which meant a
    $1 payment against this exact limit was wrongly rejected as if it
    were a $100 payment against a $0.05 limit."""
    limits = replace(BASE_LIMITS, per_transaction_limit=5)
    _, checker = make_checker(tmp_path, limits)
    result = checker.check(wallet_address=WALLET, recipient="v", token="USDC", amount=Decimal("1"), rail="mpp")
    assert result.allowed, result.reason


def test_rejects_over_per_transaction_limit(tmp_path):
    limits = replace(BASE_LIMITS, per_transaction_limit=10)  # $10.00 — a whole-unit value, not cents
    _, checker = make_checker(tmp_path, limits)
    result = checker.check(wallet_address=WALLET, recipient="v", token="USDC", amount=Decimal("10.01"), rail="mpp")
    assert not result.allowed
    assert "per_transaction_limit" in result.reason


def test_rejects_over_maximum_payout_limit(tmp_path):
    limits = replace(BASE_LIMITS, maximum_payout_limit=5)  # $5.00
    _, checker = make_checker(tmp_path, limits)
    result = checker.check(wallet_address=WALLET, recipient="v", token="USDC", amount=Decimal("5.01"), rail="mpp")
    assert not result.allowed
    assert "maximum_payout_limit" in result.reason


def test_daily_limit_accounts_for_prior_spend_today(tmp_path):
    limits = replace(BASE_LIMITS, daily_transaction_limit=10)  # $10.00/day
    ledger, checker = make_checker(tmp_path, limits)
    ledger.record(rail="mpp", recipient="v", token="USDC", amount=Decimal("6"), status=STATUS_SETTLED)

    ok = checker.check(wallet_address=WALLET, recipient="v", token="USDC", amount=Decimal("3"), rail="mpp")
    assert ok.allowed

    over = checker.check(wallet_address=WALLET, recipient="v", token="USDC", amount=Decimal("4.01"), rail="mpp")
    assert not over.allowed
    assert "daily_transaction_limit" in over.reason


def test_vendor_wise_limit_is_scoped_per_recipient(tmp_path):
    limits = replace(BASE_LIMITS, vendor_wise_spending_limit=10)  # $10.00 per vendor, lifetime
    ledger, checker = make_checker(tmp_path, limits)
    ledger.record(rail="mpp", recipient="vendor-a", token="USDC", amount=Decimal("9"), status=STATUS_SETTLED)

    still_ok_other_vendor = checker.check(
        wallet_address=WALLET, recipient="vendor-b", token="USDC", amount=Decimal("5"), rail="mpp"
    )
    assert still_ok_other_vendor.allowed

    over_same_vendor = checker.check(
        wallet_address=WALLET, recipient="vendor-a", token="USDC", amount=Decimal("2"), rail="mpp"
    )
    assert not over_same_vendor.allowed
    assert "vendor_wise_spending_limit" in over_same_vendor.reason


def test_daily_transaction_count_limit(tmp_path):
    limits = replace(BASE_LIMITS, daily_transaction_count_limit=2)
    ledger, checker = make_checker(tmp_path, limits)
    ledger.record(rail="mpp", recipient="v", token="USDC", amount=Decimal("1"), status=STATUS_SETTLED)
    ledger.record(rail="mpp", recipient="v", token="USDC", amount=Decimal("1"), status=STATUS_SETTLED)

    result = checker.check(wallet_address=WALLET, recipient="v", token="USDC", amount=Decimal("1"), rail="mpp")
    assert not result.allowed
    assert "daily_transaction_count_limit" in result.reason


def test_rate_limit_per_period(tmp_path):
    limits = replace(BASE_LIMITS, rate_limit_max_transactions_count=1, rate_limit_per_period="per_hour")
    ledger, checker = make_checker(tmp_path, limits)
    ledger.record(rail="mpp", recipient="v", token="USDC", amount=Decimal("1"), status=STATUS_SETTLED)

    result = checker.check(
        wallet_address=WALLET, recipient="v", token="USDC", amount=Decimal("1"), rail="mpp",
        now=datetime.now(timezone.utc),
    )
    assert not result.allowed
    assert "rate limit" in result.reason
