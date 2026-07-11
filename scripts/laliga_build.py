#!/usr/bin/env python3
"""Build La Liga leaderboards. Thin wrapper over league_build.run;
the shared logic lives in scripts/league_build.py.

Season window is the 2025-26 season; override with LALIGA_START / LALIGA_END
(YYYYMMDD) when a new season starts.
"""

import os

import league_build

if __name__ == "__main__":
    league_build.run(
        slug="esp.1",
        start=os.environ.get("LALIGA_START", "20250801"),
        end=os.environ.get("LALIGA_END", "20260610"),
        out_name="laliga",
        tm_limit=int(os.environ.get("LALIGA_TM_LIMIT", "26")),
    )
