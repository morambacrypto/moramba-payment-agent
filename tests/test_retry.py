import pytest

from agent.retry import retry_call


def test_returns_at_once_when_the_first_attempt_works():
    calls = []
    assert retry_call(lambda: calls.append(1) or "ok") == "ok"
    assert len(calls) == 1


def test_retries_until_it_works():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("blip")
        return "ok"

    assert retry_call(flaky, attempts=3, delay=0) == "ok"
    assert calls["n"] == 3


def test_raises_the_last_error_when_every_attempt_fails():
    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise ConnectionError(f"attempt {calls['n']}")

    with pytest.raises(ConnectionError, match="attempt 3"):
        retry_call(always_fails, attempts=3, delay=0)
    assert calls["n"] == 3


def test_an_error_outside_retry_on_is_raised_straight_away():
    calls = {"n": 0}

    def wrong_kind():
        calls["n"] += 1
        raise ValueError("not a connection problem")

    with pytest.raises(ValueError):
        retry_call(wrong_kind, retry_on=(ConnectionError,), attempts=3, delay=0)
    assert calls["n"] == 1
