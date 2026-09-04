"""add_iip_sightings

Revision ID: ce9822fbab09
Revises: 0001
Create Date: 2026-08-19 18:16:10.851240

"""

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ce9822fbab09"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "iip_sightings",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("sighting_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "geom_wgs84",
            geoalchemy2.types.Geometry(
                geometry_type="POINT",
                srid=4326,
                spatial_index=True,
                from_text="ST_GeomFromEWKT",
                name="geometry",
            ),
            nullable=False,
        ),
        sa.Column(
            "geom_epsg3978",
            geoalchemy2.types.Geometry(
                geometry_type="POINT",
                srid=3978,
                spatial_index=True,
                from_text="ST_GeomFromEWKT",
                name="geometry",
            ),
            nullable=False,
        ),
        sa.Column("size_class", sa.String(length=32), nullable=True),
        sa.Column("shape", sa.String(length=32), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="IIP"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_iip_sightings_sighting_time"),
        "iip_sightings",
        ["sighting_time"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_iip_sightings_sighting_time"), table_name="iip_sightings")
    # GeoAlchemy handles spatial index drops automatically
    op.drop_table("iip_sightings")
