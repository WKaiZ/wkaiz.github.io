#!/usr/bin/env python3
"""Refresh 2026 anime catalog metadata from AniList.

Preserves personal fields (status, score, notes) for existing series ids.
New series are added as plan_to_watch with blank scores.

Series rule: Season 1 / Season 2 / Part N of the same show collapse to one entry.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "assets" / "anime" / "list.json"
SEASON_YEAR = 2026
CHINESE_TITLES_URL = (
    "https://raw.githubusercontent.com/soruly/anilist-chinese/master/anilist-chinese.json"
)

PERSONAL_KEYS = ("status", "score", "notes")

SEASON_RE = re.compile(
    r"""(?:
        \s*[:\-–—]?\s*Season\s*[0-9０-９]+.*$|
        \s+\d+(?:st|nd|rd|th)\s+Season.*$|
        \s*[:\-–—]?\s*Part\s*[0-9０-９]+.*$|
        \s*[:\-–—]?\s*Cour\s*[0-9０-９]+.*$|
        \s*[:\-–—]?\s*(?:The\s+)?(?:Final|Last)\s+Season.*$|
        \s+S\d+\b.*$|
        \s*[:\-–—]?\s*Second Year.*$|
        \s*[:\-–—]?\s*\d+-nensei.*$
    )""",
    re.I | re.X,
)

# Japanese native titles often use 第N期 / SeasonＮ / N年生編…
NATIVE_SEASON_RE = re.compile(
    r"""(?:
        \s*Season\s*[0-9０-９]+.*$|
        \s*第[0-9０-９一二三四五六七八九十百]+期.*$|
        \s*\d+(?:st|nd|rd|th)\s+[Ss]eason.*$|
        \s+[0-9０-９]+年生編.*$|
        \s*Part\s*[0-9０-９]+.*$|
        \s*[最终最終][季章].*$
    )""",
    re.I | re.X,
)

CN_SEASON_RE = re.compile(
    r"""(?:
        \s*第[0-9一二三四五六七八九十百零〇两兩]+[季期部部曲].*$|
        \s*Season\s*[0-9０-９]+.*$|
        \s*[最终最終][季章].*$|
        \s*[前後前后]篇.*$|
        \s*續篇.*$|
        \s*続編.*$|
        \s*～[^～\n]*～\s*$|
        \s+[0-9０-９]+$
    )""",
    re.I | re.X,
)


def http_gql(query: str, variables: dict) -> dict:
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        "https://graphql.anilist.co",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "wkaiz.github.io-anime-log/1.0",
        },
        method="POST",
    )
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503):
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    raise RuntimeError("AniList request failed after retries")


def prefer_title(title: dict | None) -> str:
    if not title:
        return "Unknown"
    return (title.get("english") or title.get("romaji") or title.get("native") or "Unknown").strip()


def series_base(title: str) -> str:
    t = title.strip()
    prev = None
    while prev != t:
        prev = t
        t = SEASON_RE.sub("", t).strip()
        t = re.sub(r"\s+", " ", t).strip(" -–—:")
        t = re.sub(r"(\D)\d+$", r"\1", t).strip()
    return t


def chinese_base(title: str) -> str:
    t = title.strip()
    prev = None
    while prev != t:
        prev = t
        t = CN_SEASON_RE.sub("", t).strip()
        t = re.sub(r"\s+", " ", t).strip(" -–—:·・")
    return t


def native_base(title: str) -> str:
    t = title.strip()
    prev = None
    while prev != t:
        prev = t
        t = NATIVE_SEASON_RE.sub("", t).strip()
        t = SEASON_RE.sub("", t).strip()
        t = re.sub(r"\s+", " ", t).strip(" -–—:·・")
    return t


def fetch_chinese_titles() -> dict[int, dict]:
    """Map AniList id → {title, synonyms} from anilist-chinese."""
    req = urllib.request.Request(
        CHINESE_TITLES_URL,
        headers={"User-Agent": "wkaiz.github.io-anime-log/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        rows = json.loads(resp.read().decode())
    out: dict[int, dict] = {}
    for row in rows:
        aid = row.get("id")
        title = (row.get("title") or "").strip()
        if not aid or not title:
            continue
        out[int(aid)] = {
            "title": title,
            "synonyms": [s.strip() for s in (row.get("synonyms") or []) if s and str(s).strip()],
        }
    return out


def has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def pick_chinese(members: list[dict], chinese_map: dict[int, dict]) -> str | None:
    candidates: list[str] = []
    for m in members:
        row = chinese_map.get(m["id"])
        if not row:
            continue
        for raw in [row["title"], *row["synonyms"]]:
            if not raw or not has_cjk(raw):
                continue
            candidates.append(chinese_base(raw) or raw)
    if not candidates:
        return None
    # Prefer shortest cleaned title (usually the series name without cour/arc).
    candidates.sort(key=lambda s: (len(s), s))
    return candidates[0]


def series_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", series_base(title).lower())


def fetch_season_media(year: int) -> list[dict]:
    query = """
    query ($page: Int, $seasonYear: Int) {
      Page(page: $page, perPage: 50) {
        pageInfo { hasNextPage }
        media(
          seasonYear: $seasonYear
          type: ANIME
          format_in: [TV, TV_SHORT]
          sort: POPULARITY_DESC
        ) {
          id
          idMal
          episodes
          season
          seasonYear
          title { romaji english native }
          coverImage { large }
        }
      }
    }
    """
    media: list[dict] = []
    page = 1
    # AniList sometimes reports hasNextPage=false early; keep going until an empty page.
    while page <= 20:
        data = http_gql(query, {"page": page, "seasonYear": year})
        batch = data["data"]["Page"]["media"]
        if not batch:
            break
        media.extend(batch)
        page += 1
        time.sleep(0.4)
    return media


def consolidate(media: list[dict], chinese_map: dict[int, dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in media:
        title = prefer_title(item.get("title"))
        key = series_key(title)
        if key:
            groups[key].append(item)

    entries = []
    for _, members in groups.items():
        members_sorted = sorted(
            members,
            key=lambda x: (
                x.get("seasonYear") or 0,
                {"WINTER": 1, "SPRING": 2, "SUMMER": 3, "FALL": 4}.get(
                    x.get("season") or "", 0
                ),
                x["id"],
            ),
        )
        bases = [(series_base(prefer_title(m.get("title"))), m) for m in members_sorted]
        bases.sort(key=lambda pair: (len(pair[0]), pair[0]))
        display = bases[0][0] or prefer_title(members_sorted[0].get("title"))
        latest = members_sorted[-1]

        native_raw = next(
            (
                (m.get("title") or {}).get("native")
                for m in members_sorted
                if (m.get("title") or {}).get("native")
            ),
            None,
        )
        native = native_base(native_raw) if native_raw else None
        chinese = pick_chinese(members_sorted, chinese_map)
        mal = next((m.get("idMal") for m in members_sorted if m.get("idMal")), None)
        aid = members_sorted[0]["id"]
        # Series-level record only — no cour / broadcast season metadata.
        entries.append(
            {
                "id": f"anilist-{aid}",
                "anilist_id": aid,
                "mal_id": mal,
                "title": display,
                "title_native": native,
                "title_chinese": chinese,
                "cover": (latest.get("coverImage") or {}).get("large"),
                "url": f"https://anilist.co/anime/{aid}",
                "mal_url": f"https://myanimelist.net/anime/{mal}" if mal else None,
                "status": "plan_to_watch",
                "score": None,
                "notes": "",
            }
        )

    entries.sort(key=lambda e: (e.get("title") or "").lower())
    return entries


def load_existing() -> dict[str, dict]:
    if not OUT.exists():
        return {}
    data = json.loads(OUT.read_text())
    items = data if isinstance(data, list) else data.get("series") or []
    return {item["id"]: item for item in items if item.get("id")}


def merge_personal(catalog: list[dict], existing: dict[str, dict]) -> list[dict]:
    # Match by id first, then by normalized title.
    by_title = {
        series_key(item.get("title") or ""): item for item in existing.values()
    }
    merged = []
    for item in catalog:
        prev = existing.get(item["id"]) or by_title.get(series_key(item["title"]))
        if prev:
            for key in PERSONAL_KEYS:
                if key in prev:
                    item[key] = prev[key]
            # Keep a stable id if the user already has progress under an older id.
            if prev.get("id"):
                item["id"] = prev["id"]
        merged.append(item)
    return merged


def main() -> int:
    print(f"Fetching TV anime for {SEASON_YEAR} from AniList…")
    media = fetch_season_media(SEASON_YEAR)
    print(f"Got {len(media)} seasonal entries")
    print("Fetching Chinese titles (anilist-chinese)…")
    chinese_map = fetch_chinese_titles()
    print(f"Loaded {len(chinese_map)} Chinese title rows")
    catalog = consolidate(media, chinese_map)
    with_cn = sum(1 for e in catalog if e.get("title_chinese"))
    print(f"Consolidated to {len(catalog)} series ({with_cn} with Chinese titles)")
    merged = merge_personal(catalog, load_existing())

    payload = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "year": SEASON_YEAR,
        "series": merged,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(f"Wrote {len(merged)} series → {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
