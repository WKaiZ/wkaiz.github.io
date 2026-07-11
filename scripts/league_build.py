#!/usr/bin/env python3
"""Shared builder for soccer-league player leaderboards (appearances / goals /
assists). Every ESPN soccer competition — the UEFA club competitions and the
big-five domestic leagues alike — shares one API shape and photo pipeline, so
each has a thin wrapper (e.g. ucl_build.py, epl_build.py) that just calls run()
with its ESPN slug and season window.

Data source: ESPN's public site API. Completed matches are immutable, so each
match summary is fetched exactly once and cached in match_cache.json — a daily
run costs one scoreboard request plus one request per newly finished match.

Photos follow a Transfermarkt-first pipeline and share one tid -> photo cache
(scripts/imgcache.py) with every other page. Clubs don't map to a pes.db
country, so we flatten pes.db into a single name -> Transfermarkt-id map: most
players are also internationals whose ids (and often photos) we already know.
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

import imgcache  # shared Transfermarkt photo cache (see scripts/imgcache.py)

API = "https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}"
HEADSHOT = "https://a.espncdn.com/i/headshots/soccer/players/full/{id}.png"
FOTMOB_IMG = "https://images.fotmob.com/image_resources/playerimages/{id}.png"

PES_DB_URL = "https://raw.githubusercontent.com/WKaiZ/efootball/main/pes.db"
TM_SEARCH = "https://www.transfermarkt.com/schnellsuche/ergebnis/schnellsuche?query={q}"
TM_PROFILE = "https://www.transfermarkt.us/x/profil/spieler/{id}"
TM_IMG = "https://img.a.transfermarkt.technology/portrait/big/{id}-{ts}.{ext}"
TM_NULL_LIMIT, TM_ERROR_LIMIT, SEARCH_MISS_LIMIT = 3, 5, 3

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
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


def pes_name_index():
    """A single unambiguous normalized-name -> Transfermarkt-id map, pooled
    across every country in pes.db (plus eFootball overrides). Clubs have no
    country, so we match players by name alone; names that map to more than one
    id are dropped to avoid mixing up different players."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    name_ids = {}
    try:
        urllib.request.urlretrieve(PES_DB_URL, path)
        conn = sqlite3.connect(path)
        for pid, name in conn.execute(
            "SELECT DISTINCT g.player_id, p.name "
            "FROM game_data g JOIN players p ON p.player_id = g.player_id"
        ):
            name_ids.setdefault(norm(name), set()).add(str(pid))
        conn.close()
    finally:
        os.unlink(path)
    if os.path.exists(EF_OVERRIDES):
        for c, m in json.load(open(EF_OVERRIDES)).items():
            if str(c).startswith("_") or not isinstance(m, dict):
                continue
            for name, tid in m.items():
                if not str(name).startswith("_"):
                    name_ids.setdefault(norm(name), set()).add(str(tid))
    return {n: ids.pop() for n, ids in name_ids.items() if len(ids) == 1}


def tm_search(name):
    html = tm_get(TM_SEARCH.format(q=urllib.parse.quote_plus(name)))
    m = re.search(r"profil/spieler/(\d+)", html)
    return m.group(1) if m else None


def resolve_tm_img(tm_id):
    html = tm_get(TM_PROFILE.format(id=tm_id))
    m = re.search(rf"portrait/(?:big|header)/{tm_id}-(\d+)\.(jpg|png)", html)
    return TM_IMG.format(id=tm_id, ts=m.group(1), ext=m.group(2)) if m else None


def completed_events(base, start, end):
    url = base + f"/scoreboard?dates={start}-{end}&limit=400"
    out = []
    for e in get_json(url).get("events", []):
        st = e.get("status", {}).get("type", {})
        if st.get("state") == "post" and st.get("completed"):
            out.append((e["id"], e.get("date", "")[:10], e.get("name", "")))
    return sorted(out, key=lambda x: x[1])


def season_started(slug, start, end, grace_days=1):
    """True once the season's first scheduled match kicked off at least
    grace_days ago. Lets a workflow keep last season's board frozen over the
    off-season and only begin refreshing a day after the opening match, so the
    board flips straight to fresh data instead of showing an empty new season."""
    url = API.format(slug=slug) + f"/scoreboard?dates={start}-{end}&limit=400"
    dates = sorted(e.get("date", "")[:10]
                   for e in get_json(url).get("events", []) if e.get("date"))
    if not dates:
        return False
    first = datetime.strptime(dates[0], "%Y-%m-%d").date()
    return (datetime.now(timezone.utc).date() - first).days >= grace_days


def stat_map(entry):
    return {s.get("name"): s.get("value") or 0 for s in entry.get("stats", [])}


def nominal_minute(ev):
    # "58'" -> 58, "90'+7'" -> 90 (stoppage time is not credited), "105'" -> 105
    m = re.match(r"(\d+)", str(ev.get("clock", {}).get("displayValue") or ""))
    return int(m.group(1)) if m else None


def parse_events(data):
    # Substitutions: participants[0] comes on, participants[1] goes off.
    # Red cards end a player's match.
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


def extract_match(base, event_id):
    data = get_json(base + f"/summary?event={event_id}")
    status = data.get("header", {}).get("competitions", [{}])[0].get("status", {})
    length = 90 if status.get("type", {}).get("detail") == "FT" else 120  # AET / Pens
    subs_in, subs_out, reds = parse_events(data)
    players = []
    for side in data.get("rosters", []):
        team = side.get("team", {}) or {}
        team_name = team.get("displayName", "")
        team_id = str(team.get("id", "") or "")
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
                "team": team_name,
                "team_id": team_id,
                "pos": (entry.get("position") or {}).get("abbreviation", ""),
                "apps": int(stats.get("appearances", 0)),
                "mins": max(1, end - start),
                "goals": int(stats.get("totalGoals", 0)),
                "assists": int(stats.get("goalAssists", 0)),
            })
    return length, players


def ef_fotmob_name_index():
    """Reuse the fotmob player photos the eFootball builder resolved, keyed by
    normalized name (unambiguous only), as a fallback for players with no
    Transfermarkt photo."""
    if not os.path.exists(EF_IMG_CACHE):
        return {}
    fm = json.load(open(EF_IMG_CACHE)).get("fotmob") or {}
    name_ids = {}
    for k, v in fm.items():
        if v:
            name_ids.setdefault(k.split("|", 1)[-1], set()).add(v)
    return {n: ids.pop() for n, ids in name_ids.items() if len(ids) == 1}


def attach_images(players, img_cache_path, tm_limit):
    cache = (json.load(open(img_cache_path)) if os.path.exists(img_cache_path)
             else {})
    for k in ("tm_map", "search_miss"):
        cache.setdefault(k, {})
    # tid -> photo url and the null/error backoff counters are shared with every
    # other page, so a photo resolved anywhere is reused here.
    imgcache.merge_legacy(imgcache.load(), cache)

    try:
        name_map = pes_name_index()
    except Exception as e:
        log(f"pes.db unavailable ({e}); relying on cached mappings only.")
        name_map = {}
    tm_map = cache["tm_map"]
    for p in players:
        if p["id"] not in tm_map:
            tid = name_map.get(norm(p["name"]))
            if tid:
                tm_map[p["id"]] = tid

    def img_for(tid):
        return cache["transfermarkt"].get(tid)

    # Spend the daily Transfermarkt budget on the most visible players first.
    budget = tm_limit
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

    json.dump({"tm_map": cache["tm_map"], "search_miss": cache["search_miss"]},
              open(img_cache_path, "w"), ensure_ascii=False, indent=1)
    imgcache.save(cache)

    fm_by_name = ef_fotmob_name_index()

    def fotmob_img(p):
        fid = fm_by_name.get(norm(p["name"]))
        return FOTMOB_IMG.format(id=fid) if fid else None

    tm_have = fm_have = 0
    for p in players:
        tid = tm_map.get(p["id"])
        tm = img_for(tid) if tid else None
        fm = None if tm else fotmob_img(p)  # prefer Transfermarkt, then fotmob
        p["img"] = tm or fm or HEADSHOT.format(id=p["id"])
        tm_have += 1 if tm else 0
        fm_have += 1 if fm else 0
    log(f"Photos: {tm_have}/{len(players)} via Transfermarkt ({len(tm_map)} id "
        f"mappings), {fm_have} reused from eFootball's fotmob, rest fall back "
        f"to ESPN headshots.")


def run(*, slug, start, end, out_name, tm_limit):
    """Build assets/<out_name>/stats.json for the given ESPN competition slug."""
    base = API.format(slug=slug)
    out_dir = os.path.join(ROOT, "assets", out_name)
    stats_path = os.path.join(out_dir, "stats.json")
    cache_path = os.path.join(out_dir, "match_cache.json")
    img_cache_path = os.path.join(out_dir, "img_cache.json")

    os.makedirs(out_dir, exist_ok=True)
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}

    events = completed_events(base, start, end)
    todo = [e for e in events if e[0] not in cache or "len" not in cache[e[0]]]
    log(f"{len(events)} completed match(es), {len(todo)} not yet cached.")
    for i, (eid, date, name) in enumerate(todo, 1):
        length, players = extract_match(base, eid)
        cache[eid] = {"date": date, "name": name, "len": length, "players": players}
        json.dump(cache, open(cache_path, "w"), ensure_ascii=False)
        log(f"  [{i}/{len(todo)}] {date} {name} ({length}'): {len(players)} players")
        time.sleep(1.0)

    totals = {}
    for eid, _, _ in events:
        for p in cache[eid]["players"]:
            t = totals.setdefault(p["id"], {
                "id": p["id"], "name": p["name"], "team": p["team"],
                "team_id": p.get("team_id", ""), "pos": p["pos"],
                "apps": 0, "mins": 0, "goals": 0, "assists": 0,
            })
            # keep the latest name/team/pos spelling (players can transfer)
            t["name"], t["team"] = p["name"], p["team"]
            t["team_id"], t["pos"] = p.get("team_id", ""), p["pos"]
            t["apps"] += p["apps"]
            t["mins"] += p["mins"]
            t["goals"] += p["goals"]
            t["assists"] += p["assists"]

    players = sorted(totals.values(),
                     key=lambda p: (-p["apps"], -p["mins"], -p["goals"], p["name"]))
    # Safety net: never overwrite an existing board with an empty one (e.g. an
    # off-season run before the new season has any completed matches).
    if not players and os.path.exists(stats_path):
        log(f"No completed matches in window for {out_name}; keeping existing board.")
        return
    attach_images(players, img_cache_path, tm_limit)

    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "matches": len(events),
        "players": players,
    }
    json.dump(out, open(stats_path, "w"), ensure_ascii=False, indent=1)
    log(f"Wrote {out_name}/stats.json: {len(players)} players across {len(events)} matches.")
