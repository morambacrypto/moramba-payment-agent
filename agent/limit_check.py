"""The local, unconditional limit check — the "hands" half of the brain/
hands split (README section 2). This runs before every signature, for
every rail, regardless of what any LLM asked for.

Moramba's own backend only enforces `per_transaction_limit` and
`maximum_payout_limit` live today (confirmed in
moramba-crypto-api/src/services/ap2_service.rs's `check_agent_limits`
doc comment: the other fields "require aggregating past-transaction
rows... which doesn't exist yet"). Everything else here — daily,
monthly, hourly, vendor-wise, aggregate, count and rate limits — is
enforced purely from this project's own local ledger, which is exactly
the spend-history table Moramba doesn't have yet.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from agent.ledger import Ledger
from agent.limits_client import AgentLimits, MorambaAgentClient

_PERIOD_WINDOWS = {
    "per_minute": timedelta(minutes=1),
    "per_hour": timedelta(hours=1),
    "per_day": timedelta(days=1),
}

_EPOCH_ISO = datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat()


@dataclass(frozen=True)
class LimitCheckResult:
    allowed: bool
    reason: str | None = None


def _start_of(unit: str, now: datetime) -> str:
    if unit == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    if unit == "hour":
        return now.replace(minute=0, second=0, microsecond=0).isoformat()
    if unit == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    raise ValueError(unit)


class LimitChecker:
    def __init__(self, ledger: Ledger, agents_client: MorambaAgentClient, agent_id: str):
        self._ledger = ledger
        self._agents_client = agents_client
        self._agent_id = agent_id

    def check(
        self,
        *,
        wallet_address: str,
        recipient: str,
        token: str,
        amount: Decimal,
        rail: str,
        now: datetime | None = None,
    ) -> LimitCheckResult:
        now = now or datetime.now(timezone.utc)
        limits = self._agents_client.get_agent(self._agent_id)

        deny = self._check_static(limits, wallet_address, token, amount)
        if deny is not None:
            return deny

        deny = self._check_running_totals(limits, token, recipient, amount, now)
        if deny is not None:
            return deny

        deny = self._check_counts(limits, now)
        if deny is not None:
            return deny

        return LimitCheckResult(allowed=True)

    def _check_static(
        self, limits: AgentLimits, wallet_address: str, token: str, amount: Decimal
    ) -> LimitCheckResult | None:
        if not limits.is_active:
            return LimitCheckResult(False, f"agent {limits.id} is not active (status={limits.status})")

        if wallet_address not in limits.wallet_addresses:
            return LimitCheckResult(
                False,
                "signing wallet is not one of this agent's registered payout wallets "
                "(see README section 2, 'one wallet, one job')",
            )

        # Case-insensitive: Moramba's own token registry spells this
        # "pathUSD" while a button's payment_token_name can come through
        # as "pathusd" (real production data, 2026-09-29) — an exact
        # match would reject a legitimately-allowed token.
        #
        # An empty allowed_tokens means "nothing is configured", not "no
        # restriction" — the agent only ever pays in what it explicitly
        # supports, across every rail; an unconfigured list rejects every
        # token rather than silently allowing all of them.
        if token.lower() not in {t.lower() for t in limits.allowed_tokens}:
            return LimitCheckResult(False, f"token {token!r} is not in this agent's allowed_tokens")

        if limits.per_transaction_limit is not None and amount > Decimal(limits.per_transaction_limit):
            return LimitCheckResult(False, "amount exceeds per_transaction_limit")

        if limits.maximum_payout_limit is not None and amount > Decimal(limits.maximum_payout_limit):
            return LimitCheckResult(False, "amount exceeds maximum_payout_limit")

        return None

    def _check_running_totals(
        self, limits: AgentLimits, token: str, recipient: str, amount: Decimal, now: datetime
    ) -> LimitCheckResult | None:
        if limits.daily_transaction_limit is not None:
            spent = self._ledger.spent_since(_start_of("day", now), token=token)
            if spent + amount > Decimal(limits.daily_transaction_limit):
                return LimitCheckResult(False, "would exceed daily_transaction_limit")

        if limits.monthly_transaction_limit is not None:
            spent = self._ledger.spent_since(_start_of("month", now), token=token)
            if spent + amount > Decimal(limits.monthly_transaction_limit):
                return LimitCheckResult(False, "would exceed monthly_transaction_limit")

        if limits.hourly_transaction_limit is not None:
            spent = self._ledger.spent_since(_start_of("hour", now), token=token)
            if spent + amount > Decimal(limits.hourly_transaction_limit):
                return LimitCheckResult(False, "would exceed hourly_transaction_limit")

        if limits.vendor_wise_spending_limit is not None:
            spent = self._ledger.spent_since(_EPOCH_ISO, token=token, recipient=recipient)
            if spent + amount > Decimal(limits.vendor_wise_spending_limit):
                return LimitCheckResult(False, "would exceed vendor_wise_spending_limit for this recipient")

        if limits.aggregate_spending_limit is not None:
            spent = self._ledger.spent_since(_EPOCH_ISO, token=token)
            if spent + amount > Decimal(limits.aggregate_spending_limit):
                return LimitCheckResult(False, "would exceed aggregate_spending_limit")

        return None

    def _check_counts(self, limits: AgentLimits, now: datetime) -> LimitCheckResult | None:
        if limits.daily_transaction_count_limit is not None:
            count = self._ledger.count_since(_start_of("day", now))
            if count + 1 > limits.daily_transaction_count_limit:
                return LimitCheckResult(False, "would exceed daily_transaction_count_limit")

        if limits.rate_limit_max_transactions_count is not None and limits.rate_limit_per_period:
            window = _PERIOD_WINDOWS.get(limits.rate_limit_per_period)
            if window is not None:
                since = (now - window).isoformat()
                count = self._ledger.count_since(since)
                if count + 1 > limits.rate_limit_max_transactions_count:
                    return LimitCheckResult(
                        False,
                        f"would exceed rate limit ({limits.rate_limit_max_transactions_count} "
                        f"per {limits.rate_limit_per_period})",
                    )

        return None
