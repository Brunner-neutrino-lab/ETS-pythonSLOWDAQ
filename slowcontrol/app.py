"""ETS Slow Control — entry point.

Usage::

    # Run with GUI (default)
    python -m slowcontrol.app
    python -m slowcontrol.app gui

    # Run as headless service (e.g. on the Pi)
    python -m slowcontrol.app service

    # Specify config file
    python -m slowcontrol.app -c /path/to/config.yaml
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ETS Cryostat Slow Control System"
    )
    parser.add_argument(
        "mode",
        nargs="?",
        default="gui",
        choices=["gui", "service"],
        help="Run mode: 'gui' for PyQt GUI, 'service' for headless",
    )
    parser.add_argument(
        "-c",
        "--config",
        default="config.yaml",
        help="Path to YAML config file (default: config.yaml)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config_path = Path(args.config)
    if not config_path.exists():
        # Fall back to config.yaml next to this package
        config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    if not config_path.exists():
        print(f"Config file not found: {args.config}", file=sys.stderr)
        sys.exit(1)

    if args.mode == "service":
        _run_service(config_path)
    else:
        _run_gui(config_path)


def _run_service(config_path: Path) -> None:
    from slowcontrol.core.service import SlowControlService

    service = SlowControlService(config_path)
    service.run_forever()


def _run_gui(config_path: Path) -> None:
    from slowcontrol.gui.main_window import run_gui

    run_gui(config_path)


if __name__ == "__main__":
    main()
