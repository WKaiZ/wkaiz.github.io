#!/usr/bin/env python3
"""Build Ligue 1 leaderboards. Thin wrapper over league_build.run;
the shared logic lives in scripts/league_build.py.

Season window is the 2025-26 season; override with LIGUE1_START / LIGUE1_END
(YYYYMMDD) when a new season starts.
"""

import os

import league_build

if __name__ == "__main__":
    league_build.run(
        slug="fra.1",
        start=os.environ.get("LIGUE1_START", "20250801"),
        end=os.environ.get("LIGUE1_END", "20260610"),
        out_name="ligue1",
        tm_limit=int(os.environ.get("LIGUE1_TM_LIMIT", "26")),
    )
