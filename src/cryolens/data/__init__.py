"""Labelled dataset loaders and assembly utilities.

This package holds readers that turn third-party benchmark datasets into the
common internal representation used by CryoLens detection and evaluation code.
"""

from cryolens.data.ai4arctic import (
    AI4ArcticScene,
    SceneExtent,
    build_scene_index,
    load_scene,
    scenes_intersecting_aoi,
)

__all__ = [
    "AI4ArcticScene",
    "SceneExtent",
    "build_scene_index",
    "load_scene",
    "scenes_intersecting_aoi",
]
