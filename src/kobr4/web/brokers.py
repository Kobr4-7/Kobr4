"""Vérification d'une connexion courtier au moment où l'utilisateur la saisit."""

from typing import Any, Literal

import httpx

from kobr4.execution.brokers.oanda import HOSTS


class BrokerCheckError(Exception):
    pass


async def _oanda_get(client: httpx.AsyncClient, path: str) -> dict[str, Any]:
    try:
        resp = await client.get(path)
    except httpx.HTTPError as e:
        raise BrokerCheckError(f"OANDA injoignable ({e.__class__.__name__})") from e
    if resp.status_code == 401:
        raise BrokerCheckError("jeton refusé par OANDA : vérifie qu'il est complet et actif")
    if resp.status_code == 403:
        raise BrokerCheckError("ce jeton n'a pas accès à ce compte")
    if resp.status_code == 404:
        raise BrokerCheckError("compte introuvable pour ce jeton et cet environnement")
    if resp.status_code >= 400:
        raise BrokerCheckError(f"OANDA a répondu HTTP {resp.status_code}")
    data: dict[str, Any] = resp.json()
    return data


def _client(
    token: str, environment: str, transport: httpx.AsyncBaseTransport | None
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=HOSTS[environment][0],
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
        transport=transport,
    )


async def oanda_accounts(
    token: str,
    environment: Literal["practice", "live"],
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Comptes accessibles avec ce jeton, avec leur devise et leur solde."""
    async with _client(token, environment, transport) as c:
        ids = [a["id"] for a in (await _oanda_get(c, "/v3/accounts")).get("accounts", [])]
        out = []
        for account_id in ids:
            a = (await _oanda_get(c, f"/v3/accounts/{account_id}/summary"))["account"]
            out.append(
                {
                    "id": account_id,
                    "alias": a.get("alias", ""),
                    "currency": a["currency"],
                    "balance": a["balance"],
                }
            )
        return out


async def verify_oanda(
    token: str,
    account_id: str,
    environment: Literal["practice", "live"],
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    async with _client(token, environment, transport) as c:
        a = (await _oanda_get(c, f"/v3/accounts/{account_id}/summary"))["account"]
        return {"currency": a["currency"], "balance": a["balance"], "alias": a.get("alias", "")}
