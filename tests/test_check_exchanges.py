from check_exchanges import is_scannable_market


def make_market(**overrides):
    market = {
        "swap": True,
        "linear": True,
        "quote": "USDT",
        "active": True,
        "info": {},
    }
    market.update(overrides)
    return market


def test_accepts_regular_active_usdt_swap():
    assert is_scannable_market("bingx", make_market())


def test_rejects_inactive_market():
    assert not is_scannable_market("bingx", make_market(active=False))


def test_rejects_spot_market():
    assert not is_scannable_market("bingx", make_market(swap=False))


def test_rejects_non_usdt_market():
    assert not is_scannable_market("bingx", make_market(quote="USDC"))


def test_weex_accepts_only_crypto_perpetual():
    crypto = make_market(
        info={"contractType": "PERPETUAL", "underlyingType": "COIN"}
    )
    stock = make_market(
        info={"contractType": "TRADIFI_PERPETUAL", "underlyingType": "Stocks"}
    )
    assert is_scannable_market("weex", crypto)
    assert not is_scannable_market("weex", stock)
