"""Small, explicit SQLite spatial emulation for portable API/integration tests.

This evaluates real Shapely predicates; it does not substitute for the separate
PostGIS migration and geometry checks in scripts/check_postgis.py.
"""

from typing import Any

import shapely
import shapely.geometry
from sqlalchemy import Engine, event


def register_spatial_functions(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def spatial_functions(connection: Any, record: Any) -> None:
        def intersects(left: str | None, right: str | None) -> int:
            if left is None or right is None:
                return 0
            return int(
                shapely.from_wkt(left.split(";")[-1]).intersects(
                    shapely.from_wkt(right.split(";")[-1])
                )
            )

        connection.create_function("ST_GeomFromText", 2, lambda wkt, srid: wkt)
        connection.create_function("ST_Intersects", 2, intersects)
        connection.create_function(
            "ST_MakeEnvelope", 5, lambda w, s, e, n, srid: shapely.geometry.box(w, s, e, n).wkt
        )
