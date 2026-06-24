#!/usr/bin/env python3
"""Compatibility entrypoint for WorkshopAnalysis.

The implementation lives in the workshop_analysis_app package so workflows can be
reused from tests, launchers, and future integrations without growing this file.
"""

from workshop_analysis_app import *  # noqa: F401,F403
from workshop_analysis_app.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
