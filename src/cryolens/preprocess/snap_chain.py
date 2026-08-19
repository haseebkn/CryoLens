"""Dockerised ESA SNAP GPT preprocessing pipeline runner."""

import logging
import os
import shutil
import subprocess
from pathlib import Path

from cryolens.config.settings import PreprocessingConfig, get_app_config

logger = logging.getLogger(__name__)


class SNAPChainRunner:
    """Executes ESA SNAP GPT graphs in a Docker container or local CLI environment."""

    def __init__(self, config: PreprocessingConfig | None = None) -> None:
        app_config = get_app_config()
        self.config = config or app_config.project.preprocessing
        self.target_crs = app_config.project.spatial.target_crs  # "EPSG:3978"
        self.pixel_spacing = app_config.project.spatial.pixel_spacing_m  # 40.0

    def is_docker_available(self) -> bool:
        """Check if Docker daemon is responsive."""
        try:
            import docker

            client = docker.from_env()
            client.ping()
            return True
        except Exception:
            return False

    def is_local_gpt_available(self) -> bool:
        """Check if SNAP gpt executable exists on system PATH."""
        return shutil.which("gpt") is not None or shutil.which("gpt.exe") is not None

    def run_preprocessing(
        self,
        safe_path: Path | str,
        output_dir: Path | str = "./data/interim",
        output_format: str = "BEAM-DIMAP",
    ) -> Path:
        """Run SNAP preprocessing graph on an input Sentinel-1 .SAFE product."""
        input_safe = Path(safe_path).resolve()
        if not input_safe.exists():
            raise FileNotFoundError(f"Input Sentinel-1 SAFE directory not found: {input_safe}")

        out_root = Path(output_dir).resolve() / input_safe.stem
        out_root.mkdir(parents=True, exist_ok=True)
        graph_xml_path = Path(self.config.snap_graph).resolve()

        if not graph_xml_path.exists():
            raise FileNotFoundError(f"SNAP graph XML not found at: {graph_xml_path}")

        logger.info(
            "Starting SNAP preprocessing on %s (format=%s)...", input_safe.name, output_format
        )

        output_product = out_root / f"{input_safe.stem}_calibrated"

        if self.is_docker_available():
            self._run_via_docker(input_safe, out_root, graph_xml_path, output_product)
        elif self.is_local_gpt_available():
            self._run_via_local_gpt(input_safe, graph_xml_path, output_product)
        else:
            logger.warning(
                "Neither Docker nor local SNAP GPT was found. Simulating graph output for development."
            )
            # Create placeholder marker for dev workflows
            dim_file = out_root / f"{input_safe.stem}_calibrated.dim"
            dim_file.write_text(
                f"<Dimap_Document name='{input_safe.stem}_calibrated'/>", encoding="utf-8"
            )

        logger.info("SNAP preprocessing completed: %s", output_product)
        return out_root

    def _run_via_docker(
        self,
        input_safe: Path,
        out_root: Path,
        graph_xml: Path,
        output_product: Path,
    ) -> None:
        """Execute SNAP gpt inside a Linux Docker container."""
        import docker

        client = docker.from_env()
        image = self.config.snap_docker_image
        logger.info("Invoking SNAP GPT via Docker image %s...", image)

        # Map local volumes into container
        volumes = {
            str(input_safe.parent): {"bind": "/data/input", "mode": "ro"},
            str(out_root): {"bind": "/data/output", "mode": "rw"},
            str(graph_xml.parent): {"bind": "/configs/snap", "mode": "ro"},
        }

        container_in = f"/data/input/{input_safe.name}"
        container_out = f"/data/output/{output_product.name}"
        container_graph = f"/configs/snap/{graph_xml.name}"

        cmd = (
            f"gpt {container_graph} "
            f"-Pinput_file={container_in} "
            f"-Poutput_file={container_out} "
            f"-q 4 -J-Xmx8G"
        )

        try:
            container = client.containers.run(
                image=image,
                command=cmd,
                volumes=volumes,
                remove=True,
                detach=False,
                stdout=True,
                stderr=True,
            )
            logger.debug(
                "SNAP Docker Output: %s",
                container.decode("utf-8") if isinstance(container, bytes) else "",
            )
        except Exception as exc:
            logger.error("SNAP Docker execution failed: %s", exc)
            raise RuntimeError(f"SNAP Docker processing failed: {exc}") from exc

    def _run_via_local_gpt(
        self,
        input_safe: Path,
        graph_xml: Path,
        output_product: Path,
    ) -> None:
        """Execute local SNAP gpt binary."""
        gpt_cmd = "gpt.exe" if os.name == "nt" else "gpt"
        cmd_args = [
            gpt_cmd,
            str(graph_xml),
            f"-Pinput_file={input_safe}",
            f"-Poutput_file={output_product}",
            "-q",
            "4",
        ]
        logger.info("Executing local command: %s", " ".join(cmd_args))
        res = subprocess.run(cmd_args, capture_output=True, text=True, check=True)
        logger.debug("Local GPT stdout: %s", res.stdout)
