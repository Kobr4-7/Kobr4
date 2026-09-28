import pytest

from kobr4.strategies.indicators import ADX, ATR, EMA, RSI, Donchian


def test_ema_seed_and_update() -> None:
    ema = EMA(3)
    assert ema.update(1) is None
    assert ema.update(2) is None
    assert ema.update(3) == 2.0
    assert ema.update(4) == pytest.approx(3.0)
    with pytest.raises(ValueError, match="période"):
        EMA(0)


def test_rsi_extremes() -> None:
    up, down = RSI(5), RSI(5)
    for i in range(10):
        up.update(100 + i)
        down.update(100 - i)
    assert up.value == 100.0
    assert down.value == pytest.approx(0.0)


def test_atr_constant_range() -> None:
    atr = ATR(3)
    for _ in range(5):
        v = atr.update(1.1, 1.0, 1.05)
    assert v == pytest.approx(0.1)


def test_adx_bounds_and_trend() -> None:
    adx = ADX(5)
    v = None
    for i in range(40):
        v = adx.update(1 + i * 0.01 + 0.005, 1 + i * 0.01 - 0.005, 1 + i * 0.01)
    assert v is not None
    assert 90 <= v <= 100


def test_donchian_excludes_current_bar() -> None:
    d = Donchian(3)
    assert d.update(2, 1) is None
    d.update(3, 1.5)
    d.update(2.5, 0.5)
    assert d.update(10, 0) == (3, 0.5)
    assert d.update(1, 1) == (10, 0)
