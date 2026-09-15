"""Explain the unavailable drift capability with an unsuccessful CLI exit."""

import argparse

from cryolens.drift.model import FORECAST_UNAVAILABLE


def main() -> None:
    parser = argparse.ArgumentParser(description="Drift forecasting (unavailable).")
    parser.add_argument("--scene")
    parser.add_argument("--hours", type=float, default=72.0)
    parser.parse_args()
    parser.exit(2, FORECAST_UNAVAILABLE + "\n")


if __name__ == "__main__":
    main()
