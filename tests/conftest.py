import pytest

from agent import balance
from agent.adapters import ap2, pay_button, payin

_MODULES_USING_BALANCE_CHECK = (ap2, pay_button, payin)


@pytest.fixture(autouse=True)
def _skip_real_token_balance_check(request):
    """The settle flows call `check_wallet_token_balance`, and the MPP and
    x402 engine flows call `balance.insufficient_balance_reason`, each
    against a Web3 client built from an RPC url. Most tests mock the flow
    around that step and never give the client real chain data, so the
    checks would make a live RPC call. They are no-ops by default here;
    mark a test with `@pytest.mark.real_balance_check` to run the real ones."""
    if request.node.get_closest_marker("real_balance_check"):
        yield
        return
    originals = [(m, m.check_wallet_token_balance) for m in _MODULES_USING_BALANCE_CHECK]
    for module, _ in originals:
        module.check_wallet_token_balance = lambda *args, **kwargs: None
    original_reason = balance.insufficient_balance_reason
    balance.insufficient_balance_reason = lambda **kwargs: None
    try:
        yield
    finally:
        for module, original in originals:
            module.check_wallet_token_balance = original
        balance.insufficient_balance_reason = original_reason
