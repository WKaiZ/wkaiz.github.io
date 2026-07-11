#!/usr/bin/env python3
"""Build Premier League leaderboards. Thin wrapper over league_build.run;
the shared logic lives in scripts/league_build.py.

Season window is the 2025-26 season; override with EPL_START / EPL_END
(YYYYMMDD) when a new season starts.
"""

import os

import league_build

if __name__ == "__main__":
    league_build.run(
        slug="eng.1",
        start=os.environ.get("EPL_START", "20250801"),
        end=os.environ.get("EPL_END", "20260610"),
        out_name="epl",
        tm_limit=int(os.environ.get("EPL_TM_LIMIT", "26")),
    )
