"""Initial PostGIS schema for scenes, detections, validations, and drift_forecasts.

Revision ID: 0001
Revises: None
Create Date: 2026-08-19 18:00:00.000000

"""

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Ensure PostGIS extension is available
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis;")

    # 1. scenes table
    op.create_table(
        "scenes",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("product_id", sa.String(length=255), nullable=False, unique=True),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("polarizations", JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("acquisition_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "footprint_epsg3978",
            geoalchemy2.types.Geometry(
                geometry_type="POLYGON", srid=3978, from_text="ST_GeomFromEWKT", name="geometry"
            ),
            nullable=True,
        ),
        sa.Column(
            "footprint_wgs84",
            geoalchemy2.types.Geometry(
                geometry_type="POLYGON", srid=4326, from_text="ST_GeomFromEWKT", name="geometry"
            ),
            nullable=True,
        ),
        sa.Column(
            "processing_provenance",
            JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("cog_path", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="PROCESSED"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("idx_scenes_acquisition_time", "scenes", ["acquisition_time"])
    op.create_index("idx_scenes_status", "scenes", ["status"])
    op.create_index("idx_scenes_product_id", "scenes", ["product_id"])

    # 2. detections table
    op.create_table(
        "detections",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "scene_id",
            sa.String(length=64),
            sa.ForeignKey("scenes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "geom_epsg3978",
            geoalchemy2.types.Geometry(
                geometry_type="POLYGON", srid=3978, from_text="ST_GeomFromEWKT", name="geometry"
            ),
            nullable=True,
        ),
        sa.Column(
            "geom_wgs84",
            geoalchemy2.types.Geometry(
                geometry_type="POLYGON", srid=4326, from_text="ST_GeomFromEWKT", name="geometry"
            ),
            nullable=True,
        ),
        sa.Column(
            "centroid_wgs84",
            geoalchemy2.types.Geometry(
                geometry_type="POINT", srid=4326, from_text="ST_GeomFromEWKT", name="geometry"
            ),
            nullable=True,
        ),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("detector_name", sa.String(length=64), nullable=False, server_default="CA-CFAR"),
        sa.Column(
            "detector_params",
            JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "predicted_class", sa.String(length=64), nullable=False, server_default="iceberg"
        ),
        sa.Column("length_m", sa.Float(), nullable=True),
        sa.Column("width_m", sa.Float(), nullable=True),
        sa.Column("estimated_area_m2", sa.Float(), nullable=True),
        sa.Column("peak_sigma0_hv_db", sa.Float(), nullable=True),
        sa.Column("mean_sigma0_hv_db", sa.Float(), nullable=True),
        sa.Column("peak_sigma0_hh_db", sa.Float(), nullable=True),
        sa.Column("hh_hv_ratio_db", sa.Float(), nullable=True),
        sa.Column("incidence_angle_deg", sa.Float(), nullable=True),
        sa.Column(
            "properties",
            JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("idx_detections_scene_id", "detections", ["scene_id"])
    op.create_index("idx_detections_confidence", "detections", ["confidence"])
    op.create_index("idx_detections_scene_class", "detections", ["scene_id", "predicted_class"])

    # 3. validations table
    op.create_table(
        "validations",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "detection_id",
            sa.String(length=64),
            sa.ForeignKey("detections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("analyst_verdict", sa.String(length=64), nullable=False),
        sa.Column("corrected_class", sa.String(length=64), nullable=True),
        sa.Column(
            "corrected_geom_wgs84",
            geoalchemy2.types.Geometry(
                geometry_type="POLYGON", srid=4326, from_text="ST_GeomFromEWKT", name="geometry"
            ),
            nullable=True,
        ),
        sa.Column("analyst_id", sa.String(length=128), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "validated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("idx_validations_detection_id", "validations", ["detection_id"])

    # 4. drift_forecasts table (schema-only placeholder)
    op.create_table(
        "drift_forecasts",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "detection_id",
            sa.String(length=64),
            sa.ForeignKey("detections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "forecast_init_time",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("valid_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "geom_wgs84",
            geoalchemy2.types.Geometry(
                geometry_type="POINT", srid=4326, from_text="ST_GeomFromEWKT", name="geometry"
            ),
            nullable=True,
        ),
        sa.Column("method", sa.String(length=64), nullable=False, server_default="openberg"),
        sa.Column("uncertainty_radius_m", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("wind_speed_ms", sa.Float(), nullable=True),
        sa.Column("current_speed_ms", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("idx_drift_forecasts_detection_id", "drift_forecasts", ["detection_id"])
    op.create_index("idx_drift_forecasts_valid_time", "drift_forecasts", ["valid_time"])


def downgrade() -> None:
    op.drop_table("drift_forecasts")
    op.drop_table("validations")
    op.drop_table("detections")
    op.drop_table("scenes")
