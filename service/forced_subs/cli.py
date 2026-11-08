"""Run the pipeline on one file from the command line (handy for testing).

    python -m forced_subs.cli "D:\\Movies\\Some Movie (2020)\\Some Movie.mkv" --overwrite
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .pipeline import ModelHolder, Settings, SkipItem, generate_subtitles


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate forced and/or full subtitles for one video")
    ap.add_argument("video")
    ap.add_argument("--overwrite", action="store_true", help="Regenerate even if the subtitles already exist")
    ap.add_argument("--model", default=Settings.model)
    ap.add_argument("--device", default=Settings.device)
    ap.add_argument("--compute-type", default=Settings.compute_type)
    ap.add_argument("--option", "-o", action="append", default=[], metavar="KEY=VALUE",
                    help="Override any setting, e.g. -o skip_languages=es,fr -o language_prefix=true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    overrides = dict(o.split("=", 1) for o in args.option if "=" in o)
    settings = Settings(model=args.model, device=args.device,
                        compute_type=args.compute_type).with_options(overrides)
    last = [""]

    def progress(stage: str, fraction: float) -> None:
        line = f"{stage}: {fraction * 100:5.1f}%"
        if line != last[0]:
            print("\r" + line.ljust(40), end="", file=sys.stderr, flush=True)
            last[0] = line

    try:
        res = generate_subtitles(Path(args.video), ModelHolder(), settings,
                                        overwrite=args.overwrite, progress=progress)
    except SkipItem as e:
        print(f"\nSkipped: {e}")
        return 0
    print()
    print(json.dumps(res.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
