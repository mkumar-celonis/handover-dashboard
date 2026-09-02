from pathlib import Path
import json
import argparse

from scanner import scan_repo, to_markdown


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan a git repo for handover readiness")
    parser.add_argument("path", help="Path to a local git repository")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of markdown")
    parser.add_argument(
        "--window-days",
        type=int,
        default=90,
        help="Live-source lookback in days (7/14/30/90/180/365)",
    )
    args = parser.parse_args()
    data = scan_repo(Path(args.path), window_days=args.window_days)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(to_markdown(data))


if __name__ == "__main__":
    main()
