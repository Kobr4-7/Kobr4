from datetime import datetime
from decimal import Decimal

import pytest

from kobr4.core.instruments import get_instrument
from kobr4.core.models import Tick
from kobr4.core.prices import MissingPriceError, PriceBook, conversion_symbols


def book(t0: datetime, **quotes: str) -> PriceBook:
    pb = PriceBook()
    for sym, px in quotes.items():
        s = f"{sym[:3]}/{sym[3:]}"
        pb.update(Tick(symbol=s, bid=Decimal(px), ask=Decimal(px), ts=t0))
    return pb


def test_direct_inverse_and_cross(t0: datetime) -> None:
    pb = book(t0, EURUSD="1.10", USDJPY="150", GBPUSD="1.25")
    assert pb.rate("USD", "USD") == 1
    assert pb.rate("EUR", "USD") == Decimal("1.10")
    assert pb.rate("JPY", "USD") == 1 / Decimal(150)
    assert pb.rate("EUR", "JPY") == Decimal("1.10") * 150
    assert pb.rate("GBP", "EUR") == Decimal("1.25") / Decimal("1.10")


def test_missing_rate(t0: datetime) -> None:
    with pytest.raises(MissingPriceError):
        book(t0, EURUSD="1.10").rate("CHF", "USD")
    with pytest.raises(MissingPriceError):
        PriceBook().tick("EUR/USD")


def test_pip_value(t0: datetime) -> None:
    pb = book(t0, USDJPY="150")
    assert pb.pip_value_per_unit(get_instrument("EUR/USD"), "USD") == Decimal("0.0001")
    assert pb.pip_value_per_unit(get_instrument("USD/JPY"), "USD") == Decimal("0.01") / 150


def test_conversion_symbols() -> None:
    assert conversion_symbols(["EUR/USD", "USD/JPY"], "USD") == set()
    assert conversion_symbols(["EUR/GBP"], "USD") == {"GBP/USD", "EUR/USD"}
    assert conversion_symbols(["EUR/USD"], "EUR") == set()
    assert conversion_symbols(["USD/JPY"], "EUR") == {"EUR/JPY", "EUR/USD"}
