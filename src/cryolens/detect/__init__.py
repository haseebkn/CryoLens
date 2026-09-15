"""Detection module for CryoLens."""

from cryolens.detect.cfar import (
    BaseCFARDetector,
    CACFARDetector,
    CFARResult,
    GammaCFARDetector,
    get_cfar_detector,
)

__all__ = [
    "BaseCFARDetector",
    "CACFARDetector",
    "CFARResult",
    "GammaCFARDetector",
    "get_cfar_detector",
]
