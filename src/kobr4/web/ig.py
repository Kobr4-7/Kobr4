"""Sessions IG partagées : une seule session (et un seul limiteur de débit) par
connexion, quel que soit le nombre de bots qui l'utilisent."""

import httpx

from kobr4.db.models import BrokerConnection
from kobr4.execution.brokers.ig import Environment, IgClient, credentials_loads


class IgClients:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.transport = transport
        self.clients: dict[str, IgClient] = {}

    def client_for(self, conn: BrokerConnection, raw: str) -> IgClient:
        c = self.clients.get(conn.id)
        if c is None:
            env: Environment = "live" if conn.environment == "live" else "practice"
            api_key, identifier, password = credentials_loads(raw)
            c = IgClient(
                env, api_key, identifier, password, conn.account_id, transport=self.transport
            )
            self.clients[conn.id] = c
        return c

    async def forget(self, conn_id: str) -> None:
        c = self.clients.pop(conn_id, None)
        if c is not None:
            await c.aclose()

    async def aclose(self) -> None:
        for conn_id in list(self.clients):
            await self.forget(conn_id)
