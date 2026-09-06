import pytest

import check_api
from check_api import select_smoke_symbol
from check_exchanges import connectivity_summary, is_scannable_market
from exchange import BinanceRestrictedLocation


def test_smoke_prefers_btc_then_falls_back_to_first_market():
    assert select_smoke_symbol(
        ["ETH/USDT:USDT", "BTC/USDT:USDT"]
    ) == "BTC/USDT:USDT"
    assert select_smoke_symbol(["ETH/USDT:USDT"]) == "ETH/USDT:USDT"


def test_connectivity_summary_is_binance_only():
    result = connectivity_summary(500, "BTC/USDT:USDT", 149, 10, 20, 1.234)
    assert result["exchange"] == "binanceusdm"
    assert result["active_markets"] == 500
    assert result["completed_ohlcv_rows"] == 149
    assert result["elapsed_seconds"] == 1.23
    assert "bingx" not in str(result).lower()
    assert "weex" not in str(result).lower()


def test_smoke_and_runtime_share_strict_market_filter():
    assert is_scannable_market(
        {
            "swap": True,
            "linear": True,
            "quote": "USDT",
            "settle": "USDT",
            "active": True,
            "info": {
                "status": "TRADING",
                "contractType": "PERPETUAL",
            },
        }
    )


@pytest.mark.asyncio
async def test_smoke_main_reports_restricted_location(monkeypatch, capsys):
    async def restricted():
        raise BinanceRestrictedLocation("HTTP 451")

    monkeypatch.setattr(check_api, "check", restricted)
    assert await check_api.main() == 2
    assert "HTTP 451" in capsys.readouterr().err
