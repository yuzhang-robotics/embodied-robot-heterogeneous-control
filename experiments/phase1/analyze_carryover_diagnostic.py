"""Compatibility entry point for the published carryover analysis command."""

from experiments.phase1.carryover.analysis import *  # noqa: F403
from experiments.phase1.carryover.analysis import main


if __name__ == "__main__":
    raise SystemExit(main())
