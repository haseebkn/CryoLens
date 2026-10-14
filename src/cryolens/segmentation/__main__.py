"""Development-only preparation and bounded research comparison."""

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["prepare", "train", "lifecycle"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if args.stage == "prepare":
        from cryolens.segmentation.data import prepare

        prepare(config, args.project, args.output)
    elif args.stage == "train":
        from cryolens.segmentation.train import run

        if args.dataset is None:
            parser.error("--dataset is required")
        run(config, args.project, args.dataset, args.output)
    else:
        from cryolens.segmentation.lifecycle import demonstrate

        if args.dataset is None:
            parser.error("--dataset must name the completed training output")
        demonstrate(args.dataset, args.output)


if __name__ == "__main__":
    main()
