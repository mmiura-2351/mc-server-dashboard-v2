"""SQLAlchemy ORM models for the identity tables (DATABASE.md Section 4).

These map the ``user`` and ``refresh_token`` tables. They are an adapter detail:
the domain entities (``domain.entities``) are framework-free, and these models
are translated to/from them in the repositories.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from mc_server_dashboard_api.core.adapters.database import Base


class UserModel(Base):
    """Row of the ``user`` table: a global identity (DATABASE.md Section 4)."""

    __tablename__ = "user"
    __table_args__ = (
        # Case-insensitive username uniqueness (DATABASE.md Section 4).
        Index("uq_user_username_lower", text("lower(username)"), unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    username: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    is_platform_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false()
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class RefreshTokenModel(Base):
    """Row of the ``refresh_token`` table (DATABASE.md Section 4)."""

    __tablename__ = "refresh_token"
    __table_args__ = (
        # "Revoke all sessions" for a user (DATABASE.md Section 4).
        Index("ix_refresh_token_user_id", "user_id"),
        # Expiry sweeps over still-live tokens (DATABASE.md Section 4).
        Index(
            "ix_refresh_token_expires_at",
            "expires_at",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    issued_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class LoginAttemptModel(Base):
    """Row of the ``login_attempt`` table (SECURITY.md Section 3).

    Append-only record of each authentication attempt. The sliding-window counts
    are ``COUNT`` queries over this table within the window bound; the
    ``(username, created_at)`` and ``(ip, created_at)`` indexes serve them. ``ip``
    is nullable because the per-IP path is skipped when no trustworthy client IP
    is available (SECURITY.md Section 4).
    """

    __tablename__ = "login_attempt"
    __table_args__ = (
        Index("ix_login_attempt_username_created_at", "username", "created_at"),
        Index("ix_login_attempt_ip_created_at", "ip", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String, nullable=False)
    ip: Mapped[str | None] = mapped_column(String, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class AccountLockoutModel(Base):
    """Row of the ``account_lockout`` table (SECURITY.md Section 3).

    At most one row per username, holding the active lockout (``locked_until``)
    and the historic lockout count that drives the exponential back-off. The
    username is the primary key, giving the one-row-per-account invariant.
    """

    __tablename__ = "account_lockout"

    username: Mapped[str] = mapped_column(String, primary_key=True)
    locked_until: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lockout_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
