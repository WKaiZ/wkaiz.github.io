#!/usr/bin/env python3
"""Build Serie A leaderboards. Thin wrapper over league_build.run;
the shared logic lives in scripts/league_build.py.

Season window is the 2025-26 season; override with SERIEA_START / SERIEA_END
(YYYYMMDD) when a new season starts.
"""

import os

import league_build

if __name__ == "__main__":
    league_build.run(
        slug="ita.1",
        start=os.environ.get("SERIEA_START", "20250801"),
        end=os.environ.get("SERIEA_END", "20260610"),
        out_name="seriea",
        tm_limit=int(os.environ.get("SERIEA_TM_LIMIT", "26")),
    )
