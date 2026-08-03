#!/usr/bin/env python3
"""Build Serie A leaderboards. Thin wrapper over league_build.run;
the shared logic lives in scripts/league_build.py.

Targets the 2026-27 season; override with SERIEA_START / SERIEA_END (YYYYMMDD)
when a new season starts. Run with --gate to exit 0 only once the season's
first match kicked off a day ago — the workflow uses this to keep last season's
board frozen through the off-season and refresh straight into the new one.
"""

import os
import sys

import league_build

CONFIG = dict(
    slug="ita.1",
    start=os.environ.get("SERIEA_START", "20260701"),
    end=os.environ.get("SERIEA_END", "20270630"),
    out_name="seriea",
    tm_limit=int(os.environ.get("SERIEA_TM_LIMIT") or "26"),
)

if __name__ == "__main__":
    if "--gate" in sys.argv:
        started = league_build.season_started(
            CONFIG["slug"], CONFIG["start"], CONFIG["end"])
        sys.exit(0 if started else 1)
    if "--images-only" in sys.argv:
        league_build.refresh_images(
            out_name=CONFIG["out_name"], tm_limit=CONFIG["tm_limit"])
    else:
        league_build.run(**CONFIG)
