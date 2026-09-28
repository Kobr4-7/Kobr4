import os
from pathlib import Path

import pytest
from alembic import command

from kobr4.db.migrate import alembic_config, upgrade


def test_migrations_match_models(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'm.db'}"
    upgrade(url)
    command.check(alembic_config(url))  # lève une erreur si le schéma a divergé


@pytest.mark.skipif(
    not os.environ.get("KOBR4_TEST_PG_MIGRATE_URL"), reason="KOBR4_TEST_PG_MIGRATE_URL non défini"
)
def test_migrations_on_postgres() -> None:
    url = os.environ["KOBR4_TEST_PG_MIGRATE_URL"]
    upgrade(url)
    command.check(alembic_config(url))
