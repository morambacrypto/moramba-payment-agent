import pytest

from agent.adapters import ap2, pay_button, payin

_MODULES_USING_BALANCE_CHECK = (ap2, pay_button, payin)


@pytest.fixture(autouse=True)
def _skip_real_token_balance_check(request):
    """The settle flows call `check_wallet_token_balance` right after
    building a Web3 client from the payin's RPC url. Most tests mock the
    flow around that step and never give that client real chain data, so
    the check would make a live RPC call. It is a no-op by default here;
    mark a test with `@pytest.mark.real_balance_check` to run the real one."""
    if request.node.get_closest_marker("real_balance_check"):
        yield
        return
    originals = [(m, m.check_wallet_token_balance) for m in _MODULES_USING_BALANCE_CHECK]
    for module, _ in originals:
        module.check_wallet_token_balance = lambda *args, **kwargs: None
    try:
        yield
    finally:
        for module, original in originals:
            module.check_wallet_token_balance = original
