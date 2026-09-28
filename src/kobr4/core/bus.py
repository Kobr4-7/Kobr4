"""Bus d'événements.

Les modules ne s'appellent pas directement : ils publient et s'abonnent à des événements.
L'interface `EventBus` permettra de passer à Redis Streams ou NATS sans toucher aux modules.
"""

import inspect
import logging
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from kobr4.core.events import Event

log = logging.getLogger(__name__)

Handler = Callable[[Any], Awaitable[None] | None]
Unsubscribe = Callable[[], None]


class EventBus(ABC):
    @abstractmethod
    def subscribe[E: Event](
        self, event_type: type[E], handler: Callable[[E], Awaitable[None] | None]
    ) -> Unsubscribe:
        """Abonne `handler` à `event_type` et à ses sous-classes."""

    @abstractmethod
    async def publish(self, event: Event) -> None:
        """Publie un événement."""


class InMemoryEventBus(EventBus):
    """Bus en mémoire, déterministe.

    Les événements sont traités dans l'ordre de publication (FIFO), un par un, et chaque
    abonné est appelé dans l'ordre d'abonnement. Un événement publié par un abonné est mis
    en file et traité après l'événement en cours : le `publish` imbriqué rend la main
    immédiatement, le `publish` extérieur ne rend la main qu'une fois la file vide.

    Une erreur dans un abonné est journalisée et n'empêche pas les autres abonnés de
    recevoir l'événement.
    """

    def __init__(self) -> None:
        self._handlers: defaultdict[type[Event], list[Handler]] = defaultdict(list)
        self._queue: deque[Event] = deque()
        self._dispatching = False
        self.handler_errors = 0

    def subscribe[E: Event](
        self, event_type: type[E], handler: Callable[[E], Awaitable[None] | None]
    ) -> Unsubscribe:
        handlers = self._handlers[event_type]
        handlers.append(handler)

        def unsubscribe() -> None:
            if handler in handlers:
                handlers.remove(handler)

        return unsubscribe

    async def publish(self, event: Event) -> None:
        self._queue.append(event)
        if self._dispatching:
            return
        self._dispatching = True
        try:
            while self._queue:
                await self._dispatch(self._queue.popleft())
        finally:
            self._dispatching = False

    async def _dispatch(self, event: Event) -> None:
        for cls in type(event).__mro__:
            if not (isinstance(cls, type) and issubclass(cls, Event)):
                continue
            for handler in list(self._handlers.get(cls, ())):
                try:
                    result = handler(event)
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    self.handler_errors += 1
                    log.exception("erreur dans l'abonné %r pour %s", handler, type(event).__name__)
