"""Evaluate the published algorithm with gap filling explicitly disabled.

Run inside validation/Dockerfile.nersc. No production pipeline integration is
implied. C/D instruments require separately qualified coefficients.
"""

import argparse
import json
from pathlib import Path

import numpy as np


def main() -> None:
    from s1denoise import Sentinel1Image

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("output")
    args = parser.parse_args()
    if not Path(args.source).name.startswith(("S1A_", "S1B_")):
        raise ValueError("NERSC coefficient validation here covers S1A/S1B only")
    s1 = Sentinel1Image(args.source)
    actual = {
        float(v["version"])
        for v in s1.xml.manifest.find_all("safe:software")
        if v.get("name") == "Sentinel-1 IPF"
    }
    if len(actual) != 1:
        raise ValueError("Ambiguous named IPF version")
    read_version = s1.IPFversion
    s1.IPFversion = actual.pop()
    power = s1.remove_thermal_noise("HV", algorithm="NERSC", remove_negative=False)
    finite = np.isfinite(power)
    report = {
        "source": Path(args.source).name,
        "upstream_commit": "8259f0560b79c49177d865ba10d213c0ce25fe7c",
        "algorithm": "NERSC",
        "ipf_version": s1.IPFversion,
        "metadata_adapter": {
            "original_library_read_version": read_version,
            "actual_named_ipf_version": s1.IPFversion,
            "reason": "First software is COGifier, not IPF",
        },
        "negative_gap_filling_enabled": False,
        "angular_correction_applied": False,
        "finite_pixels": int(finite.sum()),
        "nonpositive_fraction": float(np.count_nonzero((power <= 0) & finite) / finite.sum())
        if finite.any()
        else 1.0,
    }
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    np.save(output / "nersc-hv.npy", power.astype(np.float32))
    (output / "nersc-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
