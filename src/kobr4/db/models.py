"""Schéma de la base : plateforme (comptes, connexions, bots) et journal des bots.

PostgreSQL en production, SQLite pour le développement et les tests.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class UtcDateTime(TypeDecorator[datetime]):
    """Dates toujours en UTC avec fuseau, y compris sous SQLite."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("date sans fuseau horaire")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


Json = JSON().with_variant(JSONB(), "postgresql")
Money = Numeric(24, 8)
# SQLite n'auto-incrémente que les colonnes INTEGER.
BigId = BigInteger().with_variant(Integer(), "sqlite")


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        datetime: UtcDateTime,
        Decimal: Money,
        dict[str, Any]: Json,
        list[str]: Json,
    }


# Plateforme


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(80), default="")
    totp_secret: Mapped[str | None] = mapped_column(Text)
    """Chiffré."""
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    recovery_codes: Mapped[list[str]] = mapped_column(Json, default=list)
    """Empreintes des codes de secours de la double authentification (usage unique)."""
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    onboarding_step: Mapped[str] = mapped_column(String(32), default="profile")
    base_currency: Mapped[str] = mapped_column(String(3), default="USD")
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Paris")
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column()

    brokers: Mapped[list["BrokerConnection"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    bots: Mapped[list["BotRecord"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserSession(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    """Empreinte SHA-256 du jeton de session (le jeton lui-même n'est jamais stocké)."""
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime] = mapped_column()
    user_agent: Mapped[str] = mapped_column(String(255), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    mfa_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class BrokerConnection(Base):
    __tablename__ = "broker_connections"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    broker: Mapped[str] = mapped_column(String(32))
    environment: Mapped[str] = mapped_column(String(16))
    """practice ou live."""
    account_id: Mapped[str] = mapped_column(String(64))
    token_encrypted: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(String(80), default="")
    account_currency: Mapped[str | None] = mapped_column(String(3))
    verified_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    user: Mapped[User] = relationship(back_populates="brokers")


class NotificationSettings(Base):
    __tablename__ = "notification_settings"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    telegram_token_encrypted: Mapped[str | None] = mapped_column(Text)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64))
    notify_trades: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_risk: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_daily_summary: Mapped[bool] = mapped_column(Boolean, default=True)


class BotRecord(Base):
    __tablename__ = "bots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    broker_connection_id: Mapped[str | None] = mapped_column(
        ForeignKey("broker_connections.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(80))
    mode: Mapped[str] = mapped_column(String(16), default="paper")
    """paper ou live."""
    desired_state: Mapped[str] = mapped_column(String(16), default="stopped")
    """running ou stopped : ce que l'utilisateur a demandé (repris après un redémarrage)."""
    status: Mapped[str] = mapped_column(String(16), default="stopped")
    """stopped, starting, running, error : l'état réel."""
    config: Mapped[dict[str, Any]] = mapped_column(Json)
    """Instruments, risque et stratégies (format de `Settings`)."""
    last_error: Mapped[str | None] = mapped_column(Text)
    live_confirmed_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="bots")


class AuditEntry(Base):
    """Actions humaines sensibles : connexion, changement de réglages, arrêt d'urgence,
    passage en réel. En ajout seul."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    user_id: Mapped[str | None] = mapped_column(String(32), index=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow)
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(Json, default=dict)
    ip: Mapped[str] = mapped_column(String(64), default="")


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    config: Mapped[dict[str, Any]] = mapped_column(Json)
    result: Mapped[dict[str, Any] | None] = mapped_column(Json)
    error: Mapped[str | None] = mapped_column(Text)


class ProposalRecord(Base):
    __tablename__ = "proposals"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    bot_id: Mapped[str | None] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"))
    strategy_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    payload: Mapped[dict[str, Any]] = mapped_column(Json)


# Journal des bots


class BotEvent(Base):
    __tablename__ = "bot_events"
    __table_args__ = (Index("ix_bot_events_bot_ts", "bot_id", "ts"),)

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    bot_id: Mapped[str] = mapped_column(String(32))
    ts: Mapped[datetime] = mapped_column()
    type: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict[str, Any]] = mapped_column(Json)


class TradeRecord(Base):
    __tablename__ = "trades"
    __table_args__ = (
        UniqueConstraint("bot_id", "position_id"),
        Index("ix_trades_bot_closed", "bot_id", "closed_at"),
    )

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    bot_id: Mapped[str] = mapped_column(String(32))
    position_id: Mapped[str] = mapped_column(String(80))
    strategy_id: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(7))
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[int] = mapped_column(BigInteger)
    entry_price: Mapped[Decimal] = mapped_column()
    exit_price: Mapped[Decimal] = mapped_column()
    opened_at: Mapped[datetime] = mapped_column()
    closed_at: Mapped[datetime] = mapped_column()
    reason: Mapped[str] = mapped_column(String(16))
    pnl: Mapped[Decimal] = mapped_column()
    commission: Mapped[Decimal] = mapped_column(default=Decimal(0))
    financing: Mapped[Decimal] = mapped_column(default=Decimal(0))


class EquityPoint(Base):
    __tablename__ = "equity_points"

    bot_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[datetime] = mapped_column(primary_key=True)
    balance: Mapped[Decimal] = mapped_column()
    equity: Mapped[Decimal] = mapped_column()
    open_positions: Mapped[int] = mapped_column(Integer)
