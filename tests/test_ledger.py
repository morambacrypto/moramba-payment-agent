from decimal import Decimal

from agent.ledger import STATUS_FAILED, STATUS_REJECTED, STATUS_SETTLED, Ledger


def make_ledger(tmp_path):
    return Ledger(str(tmp_path / "test.db"))


def test_record_and_get_roundtrip(tmp_path):
    ledger = make_ledger(tmp_path)
    record = ledger.record(
        rail="mpp", recipient="agent-123", token="USDC", amount=Decimal("12.50"), status=STATUS_SETTLED,
        tx_hash="0xabc",
    )
    fetched = ledger.get(record.id)
    assert fetched.amount == Decimal("12.50")
    assert fetched.tx_hash == "0xabc"
    assert fetched.synced_at is None
    assert fetched.sync_attempts == 0


def test_spent_since_only_counts_settled(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.record(rail="mpp", recipient="r1", token="USDC", amount=Decimal("10"), status=STATUS_SETTLED)
    ledger.record(rail="mpp", recipient="r1", token="USDC", amount=Decimal("999"), status=STATUS_REJECTED)
    ledger.record(rail="mpp", recipient="r1", token="USDC", amount=Decimal("999"), status=STATUS_FAILED)
    ledger.record(rail="mpp", recipient="r1", token="USDC", amount=Decimal("5"), status=STATUS_SETTLED)

    total = ledger.spent_since("1970-01-01T00:00:00+00:00")
    assert total == Decimal("15")


def test_spent_since_scoped_by_token_and_recipient(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.record(rail="mpp", recipient="vendor-a", token="USDC", amount=Decimal("10"), status=STATUS_SETTLED)
    ledger.record(rail="mpp", recipient="vendor-b", token="USDC", amount=Decimal("20"), status=STATUS_SETTLED)
    ledger.record(rail="mpp", recipient="vendor-a", token="USDT", amount=Decimal("30"), status=STATUS_SETTLED)

    assert ledger.spent_since("1970-01-01T00:00:00+00:00", recipient="vendor-a") == Decimal("40")
    assert ledger.spent_since("1970-01-01T00:00:00+00:00", token="USDC") == Decimal("30")
    assert ledger.spent_since("1970-01-01T00:00:00+00:00", token="USDC", recipient="vendor-a") == Decimal("10")


def test_pending_sync_and_mark_synced(tmp_path):
    ledger = make_ledger(tmp_path)
    r1 = ledger.record(rail="mpp", recipient="r1", token="USDC", amount=Decimal("1"), status=STATUS_SETTLED)
    r2 = ledger.record(rail="mpp", recipient="r2", token="USDC", amount=Decimal("2"), status=STATUS_SETTLED)

    pending = ledger.pending_sync()
    assert [r.id for r in pending] == [r1.id, r2.id]

    ledger.mark_synced(r1.id)
    pending = ledger.pending_sync()
    assert [r.id for r in pending] == [r2.id]
    assert ledger.get(r1.id).synced_at is not None


def test_bump_sync_attempts(tmp_path):
    ledger = make_ledger(tmp_path)
    r1 = ledger.record(rail="mpp", recipient="r1", token="USDC", amount=Decimal("1"), status=STATUS_SETTLED)
    ledger.bump_sync_attempts(r1.id)
    ledger.bump_sync_attempts(r1.id)
    assert ledger.get(r1.id).sync_attempts == 2


def test_history_filters(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.record(rail="mpp", recipient="r1", token="USDC", amount=Decimal("1"), status=STATUS_SETTLED)
    ledger.record(rail="erc20", recipient="r2", token="USDC", amount=Decimal("2"), status=STATUS_SETTLED)

    assert len(ledger.history()) == 2
    assert len(ledger.history(rail="mpp")) == 1
    assert len(ledger.history(recipient="r2")) == 1


def test_count_since(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.record(rail="mpp", recipient="r1", token="USDC", amount=Decimal("1"), status=STATUS_SETTLED)
    ledger.record(rail="mpp", recipient="r1", token="USDC", amount=Decimal("1"), status=STATUS_REJECTED)
    ledger.record(rail="mpp", recipient="r1", token="USDC", amount=Decimal("1"), status=STATUS_SETTLED)

    assert ledger.count_since("1970-01-01T00:00:00+00:00") == 2
