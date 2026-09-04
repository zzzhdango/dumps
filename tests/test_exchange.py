import pytest

from exchange import is_paused_market_error


@pytest.mark.parametrize("message", [
    'bingx {"code":109415,"msg":"ABC-USDT is pause currently"}',
    "BINGX: market is pause currently",
])
def test_detects_paused_bingx_market(message):
    assert is_paused_market_error(RuntimeError(message))


@pytest.mark.parametrize("message", [
    'bingx {"code":100001,"msg":"temporary problem"}',
    "another exchange is pause currently",
])
def test_does_not_hide_other_exchange_errors(message):
    assert not is_paused_market_error(RuntimeError(message))
