"""Commandes `kobr4 server …` : lancer la plateforme, générer la clé, migrer la base."""

import argparse
import logging

from kobr4.db.migrate import upgrade
from kobr4.security.crypto import CryptoError, generate_master_key
from kobr4.web.config import ServerConfig

log = logging.getLogger("kobr4.server")


def add_parser(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    server = sub.add_parser("server", help="plateforme web")
    cmds = server.add_subparsers(dest="server_cmd", required=True)
    run = cmds.add_parser("run", help="démarrer le site et les bots")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8000)
    run.add_argument("--no-migrate", action="store_true", help="ne pas appliquer les migrations")
    cmds.add_parser("gen-key", help="générer une clé maîtresse (KOBR4_MASTER_KEY)")
    cmds.add_parser("migrate", help="mettre le schéma de la base à jour")


def run(args: argparse.Namespace) -> int:
    if args.server_cmd == "gen-key":
        print(generate_master_key())
        return 0
    config = ServerConfig.from_env()
    if args.server_cmd == "migrate":
        upgrade(config.database_url)
        log.info("base à jour")
        return 0
    if not config.master_key:
        log.error(
            "KOBR4_MASTER_KEY absente : `kobr4 server gen-key`, puis la définir (et la sauvegarder)"
        )
        return 2
    if not args.no_migrate:
        upgrade(config.database_url)

    import uvicorn

    from kobr4.web.app import create_app

    try:
        app = create_app(config)
    except CryptoError as e:
        log.error("%s", e)
        return 2
    uvicorn.run(
        app, host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="127.0.0.1"
    )
    return 0
