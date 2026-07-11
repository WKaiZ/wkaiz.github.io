#!/usr/bin/env python3
"""Build UEFA Europa League leaderboards. Thin wrapper over league_build.run;
the shared logic lives in scripts/league_build.py.

Season window is the 2025-26 competition; override with EUROPA_START /
EUROPA_END (YYYYMMDD) when a new season starts.
"""

import os

import league_build

if __name__ == "__main__":
    league_build.run(
        slug="uefa.europa",
        start=os.environ.get("EUROPA_START", "20250916"),
        end=os.environ.get("EUROPA_END", "20260603"),
        out_name="europa",
        tm_limit=int(os.environ.get("EUROPA_TM_LIMIT", "26")),
    )
