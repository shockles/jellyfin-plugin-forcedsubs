"""Run the pipeline on one file from the command line (handy for testing).

    python -m forced_subs.cli "D:\\Movies\\Some Movie (2020)\\Some Movie.mkv" --overwrite
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .pipeline import ModelHolder, Settings, SkipItem, generate_forced_subtitles


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate forced English subtitles for one video")
    ap.add_argument("video")
    ap.add_argument("--overwrite", action="store_true", help="Ignore existing forced subtitles")
    ap.add_argument("--model", default=Settings.model)
    ap.add_argument("--device", default=Settings.device)
    ap.add_argument("--compute-type", default=Settings.compute_type)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    settings = Settings(model=args.model, device=args.device, compute_type=args.compute_type)
    last = [""]

    def progress(stage: str, fraction: float) -> None:
        line = f"{stage}: {fraction * 100:5.1f}%"
        if line != last[0]:
            print("\r" + line.ljust(40), end="", file=sys.stderr, flush=True)
            last[0] = line

    try:
        res = generate_forced_subtitles(Path(args.video), ModelHolder(settings), settings,
                                        overwrite=args.overwrite, progress=progress)
    except SkipItem as e:
        print(f"\nSkipped: {e}")
        return 0
    print()
    print(json.dumps(res.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
