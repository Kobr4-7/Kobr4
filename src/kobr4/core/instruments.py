"""Instruments : paires de devises et or (XAU/USD) ; taille du pip, précision des prix."""

import re
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SYMBOL_RE = re.compile(r"^[A-Z]{3}/[A-Z]{3}$")


class Instrument(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    pip_size: Decimal = Field(gt=0)
    price_precision: int = Field(ge=0, le=8)
    contract_size: int = Field(default=100_000, gt=0)

    @field_validator("symbol")
    @classmethod
    def _check_symbol(cls, v: str) -> str:
        if not _SYMBOL_RE.match(v):
            raise ValueError(f"symbole invalide : {v!r} (format attendu : EUR/USD)")
        return v

    @property
    def base(self) -> str:
        return self.symbol[:3]

    @property
    def quote(self) -> str:
        return self.symbol[4:]

    @property
    def tick_size(self) -> Decimal:
        return Decimal(1).scaleb(-self.price_precision)

    def round_price(self, price: Decimal) -> Decimal:
        return price.quantize(self.tick_size)

    def to_pips(self, price_distance: Decimal) -> Decimal:
        return price_distance / self.pip_size

    def from_pips(self, pips: Decimal) -> Decimal:
        return pips * self.pip_size

    def pip_value(self, units: int) -> Decimal:
        """Valeur d'un pip, en devise de cotation, pour une quantité donnée."""
        return self.pip_size * units


def _major(symbol: str) -> Instrument:
    jpy = symbol.endswith("JPY")
    return Instrument(
        symbol=symbol,
        pip_size=Decimal("0.01") if jpy else Decimal("0.0001"),
        price_precision=3 if jpy else 5,
    )


GOLD = Instrument(
    symbol="XAU/USD",
    pip_size=Decimal("1"),
    price_precision=3,
    contract_size=100,
)
"""Or en dollars, une unité = une once. Le « pip » vaut 1 $ : les réglages des stratégies
en pips (stop de 25 pips = 25 $) gardent ainsi une taille comparable, en proportion de la
volatilité, à celle des paires de devises. Précision de 3 décimales, comme chez Dukascopy."""

INSTRUMENTS: dict[str, Instrument] = {
    "XAU/USD": GOLD,
    **{
        s: _major(s)
        for s in (
            "EUR/USD",
            "GBP/USD",
            "USD/JPY",
            "USD/CHF",
            "AUD/USD",
            "USD/CAD",
            "NZD/USD",
            "EUR/GBP",
            "EUR/JPY",
            "GBP/JPY",
        )
    },
}


TYPICAL_SPREAD_PIPS: dict[str, Decimal] = {
    "EUR/USD": Decimal("0.6"),
    "GBP/USD": Decimal("0.9"),
    "USD/JPY": Decimal("0.7"),
    "USD/CHF": Decimal("1.0"),
    "AUD/USD": Decimal("0.8"),
    "USD/CAD": Decimal("1.0"),
    "NZD/USD": Decimal("1.2"),
    "EUR/GBP": Decimal("0.9"),
    "EUR/JPY": Decimal("1.2"),
    "GBP/JPY": Decimal("1.8"),
    "XAU/USD": Decimal("0.3"),  # 1 pip = 1 $ pour l'or
}
"""Spread typique d'un compte standard, quand la source n'en fournit pas."""


def typical_spread_pips(symbol: str) -> Decimal:
    return TYPICAL_SPREAD_PIPS.get(symbol, Decimal("1.2"))


def get_instrument(symbol: str) -> Instrument:
    try:
        return INSTRUMENTS[symbol]
    except KeyError:
        raise KeyError(f"instrument inconnu : {symbol}") from None
