"""Métriques Prometheus des bots (une série par bot, étiquette `bot`)."""

from datetime import datetime

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from kobr4.core.bus import EventBus
from kobr4.core.events import (
    EquitySnapshot,
    KillSwitchActivated,
    KillSwitchReleased,
    OrderStatusChanged,
    OrderSubmitted,
    ReconciliationMismatch,
    RiskApproved,
    RiskRejected,
    SignalEmitted,
    TickReceived,
)

REGISTRY = CollectorRegistry()

TICKS = Counter("kobr4_ticks_total", "Cotations reçues", ["bot"], registry=REGISTRY)
LAST_TICK = Gauge(
    "kobr4_last_tick_timestamp_seconds",
    "Heure de la dernière cotation",
    ["bot", "symbol"],
    registry=REGISTRY,
)
SIGNALS = Counter(
    "kobr4_signals_total", "Intentions émises par les stratégies", ["bot"], registry=REGISTRY
)
REJECTIONS = Counter(
    "kobr4_risk_rejections_total", "Refus du risque", ["bot", "rule"], registry=REGISTRY
)
ORDERS = Counter(
    "kobr4_orders_total", "Ordres par état final", ["bot", "status"], registry=REGISTRY
)
LATENCY = Histogram(
    "kobr4_signal_to_order_seconds",
    "Délai entre l'intention et l'envoi de l'ordre",
    ["bot"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
    registry=REGISTRY,
)
EQUITY = Gauge("kobr4_equity", "Équité", ["bot"], registry=REGISTRY)
BALANCE = Gauge("kobr4_balance", "Solde", ["bot"], registry=REGISTRY)
OPEN_POSITIONS = Gauge("kobr4_open_positions", "Positions ouvertes", ["bot"], registry=REGISTRY)
KILL_SWITCH = Gauge(
    "kobr4_kill_switch", "Arrêt d'urgence actif (1) ou non (0)", ["bot"], registry=REGISTRY
)
MISMATCHES = Counter(
    "kobr4_reconciliation_mismatches_total", "Écarts de réconciliation", ["bot"], registry=REGISTRY
)
UP = Gauge("kobr4_bot_up", "Bot en marche (1) ou non (0)", ["bot"], registry=REGISTRY)


class BotMetrics:
    def __init__(self, bot_id: str, bus: EventBus) -> None:
        self.bot = bot_id
        self._signal_ts: dict[int, datetime] = {}
        bus.subscribe(TickReceived, self._tick)
        bus.subscribe(SignalEmitted, self._signal)
        bus.subscribe(RiskApproved, self._approved)
        bus.subscribe(RiskRejected, lambda e: REJECTIONS.labels(self.bot, e.rule).inc())
        bus.subscribe(OrderSubmitted, self._submitted)
        bus.subscribe(OrderStatusChanged, self._status)
        bus.subscribe(EquitySnapshot, self._equity)
        bus.subscribe(KillSwitchActivated, lambda e: KILL_SWITCH.labels(self.bot).set(1))
        bus.subscribe(KillSwitchReleased, lambda e: KILL_SWITCH.labels(self.bot).set(0))
        bus.subscribe(ReconciliationMismatch, lambda e: MISMATCHES.labels(self.bot).inc())
        self._pending: dict[str, datetime] = {}

    def _tick(self, e: TickReceived) -> None:
        TICKS.labels(self.bot).inc()
        LAST_TICK.labels(self.bot, e.tick.symbol).set(e.ts.timestamp())

    def _signal(self, e: SignalEmitted) -> None:
        SIGNALS.labels(self.bot).inc()

    def _approved(self, e: RiskApproved) -> None:
        self._pending[e.order.id] = e.intent.ts

    def _submitted(self, e: OrderSubmitted) -> None:
        start = self._pending.pop(e.order.id, None)
        if start is not None:
            LATENCY.labels(self.bot).observe(max(0.0, (e.ts - start).total_seconds()))

    def _status(self, e: OrderStatusChanged) -> None:
        if e.order.status.is_terminal or e.order.status.value == "unknown":
            ORDERS.labels(self.bot, e.order.status.value).inc()

    def _equity(self, e: EquitySnapshot) -> None:
        EQUITY.labels(self.bot).set(float(e.equity))
        BALANCE.labels(self.bot).set(float(e.balance))
        OPEN_POSITIONS.labels(self.bot).set(e.open_positions)

    def set_up(self, up: bool) -> None:
        UP.labels(self.bot).set(1 if up else 0)
