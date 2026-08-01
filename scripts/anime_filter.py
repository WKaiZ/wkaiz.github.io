#!/usr/bin/env python3
"""Keep only series whose Chinese title appears in the watchlist txt.

Edit assets/anime/watchlist-zh.txt (one Chinese title per line), then run:

  python3 scripts/anime_filter.py

Series without a Chinese title, or whose Chinese title is not on the list,
are removed from assets/anime/list.json (and docs/assets/anime/list.json).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIST = REPO / "assets" / "anime" / "list.json"
DOCS_LIST = REPO / "docs" / "assets" / "anime" / "list.json"
WATCHLIST = REPO / "assets" / "anime" / "watchlist-zh.txt"


def load_watchlist(path: Path) -> set[str]:
    if not path.exists():
        raise SystemExit(f"Missing watchlist: {path.relative_to(REPO)}")
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        names.add(name)
    return names


def main() -> int:
    allowed = load_watchlist(WATCHLIST)
    data = json.loads(LIST.read_text(encoding="utf-8"))
    series = data.get("series") or []
    before = len(series)

    kept = []
    dropped_no_cn = 0
    dropped_not_listed = 0
    for item in series:
        cn = (item.get("title_chinese") or "").strip()
        if not cn:
            dropped_no_cn += 1
            continue
        if cn not in allowed:
            dropped_not_listed += 1
            continue
        kept.append(item)

    data["series"] = kept
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    LIST.write_text(text, encoding="utf-8")
    DOCS_LIST.parent.mkdir(parents=True, exist_ok=True)
    DOCS_LIST.write_text(text, encoding="utf-8")

    print(f"Watchlist: {len(allowed)} Chinese titles")
    print(f"Kept {len(kept)} / {before} series")
    if dropped_no_cn:
        print(f"  dropped (no Chinese title): {dropped_no_cn}")
    if dropped_not_listed:
        print(f"  dropped (not in watchlist): {dropped_not_listed}")
    print(f"Wrote {LIST.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
