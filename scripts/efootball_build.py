#!/usr/bin/env python3

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import imgcache

REPO_URL = "https://github.com/WKaiZ/efootball"
GROUPS = [("contenders", "contender"), ("challengers", "challenger")]

FOTMOB_SUGGEST = "https://apigw.fotmob.com/searchapi/suggest"
FOTMOB_IMG = "https://images.fotmob.com/image_resources/playerimages/{id}.png"
TM_PROFILE = "https://www.transfermarkt.us/x/profil/spieler/{id}"
TM_LINK = "https://www.transfermarkt.com/x/profil/spieler/{id}"
TM_IMG = "https://img.a.transfermarkt.technology/portrait/big/{id}-{ts}.{ext}"
TM_DAILY_LIMIT = int(os.environ.get("EFOOTBALL_TM_LIMIT", "23"))
TM_NULL_LIMIT = int(os.environ.get("EFOOTBALL_TM_NULL_LIMIT", "3"))
TM_ERROR_LIMIT = int(os.environ.get("EFOOTBALL_TM_ERROR_LIMIT", "5"))

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_DIR = os.path.join(ROOT, "assets", "efootball")
SQUADS_PATH = os.path.join(OUT_DIR, "squads.json")
CACHE_PATH = os.path.join(OUT_DIR, "img_cache.json")
OVERRIDES_PATH = os.path.join(OUT_DIR, "overrides.json")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
TM_HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

LINE_RE = re.compile(
    r"^\s*\[(?P<slot>[A-Z]+)\]\s+(?P<name>.+?)\s+\((?P<pos>[A-Z]+)\)\s+"
    r"rating\s+(?P<rating>[\d.]+)\s+#(?P<num>\d+)\s*$"
)

# Flags for the four UK home nations are subdivision flag sequences that can't
# be derived from a two-letter code, so they're kept explicit.
_SCOT = "\U0001F3F4\U000E0067\U000E0062\U000E0073\U000E0063\U000E0074\U000E007F"
_WALES = "\U0001F3F4\U000E0067\U000E0062\U000E0077\U000E006C\U000E0073\U000E007F"
_ENG = "\U0001F3F4\U000E0067\U000E0062\U000E0065\U000E006E\U000E0067\U000E007F"
SUBDIVISION_FLAGS = {"england": _ENG, "scotland": _SCOT, "wales": _WALES}

# Map each squad directory id -> ISO 3166-1 alpha-2 code. The flag emoji is
# generated from the code (see flag_for), so subbing a new nation in/out only
# needs an entry here — no emoji to hand-pick. Ids match the folder names in
# the efootball source repo (lowercase, hyphenated).
ISO2 = {
    "algeria": "dz", "angola": "ao", "argentina": "ar", "australia": "au",
    "austria": "at", "azerbaijan": "az", "belgium": "be", "bolivia": "bo",
    "bosnia": "ba", "brazil": "br", "bulgaria": "bg", "burkina-faso": "bf",
    "cameroon": "cm", "canada": "ca", "cape-verde": "cv", "chile": "cl",
    "china": "cn", "colombia": "co", "congo": "cd", "costa-rica": "cr",
    "croatia": "hr", "czechia": "cz", "denmark": "dk", "dr-congo": "cd",
    "ecuador": "ec", "egypt": "eg", "el-salvador": "sv", "finland": "fi",
    "france": "fr", "gabon": "ga", "georgia": "ge", "germany": "de",
    "ghana": "gh", "greece": "gr", "guatemala": "gt", "guinea": "gn",
    "honduras": "hn", "hungary": "hu", "iceland": "is", "india": "in",
    "indonesia": "id", "iran": "ir", "iraq": "iq", "ireland": "ie",
    "israel": "il", "italy": "it", "ivory-coast": "ci", "jamaica": "jm",
    "japan": "jp", "jordan": "jo", "kazakhstan": "kz", "kenya": "ke",
    "korea": "kr", "kosovo": "xk", "kuwait": "kw", "luxembourg": "lu",
    "mali": "ml", "mexico": "mx", "montenegro": "me", "morocco": "ma",
    "netherlands": "nl", "new-zealand": "nz", "nigeria": "ng",
    "north-macedonia": "mk", "norway": "no", "oman": "om", "panama": "pa",
    "paraguay": "py", "peru": "pe", "poland": "pl", "portugal": "pt",
    "qatar": "qa", "romania": "ro", "russia": "ru", "saudi-arabia": "sa",
    "senegal": "sn", "serbia": "rs", "slovakia": "sk", "slovenia": "si",
    "south-africa": "za", "spain": "es", "sweden": "se", "switzerland": "ch",
    "thailand": "th", "tunisia": "tn", "turkey": "tr", "uae": "ae",
    "ukraine": "ua", "uruguay": "uy", "usa": "us", "uzbekistan": "uz",
    "venezuela": "ve", "vietnam": "vn", "zambia": "zm",
}
DISPLAY = {"usa": "USA", "uae": "UAE", "korea": "South Korea",
           "ivory-coast": "Ivory Coast", "congo": "DR Congo",
           "dr-congo": "DR Congo", "bosnia": "Bosnia & Herzegovina"}

def flag_for(country):
    if country in SUBDIVISION_FLAGS:
        return SUBDIVISION_FLAGS[country]
    iso = ISO2.get(country)
    if not iso:
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("a")) for c in iso.lower())

def log(*a):
    print(*a, file=sys.stderr, flush=True)

def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()

def deaccent(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c))

def display_name(country):
    return DISPLAY.get(country, country.replace("-", " ").replace("_", " ").title())

def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True).stdout

def row_sig(line):
    return re.sub(r"\s+", " ", line.strip())

def parse_sigs(text):
    return {row_sig(ln) for ln in text.splitlines() if LINE_RE.match(ln)}

POOL_ROW_RE = re.compile(r"^\s*(?P<name>[^,\[\]]+?),\s*(?P<pos>[A-Z]+),")
POOL_BOOL_RE = re.compile(r",\s*(?:True|False),")

def pool_sig(line):
    return row_sig(POOL_BOOL_RE.sub(",", line, count=1))

def parse_pool_sigs(text):
    return {pool_sig(ln) for ln in text.splitlines() if POOL_ROW_RE.match(ln)}

def parse_pool_rows(text):
    rows = {}
    for ln in text.splitlines():
        m = POOL_ROW_RE.match(ln)
        if m:
            rows[(norm(m["name"]), m["pos"])] = pool_sig(ln)
    return rows

def history_versions(repo, rel_path, parser=parse_sigs):
    out = git(repo, "log", "--follow", "--format=C|%H|%cI", "--name-only", "--", rel_path)
    commits, h, d = [], None, None
    for ln in out.splitlines():
        if ln.startswith("C|"):
            _, h, d = ln.split("|")
        elif ln.strip():
            commits.append((h, d[:10], ln.strip()))
    return [(d, parser(git(repo, "show", f"{h}:{path}"))) for h, d, path in commits]

def compute_since(versions, sig):
    since = None
    for date, sigs in versions:
        if sig in sigs:
            since = date
        else:
            break
    return since

def http_get(url, timeout=30, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

def _fotmob_query(term):
    url = f"{FOTMOB_SUGGEST}?term={urllib.parse.quote(term)}&lang=en"
    try:
        data = json.loads(http_get(url, timeout=15))
    except Exception:
        return None
    best_player = best_any = None
    for group in data.get("squadMemberSuggest", []):
        for opt in group.get("options", []):
            payload = opt.get("payload") or {}
            pid = payload.get("id") or (opt.get("text", "").rsplit("|", 1)[-1])
            score = opt.get("score") or 0
            if not pid:
                continue
            if best_any is None or score > best_any[1]:
                best_any = (str(pid), score)
            if not payload.get("isCoach") and (best_player is None or score > best_player[1]):
                best_player = (str(pid), score)
    chosen = best_player or best_any
    return chosen[0] if chosen else None

def fotmob_lookup(name):
    tokens = name.split()
    variants = [name, deaccent(name)]
    if len(tokens) > 2:
        fl = f"{tokens[0]} {tokens[-1]}"
        variants += [fl, deaccent(fl)]
    seen = set()
    for v in variants:
        if v in seen:
            continue
        seen.add(v)
        pid = _fotmob_query(v)
        if pid:
            return pid
        time.sleep(0.2)
    return None

def load_game_data(db_path):
    conn = sqlite3.connect(db_path)
    rows, cards = {}, {}
    for country, pid, name, pos, rating, card_type in conn.execute(
        "SELECT g.country, g.player_id, p.name, g.position, g.rating, g.card_type "
        "FROM game_data g JOIN players p ON p.player_id = g.player_id"
    ):
        rows.setdefault(country, []).append((str(pid), name, pos, rating))
        cards[(country, str(pid), pos)] = card_type
    conn.close()
    return rows, cards

def find_tm_id(rows, player, override):
    if override:
        return str(override)
    target = norm(player["name"])
    for pid, name, pos, _ in rows:
        if pos == player["pos"] and norm(name) == target:
            return pid
    cands = [r for r in rows if r[2] == player["pos"] and abs(r[3] - player["rating"]) < 0.01]
    if len(cands) == 1:
        return cands[0][0]
    if len(cands) > 1:
        for pid, name, _, _ in cands:
            if target in norm(name) or norm(name) in target:
                return pid
        return cands[0][0]
    for pid, name, _, _ in rows:
        if norm(name) == target:
            return pid
    return None

def resolve_transfermarkt(tm_id):
    with urllib.request.urlopen(
        urllib.request.Request(TM_PROFILE.format(id=tm_id), headers=TM_HEADERS), timeout=30
    ) as r:
        html = r.read().decode("utf-8", "replace")
    m = re.search(rf"portrait/(?:big|header)/{tm_id}-(\d+)\.(jpg|png)", html)
    return TM_IMG.format(id=tm_id, ts=m.group(1), ext=m.group(2)) if m else None

def load_cache():
    if not os.path.exists(CACHE_PATH):
        return {"fotmob": {}, "transfermarkt": {}, "tm_null": {}}
    data = json.load(open(CACHE_PATH))
    if "fotmob" in data or "transfermarkt" in data:
        data.setdefault("fotmob", {})
        data.setdefault("transfermarkt", {})
        data.setdefault("tm_null", {})
        data.setdefault("tm_err", {})
        return data
    return {"fotmob": {k: v for k, v in data.items() if v},
            "transfermarkt": {}, "tm_null": {}, "tm_err": {}}

def save_cache(cache):
    json.dump({"fotmob": cache["fotmob"]}, open(CACHE_PATH, "w"),
              ensure_ascii=False, indent=1)
    imgcache.save(cache)

def _parse_players(text):
    players, section = [], None
    for line in text.splitlines():
        low = line.strip().lower()
        if low.startswith("starters"):
            section = "starters"; continue
        if low.startswith("substitutes"):
            section = "substitutes"; continue
        if low.startswith("wildcard"):
            section = "wildcard"; continue
        m = LINE_RE.match(line)
        if m:
            players.append({
                "section": section, "slot": m["slot"], "name": m["name"].strip(),
                "pos": m["pos"], "rating": float(m["rating"]), "number": int(m["num"]),
                "row": row_sig(line),
            })
    return players

def _assign_medals(players):
    for p in players:
        p["tenureRank"] = 0
    ranked = sorted(players, key=lambda p: (p["since"], -p["rating"], p["name"]))
    for i, p in enumerate(ranked[:3]):
        p["tenureRank"] = i + 1

def build():
    os.makedirs(OUT_DIR, exist_ok=True)
    cache = load_cache()
    imgcache.merge_legacy(imgcache.load(), cache)
    fm_cache, tm_cache = cache["fotmob"], cache["transfermarkt"]
    overrides = json.load(open(OVERRIDES_PATH)) if os.path.exists(OVERRIDES_PATH) else {}

    prev_ids = set()
    if os.path.exists(SQUADS_PATH):
        try:
            prev_ids = {c["id"] for c in json.load(open(SQUADS_PATH)).get("countries", [])}
        except Exception:
            pass

    tmp = tempfile.mkdtemp(prefix="efb-")
    try:
        log("Cloning efootball repo ...")
        subprocess.run(["git", "clone", "--quiet", REPO_URL, tmp], check=True)
        game_data, card_types = load_game_data(os.path.join(tmp, "pes.db"))
        today = datetime.now(timezone.utc).date()

        out_countries, all_players = [], []
        for group_dir, group in GROUPS:
            base = os.path.join(tmp, group_dir)
            if not os.path.isdir(base):
                continue
            for c in sorted(d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))):
                rel = f"{group_dir}/{c}/{c}.txt"
                path = os.path.join(tmp, rel)
                if not os.path.exists(path):
                    continue
                players = _parse_players(open(path).read())
                versions = history_versions(tmp, rel)
                pool_rel = f"{group_dir}/{c}/{c}_players.txt"
                pool_path = os.path.join(tmp, pool_rel)
                pool_rows = (parse_pool_rows(open(pool_path).read())
                             if os.path.exists(pool_path) else {})
                pool_versions = (history_versions(tmp, pool_rel, parse_pool_sigs)
                                 if pool_rows else [])
                ov = {norm(k): v for k, v in (overrides.get(c) or {}).items()
                      if not str(k).startswith("_")}
                for p in players:
                    p["fm_id"] = fm_cache.get(f"{c}|{norm(p['name'])}")
                    p["tm_id"] = find_tm_id(game_data.get(c, []), p, ov.get(norm(p["name"])))
                    p["card_type"] = card_types.get((c, p["tm_id"], p["pos"])) or "Standard"
                    dates = [compute_since(versions, p["row"])]
                    pool_row = pool_rows.get((norm(p["name"]), p["pos"]))
                    if pool_row:
                        dates.append(compute_since(pool_versions, pool_row))
                    since = (today.isoformat() if any(d is None for d in dates)
                             else max(dates))
                    p["since"] = since
                    p["days"] = (today - datetime.fromisoformat(since).date()).days
                _assign_medals(players)
                flag = flag_for(c)
                if not flag:
                    log(f"  WARNING: no flag mapping for '{c}' — add it to "
                        f"ISO2 in efootball_build.py (flag will be blank).")
                out_countries.append({
                    "id": c, "name": display_name(c), "flag": flag,
                    "players": players,
                })
                all_players.extend((c, p) for p in players)
                log(f"  {group_dir}/{c}: {len(players)} players")

        out_countries.sort(key=lambda c: c["name"].lower())

        curr_ids = {c["id"] for c in out_countries}
        if prev_ids:
            added = sorted(curr_ids - prev_ids)
            removed = sorted(prev_ids - curr_ids)
            if added:
                log(f"Nations added: {', '.join(added)}")
            if removed:
                log(f"Nations removed: {', '.join(removed)}")
            if not added and not removed:
                log("Nations unchanged.")

        _resolve_fotmob(all_players, cache)
        _backfill_transfermarkt(all_players, cache)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    for c in out_countries:
        for p in c["players"]:
            fm = FOTMOB_IMG.format(id=p["fm_id"]) if p["fm_id"] else None
            tm = tm_cache.get(p["tm_id"]) if p["tm_id"] else None
            p["img"] = tm or fm
            if p["tm_id"]:
                p["tm"] = TM_LINK.format(id=p["tm_id"])
            p.pop("fm_id", None); p.pop("tm_id", None); p.pop("row", None)

    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "source": REPO_URL,
        "countries": out_countries,
    }
    json.dump(out, open(SQUADS_PATH, "w"), ensure_ascii=False, indent=1)
    save_cache(cache)

    have = sum(1 for c in out_countries for p in c["players"] if p["img"])
    tot = sum(len(c["players"]) for c in out_countries)
    log(f"Wrote squads.json: {len(out_countries)} nations, {have}/{tot} photos "
        f"({sum(1 for v in tm_cache.values() if v)} via Transfermarkt backfill).")

def _resolve_fotmob(all_players, cache):
    fm_cache, tm_cache = cache["fotmob"], cache["transfermarkt"]
    todo = [(c, p) for c, p in all_players
            if not p["fm_id"] and not (p["tm_id"] and tm_cache.get(p["tm_id"]))]
    if not todo:
        log("Fotmob: nothing to look up.")
        return
    log(f"Fotmob: looking up {len(todo)} player(s) ...")
    for i, (c, p) in enumerate(todo, 1):
        pid = fotmob_lookup(p["name"])
        fm_cache[f"{c}|{norm(p['name'])}"] = pid
        p["fm_id"] = pid
        if i % 25 == 0:
            save_cache(cache)
            log(f"  ... {i}/{len(todo)}")
        time.sleep(0.15)
    save_cache(cache)

def _backfill_transfermarkt(all_players, cache):
    tm_cache = cache["transfermarkt"]
    tm_null = cache.setdefault("tm_null", {})
    tm_err = cache.setdefault("tm_err", {})
    pending = {}
    for c, p in all_players:
        tid = p["tm_id"]
        if not tid or tm_cache.get(tid):
            continue
        if tm_null.get(tid, 0) >= TM_NULL_LIMIT or tm_err.get(tid, 0) >= TM_ERROR_LIMIT:
            continue
        if tid not in pending or p["days"] < pending[tid][0]:
            pending[tid] = (p["days"], p["name"])
    queue = sorted(pending.items(), key=lambda kv: kv[1][0])[:TM_DAILY_LIMIT]
    if not queue:
        log("Transfermarkt: nothing to backfill.")
        return
    log(f"Transfermarkt: resolving {len(queue)} photo(s) (limit {TM_DAILY_LIMIT}).")
    for tid, (days, name) in queue:
        try:
            url = resolve_transfermarkt(tid)
            if url:
                tm_cache[tid] = url
                tm_null.pop(tid, None)
                tm_err.pop(tid, None)
                log(f"  ok   {name} (#{tid}, {days}d)")
            else:
                n = tm_null.get(tid, 0) + 1
                tm_null[tid] = n
                if n >= TM_NULL_LIMIT:
                    log(f"  null {name} (#{tid}) — giving up after {n} null result(s)")
                else:
                    log(f"  null {name} (#{tid}) — attempt {n}/{TM_NULL_LIMIT}, will retry")
        except Exception as e:
            n = tm_err.get(tid, 0) + 1
            tm_err[tid] = n
            if n >= TM_ERROR_LIMIT:
                log(f"  fail {name} (#{tid}): {e} — giving up after {n} error(s)")
            else:
                log(f"  fail {name} (#{tid}): {e} — attempt {n}/{TM_ERROR_LIMIT}, will retry")
        time.sleep(1.0)
    save_cache(cache)

if __name__ == "__main__":
    build()
