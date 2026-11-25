"""initial tables: campaigns + per-comment action ledger

Revision ID: 0001
Revises:
Create Date: 2026-11-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "engagement_campaigns",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("approved_fingerprint", sa.String(32), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "engagement_actions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("campaign_id", sa.String(32), nullable=False, index=True),
        sa.Column("comment_id", sa.String(64), nullable=False),
        sa.Column("media_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False, index=True),
        sa.Column("username", sa.String(100), nullable=False),
        sa.Column("comment_text", sa.Text(), nullable=False),
        sa.Column("comment_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(30), nullable=False, index=True),
        sa.Column("dm_message_id", sa.String(255), nullable=True),
        sa.Column("reply_text", sa.Text(), nullable=True),
        sa.Column("reply_comment_id", sa.String(64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("campaign_id", "comment_id", name="uq_engagement_campaign_comment"),
    )


def downgrade() -> None:
    op.drop_table("engagement_actions")
    op.drop_table("engagement_campaigns")
