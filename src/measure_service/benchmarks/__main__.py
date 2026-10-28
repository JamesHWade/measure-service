"""Explicit download and offline replay commands for named reference data."""

import argparse
import json
from pathlib import Path

from ..moses_score_qualification import qualify_moses_scores
from .moses_archive import fetch_archive, inspect_archive
from .moses_replay import analyze_archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch-moses", help="download and verify the authors' 1.49 GB ZIP")
    fetch.add_argument("--cache-dir", type=Path, required=True)
    inspect = commands.add_parser("inspect-moses", help="verify the local archive and list members")
    inspect.add_argument("--archive", type=Path, required=True)
    inspect.add_argument("--output", type=Path, required=True)
    replay = commands.add_parser(
        "replay-moses", help="recompute archived metrics and score trajectories"
    )
    replay.add_argument("--archive", type=Path, required=True)
    replay.add_argument("--output-dir", type=Path, required=True)
    qualify = commands.add_parser(
        "qualify-moses-scores", help="compare explicit native score profiles with archived scores"
    )
    qualify.add_argument("--archive", type=Path, required=True)
    qualify.add_argument("--replay-dir", type=Path, required=True)
    qualify.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "fetch-moses":
        print(fetch_archive(args.cache_dir))
    elif args.command == "inspect-moses":
        manifest = inspect_archive(args.archive)
        with args.output.open("x", encoding="utf-8") as output:
            output.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        print(args.output)
    elif args.command == "replay-moses":
        result = analyze_archive(args.archive, args.output_dir)
        print(
            json.dumps(
                {
                    key: result[key]
                    for key in (
                        "profile",
                        "configuration_count",
                        "campaign_count",
                        "observation_count",
                    )
                },
                indent=2,
            )
        )

    else:
        qualify_moses_scores(args.archive, args.replay_dir, args.output_dir)
        print(args.output_dir / "report.md")


if __name__ == "__main__":
    main()
