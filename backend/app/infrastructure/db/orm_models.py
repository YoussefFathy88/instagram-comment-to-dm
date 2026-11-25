"""SQLAlchemy tables. Only repositories import this module."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class CampaignRow(Base):
    __tablename__ = "engagement_campaigns"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    status: Mapped[str] = mapped_column(String(20))
    approved_fingerprint: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON)  # media_ids, keywords, dm_message, replies, limits
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EngagementActionRow(Base):
    __tablename__ = "engagement_actions"
    __table_args__ = (UniqueConstraint("campaign_id", "comment_id", name="uq_engagement_campaign_comment"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[str] = mapped_column(String(32), index=True)
    comment_id: Mapped[str] = mapped_column(String(64))
    media_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    username: Mapped[str] = mapped_column(String(100))
    comment_text: Mapped[str] = mapped_column(Text)
    comment_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(30), index=True)
    dm_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reply_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    reply_comment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
