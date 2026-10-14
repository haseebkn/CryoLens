"""Independent optical pairing and append-only evidence.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "optical_pairs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "detection_id",
            sa.String(64),
            sa.ForeignKey("detections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("metadata_json", json_type, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_optical_pairs_detection_id", "optical_pairs", ["detection_id"])
    op.create_table(
        "optical_evidence",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "pair_id",
            sa.String(64),
            sa.ForeignKey("optical_pairs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("assessment", sa.String(32), nullable=False),
        sa.Column("visibility", sa.String(32), nullable=False),
        sa.Column("observed_lonlat", json_type, nullable=True),
        sa.Column("analyst_id", sa.String(128), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_optical_evidence_pair_id", "optical_evidence", ["pair_id"])


def downgrade() -> None:
    raise RuntimeError(
        "Downgrade would discard analyst evidence; restore a reviewed backup instead"
    )
