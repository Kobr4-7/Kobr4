"""Derniers prix connus et conversion entre devises."""

from datetime import datetime
from decimal import Decimal

from kobr4.core.instruments import INSTRUMENTS, Instrument
from kobr4.core.models import Bar, Tick


class MissingPriceError(LookupError):
    pass


class PriceBook:
    """Dernière cotation de chaque instrument, et conversions de devises qui en découlent.

    En direct, alimenté par les cotations. En backtest, par la clôture des bougies
    (bid = close, ask = close + spread).
    """

    def __init__(self) -> None:
        self._ticks: dict[str, Tick] = {}

    def update(self, tick: Tick) -> None:
        self._ticks[tick.symbol] = tick

    def update_from_bar(self, bar: Bar, ts: datetime | None = None) -> Tick:
        spread = bar.spread or Decimal(0)
        tick = Tick(
            symbol=bar.symbol, bid=bar.close, ask=bar.close + spread, ts=ts or bar.close_time
        )
        self.update(tick)
        return tick

    def get(self, symbol: str) -> Tick | None:
        return self._ticks.get(symbol)

    def tick(self, symbol: str) -> Tick:
        t = self._ticks.get(symbol)
        if t is None:
            raise MissingPriceError(f"aucun prix connu pour {symbol}")
        return t

    def rate(self, from_ccy: str, to_ccy: str) -> Decimal:
        """Taux de conversion (prix milieu) : 1 `from_ccy` = rate `to_ccy`.

        Cherche la paire directe ou inverse, puis un passage par l'USD ou l'EUR.
        """
        direct = self._direct(from_ccy, to_ccy)
        if direct is not None:
            return direct
        for pivot in ("USD", "EUR"):
            a = self._direct(from_ccy, pivot)
            b = self._direct(pivot, to_ccy)
            if a is not None and b is not None:
                return a * b
        raise MissingPriceError(f"impossible de convertir {from_ccy} en {to_ccy}")

    def _direct(self, from_ccy: str, to_ccy: str) -> Decimal | None:
        if from_ccy == to_ccy:
            return Decimal(1)
        direct = self._ticks.get(f"{from_ccy}/{to_ccy}")
        if direct is not None:
            return direct.mid
        inverse = self._ticks.get(f"{to_ccy}/{from_ccy}")
        if inverse is not None:
            return 1 / inverse.mid
        return None

    def convert(self, amount: Decimal, from_ccy: str, to_ccy: str) -> Decimal:
        return amount * self.rate(from_ccy, to_ccy)

    def pip_value_per_unit(self, instrument: Instrument, account_ccy: str) -> Decimal:
        """Valeur d'un pip pour une unité, en devise du compte."""
        return instrument.pip_size * self.rate(instrument.quote, account_ccy)


def conversion_symbols(symbols: list[str], account_ccy: str) -> set[str]:
    """Instruments supplémentaires à suivre pour convertir en devise du compte les P&L
    (devise de cotation) et les marges (devise de base)."""
    needed: set[str] = set()
    for s in symbols:
        inst = INSTRUMENTS[s]
        for ccy in (inst.base, inst.quote):
            if ccy == account_ccy:
                continue
            for candidate in (f"{ccy}/{account_ccy}", f"{account_ccy}/{ccy}"):
                if candidate in INSTRUMENTS:
                    needed.add(candidate)
                    break
    return needed - set(symbols)
