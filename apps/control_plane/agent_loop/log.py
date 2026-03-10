"""Logging for the agent loop."""

from __future__ import annotations

import logging
import sys
from pathlib import Path


def setup_logger(log_file: Path) -> logging.Logger:
    """Set up logger that writes to both file and stdout."""
    logger = logging.getLogger("agent-loop")
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(log_file, mode="a")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger
