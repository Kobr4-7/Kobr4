from decimal import Decimal

import pytest
from pydantic import ValidationError

from kobr4.core.instruments import Instrument, get_instrument


def test_major_pip_sizes() -> None:
    eurusd = get_instrument("EUR/USD")
    usdjpy = get_instrument("USD/JPY")
    assert (eurusd.pip_size, eurusd.price_precision) == (Decimal("0.0001"), 5)
    assert (usdjpy.pip_size, usdjpy.price_precision) == (Decimal("0.01"), 3)
    assert (eurusd.base, eurusd.quote) == ("EUR", "USD")


def test_pip_conversions() -> None:
    eurusd = get_instrument("EUR/USD")
    assert eurusd.to_pips(Decimal("1.08450") - Decimal("1.08200")) == Decimal("25")
    assert eurusd.from_pips(Decimal("25")) == Decimal("0.0025")
    assert eurusd.pip_value(100_000) == Decimal("10")


def test_round_price() -> None:
    assert get_instrument("EUR/USD").round_price(Decimal("1.084256")) == Decimal("1.08426")
    assert get_instrument("USD/JPY").round_price(Decimal("149.3214")) == Decimal("149.321")


def test_unknown_instrument() -> None:
    with pytest.raises(KeyError, match="XAG/USD"):
        get_instrument("XAG/USD")


@pytest.mark.parametrize("symbol", ["EURUSD", "eur/usd", "EUR-USD", "EURO/USD"])
def test_invalid_symbol(symbol: str) -> None:
    with pytest.raises(ValidationError):
        Instrument(symbol=symbol, pip_size=Decimal("0.0001"), price_precision=5)


def test_gold() -> None:
    gold = get_instrument("XAU/USD")
    assert (gold.base, gold.quote) == ("XAU", "USD")
    assert gold.round_price(Decimal("3812.34567")) == Decimal("3812.346")
    assert gold.from_pips(Decimal(25)) == Decimal(25)  # stop de 25 pips = 25 $
