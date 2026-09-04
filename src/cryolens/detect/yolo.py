"""YOLOv8 detector interface for 4-channel SAR input.

Status: **interface only — no trained model exists yet.**

This module deliberately does not return detections. An earlier revision
returned a hardcoded synthetic target so that the pipeline would "work" end to
end; that made the benchmark meaningless, because a detector that emits a fixed
point regardless of its input cannot be compared against CFAR on a false-alarm
curve. Fabricated detections are worse than an unimplemented detector, so the
fabrication was removed rather than improved.

What is required before this can be implemented, per the project plan's Phase 4:

* **Training labels.** The ship/iceberg discriminator is trained on the
  Statoil/C-CORE Kaggle dual-pol chips; detection training uses xView3-SAR.
  Both are behind terms acceptance, so both are operator-supplied.
* **Architecture changes for tiny objects.** At 40 m pixel spacing a 100 m
  iceberg is 2-3 pixels across and YOLOv8's finest stride is 8, so a P2
  detection head and 256 px tiles are needed; the stock configuration cannot
  resolve these targets.
* **Input adaptation.** The first convolution must accept the 4-band
  polarimetric stack rather than 3-channel RGB.
* **Augmentation retuning.** Default mosaic augmentation degrades sparse
  tiny-object detection and must be disabled or retuned.

Until then the operational detector is the CFAR baseline in
:mod:`cryolens.detect.cfar`, which is measured honestly in
:mod:`cryolens.eval.benchmark`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from cryolens.db.models import DetectionModel, SceneModel

logger = logging.getLogger(__name__)


class YoloSARDetector:
    """Placeholder interface for a future 4-channel YOLOv8 detector."""

    #: Set to True once trained weights and the adapted architecture exist.
    IMPLEMENTED = False

    def __init__(self, weights_path: Path | str | None = None) -> None:
        """Record the intended weights path without loading any model."""
        self.weights_path = Path(weights_path) if weights_path else None

    def detect(self, scene: SceneModel) -> list[DetectionModel]:
        """Raise, because no trained model exists.

        Raises:
            NotImplementedError: Always. See the module docstring for the
                prerequisites and docs/LIMITATIONS.md for the reasoning.
        """
        raise NotImplementedError(
            "YoloSARDetector has no trained weights and no adapted architecture. "
            "Training requires the xView3-SAR and Statoil/C-CORE datasets, which are "
            "behind terms acceptance and must be supplied by the operator. "
            "Use the CFAR detector (cryolens.detect.cfar) for the measured baseline."
        )
