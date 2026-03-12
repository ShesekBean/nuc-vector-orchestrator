"""Entry point for ``python3 -m apps.vector``.

Parses CLI arguments and launches the VectorSupervisor.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vector process supervisor")
    parser.add_argument(
        "--serial",
        type=str,
        default=os.environ.get("VECTOR_SERIAL", "0dd1cdcf"),
        help="Vector serial number",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    from apps.vector.supervisor import VectorSupervisor

    supervisor = VectorSupervisor(serial=args.serial)
    supervisor.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
