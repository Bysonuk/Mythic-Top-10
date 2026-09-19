#!/usr/bin/env python3
"""
Work out which hero talent tree a player picked, from their talent loadout string.

Blizzard's export string encodes a bit per node in a fixed order, and one of those
nodes is the hero tree choice. The node order and the hero trees themselves come
from Raidbots' public talent data, which is cached locally for a week.

    python talents.py --decode "C4DAAAA..."     # decode one string
    python talents.py --dump                    # list the hero trees per spec

Used by wcl_mythic_stats.py and mplus.py; safe to import on its own.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

TALENTS_URL = "https://www.raidbots.com/static/data/live/talents.json"
CACHE_TTL = 7 * 24 * 3600
B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
IDX = {c: i for i, c in enumerate(B64)}
_INDEX = None


def log(m):
    print(m, flush=True)


# --------------------------------------------------------------------------- #
# Talent data
# --------------------------------------------------------------------------- #

def fetch_tree_data(cache_dir):
    path = os.path.join(cache_dir, "raidbots-talents.json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < CACHE_TTL:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except ValueError:
            pass
    req = urllib.request.Request(TALENTS_URL, headers={"User-Agent": "MythicTop10/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.load(r)
    os.makedirs(cache_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data


def build_index(data):
    """spec id -> node order, the hero-choice node, and what each choice means."""
    index = {}
    for tree in data if isinstance(data, list) else []:
        spec_id = tree.get("specId")
        order = tree.get("fullNodeOrder") or []
        if not spec_id or not order:
            continue
        choices = {}
        node_id = None
        for node in tree.get("subTreeNodes") or []:
            entries = node.get("entries") or []
            if not entries:
                continue
            node_id = node.get("id")
            for i, e in enumerate(entries):
                choices[i] = {
                    "name": e.get("name") or "",
                    "id": e.get("traitSubTreeId") or e.get("id"),
                    "atlas": e.get("atlasMemberName") or "",
                }
        icons = {}
        for node in tree.get("heroNodes") or []:
            sub = node.get("subTreeId")
            ent = (node.get("entries") or [{}])[0]
            if sub and ent.get("icon") and sub not in icons:
                icons[sub] = ent["icon"]
        for c in choices.values():
            c["icon"] = icons.get(c["id"], "")
        index[int(spec_id)] = {"order": order, "node": node_id, "choices": choices,
                               "spec": tree.get("specName", ""), "class": tree.get("className", "")}
    return index


def load_index(cache_dir):
    global _INDEX
    if _INDEX is None:
        _INDEX = build_index(fetch_tree_data(cache_dir))
    return _INDEX


# --------------------------------------------------------------------------- #
# Decoding
# --------------------------------------------------------------------------- #

class Bits:
    """Blizzard packs bits least-significant first, six to a character."""

    def __init__(self, s):
        self.s = s
        self.pos = 0

    def read(self, n):
        v = 0
        for i in range(n):
            ch = self.pos // 6
            if ch >= len(self.s):
                raise EOFError("end of loadout string")
            v |= ((IDX[self.s[ch]] >> (self.pos % 6)) & 1) << i
            self.pos += 1
        return v


def decode_header(code):
    b = Bits(code)
    version = b.read(8)
    spec_id = b.read(16)
    for _ in range(16):
        b.read(8)              # tree hash, not needed
    return version, spec_id, b


def hero_tree(code, cache_dir, index=None):
    """Return {'name', 'icon', 'atlas'} for the hero tree, or None if it can't be read."""
    if not code or len(code) < 40:
        return None
    try:
        index = index if index is not None else load_index(cache_dir)
        version, spec_id, b = decode_header(code)
        spec = index.get(spec_id)
        if not spec or not spec["node"]:
            return None
        for node_id in spec["order"]:
            selected = b.read(1)
            if not selected:
                continue
            purchased = b.read(1) if version >= 2 else 1
            if not purchased:
                continue
            partial = b.read(1)
            if partial:
                b.read(6)
            is_choice = b.read(1)
            choice = b.read(2) if is_choice else None
            if node_id == spec["node"]:
                if choice is None:
                    return None
                pick = spec["choices"].get(choice)
                return dict(pick) if pick else None
    except (EOFError, KeyError, ValueError):
        return None
    return None


# --------------------------------------------------------------------------- #
# CLI, for checking against real strings
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description="Decode the hero tree from a loadout string")
    ap.add_argument("--decode", help="a talent loadout string")
    ap.add_argument("--dump", action="store_true", help="list hero trees per spec")
    ap.add_argument("--cache", default="wcl_cache", help="where to keep the talent data")
    args = ap.parse_args()

    try:
        index = load_index(args.cache)
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        log(f"Couldn't fetch the talent data: {e}")
        sys.exit(1)
    log(f"Talent data covers {len(index)} specs.")

    if args.dump:
        for spec_id, s in sorted(index.items()):
            trees = ", ".join(f"{c['name']}" for c in s["choices"].values()) or "none"
            log(f"  {spec_id:>4} {s['spec']} {s['class']}: {trees}")

    if args.decode:
        version, spec_id, _ = decode_header(args.decode)
        s = index.get(spec_id, {})
        log(f"version {version}, spec {spec_id} ({s.get('spec','?')} {s.get('class','?')})")
        hero = hero_tree(args.decode, args.cache, index)
        log(f"hero tree: {hero}" if hero else "hero tree: could not read it")


if __name__ == "__main__":
    main()
