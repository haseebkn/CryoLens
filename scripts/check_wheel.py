"""Smoke-test the built wheel independently of the repository working directory."""

import argparse
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    with tempfile.TemporaryDirectory(prefix="cryolens-wheel-check-") as scratch:
        target = Path(scratch)
        with zipfile.ZipFile(wheel) as archive:
            for member in archive.infolist():
                resolved = (target / member.filename).resolve()
                if not resolved.is_relative_to(target):
                    raise ValueError("Wheel member escapes temporary extraction directory")
            archive.extractall(target)
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(target)
        check = (
            "from cryolens.config.settings import get_project_config; "
            "from cryolens.geo.aoi import contains_point; "
            "from cryolens.api.main import app; "
            "from fastapi.testclient import TestClient; "
            'assert get_project_config().project.name == "CryoLens"; '
            "assert contains_point(-52, 48); "
            'assert TestClient(app).get("/").status_code == 200; '
            'print("Built wheel works outside repository cwd: config, AOI, dashboard.")'
        )
        subprocess.run([sys.executable, "-c", check], cwd=target, env=environment, check=True)


if __name__ == "__main__":
    main()
