"""Configuration du serveur, lue dans les variables d'environnement."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "oui")


@dataclass(frozen=True)
class ServerConfig:
    database_url: str = "sqlite+aiosqlite:///kobr4.db"
    master_key: str = ""
    public_url: str = "http://localhost:8000"
    data_dir: Path = Path("data")
    lab_dir: Path = Path("lab")
    static_dir: Path | None = None
    invite_code: str | None = None
    """Si défini, exigé à l'inscription (sauf pour le tout premier compte)."""
    open_signup: bool = False
    """Inscription libre. Par défaut, seul le premier compte peut s'inscrire sans invitation."""
    min_paper_days: int = 28
    """Durée minimale en démo avant de pouvoir passer un bot en réel."""
    metrics_token: str | None = None
    session_days: int = 30
    start_bots: bool = True
    trusted_hosts: list[str] = field(default_factory=lambda: ["*"])

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @classmethod
    def from_env(cls) -> "ServerConfig":
        static = os.environ.get("KOBR4_STATIC_DIR")
        return cls(
            database_url=os.environ.get("KOBR4_DATABASE_URL", cls.database_url),
            master_key=os.environ.get("KOBR4_MASTER_KEY", ""),
            public_url=os.environ.get("KOBR4_PUBLIC_URL", cls.public_url).rstrip("/"),
            data_dir=Path(os.environ.get("KOBR4_DATA_DIR", "data")),
            lab_dir=Path(os.environ.get("KOBR4_LAB_DIR", "lab")),
            static_dir=Path(static) if static else None,
            invite_code=os.environ.get("KOBR4_INVITE_CODE") or None,
            open_signup=_bool("KOBR4_OPEN_SIGNUP", False),
            min_paper_days=int(os.environ.get("KOBR4_MIN_PAPER_DAYS", "28")),
            metrics_token=os.environ.get("KOBR4_METRICS_TOKEN") or None,
            start_bots=_bool("KOBR4_START_BOTS", True),
        )
