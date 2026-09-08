"""Compatibility entry point for the Phase 1 formal protocol."""

from experiments.phase1.formal.protocol import *  # noqa: F403
from experiments.phase1.formal.protocol import main


if __name__ == "__main__":
    raise SystemExit(main())
