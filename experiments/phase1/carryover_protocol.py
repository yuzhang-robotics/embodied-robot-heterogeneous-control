"""Compatibility entry point for the ASR/VLM carryover protocol."""

from experiments.phase1.carryover.protocol import *  # noqa: F403
from experiments.phase1.carryover.protocol import main


if __name__ == "__main__":
    raise SystemExit(main())
