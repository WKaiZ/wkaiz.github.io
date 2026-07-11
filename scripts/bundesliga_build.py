#!/usr/bin/env python3
"""Build Bundesliga leaderboards. Thin wrapper over league_build.run;
the shared logic lives in scripts/league_build.py.

Season window is the 2025-26 season; override with BUNDESLIGA_START / BUNDESLIGA_END
(YYYYMMDD) when a new season starts.
"""

import os

import league_build

if __name__ == "__main__":
    league_build.run(
        slug="ger.1",
        start=os.environ.get("BUNDESLIGA_START", "20250801"),
        end=os.environ.get("BUNDESLIGA_END", "20260610"),
        out_name="bundesliga",
        tm_limit=int(os.environ.get("BUNDESLIGA_TM_LIMIT", "26")),
    )
