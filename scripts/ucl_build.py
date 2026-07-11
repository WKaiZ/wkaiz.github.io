#!/usr/bin/env python3
"""Build UEFA Champions League leaderboards. Thin wrapper over league_build.run;
the shared logic lives in scripts/league_build.py.

Targets the 2026-27 season; override with UCL_START / UCL_END (YYYYMMDD)
when a new season starts. Run with --gate to exit 0 only once the season's
first match kicked off a day ago — the workflow uses this to keep last season's
board frozen through the off-season and refresh straight into the new one.
"""

import os
import sys

import league_build

CONFIG = dict(
    slug="uefa.champions",
    start=os.environ.get("UCL_START", "20260901"),
    end=os.environ.get("UCL_END", "20270701"),
    out_name="ucl",
    tm_limit=int(os.environ.get("UCL_TM_LIMIT", "26")),
)

if __name__ == "__main__":
    if "--gate" in sys.argv:
        started = league_build.season_started(
            CONFIG["slug"], CONFIG["start"], CONFIG["end"])
        sys.exit(0 if started else 1)
    league_build.run(**CONFIG)
