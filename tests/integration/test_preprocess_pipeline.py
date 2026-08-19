"""End-to-end integration test for SAR radiometric preprocessing pipeline."""

from pathlib import Path

import numpy as np
import rasterio
from rio_cogeo.cogeo import cog_validate

from cryolens.preprocess.python_chain import PurePythonSARProcessor
from cryolens.preprocess.stack import COGStackBuilder


def test_full_preprocessing_pipeline_end_to_end(tmp_path: Path) -> None:
    """Run full calibration, denoising, geocoding, and COG export pipeline."""
    scene_id = "S1B_EW_GRDM_1SDH_20200515T094821_INTEGRATION_TEST"
    output_dir = tmp_path / "processed"

    processor = PurePythonSARProcessor(target_crs="EPSG:3978", pixel_spacing_m=40.0)
    stack_builder = COGStackBuilder(output_dir=output_dir)

    # 1. Simulate Sentinel-1 EW GRD measurements over the Grand Banks
    h, w = 256, 256
    # Calm ocean background: HH ~ -18 dB (linear ~ 0.015), HV ~ -32 dB (linear ~ 0.00063)
    np.random.seed(123)
    ocean_hh_linear = np.random.exponential(scale=0.015, size=(h, w)).astype(np.float32)
    ocean_hv_linear = np.random.exponential(scale=0.0006, size=(h, w)).astype(np.float32)

    # Add NESZ noise scalloping across range swath
    x = np.linspace(-1, 1, w)
    nesz_pattern = 10 ** ((-28.0 + 3.0 * (x**2)) / 10.0)
    raw_hv_linear = ocean_hv_linear + nesz_pattern

    # Add an iceberg point target (RCS contrast)
    ocean_hh_linear[100:103, 120:123] = 0.50  # -3 dB
    raw_hv_linear[100:103, 120:123] = 0.12  # -9 dB

    # Convert linear power to raw DN using calibration constant K=100
    hh_dn = np.sqrt(ocean_hh_linear * (100.0**2))
    hv_dn = np.sqrt(raw_hv_linear * (100.0**2))

    # Grand Banks bounding coordinates (WGS84)
    bounds = (-54.5, 47.5, -53.0, 48.8)

    # 2. Execute preprocessing chain
    result = processor.process_scene_arrays(
        hh_dn=hh_dn,
        hv_dn=hv_dn,
        source_bounds=bounds,
        source_crs="EPSG:4326",
        calibration_lut_hh=100.0,
        calibration_lut_hv=100.0,
        apply_denoise=True,
    )

    assert result["crs"] == "EPSG:3978"
    assert "sigma0_hh_db" in result["bands"]
    assert "sigma0_hv_db" in result["bands"]

    # 3. Export to Cloud Optimized GeoTIFF
    cog_path = stack_builder.build_and_export_cog(
        scene_id=scene_id,
        bands=result["bands"],
        transform=result["transform"],
        crs=result["crs"],
    )

    assert cog_path.exists()

    # 4. Verify COG validity with rio-cogeo
    is_valid, errors, warnings = cog_validate(str(cog_path))
    assert is_valid is True, f"COG validation failed: {errors}"

    # 5. Physical assertions on calibrated output raster
    with rasterio.open(cog_path) as src:
        assert src.count == 4
        assert src.crs.to_string() == "EPSG:3978"

        hh_db_out = src.read(1)
        hv_db_out = src.read(2)
        ratio_out = src.read(3)
        inc_out = src.read(4)

        valid_mask = (hh_db_out > -100.0) & (hv_db_out > -100.0)
        assert np.any(valid_mask)

        hh_valid = hh_db_out[valid_mask]
        hv_valid = hv_db_out[valid_mask]
        ratio_valid = ratio_out[valid_mask]
        inc_valid = inc_out[valid_mask]

        assert hh_db_out.shape == hv_db_out.shape
        assert float(np.median(hh_valid)) > float(np.median(hv_valid))

        # Open ocean HV backscatter should sit well below -25 dB
        ocean_hv_median = float(np.median(hv_valid))
        assert ocean_hv_median < -25.0, f"Expected calm ocean HV < -25 dB, got {ocean_hv_median} dB"

        # Check incidence angle range over valid pixels
        assert np.all((inc_valid >= 18.0) & (inc_valid <= 48.0))

        # Check polarimetric ratio (HH/HV in dB is positive over open ocean)
        assert np.median(ratio_valid) > 0.0
