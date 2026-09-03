"""Training-chip extraction from real SAR scenes and validated detections.

Produces YOLO-format image/label pairs from analyst-validated detections so that
corrections made in the QGIS validation tool feed the next training run. Chips
are written as real multi-band GeoTIFFs cut from the source raster; an earlier
revision wrote zero-byte placeholder files, which would have silently produced a
training set of empty images.

Chip geometry follows the project plan's tiling configuration: 256 pixels rather
than 512, because at Sentinel-1 EW resolution a 512-pixel tile spans roughly
20 km and a 100 m iceberg occupies 2-3 pixels, which is below what a
stride-8 detector can resolve.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from sqlalchemy.orm import Session

from cryolens.db.models import ValidationModel

logger = logging.getLogger(__name__)

#: Analyst verdict to YOLO class index.
VERDICT_TO_CLASS: dict[str, int] = {
    "CONFIRMED_ICEBERG": 0,
    "VESSEL": 1,
    "OFFSHORE_STRUCTURE": 2,
    "SEA_ICE_FEATURE": 3,
    "REJECTED_CLUTTER": 4,
}

CLASS_NAMES = ["iceberg", "ship", "offshore_structure", "sea_ice_feature", "clutter"]


@dataclass
class ChipRecord:
    """One extracted training chip and its label."""

    image_path: Path
    label_path: Path
    class_id: int
    scene_id: str
    detection_id: int
    centre_row: int
    centre_col: int


class DatasetBuilder:
    """Extracts multi-band chips and YOLO labels from validated detections."""

    def __init__(
        self,
        output_dir: Path | str = "./data/processed/yolo_dataset",
        chip_size: int = 256,
    ) -> None:
        """Create the output tree.

        Args:
            output_dir: Root for ``images/`` and ``labels/``.
            chip_size: Square chip edge length in pixels.
        """
        self.output_dir = Path(output_dir)
        self.images_dir = self.output_dir / "images"
        self.labels_dir = self.output_dir / "labels"
        self.chip_size = chip_size

        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.labels_dir.mkdir(parents=True, exist_ok=True)

    def extract_chip(
        self,
        bands: dict[str, NDArray[np.floating]],
        centre_row: int,
        centre_col: int,
    ) -> tuple[dict[str, NDArray[np.floating]], tuple[int, int]] | None:
        """Cut a square chip around a pixel, returning None if it falls outside.

        Chips that would extend past the raster edge are rejected rather than
        zero-padded, because padded regions have no clutter statistics and would
        teach a detector that the scene border is informative.
        """
        half = self.chip_size // 2
        r0, r1 = centre_row - half, centre_row + half
        c0, c1 = centre_col - half, centre_col + half

        first = next(iter(bands.values()))
        h, w = first.shape
        if r0 < 0 or c0 < 0 or r1 > h or c1 > w:
            return None

        chip = {name: arr[r0:r1, c0:c1] for name, arr in bands.items()}
        return chip, (centre_row - r0, centre_col - c0)

    def write_chip(
        self,
        chip: dict[str, NDArray[np.floating]],
        image_path: Path,
    ) -> None:
        """Write a multi-band chip as a float32 GeoTIFF."""
        import rasterio

        names = list(chip.keys())
        stack = np.stack([np.asarray(chip[n], dtype=np.float32) for n in names])

        with rasterio.open(
            image_path,
            "w",
            driver="GTiff",
            height=stack.shape[1],
            width=stack.shape[2],
            count=stack.shape[0],
            dtype="float32",
            compress="deflate",
        ) as dst:
            dst.write(stack)
            for i, name in enumerate(names, start=1):
                dst.set_band_description(i, name)

    @staticmethod
    def write_label(
        label_path: Path,
        class_id: int,
        centre_xy: tuple[float, float],
        box_wh: tuple[float, float],
    ) -> None:
        """Write a single-object YOLO label file with normalised coordinates."""
        cx, cy = centre_xy
        bw, bh = box_wh
        label_path.write_text(
            f"{class_id} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}\n", encoding="utf-8"
        )

    def write_data_yaml(self) -> Path:
        """Write the Ultralytics dataset descriptor."""
        path = self.output_dir / "data.yaml"
        lines = [
            f"path: {self.output_dir.resolve().as_posix()}",
            "train: images",
            "val: images",
            f"nc: {len(CLASS_NAMES)}",
            "names:",
        ]
        lines.extend(f"  {i}: {name}" for i, name in enumerate(CLASS_NAMES))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def build_dataset(
        self,
        session: Session,
        band_loader: object | None = None,
    ) -> list[ChipRecord]:
        """Extract chips for every validated detection.

        Args:
            session: Database session holding validations.
            band_loader: Callable taking a scene id and returning a dict of
                band arrays. Required; without it there is no pixel source and
                the method refuses to write placeholder files.

        Raises:
            ValueError: If no ``band_loader`` is supplied.
        """
        if band_loader is None or not callable(band_loader):
            raise ValueError(
                "build_dataset requires a callable band_loader(scene_id) -> dict of "
                "band arrays. Refusing to emit placeholder chips."
            )

        validations = session.query(ValidationModel).all()
        if not validations:
            logger.warning("No validated detections in the database; nothing to extract.")
            return []

        records: list[ChipRecord] = []
        skipped_edge = 0
        band_cache: dict[str, dict[str, NDArray[np.floating]]] = {}

        for val in validations:
            detection = val.detection
            scene_id = str(detection.scene_id)

            if scene_id not in band_cache:
                band_cache[scene_id] = band_loader(scene_id)  # type: ignore[operator]
            bands = band_cache[scene_id]

            params = detection.detector_params or {}
            bbox = params.get("pixel_bbox")
            if not bbox or len(bbox) != 4:
                logger.debug("Detection %s lacks a pixel bbox; skipping.", detection.id)
                continue
            min_r, min_c, max_r, max_c = (int(v) for v in bbox)
            centre_row = (min_r + max_r) // 2
            centre_col = (min_c + max_c) // 2

            cut = self.extract_chip(bands, centre_row, centre_col)
            if cut is None:
                skipped_edge += 1
                continue
            chip, (local_r, local_c) = cut

            class_id = VERDICT_TO_CLASS.get(str(val.analyst_verdict), VERDICT_TO_CLASS["REJECTED_CLUTTER"])

            stem = f"{scene_id}_{detection.id}"
            image_path = self.images_dir / f"{stem}.tif"
            label_path = self.labels_dir / f"{stem}.txt"

            self.write_chip(chip, image_path)
            self.write_label(
                label_path,
                class_id,
                (local_c / self.chip_size, local_r / self.chip_size),
                (
                    max(max_c - min_c, 1) / self.chip_size,
                    max(max_r - min_r, 1) / self.chip_size,
                ),
            )

            records.append(
                ChipRecord(
                    image_path=image_path,
                    label_path=label_path,
                    class_id=class_id,
                    scene_id=scene_id,
                    detection_id=int(detection.id),
                    centre_row=centre_row,
                    centre_col=centre_col,
                )
            )

        self.write_data_yaml()

        counts: dict[str, int] = {}
        for rec in records:
            counts[CLASS_NAMES[rec.class_id]] = counts.get(CLASS_NAMES[rec.class_id], 0) + 1
        logger.info(
            "Extracted %d chips (%s); %d skipped at the raster edge.",
            len(records),
            ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none",
            skipped_edge,
        )
        return records
