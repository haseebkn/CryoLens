"""CLI Entrypoint for running OpenDrift iceberg forecasts on a scene's detections."""

import argparse
import logging
import sys

from cryolens.db.repositories import DetectionRepository, DriftForecastRepository
from cryolens.db.session import get_db_session_factory
from cryolens.drift.model import IcebergDriftRunner

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run drift forecasts for detections in a scene.")
    parser.add_argument("--scene", required=True, help="Scene ID to process")
    parser.add_argument("--hours", type=float, default=72.0, help="Forecast duration in hours")
    args = parser.parse_args()

    session_factory = get_db_session_factory()
    session = session_factory()

    det_repo = DetectionRepository()
    drift_repo = DriftForecastRepository()
    runner = IcebergDriftRunner()

    logger.info(f"Fetching validated iceberg detections for scene {args.scene}")

    detections = det_repo.list_detections(
        session=session, scene_id=args.scene, predicted_class="iceberg"
    )

    # In a real operational scenario, we'd only drift validated ones,
    # but for bulk testing, we'll run on all predicted icebergs.

    if not detections:
        logger.info(f"No iceberg detections found for scene {args.scene}.")
        sys.exit(0)

    logger.info(f"Found {len(detections)} icebergs. Running forecasts...")

    for det in detections:
        try:
            trajectory = runner.run_forecast(det, hours=args.hours)
            if trajectory:
                drift_repo.save_trajectory(session, det.id, trajectory)
                session.commit()
                logger.info(f"Saved {len(trajectory)} waypoints for detection {det.id}")
            else:
                logger.warning(f"Failed to generate trajectory for detection {det.id}")
        except Exception as e:
            session.rollback()
            logger.error(f"Error forecasting detection {det.id}: {e}")

    session.close()
    logger.info("Drift forecasting complete.")


if __name__ == "__main__":
    main()
