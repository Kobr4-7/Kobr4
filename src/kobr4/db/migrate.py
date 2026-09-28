"""Migrations du schéma (Alembic), appliquées au démarrage du serveur."""

from pathlib import Path

from alembic import command
from alembic.config import Config

MIGRATIONS = Path(__file__).parent / "migrations"


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def upgrade(url: str) -> None:
    command.upgrade(alembic_config(url), "head")


def revision(url: str, message: str) -> None:
    command.revision(alembic_config(url), message=message, autogenerate=True)
