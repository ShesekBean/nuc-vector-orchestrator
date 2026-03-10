"""Entry point for the NUC agent loop.

Usage: python3 -m apps.control_plane.agent_loop
"""

from __future__ import annotations


from .config import load_config
from .log import setup_logger
from .loop import AgentLoop


def main() -> None:
    cfg = load_config()
    setup_logger(cfg.log_file)
    loop = AgentLoop(cfg)
    loop.run()


if __name__ == "__main__":
    main()
