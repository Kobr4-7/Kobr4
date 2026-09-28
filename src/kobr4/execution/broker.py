"""Interface commune des courtiers.

Le cœur ne connaît que cette interface. Chaque courtier (simulé, OANDA…) est un
adaptateur qui la réalise.
"""

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Protocol

from kobr4.core.models import Account, ClosedTrade, ExitReason, Position
from kobr4.core.orders import Order


class BrokerError(Exception):
    pass


class BrokerListener(Protocol):
    """Reçoit les changements qui viennent du courtier (exécutions, stops touchés…)."""

    async def on_position_opened(self, position: Position) -> None: ...

    async def on_position_closed(self, trade: ClosedTrade) -> None: ...

    async def on_position_modified(self, position: Position) -> None: ...


class Broker(ABC):
    account_currency: str

    def __init__(self) -> None:
        self._listener: BrokerListener | None = None

    def set_listener(self, listener: BrokerListener) -> None:
        self._listener = listener

    @property
    def listener(self) -> BrokerListener:
        if self._listener is None:
            raise BrokerError("aucun écouteur branché sur le courtier")
        return self._listener

    async def connect(self) -> None:  # noqa: B027
        """Ouvre la connexion. Rien à faire par défaut."""

    async def close(self) -> None:  # noqa: B027
        """Ferme la connexion. Rien à faire par défaut."""

    @abstractmethod
    async def submit(self, order: Order) -> Order:
        """Envoie l'ordre et renvoie son nouvel état (exécuté, accepté ou refusé).

        Une exécution est aussi signalée à l'écouteur (`on_position_opened`) avant le retour.
        """

    @abstractmethod
    async def close_position(self, position_id: str, reason: ExitReason) -> None:
        """Ferme une position au marché. La fermeture est signalée à l'écouteur."""

    @abstractmethod
    async def modify_position(
        self, position_id: str, stop_loss: Decimal, take_profit: Decimal | None
    ) -> Position: ...

    @abstractmethod
    async def positions(self) -> list[Position]: ...

    @abstractmethod
    async def account(self) -> Account: ...

    @abstractmethod
    async def find_order(self, order_id: str) -> Order | None:
        """Retrouve un ordre par son identifiant client (réconciliation)."""
