#!/usr/bin/env python3
"""Local launcher for the Video Downloader Tool web UI."""

from __future__ import annotations

import os
import sys

current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

if hasattr(sys, "_MEIPASS") and sys._MEIPASS not in sys.path:  # type: ignore[attr-defined]
    sys.path.insert(0, sys._MEIPASS)  # type: ignore[attr-defined]

from web_ui.server import main as web_main


def main(argv: list[str] | None = None) -> int:
    return web_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
