"""Retries for steps that are safe to repeat.

Only for reads and for the unpaid probe of a payable URL — requests that
can't spend anything, so trying again after a hiccup (a flaky public RPC, a
dropped connection, a 503) can only help. A payment is deliberately never
retried here: sending it twice could pay twice, and a payment whose outcome
is unknown is reported as `pending` for a person to check instead.
"""

import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

DEFAULT_ATTEMPTS = 3
# Seconds before the first retry; doubles each time (0.3, 0.6, ...). A
# module variable, read at call time, so tests can set it to 0.
DEFAULT_DELAY = 0.3


def retry_call(
    fn: Callable[[], T],
    *,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    attempts: int | None = None,
    delay: float | None = None,
) -> T:
    """Call `fn`, trying again with a growing pause if it raises one of
    `retry_on`. The last error is raised if every attempt fails; any other
    exception is raised at once."""
    attempts = DEFAULT_ATTEMPTS if attempts is None else attempts
    pause = DEFAULT_DELAY if delay is None else delay
    for attempt in range(attempts):
        try:
            return fn()
        except retry_on:
            if attempt == attempts - 1:
                raise
            time.sleep(pause * (2 ** attempt))
    raise AssertionError("unreachable")  # pragma: no cover
