#!/usr/bin/env python3
"""Build WC26 player leaderboards (appearances / goals / assists).

Data source: ESPN's public site API. Completed matches are immutable, so each
match summary is fetched exactly once and cached in match_cache.json — a daily
run costs one scoreboard request plus one request per newly finished match.
"""

import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world"
SCOREBOARD = BASE + "/scoreboard?dates={start}-{end}&limit=300"
SUMMARY = BASE + "/summary?event={id}"
WC_START, WC_END = "20260611", "20260719"
HEADSHOT = "https://a.espncdn.com/i/headshots/soccer/players/full/{id}.png"

# Player photos come from Transfermarkt, like the eFootball page: reuse the
# eFootball image cache and pes.db name->tm_id mapping for free, and spend at
# most WC26_TM_LIMIT Transfermarkt requests per day on the rest.
PES_DB_URL = "https://raw.githubusercontent.com/WKaiZ/efootball/main/pes.db"
TM_SEARCH = "https://www.transfermarkt.com/schnellsuche/ergebnis/schnellsuche?query={q}"
TM_PROFILE = "https://www.transfermarkt.us/x/profil/spieler/{id}"
TM_IMG = "https://img.a.transfermarkt.technology/portrait/big/{id}-{ts}.{ext}"
TM_DAILY_LIMIT = int(os.environ.get("WC26_TM_LIMIT", "26"))
TM_NULL_LIMIT, TM_ERROR_LIMIT, SEARCH_MISS_LIMIT = 3, 5, 3
TEAM2DB = {"Congo DR": "congo", "Ivory Coast": "ivory-coast",
           "South Korea": "korea", "Türkiye": "turkey", "United States": "usa"}

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_DIR = os.path.join(ROOT, "assets", "wc26")
STATS_PATH = os.path.join(OUT_DIR, "stats.json")
CACHE_PATH = os.path.join(OUT_DIR, "match_cache.json")
IMG_CACHE_PATH = os.path.join(OUT_DIR, "img_cache.json")
EF_IMG_CACHE = os.path.join(ROOT, "assets", "efootball", "img_cache.json")
EF_OVERRIDES = os.path.join(ROOT, "assets", "efootball", "overrides.json")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
TM_HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def tm_get(url):
    req = urllib.request.Request(url, headers=TM_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def pes_index():
    """team slug -> {normalized player name -> transfermarkt id} from pes.db."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        urllib.request.urlretrieve(PES_DB_URL, path)
        conn = sqlite3.connect(path)
        idx = {}
        for country, pid, name in conn.execute(
            "SELECT DISTINCT g.country, g.player_id, p.name "
            "FROM game_data g JOIN players p ON p.player_id = g.player_id"
        ):
            idx.setdefault(country, {})[norm(name)] = str(pid)
        conn.close()
    finally:
        os.unlink(path)
    idx.setdefault("usa", {}).update(idx.pop("united-states", {}))
    if os.path.exists(EF_OVERRIDES):
        for c, m in json.load(open(EF_OVERRIDES)).items():
            if str(c).startswith("_") or not isinstance(m, dict):
                continue
            for name, tid in m.items():
                if not str(name).startswith("_"):
                    idx.setdefault(c, {})[norm(name)] = str(tid)
    return idx


def match_tm_id(idx, player):
    pool = idx.get(TEAM2DB.get(player["team"],
                               player["team"].lower().replace(" ", "-")))
    if not pool:
        return None
    target = norm(player["name"])
    if target in pool:
        return pool[target]
    hits = {v for k, v in pool.items() if target in k or k in target}
    return hits.pop() if len(hits) == 1 else None


def tm_search(name):
    html = tm_get(TM_SEARCH.format(q=urllib.parse.quote_plus(name)))
    m = re.search(r"profil/spieler/(\d+)", html)
    return m.group(1) if m else None


def resolve_tm_img(tm_id):
    html = tm_get(TM_PROFILE.format(id=tm_id))
    m = re.search(rf"portrait/(?:big|header)/{tm_id}-(\d+)\.(jpg|png)", html)
    return TM_IMG.format(id=tm_id, ts=m.group(1), ext=m.group(2)) if m else None


def completed_events():
    data = get_json(SCOREBOARD.format(start=WC_START, end=WC_END))
    out = []
    for e in data.get("events", []):
        st = e.get("status", {}).get("type", {})
        if st.get("state") == "post" and st.get("completed"):
            out.append((e["id"], e.get("date", "")[:10], e.get("name", "")))
    return sorted(out, key=lambda x: x[1])


def stat_map(entry):
    return {s.get("name"): s.get("value") or 0 for s in entry.get("stats", [])}


def nominal_minute(ev):
    # "58'" -> 58, "90'+7'" -> 90 (stoppage time is not credited), "105'" -> 105
    m = re.match(r"(\d+)", str(ev.get("clock", {}).get("displayValue") or ""))
    return int(m.group(1)) if m else None


def parse_events(data):
    # Substitutions: participants[0] comes on, participants[1] goes off
    # ("José Canale replaces Omar Alderete"). Red cards end a player's match.
    subs_in, subs_out, reds = {}, {}, {}
    for ev in data.get("keyEvents", []):
        if ev.get("shootout"):
            continue
        kind = str(ev.get("type", {}).get("text", "")).lower()
        minute = nominal_minute(ev)
        if minute is None:
            continue
        pids = [str((p.get("athlete") or {}).get("id", ""))
                for p in ev.get("participants", [])]
        if kind == "substitution":
            if len(pids) > 0 and pids[0]:
                subs_in[pids[0]] = minute
            if len(pids) > 1 and pids[1]:
                subs_out[pids[1]] = minute
        elif "red card" in kind and pids and pids[0]:
            reds[pids[0]] = minute
    return subs_in, subs_out, reds


def extract_match(event_id):
    data = get_json(SUMMARY.format(id=event_id))
    status = data.get("header", {}).get("competitions", [{}])[0].get("status", {})
    length = 90 if status.get("type", {}).get("detail") == "FT" else 120  # AET / FT-Pens
    subs_in, subs_out, reds = parse_events(data)
    players = []
    for side in data.get("rosters", []):
        team = side.get("team", {}).get("displayName", "")
        for entry in side.get("roster", []):
            stats = stat_map(entry)
            if not stats.get("appearances"):
                continue
            ath = entry.get("athlete", {})
            pid = str(ath.get("id", ""))
            start = 0 if entry.get("starter") else subs_in.get(pid)
            if start is None:
                log(f"    ! no sub-in event for {ath.get('displayName')} ({pid})")
                start = length - 1
            end = min(x for x in (subs_out.get(pid), reds.get(pid), length)
                      if x is not None)
            players.append({
                "id": pid,
                "name": ath.get("displayName", ""),
                "team": team,
                "pos": (entry.get("position") or {}).get("abbreviation", ""),
                "apps": int(stats.get("appearances", 0)),
                "mins": max(1, end - start),
                "goals": int(stats.get("totalGoals", 0)),
                "assists": int(stats.get("goalAssists", 0)),
            })
    return length, players


def build():
    os.makedirs(OUT_DIR, exist_ok=True)
    cache = json.load(open(CACHE_PATH)) if os.path.exists(CACHE_PATH) else {}

    events = completed_events()
    # "len" also marks cache entries that predate minutes tracking
    todo = [e for e in events if e[0] not in cache or "len" not in cache[e[0]]]
    log(f"{len(events)} completed match(es), {len(todo)} not yet cached.")
    for i, (eid, date, name) in enumerate(todo, 1):
        length, players = extract_match(eid)
        cache[eid] = {"date": date, "name": name, "len": length, "players": players}
        json.dump(cache, open(CACHE_PATH, "w"), ensure_ascii=False)
        log(f"  [{i}/{len(todo)}] {date} {name} ({length}'): {len(players)} players")
        time.sleep(1.0)

    totals = {}
    for eid, _, _ in events:
        for p in cache[eid]["players"]:
            t = totals.setdefault(p["id"], {
                "id": p["id"], "name": p["name"], "team": p["team"],
                "pos": p["pos"], "apps": 0, "mins": 0, "goals": 0, "assists": 0,
            })
            # keep the latest name/team/pos spelling
            t["name"], t["team"], t["pos"] = p["name"], p["team"], p["pos"]
            t["apps"] += p["apps"]
            t["mins"] += p["mins"]
            t["goals"] += p["goals"]
            t["assists"] += p["assists"]

    players = sorted(totals.values(),
                     key=lambda p: (-p["apps"], -p["mins"], -p["goals"], p["name"]))
    attach_images(players)

    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "matches": len(events),
        "players": players,
    }
    json.dump(out, open(STATS_PATH, "w"), ensure_ascii=False, indent=1)
    log(f"Wrote stats.json: {len(players)} players across {len(events)} matches.")


def attach_images(players):
    cache = (json.load(open(IMG_CACHE_PATH)) if os.path.exists(IMG_CACHE_PATH)
             else {})
    for k in ("tm_map", "search_miss", "transfermarkt", "tm_null", "tm_err"):
        cache.setdefault(k, {})
    ef = json.load(open(EF_IMG_CACHE)) if os.path.exists(EF_IMG_CACHE) else {}
    ef_tm = {k: v for k, v in (ef.get("transfermarkt") or {}).items() if v}

    try:
        idx = pes_index()
    except Exception as e:
        log(f"pes.db unavailable ({e}); relying on cached mappings only.")
        idx = {}
    tm_map = cache["tm_map"]
    for p in players:
        if p["id"] not in tm_map:
            tid = match_tm_id(idx, p)
            if tid:
                tm_map[p["id"]] = tid

    def img_for(tid):
        return ef_tm.get(tid) or cache["transfermarkt"].get(tid)

    # Spend the daily Transfermarkt budget on the most visible players first.
    budget = TM_DAILY_LIMIT
    prio = sorted(players,
                  key=lambda p: -(p["mins"] + 120 * (p["goals"] + p["assists"])))
    for p in prio:
        if budget <= 0:
            break
        tid = tm_map.get(p["id"])
        if not tid:
            if cache["search_miss"].get(p["id"], 0) >= SEARCH_MISS_LIMIT:
                continue
            budget -= 1
            try:
                tid = tm_search(p["name"])
            except Exception as e:
                log(f"  search fail {p['name']}: {e}")
                tid = None
            time.sleep(1.0)
            if tid:
                tm_map[p["id"]] = tid
                log(f"  found {p['name']} -> #{tid}")
            else:
                n = cache["search_miss"].get(p["id"], 0) + 1
                cache["search_miss"][p["id"]] = n
                log(f"  no TM match for {p['name']} ({p['team']}) — {n}/{SEARCH_MISS_LIMIT}")
                continue
        if img_for(tid):
            continue
        if (cache["tm_null"].get(tid, 0) >= TM_NULL_LIMIT
                or cache["tm_err"].get(tid, 0) >= TM_ERROR_LIMIT):
            continue
        budget -= 1
        try:
            url = resolve_tm_img(tid)
            if url:
                cache["transfermarkt"][tid] = url
                cache["tm_null"].pop(tid, None)
                cache["tm_err"].pop(tid, None)
                log(f"  img  {p['name']} (#{tid})")
            else:
                cache["tm_null"][tid] = cache["tm_null"].get(tid, 0) + 1
                log(f"  null {p['name']} (#{tid})")
        except Exception as e:
            cache["tm_err"][tid] = cache["tm_err"].get(tid, 0) + 1
            log(f"  fail {p['name']} (#{tid}): {e}")
        time.sleep(1.0)

    json.dump(cache, open(IMG_CACHE_PATH, "w"), ensure_ascii=False, indent=1)
    have = 0
    for p in players:
        tid = tm_map.get(p["id"])
        tm = img_for(tid) if tid else None
        p["img"] = tm or HEADSHOT.format(id=p["id"])
        have += 1 if tm else 0
    log(f"Photos: {have}/{len(players)} via Transfermarkt "
        f"({len(tm_map)} id mappings), rest fall back to ESPN headshots.")


if __name__ == "__main__":
    build()
