"""Validate one completed Phase 1 Jetson simulation pilot session."""

from __future__ import annotations

import argparse
from pathlib import Path

from experiments.phase1.pilot import validate_pilot_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_dir", type=Path)
    args = parser.parse_args(argv)
    errors = validate_pilot_dir(args.session_dir)
    if errors:
        print("INVALID")
        for error in errors:
            print(f"- {error}")
        return 1
    print("VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
