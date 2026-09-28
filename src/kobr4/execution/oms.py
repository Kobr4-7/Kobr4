"""Gestion des ordres (OMS) : envoi au courtier, suivi des états, réconciliation."""

import asyncio
import logging
from decimal import Decimal

from kobr4.core.bus import EventBus
from kobr4.core.clock import Clock
from kobr4.core.events import (
    CloseRequested,
    FillReceived,
    KillSwitchActivated,
    OrderStatusChanged,
    OrderSubmitted,
    PositionClosed,
    PositionModified,
    PositionOpened,
    ReconciliationMismatch,
    RiskApproved,
)
from kobr4.core.models import ClosedTrade, ExitReason, Fill, Position
from kobr4.core.orders import TRANSITIONS, Order, OrderStatus
from kobr4.execution.broker import Broker, BrokerError
from kobr4.portfolio import Portfolio

log = logging.getLogger(__name__)


class OrderManager:
    """Reçoit les ordres validés par le risque et les demandes de fermeture, parle au
    courtier, et publie ce qui se passe réellement (positions ouvertes, fermées…).

    Un ordre dont l'envoi n'a pas eu de réponse passe à l'état `UNKNOWN` : il n'est jamais
    renvoyé à l'aveugle, la réconciliation interroge le courtier.
    """

    def __init__(
        self,
        bus: EventBus,
        broker: Broker,
        portfolio: Portfolio,
        clock: Clock,
        submit_timeout: float = 10.0,
    ) -> None:
        self.bus = bus
        self.broker = broker
        self.portfolio = portfolio
        self.clock = clock
        self.submit_timeout = submit_timeout
        self.orders: dict[str, Order] = {}
        broker.set_listener(self)
        bus.subscribe(RiskApproved, self._on_approved)
        bus.subscribe(CloseRequested, self._on_close_requested)
        bus.subscribe(KillSwitchActivated, self._on_kill_switch)

    # Écouteur du courtier

    async def on_position_opened(self, position: Position) -> None:
        self.portfolio.apply_opened(position)
        now = self.clock.now()
        await self.bus.publish(
            FillReceived(
                ts=now,
                fill=Fill(
                    fill_id=f"{position.id}-open",
                    order_id=position.id,
                    symbol=position.symbol,
                    side=position.side,
                    quantity=position.quantity,
                    price=position.entry_price,
                    ts=position.opened_at,
                ),
            )
        )
        await self.bus.publish(PositionOpened(ts=now, position=position))

    async def on_position_closed(self, trade: ClosedTrade) -> None:
        self.portfolio.apply_closed(trade)
        await self.bus.publish(PositionClosed(ts=self.clock.now(), trade=trade))

    async def on_position_modified(self, position: Position) -> None:
        self.portfolio.apply_modified(position)
        await self.bus.publish(PositionModified(ts=self.clock.now(), position=position))

    # Ordres

    async def _set(self, order: Order) -> None:
        prev = self.orders.get(order.id)
        self.orders[order.id] = order
        if prev is None or prev.status is not order.status:
            await self.bus.publish(
                OrderStatusChanged(
                    ts=self.clock.now(),
                    order=order,
                    previous_status=prev.status if prev else OrderStatus.CREATED,
                )
            )

    async def _on_approved(self, e: RiskApproved) -> None:
        await self.submit(e.order)

    async def submit(self, order: Order) -> Order:
        self.orders[order.id] = order
        await self.bus.publish(OrderSubmitted(ts=self.clock.now(), order=order))
        try:
            result = await asyncio.wait_for(self.broker.submit(order), self.submit_timeout)
        except TimeoutError:
            result = order.transition(OrderStatus.SUBMITTED, self.clock.now()).transition(
                OrderStatus.UNKNOWN, self.clock.now()
            )
            log.error("ordre %s : pas de réponse du courtier, état inconnu", order.id)
        except BrokerError as exc:
            result = order.transition(OrderStatus.SUBMITTED, self.clock.now()).transition(
                OrderStatus.REJECTED, self.clock.now(), reject_reason=str(exc)
            )
        await self._set(result)
        if result.status is OrderStatus.REJECTED:
            log.warning("ordre %s refusé par le courtier : %s", order.id, result.reject_reason)
        return result

    # Fermetures

    async def _on_close_requested(self, e: CloseRequested) -> None:
        intent = e.intent
        for pos in list(self.portfolio.positions(intent.strategy_id, intent.symbol)):
            if intent.side is None or pos.side is intent.side:
                await self.close(pos.id, ExitReason.SIGNAL)

    async def _on_kill_switch(self, e: KillSwitchActivated) -> None:
        if e.close_positions:
            await self.close_all(ExitReason.KILL_SWITCH)

    async def close(self, position_id: str, reason: ExitReason) -> None:
        try:
            await self.broker.close_position(position_id, reason)
        except BrokerError as exc:
            log.error("fermeture de %s impossible : %s", position_id, exc)

    async def close_all(self, reason: ExitReason) -> None:
        for pos in list(self.portfolio.positions()):
            await self.close(pos.id, reason)

    async def modify(
        self, position_id: str, stop_loss: Decimal, take_profit: Decimal | None
    ) -> None:
        await self.broker.modify_position(position_id, stop_loss, take_profit)

    # Réconciliation

    async def reconcile(self) -> list[str]:
        """Compare l'état local avec celui du courtier et adopte la vue du courtier.

        Renvoie la liste des écarts trouvés (vide si tout concorde).
        """
        issues: list[str] = []
        now = self.clock.now()
        for order in [o for o in self.orders.values() if o.status is OrderStatus.UNKNOWN]:
            remote = await self.broker.find_order(order.id)
            if remote is not None and remote.status not in TRANSITIONS[OrderStatus.UNKNOWN]:
                continue  # le courtier ne sait pas encore : on réessaiera
            if remote is None:
                await self._set(
                    order.transition(OrderStatus.REJECTED, now, reject_reason="inconnu du courtier")
                )
            else:
                await self._set(order.transition(remote.status, now, **_fill_fields(remote)))
            issues.append(f"ordre {order.id} : état inconnu résolu")

        remote_positions = {p.id: p for p in await self.broker.positions()}
        local = {p.id: p for p in self.portfolio.positions()}
        for pid in remote_positions.keys() - local.keys():
            issues.append(f"position {pid} présente chez le courtier mais pas localement")
        for pid in local.keys() - remote_positions.keys():
            issues.append(f"position {pid} fermée chez le courtier à notre insu")
        for pid in local.keys() & remote_positions.keys():
            if local[pid] != remote_positions[pid]:
                issues.append(f"position {pid} différente chez le courtier")
        if issues:
            self.portfolio.replace_positions(list(remote_positions.values()))
            account = await self.broker.account()
            self.portfolio.sync_balance(account.balance)
            for detail in issues:
                log.warning("réconciliation : %s", detail)
                await self.bus.publish(ReconciliationMismatch(ts=now, detail=detail))
        return issues


def _fill_fields(order: Order) -> dict[str, object]:
    return {
        "filled_quantity": order.filled_quantity,
        "avg_fill_price": order.avg_fill_price,
        "broker_order_id": order.broker_order_id,
    }
