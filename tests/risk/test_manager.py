from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from kobr4.config.settings import RiskSettings
from kobr4.core.events import (
    EquitySnapshot,
    KillSwitchActivated,
    MarketDataResumed,
    MarketDataStale,
    PositionClosed,
    RiskApproved,
    RiskLimitReached,
    RiskRejected,
    SignalEmitted,
)
from kobr4.core.orders import Order
from kobr4.core.types import Side
from kobr4.risk.calendar import EconomicCalendar, Impact, NewsEvent
from kobr4.risk.manager import Rejection
from tests.stack import intent, make_stack


def test_position_sizing_from_risk(t0: datetime) -> None:
    st = make_stack(t0)
    st.quote("EUR/USD", "1.08000")
    order = st.risk.evaluate(intent(t0, sl="25"))
    assert isinstance(order, Order)
    # 1 % de 10 000 = 100 $ ; 25 pips × 0,0001 $ par unité → 40 000 unités
    assert order.quantity == 40_000
    assert order.stop_loss_distance == Decimal("0.0025")
    assert order.take_profit_distance == Decimal("0.0050")


def test_sizing_in_jpy(t0: datetime) -> None:
    st = make_stack(t0)
    st.quote("USD/JPY", "150.000", spread="0.010")
    order = st.risk.evaluate(intent(t0, symbol="USD/JPY", sl="25"))
    assert isinstance(order, Order)
    # valeur du pip : 0,01 / 150.005 $ par unité → 100 / 25 / 0,0000667 ≈ 60 001 → 60 000
    assert order.quantity == 60_000


def test_sizing_gold_in_ounces(t0: datetime) -> None:
    st = make_stack(t0)
    st.quote("XAU/USD", "3000.000", spread="0.300")
    order = st.risk.evaluate(intent(t0, symbol="XAU/USD", sl="25"))
    assert isinstance(order, Order)
    # 1 pip = 1 $ par once : 100 $ de risque / 25 $ de stop → 4 onces (pas de 1 once)
    assert order.quantity == 4
    assert order.stop_loss_distance == Decimal("25")


def rejected(result: object) -> str:
    assert isinstance(result, Rejection), result
    return result.rule


def test_size_too_small(t0: datetime) -> None:
    st = make_stack(t0, balance="100")
    st.quote("EUR/USD", "1.08000")
    assert rejected(st.risk.evaluate(intent(t0, sl="50"))) == "size_too_small"


def test_no_price(t0: datetime) -> None:
    assert rejected(make_stack(t0).risk.evaluate(intent(t0))) == "no_price"


async def test_max_positions_rules(t0: datetime) -> None:
    st = make_stack(t0, RiskSettings(max_open_positions=2, max_currency_risk_pct=Decimal(20)))
    for sym, px in (("EUR/USD", "1.08"), ("GBP/USD", "1.27"), ("AUD/USD", "0.65")):
        st.quote(sym, px)
    await st.bus.publish(SignalEmitted(ts=t0, intent=intent(t0)))
    assert rejected(st.risk.evaluate(intent(t0))) == "max_positions_per_symbol_strategy"
    assert isinstance(st.risk.evaluate(intent(t0, strategy="s2")), Order)
    await st.bus.publish(SignalEmitted(ts=t0, intent=intent(t0, symbol="GBP/USD")))
    assert rejected(st.risk.evaluate(intent(t0, symbol="AUD/USD"))) == "max_open_positions"
    assert st.risk.rejections == {}


async def test_currency_exposure(t0: datetime) -> None:
    st = make_stack(t0, RiskSettings(max_currency_risk_pct=Decimal("1.5")))
    st.quote("EUR/USD", "1.08")
    st.quote("GBP/USD", "1.27")
    await st.bus.publish(SignalEmitted(ts=t0, intent=intent(t0)))
    rule = rejected(st.risk.evaluate(intent(t0, symbol="GBP/USD")))
    assert rule == "currency_exposure"


async def test_margin(t0: datetime) -> None:
    st = make_stack(t0, RiskSettings(leverage=Decimal(1)))
    st.quote("EUR/USD", "1.08")
    # 40 000 unités × 1,08 = 43 200 $ de marge pour 10 000 $ de capital
    assert rejected(st.risk.evaluate(intent(t0))) == "margin"


def test_spread_filter(t0: datetime) -> None:
    st = make_stack(t0)
    for _ in range(30):
        st.risk.spreads.observe("EUR/USD", Decimal("0.00008"))
    st.quote("EUR/USD", "1.08", spread="0.00030")
    assert rejected(st.risk.evaluate(intent(t0))) == "spread"
    st.quote("EUR/USD", "1.08", spread="0.00010")
    assert isinstance(st.risk.evaluate(intent(t0)), Order)


def test_news_blackout(t0: datetime) -> None:
    cal = EconomicCalendar(
        [
            NewsEvent(
                ts=t0 + timedelta(minutes=20), currency="USD", impact=Impact.HIGH, title="NFP"
            ),
            NewsEvent(
                ts=t0 + timedelta(minutes=5), currency="JPY", impact=Impact.HIGH, title="BoJ"
            ),
            NewsEvent(ts=t0, currency="EUR", impact=Impact.MEDIUM, title="PMI"),
        ]
    )
    st = make_stack(t0, calendar=cal)
    st.quote("EUR/USD", "1.08")
    st.quote("EUR/GBP", "0.85")
    st.quote("GBP/USD", "1.27")
    assert rejected(st.risk.evaluate(intent(t0))) == "news_blackout"
    assert isinstance(st.risk.evaluate(intent(t0, symbol="EUR/GBP")), Order)
    st.clock.advance(timedelta(minutes=51))
    assert isinstance(st.risk.evaluate(intent(t0)), Order)


async def test_stale_data(t0: datetime) -> None:
    st = make_stack(t0)
    st.quote("EUR/USD", "1.08")
    await st.bus.publish(MarketDataStale(ts=t0, symbol="EUR/USD", seconds_since_last_tick=90))
    assert rejected(st.risk.evaluate(intent(t0))) == "stale_data"
    await st.bus.publish(MarketDataResumed(ts=t0, symbol="EUR/USD"))
    assert isinstance(st.risk.evaluate(intent(t0)), Order)


async def test_signal_flow_publishes_decision(t0: datetime) -> None:
    st = make_stack(t0)
    await st.bus.publish(SignalEmitted(ts=t0, intent=intent(t0)))
    (r,) = st.of(RiskRejected)
    assert r.rule == "no_price"
    st.quote("EUR/USD", "1.08")
    await st.bus.publish(SignalEmitted(ts=t0, intent=intent(t0)))
    assert len(st.of(RiskApproved)) == 1
    assert st.risk.rejections == {"no_price": 1}


async def test_daily_loss_halts_until_next_day(t0: datetime) -> None:
    st = make_stack(t0)
    st.quote("EUR/USD", "1.08")
    await st.portfolio.mark(t0)
    st.portfolio.balance = Decimal(9690)  # −3,1 %
    await st.portfolio.mark(t0 + timedelta(hours=1))
    assert [e.rule for e in st.of(RiskLimitReached)] == ["max_daily_loss"]
    st.clock.advance(timedelta(hours=1))
    assert rejected(st.risk.evaluate(intent(t0))) == "max_daily_loss"
    st.clock.advance(timedelta(days=1))
    await st.portfolio.mark(st.clock.now())
    assert isinstance(st.risk.evaluate(intent(t0)), Order)


async def test_weekly_loss_halts_until_monday(t0: datetime) -> None:
    # t0 = lundi 28/09/2026 8 h UTC
    risk = RiskSettings(
        max_daily_loss_pct=Decimal(10), max_weekly_loss_pct=Decimal(4), max_drawdown_pct=Decimal(20)
    )
    st = make_stack(t0, risk)
    st.quote("EUR/USD", "1.08")
    await st.portfolio.mark(t0)
    st.portfolio.balance = Decimal(9800)  # −2 % lundi
    st.clock.advance(timedelta(days=1))
    await st.portfolio.mark(st.clock.now())
    st.portfolio.balance = Decimal(9550)  # −4,5 % sur la semaine, mardi (−2,55 % sur le jour)
    st.clock.advance(timedelta(hours=2))
    await st.portfolio.mark(st.clock.now())
    assert [e.rule for e in st.of(RiskLimitReached)] == ["max_weekly_loss"]
    assert st.portfolio.weekly_pnl_pct() == Decimal("-4.5")
    st.clock.advance(timedelta(days=2))  # jeudi : toujours en pause
    await st.portfolio.mark(st.clock.now())
    assert rejected(st.risk.evaluate(intent(t0))) == "max_weekly_loss"
    st.clock.advance(timedelta(days=4))  # lundi suivant : nouvelle semaine
    await st.portfolio.mark(st.clock.now())
    assert isinstance(st.risk.evaluate(intent(t0)), Order)
    assert st.portfolio.weekly_pnl_pct() == Decimal(0)


async def test_drawdown_triggers_kill_switch(t0: datetime) -> None:
    st = make_stack(t0, RiskSettings(max_daily_loss_pct=Decimal(10), max_drawdown_pct=Decimal(10)))
    st.quote("EUR/USD", "1.08")
    await st.bus.publish(SignalEmitted(ts=t0, intent=intent(t0)))
    await st.portfolio.mark(t0)
    st.portfolio.balance = Decimal(8900)
    await st.bus.publish(
        EquitySnapshot(ts=t0, balance=Decimal(8900), equity=Decimal(8900), open_positions=1)
    )
    (ks,) = st.of(KillSwitchActivated)
    assert not ks.close_positions  # les stops restent en place
    assert st.portfolio.positions()  # pas de fermeture automatique
    assert rejected(st.risk.evaluate(intent(t0, strategy="s2"))) == "kill_switch"
    await st.risk.release_kill_switch("vérifié")
    assert isinstance(st.risk.evaluate(intent(t0, strategy="s2")), Order)


async def test_manual_kill_switch_closes_everything(t0: datetime) -> None:
    st = make_stack(t0)
    st.quote("EUR/USD", "1.08")
    st.quote("GBP/USD", "1.27")
    await st.bus.publish(SignalEmitted(ts=t0, intent=intent(t0)))
    await st.bus.publish(SignalEmitted(ts=t0, intent=intent(t0, symbol="GBP/USD", side=Side.SELL)))
    assert len(st.portfolio.positions()) == 2
    await st.risk.activate_kill_switch("test", close_positions=True)
    assert st.portfolio.positions() == []
    assert {e.trade.reason for e in st.of(PositionClosed)} == {"kill_switch"}


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"risk_per_trade_pct": 4, "max_daily_loss_pct": 3}, "perte journalière"),
        ({"max_daily_loss_pct": 12, "max_drawdown_pct": 10}, "drawdown"),
        ({"risk_per_trade_pct": 2, "max_weekly_loss_pct": 1.5}, "hebdomadaire"),
        ({"max_weekly_loss_pct": 15, "max_drawdown_pct": 10}, "hebdomadaire"),
    ],
)
def test_settings_consistency(kw: dict[str, int], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        RiskSettings.model_validate(kw)
