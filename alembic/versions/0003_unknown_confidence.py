"""Preserve unknown confidence and forecast uncertainty as null.

Revision ID: 0003
Revises: ce9822fbab09
"""

from alembic import op

revision = "0003"
down_revision = "ce9822fbab09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("detections", "confidence", nullable=True, server_default=None)
    op.alter_column("detections", "predicted_class", server_default="unclassified")
    op.alter_column("drift_forecasts", "uncertainty_radius_m", nullable=True, server_default=None)
    # The previous default was a fabricated claim of zero forecast error.
    op.execute(
        "UPDATE drift_forecasts SET uncertainty_radius_m = NULL WHERE uncertainty_radius_m = 0"
    )


def downgrade() -> None:
    # Preserve unknown values: a destructive downgrade would invent numeric evidence.
    raise RuntimeError(
        "Downgrade would fabricate unknown confidence/uncertainty; restore a backup instead."
    )
