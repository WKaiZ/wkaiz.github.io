#!/usr/bin/env python3
"""Shared Transfermarkt photo cache for the eFootball and WC26 builders.

Both pages key player photos by Transfermarkt player id, and the two squads
overlap heavily (a striker on the eFootball page is usually also at the World
Cup). Keeping the tid -> image-url map (and the null/error backoff counters)
in one file means a photo resolved by either builder is immediately reused by
the other, instead of each spending its daily Transfermarkt request budget
re-resolving the same players.

Each builder still keeps its own project-specific mappings (eFootball's fotmob
ids, WC26's ESPN-id -> tid map and search misses) in its own img_cache.json.
"""

import json
import os

KEYS = ("transfermarkt", "tm_null", "tm_err")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SHARED_PATH = os.path.join(ROOT, "assets", "tm_img_cache.json")

def load():
    """Return the shared cache with all expected keys present."""
    data = json.load(open(SHARED_PATH)) if os.path.exists(SHARED_PATH) else {}
    for k in KEYS:
        data.setdefault(k, {})
    return data

def merge_legacy(shared, local):
    """Fold Transfermarkt entries that used to live in a per-project cache into
    the shared store, then point the local cache's keys at the shared dicts so
    callers read and write a single source of truth."""
    for k in KEYS:
        for tid, v in (local.get(k) or {}).items():
            if k == "transfermarkt":
                shared[k].setdefault(tid, v)
            else:
                shared[k][tid] = max(shared[k].get(tid, 0), v)
        local[k] = shared[k]
    return shared

def save(shared):
    os.makedirs(os.path.dirname(SHARED_PATH), exist_ok=True)
    out = {k: shared[k] for k in KEYS}
    json.dump(out, open(SHARED_PATH, "w"), ensure_ascii=False, indent=1)
