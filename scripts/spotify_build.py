#!/usr/bin/env python3

import hashlib
import hmac
import json
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PLAYLIST_ID = "2h21jaELSrCkMjjNQAyTVI"
PLAYLIST_URI = f"spotify:playlist:{PLAYLIST_ID}"
REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "assets" / "spotify" / "playlist.json"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
# Known-good persisted query hash for fetchPlaylist (playlistV2 + paginated content).
FETCH_PLAYLIST_HASH = (
    "91d4c2bc3e0cd1bc672281c4f1f59f43ff55ba726ca04a45810d99bd091f3f0e"
)
# Fallback secrets if the player JS shape changes; highest version wins.
FALLBACK_SECRETS = [
    {"version": 61, "secret": ',7/*F("rLJ2oxaKL^f+E1xvP@N'},
    {"version": 60, "secret": 'OmE{ZA.J^":0FG\\Uz?[@WW'},
    {"version": 59, "secret": "{iOFn;4}<1PFYKPV?5{%u14]M>/V0hDH"},
]


def http_json(url, data=None, headers=None):
    h = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Referer": "https://open.spotify.com/",
        "Origin": "https://open.spotify.com",
        **(headers or {}),
    }
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json;charset=UTF-8"
    req = urllib.request.Request(
        url,
        data=body,
        headers=h,
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"raw": raw[:800]}


def http_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", "replace")


def parse_js_string(literal: str) -> str:
    if literal[0] == "'" and literal[-1] == "'":
        s = literal[1:-1]
        out = []
        i = 0
        while i < len(s):
            if s[i] == "\\" and i + 1 < len(s):
                nxt = s[i + 1]
                out.append(
                    {
                        "n": "\n",
                        "r": "\r",
                        "t": "\t",
                        "\\": "\\",
                        "'": "'",
                        '"': '"',
                    }.get(nxt, nxt)
                )
                i += 2
            else:
                out.append(s[i])
                i += 1
        return "".join(out)
    return json.loads(literal)


def scrape_totp_secrets():
    html = http_text("https://open.spotify.com/")
    js_urls = re.findall(
        r"https://open\.spotifycdn\.com/cdn/build/mobile-web-player/"
        r"mobile-web-player\.[^\"']+\.js",
        html,
    )
    secrets = []
    for url in js_urls:
        try:
            js = http_text(url)
        except Exception:
            continue
        for match in re.finditer(
            r"\{secret:((?:'[^']*'|\"[^\"]*\")),version:(\d+)\}", js
        ):
            secrets.append(
                {
                    "version": int(match.group(2)),
                    "secret": parse_js_string(match.group(1)),
                }
            )
    if not secrets:
        secrets = list(FALLBACK_SECRETS)
    secrets.sort(key=lambda item: -item["version"])
    # de-dupe by version
    seen = set()
    uniq = []
    for item in secrets:
        if item["version"] in seen:
            continue
        seen.add(item["version"])
        uniq.append(item)
    return uniq


def cipher_key(secret: str) -> bytes:
    nums = [ord(ch) ^ ((i % 33) + 9) for i, ch in enumerate(secret)]
    return "".join(str(n) for n in nums).encode("utf-8")


def hotp(key: bytes, counter: int) -> str:
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (
        ((digest[offset] & 0x7F) << 24)
        | (digest[offset + 1] << 16)
        | (digest[offset + 2] << 8)
        | digest[offset + 3]
    )
    return f"{code % 1_000_000:06d}"


def get_access_token() -> str:
    _, server = http_json("https://open.spotify.com/api/server-time")
    server_time = int(server["serverTime"])
    counter = server_time // 30
    secrets = scrape_totp_secrets()
    last_err = None
    for product in ("mobile-web-player", "web-player"):
        for secret in secrets[:3]:
            totp = hotp(cipher_key(secret["secret"]), counter)
            query = urllib.parse.urlencode(
                {
                    "reason": "init",
                    "productType": product,
                    "totp": totp,
                    "totpServer": totp,
                    "totpVer": str(secret["version"]),
                }
            )
            code, body = http_json(f"https://open.spotify.com/api/token?{query}")
            token = body.get("accessToken") or body.get("access_token")
            if code == 200 and token:
                return token
            last_err = body
    raise RuntimeError(f"Could not obtain Spotify access token: {last_err}")


def pathfinder_playlist(token: str, offset: int, limit: int = 100) -> dict:
    payload = {
        "variables": {
            "uri": PLAYLIST_URI,
            "offset": offset,
            "limit": limit,
        },
        "operationName": "fetchPlaylist",
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": FETCH_PLAYLIST_HASH,
            }
        },
    }
    code, body = http_json(
        "https://api-partner.spotify.com/pathfinder/v2/query",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "app-platform": "WebPlayer",
        },
    )
    if code != 200 or not body.get("data", {}).get("playlistV2"):
        raise RuntimeError(f"pathfinder fetchPlaylist failed ({code}): {body}")
    return body["data"]["playlistV2"]


def artist_names(artists_node) -> str:
    if not isinstance(artists_node, dict):
        return ""
    names = []
    for item in artists_node.get("items") or []:
        profile = item.get("profile") or item.get("data") or item
        if isinstance(profile, dict) and profile.get("name"):
            names.append(profile["name"])
    return ", ".join(names)


def cover_url(playlist: dict):
    images = playlist.get("images") or {}
    items = images.get("items") or []
    if not items:
        return None
    sources = items[0].get("sources") or []
    if not sources:
        return None
    # Prefer a mid-size image when available.
    ranked = sorted(
        (s for s in sources if s.get("url")),
        key=lambda s: abs((s.get("width") or 300) - 300),
    )
    return ranked[0]["url"] if ranked else None


def normalize_item(item: dict) -> dict | None:
    wrapper = item.get("itemV2") or {}
    data = wrapper.get("data") or {}
    typename = data.get("__typename")

    if typename != "Track":
        return None

    track_id = (data.get("uri") or "").rsplit(":", 1)[-1]
    if not track_id:
        return None
    duration = (data.get("trackDuration") or {}).get("totalMilliseconds") or 0
    return {
        "id": track_id,
        "title": data.get("name") or "",
        "artists": artist_names(data.get("artists")),
        "duration_ms": duration,
        "url": f"https://open.spotify.com/track/{track_id}",
        "preview_url": None,
    }


def fetch_all_tracks(token: str):
    first = pathfinder_playlist(token, 0, 100)
    content = first.get("content") or {}
    total = int(content.get("totalCount") or 0)
    items = list(content.get("items") or [])
    offset = len(items)

    while offset < total:
        page = pathfinder_playlist(token, offset, 100)
        batch = (page.get("content") or {}).get("items") or []
        if not batch:
            break
        items.extend(batch)
        offset += len(batch)
        time.sleep(0.1)

    tracks = []
    for item in items:
        normalized = normalize_item(item)
        if not normalized:
            continue
        added = ((item.get("addedAt") or {}).get("isoString")) or ""
        normalized["added_at"] = added
        tracks.append(normalized)

    # Newest additions first (fallback to playlist-order reverse if timestamps missing).
    if any(t.get("added_at") for t in tracks):
        tracks.sort(key=lambda t: t.get("added_at") or "", reverse=True)
    else:
        tracks.reverse()

    owner = (
        ((first.get("ownerV2") or {}).get("data") or {}).get("name")
        or "wes"
    )
    return {
        "id": PLAYLIST_ID,
        "name": first.get("name") or "Playlist",
        "owner": owner,
        "url": f"https://open.spotify.com/playlist/{PLAYLIST_ID}",
        "cover": cover_url(first),
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total": len(tracks),
        "tracks": tracks,
    }


def main() -> int:
    token = get_access_token()
    payload = fetch_all_tracks(token)
    if len(payload["tracks"]) < 100:
        raise RuntimeError(
            f"Expected a full playlist, got only {len(payload['tracks'])} tracks "
            f"(totalCount={payload.get('total')})"
        )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(
        f"Wrote {len(payload['tracks'])}/{payload['total']} tracks "
        f"→ {OUT.relative_to(REPO)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
