#!/usr/bin/env python3
"""
Mythic Raid Stat Sheet - Warcraft Logs top 10 per spec
======================================================

Pulls the top 10 Mythic kills for every spec on every boss of the current raid
from the Warcraft Logs API, reads each player's stats, trinkets and talent import
code from the log,
and builds a single HTML dashboard (mythic_stat_sheet.html) that opens in your browser.

Needs only Python 3.8+ (no extra installs).

How to run
----------
    python wcl_mythic_stats.py              # pick bosses from a menu
    python wcl_mythic_stats.py --all        # every boss, no menu
    python wcl_mythic_stats.py --refresh    # ignore cached rankings and fetch fresh ones
    python wcl_mythic_stats.py --offline    # just rebuild the page from what's already downloaded
    python wcl_mythic_stats.py --limit      # show how much of the hourly API allowance is left
    python wcl_mythic_stats.py --compact    # shrink the saved cache folder
    python wcl_mythic_stats.py --offline --addon   # also build the in-game addon folder
    python wcl_mythic_stats.py --list-zones # show raid zone IDs
    python wcl_mythic_stats.py --zone 44    # use a specific raid zone
    python wcl_mythic_stats.py --region EU  # only EU players (US, EU, KR, TW, CN)
    python wcl_mythic_stats.py --demo       # build a page with made-up data to preview the layout

Your API key
------------
The first time you run it, you'll be asked for your Warcraft Logs Client ID and
Client Secret (from https://www.warcraftlogs.com/api/clients). They are saved
to wcl_credentials.json next to this script so you only enter them once.
You can instead set the environment variables WCL_CLIENT_ID and WCL_CLIENT_SECRET.
Never share that file.

Caching
-------
Downloaded logs are saved in the wcl_cache folder. Logs never change, so re-runs
are much faster and use far fewer API points. Rankings are refreshed after 12 hours
(or immediately with --refresh). If the hourly API limit is reached, the script
waits and then carries on by itself. The page is rebuilt after every batch of logs,
and again if you stop with Ctrl+C, so you can look at partial results at any time.
"""

import argparse
import base64
import getpass
import hashlib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DEADLINE = None   # unix time to stop by, set with --deadline
MAX_NEW = 0       # cap on logs read per run, set with --max-new
STOP_AT_LIMIT = False  # stop instead of waiting for the hourly limit (--stop-at-limit)
ADDON_DIR = None  # where to write the in-game addon, set with --addon
INTERFACE = "120100,120105"
RETRY_TRIES = 6   # how many times to wait and retry when the API is unavailable


class TimeUp(Exception):
    """Raised when the run has used its allotted time (see --deadline)."""


class ApiDown(Exception):
    """Raised when Warcraft Logs can't be used right now (key rejected, site down)."""

CRED_FILE = os.path.join(HERE, "wcl_credentials.json")
CACHE_DIR = os.path.join(HERE, "wcl_cache")
OUT_FILE = os.path.join(HERE, "mythic_stat_sheet.html")
OPEN_BROWSER = True

TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
API_URL = "https://www.warcraftlogs.com/api/v2/client"

MYTHIC = 5
TOP_N = 10
RANKINGS_TTL = 12 * 3600   # how old saved rankings may be before rechecking
SPECS_PER_QUERY = 8
FIGHTS_PER_QUERY = 4

# Gear array positions used by Warcraft Logs combatant info
TRINKET_SLOTS = (12, 13)
IGNORED_ILVL_SLOTS = (3, 17)  # shirt, tabard

HEALER_SPECS = {
    ("Druid", "Restoration"), ("Shaman", "Restoration"), ("Paladin", "Holy"),
    ("Priest", "Holy"), ("Priest", "Discipline"), ("Monk", "Mistweaver"),
    ("Evoker", "Preservation"),
}
TANK_SPECS = {
    ("DeathKnight", "Blood"), ("DemonHunter", "Vengeance"), ("Druid", "Guardian"),
    ("Monk", "Brewmaster"), ("Paladin", "Protection"), ("Warrior", "Protection"),
}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def log(msg):
    print(msg, flush=True)


def time_left():
    return None if DEADLINE is None else DEADLINE - time.time()


def check_time():
    left = time_left()
    if left is not None and left <= 0:
        raise TimeUp()


def cache_path(kind, key):
    os.makedirs(os.path.join(CACHE_DIR, kind), exist_ok=True)
    safe = hashlib.sha1(key.encode()).hexdigest()[:20]
    return os.path.join(CACHE_DIR, kind, safe + ".json")


def cache_get(kind, key, ttl=None):
    p = cache_path(kind, key)
    if not os.path.exists(p):
        return None
    if ttl is not None and time.time() - os.path.getmtime(p) > ttl:
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def cache_put(kind, key, value):
    with open(cache_path(kind, key), "w", encoding="utf-8") as f:
        json.dump(value, f)


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def unwrap(j, key):
    """Warcraft Logs JSON scalars are sometimes wrapped in {"data": {...}}."""
    if isinstance(j, dict) and "data" in j and isinstance(j["data"], dict):
        j = j["data"]
    if isinstance(j, dict) and key in j:
        return j[key]
    return j


# --------------------------------------------------------------------------- #
# Credentials and API client
# --------------------------------------------------------------------------- #

def load_credentials():
    cid = os.environ.get("WCL_CLIENT_ID")
    secret = os.environ.get("WCL_CLIENT_SECRET")
    if cid and secret:
        return cid.strip(), secret.strip()
    if os.path.exists(CRED_FILE):
        with open(CRED_FILE, "r", encoding="utf-8") as f:
            c = json.load(f)
        return c["client_id"], c["client_secret"]
    log("\nFirst run: enter your Warcraft Logs API details")
    log("(create them at https://www.warcraftlogs.com/api/clients)\n")
    cid = input("Client ID: ").strip()
    secret = getpass.getpass("Client Secret (hidden as you type): ").strip()
    with open(CRED_FILE, "w", encoding="utf-8") as f:
        json.dump({"client_id": cid, "client_secret": secret}, f)
    log(f"Saved to {CRED_FILE}\n")
    return cid, secret


class WCL:
    def __init__(self, cid, secret):
        self.cid = cid
        self.secret = secret
        self.token = None
        self.calls = 0
        self.points = 0
        self.limit = 0
        self.talents_ok = True
        self.potions_ok = True
        self.potion_filter = 0

    def auth(self):
        basic = base64.b64encode(f"{self.cid}:{self.secret}".encode()).decode()
        req = urllib.request.Request(
            TOKEN_URL,
            data=b"grant_type=client_credentials",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                self.token = json.load(r)["access_token"]
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403):
                raise ApiDown("Warcraft Logs rejected the Client ID or Secret. "
                              f"Check them, or delete {CRED_FILE} and run again to re-enter them.")
            raise ApiDown(f"Warcraft Logs returned an error while signing in ({e.code}).")
        except urllib.error.URLError as e:
            raise ApiDown(f"Couldn't reach Warcraft Logs ({e}).")

    def query(self, q, variables=None, allow_errors=False, with_errors=False):
        if not self.token:
            self.auth()
        body = json.dumps({"query": q, "variables": variables or {}}).encode()
        for attempt in range(6):
            req = urllib.request.Request(
                API_URL,
                data=body,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    res = json.load(r)
                self.calls += 1
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    self.token = None
                    self.auth()
                    continue
                if e.code == 403:
                    raise ApiDown("Warcraft Logs refused this key (403). It may have been cancelled.")
                if e.code == 429:
                    self.wait_for_reset(force=True)
                    continue
                if e.code >= 500 and attempt < 5:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise ApiDown(f"Warcraft Logs returned an error ({e.code}).")
            except urllib.error.URLError as e:
                if attempt < 5:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise ApiDown(f"Couldn't reach Warcraft Logs ({e}).")
            if res.get("errors") and not allow_errors:
                msgs = "; ".join(e.get("message", "?") for e in res["errors"])
                if "rate limit" in msgs.lower():
                    self.wait_for_reset(force=True)
                    continue
                raise RuntimeError("Warcraft Logs API error: " + msgs)
            if with_errors:
                return res.get("data") or {}, res.get("errors") or []
            return res.get("data") or {}
        raise ApiDown("Warcraft Logs kept failing, so it's probably having trouble right now.")

    def wait_for_reset(self, force=False):
        try:
            d = self.query("{ rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn } }")
            rl = d["rateLimitData"]
        except Exception:
            rl = None
        if rl:
            self.points = int(rl["pointsSpentThisHour"])
            self.limit = int(rl["limitPerHour"])
            left = rl["limitPerHour"] - rl["pointsSpentThisHour"]
            if not force and left > 60:
                return
            wait = int(rl["pointsResetIn"]) + 5
        else:
            wait = 300
        left = time_left()
        if STOP_AT_LIMIT:
            log(f"\n  Hourly API limit reached ({wait // 60} min until it resets).")
            raise TimeUp()
        if left is not None and wait > left:
            raise TimeUp()
        log(f"\n  Hourly API limit reached. Waiting {wait // 60} min {wait % 60} s, then continuing...")
        log(f"  The page so far is in {OUT_FILE} - open it any time.")
        log("  (You can stop with Ctrl+C. Everything so far is saved.)\n")
        time.sleep(wait)


# --------------------------------------------------------------------------- #
# World data: raid, bosses, specs
# --------------------------------------------------------------------------- #

def get_zones(api):
    d = api.query("""
    { worldData { zones {
        id name frozen
        expansion { id name }
        difficulties { id }
        encounters { id name }
    } } }""")
    zones = []
    for z in d["worldData"]["zones"] or []:
        diffs = {x["id"] for x in (z.get("difficulties") or [])}
        encs = z.get("encounters") or []
        if MYTHIC in diffs and len(encs) >= 2 and "complete" not in z["name"].lower():
            zones.append(z)
    zones.sort(key=lambda z: ((z.get("expansion") or {}).get("id", 0), z["id"]))
    return zones


def get_specs(api):
    d = api.query("{ gameData { classes { id name slug specs { id name slug } } } }")
    specs = []
    for c in d["gameData"]["classes"]:
        for s in c["specs"]:
            key = (c["slug"], s["slug"])
            role = "healer" if key in HEALER_SPECS else "tank" if key in TANK_SPECS else "dps"
            specs.append({
                "classSlug": c["slug"], "className": c["name"],
                "specSlug": s["slug"], "specName": s["name"], "role": role,
            })
    return specs


# --------------------------------------------------------------------------- #
# Rankings
# --------------------------------------------------------------------------- #

def rank_key(enc_id, s, region):
    return f"rank|{enc_id}|{s['classSlug']}|{s['specSlug']}|{region or 'all'}"


def get_rankings(api, enc_id, specs, region, refresh, ttl=RANKINGS_TTL, stats=None):
    """Fetch the top 10 per spec. stats, if given, counts new and changed entries."""
    out = {}
    todo = []
    for s in specs:
        key = rank_key(enc_id, s, region)
        cached = None if refresh else cache_get("rankings", key, ttl)
        if cached is not None:
            out[(s["classSlug"], s["specSlug"])] = cached
        else:
            todo.append((s, key))

    reg = f', serverRegion: "{region}"' if region else ""
    for i in range(0, len(todo), SPECS_PER_QUERY):
        chunk = todo[i:i + SPECS_PER_QUERY]
        parts = []
        for n, (s, _) in enumerate(chunk):
            metric = "hps" if s["role"] == "healer" else "dps"
            parts.append(
                f'r{n}: characterRankings(className: "{s["classSlug"]}", '
                f'specName: "{s["specSlug"]}", difficulty: {MYTHIC}, '
                f'metric: {metric}, page: 1{reg})'
            )
        q = "query($id: Int) { worldData { encounter(id: $id) { " + " ".join(parts) + " } } }"
        api.wait_for_reset()
        d = api.query(q, {"id": enc_id})
        enc = (d.get("worldData") or {}).get("encounter") or {}
        for n, (s, key) in enumerate(chunk):
            raw = enc.get(f"r{n}") or {}
            ranks = unwrap(raw, "rankings")
            if not isinstance(ranks, list):
                ranks = []
            ranks = ranks[:TOP_N]
            if stats is not None:
                before = cache_get("rankings", key) or []
                old = {(r.get("name"), (r.get("report") or {}).get("code")) for r in before}
                fresh = [r for r in ranks if (r.get("name"), (r.get("report") or {}).get("code")) not in old]
                stats["checked"] += 1
                stats["new"] += len(fresh)
                if before and fresh:
                    stats["specs_changed"] += 1
            cache_put("rankings", key, ranks)
            out[(s["classSlug"], s["specSlug"])] = ranks
    return out


# --------------------------------------------------------------------------- #
# Fight details: stats and trinkets
# --------------------------------------------------------------------------- #

FIGHT_FIELDS = """
    masterData { actors(type: "Player") { id name } }
    events(fightIDs: [%(fid)d], dataType: CombatantInfo, limit: 100) { data }
"""


def find_actor(fight, name):
    for pl in (fight or {}).get("p") or []:
        if str(pl.get("name", "")).lower() == str(name).lower():
            return pl
    return None


def fetch_stats(api, chunk):
    parts = []
    for n, (code, fid) in enumerate(chunk):
        parts.append(f'f{n}: report(code: "{code}") {{ '
                     + (FIGHT_FIELDS % {"fid": int(fid)}) + " }")
    q = "{ reportData { " + " ".join(parts) + " } }"
    api.wait_for_reset()
    try:
        d = api.query(q, allow_errors=True)
    except ApiDown:
        raise
    except Exception as e:
        log(f"    Skipped {len(chunk)} logs ({e})")
        return
    rd = d.get("reportData") or {}
    for n, key in enumerate(chunk):
        rep = rd.get(f"f{n}")
        if not rep:
            continue  # private or deleted log; try again next run
        players = ((rep.get("masterData") or {}).get("actors")) or []
        events = ((rep.get("events") or {}).get("data")) or []
        cache_put("fights", f"{key[0]}|{key[1]}", compact_fight(players, events))


def fetch_talents(api, wanted):
    """wanted: {(code, fid): [actorID, ...]}. Saves import codes per fight."""
    parts = []
    index = []
    for n, ((code, fid), ids) in enumerate(wanted.items()):
        fields = " ".join(f"a{int(a)}: talentImportCode(actorID: {int(a)})" for a in ids)
        parts.append(f't{n}: report(code: "{code}") {{ fights(fightIDs: [{int(fid)}]) {{ id {fields} }} }}')
        index.append(((code, fid), ids))
    q = "{ reportData { " + " ".join(parts) + " } }"
    api.wait_for_reset()
    try:
        d, errors = api.query(q, allow_errors=True, with_errors=True)
    except ApiDown:
        raise
    except Exception as e:
        log(f"    Skipped talents for {len(wanted)} logs ({e})")
        return
    rd = (d or {}).get("reportData") or {}
    got_any = False
    for n, (key, ids) in enumerate(index):
        rep = rd.get(f"t{n}")
        if not rep:
            continue
        fights = rep.get("fights") or []
        f = fights[0] if fights else {}
        ckey = f"{key[0]}|{key[1]}"
        have = cache_get("talents", ckey) or {}
        for a in ids:
            have[str(a)] = f.get(f"a{int(a)}") or ""
        cache_put("talents", ckey, have)
        got_any = True
    if not got_any and errors:
        msg = "; ".join(e.get("message", "?") for e in errors)
        if "talentImportCode" in msg or "Cannot query field" in msg:
            api.talents_ok = False
            log("    Warcraft Logs didn't accept the talent request, so talents are skipped this run.")


# Potions show up as auras, and their buff names rarely say "potion"
# (Light's Potential, for instance), so they're matched by spell ID.
POTION_SEED_IDS = [1236616]          # Light's Potential
POTION_ICON_WORDS = ("potion", "alchemy")
POTION_NAME_WORDS = ("potion", "elixir", "draught", "tonic")
NOT_POTION_WORDS = ("flask", "phial", "food", "feast", "well fed", "rune", "oil",
                    "sharpening", "weightstone", "healthstone", "bandage")
POTION_IDS_TTL = 7 * 24 * 3600


def classify_potions(api, auras):
    """auras: {id: name}. Returns the IDs that look like potion buffs."""
    ids = sorted(auras)
    icons = {}
    for i in range(0, len(ids), 40):
        chunk = ids[i:i + 40]
        q = "{ gameData { " + " ".join(f"a{n}: ability(id: {a}) {{ id name icon }}"
                                       for n, a in enumerate(chunk)) + " } }"
        try:
            d, errors = api.query(q, allow_errors=True, with_errors=True)
        except Exception:
            break
        gd = (d or {}).get("gameData") or {}
        for n, a in enumerate(chunk):
            ab = gd.get(f"a{n}") or {}
            icons[a] = str(ab.get("icon") or "").lower()
            if ab.get("name"):
                auras[a] = ab["name"]
    out = []
    for a, name in auras.items():
        low = str(name or "").lower()
        if any(w in low for w in NOT_POTION_WORDS):
            continue
        if any(w in low for w in POTION_NAME_WORDS) or \
           any(w in icons.get(a, "") for w in POTION_ICON_WORDS):
            out.append(a)
    return out


def potion_ids(api, sample_fights):
    """Work out which auras are potions, from a few logs, and remember the answer."""
    cached = cache_get("potions", "ids", POTION_IDS_TTL)
    if cached:
        return cached
    auras = {}
    for code, fid in list(sample_fights)[:3]:
        q = ('{ reportData { report(code: "%s") { table(fightIDs: [%d], dataType: Buffs) } } }'
             % (code, int(fid)))
        try:
            d, errors = api.query(q, allow_errors=True, with_errors=True)
        except Exception:
            break
        tbl = unwrap((((d or {}).get("reportData") or {}).get("report") or {}).get("table") or {}, "data")
        for a in (tbl.get("auras") if isinstance(tbl, dict) else None) or []:
            if a.get("guid"):
                auras[int(a["guid"])] = a.get("name") or ""
    ids = sorted(set(classify_potions(api, auras)) | set(POTION_SEED_IDS)) if auras else list(POTION_SEED_IDS)
    cache_put("potions", "ids", ids)
    log(f"  Potion auras to look for: {len(ids)}")
    return ids


def fetch_potions(api, wanted, ids):
    """wanted: {(code, fid): [actorID, ...]}. Saves the potion buff each player had."""
    if not api.potions_ok or not ids:
        return
    id_list = ", ".join(str(int(i)) for i in ids)
    parts, index = [], []
    for n, ((code, fid), actor_ids) in enumerate(wanted.items()):
        flt = f"ability.id in ({id_list})".replace('"', '')
        parts.append(f'p{n}: report(code: "{code}") {{ events(fightIDs: [{int(fid)}], '
                     f'dataType: Buffs, limit: 400, useAbilityIDs: false, '
                     f'filterExpression: "{flt}") {{ data }} }}')
        index.append(((code, fid), actor_ids))
    q = "{ reportData { " + " ".join(parts) + " } }"
    api.wait_for_reset()
    try:
        d, errors = api.query(q, allow_errors=True, with_errors=True)
    except ApiDown:
        raise
    except Exception as e:
        log(f"    Skipped potions for {len(wanted)} logs ({e})")
        return
    rd = (d or {}).get("reportData") or {}
    if not any(rd.get(f"p{n}") for n in range(len(index))) and errors:
        api.potions_ok = False
        msg = "; ".join(e.get("message", "?") for e in errors)[:160]
        log(f"    Warcraft Logs wouldn't return potion auras, so they're skipped ({msg}).")
        return
    for n, (key, actor_ids) in enumerate(index):
        rep = rd.get(f"p{n}")
        if rep is None:
            continue
        events = ((rep.get("events") or {}).get("data")) or []
        have = cache_get("potionsv2", f"{key[0]}|{key[1]}") or {}
        for a in actor_ids:
            have.setdefault(str(a), "")
        for ev in events:
            who = ev.get("targetID", ev.get("sourceID"))
            if who is None or str(who) not in have or have[str(who)]:
                continue
            ability = ev.get("ability") if isinstance(ev.get("ability"), dict) else {}
            guid = ev.get("abilityGameID") or ability.get("guid")
            if guid:
                have[str(who)] = {"name": ability.get("name") or "", "id": int(guid)}
        cache_put("potionsv2", f"{key[0]}|{key[1]}", have)


def process_fights(api, fights, names_by_fight, on_batch=None):
    """fights: list of (code, fightID), most important first.
    Reads stats/trinkets for new logs, then talent codes for the ranked players."""
    def needs(key):
        fight = load_fight(key[0], key[1])
        if fight is None:
            return True
        for kind, on in (("talents", api.talents_ok), ("potions", api.potions_ok)):
            if not on:
                continue
            have = cache_get("potionsv2" if kind == "potions" else kind, f"{key[0]}|{key[1]}") or {}
            for nm in names_by_fight.get(key, ()):
                a = find_actor(fight, nm)
                if a and a.get("id") is not None and str(a["id"]) not in have:
                    return True
        return False

    todo = [k for k in fights if needs(k)]
    if MAX_NEW:
        todo = todo[:MAX_NEW]
    total = len(todo)
    if not total:
        log("  Everything is already loaded.")
        return
    log(f"Reading {total} logs for stats, trinkets, talents and potions...")
    done = 0
    for i in range(0, total, FIGHTS_PER_QUERY):
        chunk = todo[i:i + FIGHTS_PER_QUERY]
        new = [k for k in chunk if load_fight(k[0], k[1]) is None]
        if new:
            fetch_stats(api, new)
        pot_ids = potion_ids(api, todo) if api.potions_ok else []
        for kind, fetch in (("talents", fetch_talents), ("potions", fetch_potions)):
            if kind == "talents" and not api.talents_ok:
                continue
            if kind == "potions" and not api.potions_ok:
                continue
            wanted = {}
            for k in chunk:
                fight = load_fight(k[0], k[1])
                if fight is None:
                    continue
                have = cache_get("potionsv2" if kind == "potions" else kind, f"{k[0]}|{k[1]}") or {}
                ids = []
                for nm in names_by_fight.get(k, ()):
                    a = find_actor(fight, nm)
                    if a and a.get("id") is not None and str(a["id"]) not in have and a["id"] not in ids:
                        ids.append(a["id"])
                if ids:
                    wanted[k] = ids
            if wanted:
                if kind == "potions":
                    fetch(api, wanted, pot_ids)
                else:
                    fetch(api, wanted)
        done += len(chunk)
        check_time()
        pts = f"  (API points used this hour: {api.points}/{api.limit})" if api.limit else ""
        log(f"    Logs done: {done}/{total}{pts}")
        if on_batch:
            on_batch()


def compact_fight(players, events):
    """Keep only what the page needs, so the cache stays small."""
    by_id = {}
    for pl in players or []:
        if pl.get("id") is None:
            continue
        by_id[int(pl["id"])] = {"id": int(pl["id"]), "name": pl.get("name", ""),
                                "stats": None, "trinkets": []}
        ci = pl.get("combatantInfo") or {}
        if ci:
            gear = ci.get("gear") or []
            if gear:
                by_id[int(pl["id"])]["stats"] = stats_from_details(ci)
                by_id[int(pl["id"])]["trinkets"] = trinkets_from_gear(gear)
    for ev in events or []:
        sid = ev.get("sourceID")
        if sid is None:
            continue
        p = by_id.setdefault(int(sid), {"id": int(sid), "name": "", "stats": None, "trinkets": []})
        if ev.get("gear"):
            p["stats"] = stats_from_event(ev)
            p["trinkets"] = trinkets_from_gear(ev["gear"])
    return {"v": 2, "p": list(by_id.values())}


def load_fight(code, fid):
    """Read a fight from the cache, upgrading the old bulky format on the way."""
    c = cache_get("fights", f"{code}|{fid}")
    if c is None:
        return None
    if c.get("v") == 2:
        return c
    c = compact_fight(c.get("players"), c.get("events"))
    cache_put("fights", f"{code}|{fid}", c)
    return c


def trinkets_from_gear(gear):
    by_slot = {}
    for pos, g in enumerate(gear or []):
        slot = g.get("slot", pos)
        try:
            by_slot[int(slot)] = g
        except (TypeError, ValueError):
            pass
    out = []
    for sl in TRINKET_SLOTS:
        g = by_slot.get(sl)
        if g and g.get("id"):
            out.append({"id": int(g["id"]), "ilvl": num(g.get("itemLevel")), "name": g.get("name")})
    return out


def stats_from_event(ev):
    def pick(*keys):
        return max((num(ev.get(k)) for k in keys), default=0.0)
    prim = {"Strength": num(ev.get("strength")),
            "Agility": num(ev.get("agility")),
            "Intellect": max(num(ev.get("intelligence")), num(ev.get("intellect")))}
    pname = max(prim, key=prim.get)
    return {
        "primaryName": pname,
        "primary": prim[pname],
        "stamina": num(ev.get("stamina")),
        "crit": pick("critMelee", "critSpell", "critRanged"),
        "haste": pick("hasteMelee", "hasteSpell", "hasteRanged"),
        "mastery": num(ev.get("mastery")),
        "vers": pick("versatilityDamageDone", "versatilityHealingDone"),
        "leech": num(ev.get("leech")),
        "avoidance": num(ev.get("avoidance")),
        "speed": num(ev.get("speed")),
    }


def stats_from_details(ci):
    st = ci.get("stats") or {}

    def g(*names):
        for n in names:
            v = st.get(n)
            if isinstance(v, dict):
                return num(v.get("max", v.get("min")))
            if v is not None:
                return num(v)
        return 0.0
    prim = {"Strength": g("Strength"), "Agility": g("Agility"),
            "Intellect": g("Intellect", "Intelligence")}
    pname = max(prim, key=prim.get)
    return {
        "primaryName": pname, "primary": prim[pname], "stamina": g("Stamina"),
        "crit": g("Crit", "Critical Strike"), "haste": g("Haste"),
        "mastery": g("Mastery"), "vers": g("Versatility"),
        "leech": g("Leech"), "avoidance": g("Avoidance"), "speed": g("Speed"),
    }


def build_player(rank_entry, idx, fight, item_names):
    rep = rank_entry.get("report") or {}
    server = rank_entry.get("server") or {}
    guild = rank_entry.get("guild") or {}
    name = rank_entry.get("name", "?")
    p = {
        "rank": idx + 1,
        "name": name,
        "server": server.get("name", ""),
        "region": server.get("region", ""),
        "guild": guild.get("name", "") if isinstance(guild, dict) else "",
        "amount": num(rank_entry.get("amount")),
        "ilvl": num(rank_entry.get("bracketData")),
        "duration": num(rank_entry.get("duration")),
        "date": rank_entry.get("startTime") or rep.get("startTime") or 0,
        "report": rep.get("code", ""),
        "fight": rep.get("fightID", 0),
        "stats": None,
        "trinkets": [],
        "loaded": bool(fight),
        "talents": None,
        "potion": None,
    }
    if not fight:
        return p

    detail = find_actor(fight, name)
    if detail is None:
        p["talents"] = ""
    actor_id = detail.get("id") if detail else None
    if actor_id is not None and rep.get("code"):
        fkey = f"{rep['code']}|{int(rep.get('fightID') or 0)}"
        tal = cache_get("talents", fkey) or {}
        if str(actor_id) in tal:
            p["talents"] = tal[str(actor_id)]  # "" means the log had no talent code
        pot = cache_get("potionsv2", fkey) or {}
        if str(actor_id) in pot:
            p["potion"] = pot[str(actor_id)]   # "" means no potion was cast in the fight

    if detail:
        p["stats"] = detail.get("stats")
        p["trinkets"] = [dict(t) for t in detail.get("trinkets") or []]
        for t in p["trinkets"]:
            if t.get("id") and t.get("name"):
                item_names[int(t["id"])] = t["name"]

    return p


def fill_spell_names(api, ids):
    """Spell names for potions, so a cast with only an ID still reads properly."""
    known = {int(k): v for k, v in (cache_get("spells", "names") or {}).items()}
    missing = [i for i in ids if i and i not in known]
    for i in range(0, len(missing), 40):
        chunk = missing[i:i + 40]
        q = "{ gameData { " + " ".join(f"s{n}: ability(id: {sid}) {{ id name }}"
                                       for n, sid in enumerate(chunk)) + " } }"
        try:
            d = api.query(q, allow_errors=True)
        except Exception:
            break
        gd = (d or {}).get("gameData") or {}
        for n, sid in enumerate(chunk):
            ab = gd.get(f"s{n}")
            if ab and ab.get("name"):
                known[sid] = ab["name"]
    cache_put("spells", "names", {str(k): v for k, v in known.items()})
    return known


def fill_item_names(api, item_names, ids):
    missing = [i for i in ids if i not in item_names]
    cached = cache_get("items", "names") or {}
    for k, v in cached.items():
        item_names.setdefault(int(k), v)
    missing = [i for i in missing if i not in item_names]
    for i in range(0, len(missing), 40):
        chunk = missing[i:i + 40]
        q = "{ gameData { " + " ".join(f"i{n}: item(id: {iid}) {{ id name }}"
                                       for n, iid in enumerate(chunk)) + " } }"
        try:
            d = api.query(q, allow_errors=True)
        except Exception:
            break
        gd = d.get("gameData") or {}
        for n, iid in enumerate(chunk):
            it = gd.get(f"i{n}")
            if it and it.get("name"):
                item_names[iid] = it["name"]
    cache_put("items", "names", {str(k): v for k, v in item_names.items()})


# --------------------------------------------------------------------------- #
# Main flow
# --------------------------------------------------------------------------- #

def choose_bosses(encs, take_all):
    if take_all:
        return encs
    log("\nBosses:")
    for n, e in enumerate(encs, 1):
        log(f"  {n:>2}. {e['name']}")
    ans = input("\nWhich bosses? Numbers like 1,3,5 or press Enter for all: ").strip().lower()
    if not ans or ans == "all":
        return encs
    picked = []
    for part in ans.replace(" ", "").split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            rng = range(int(a), int(b) + 1)
        else:
            rng = [int(part)]
        for k in rng:
            if 1 <= k <= len(encs) and encs[k - 1] not in picked:
                picked.append(encs[k - 1])
    return picked or encs


def get_meta(api):
    """Raid zones and specs, cached so --offline can work without the API."""
    meta = cache_get("meta", "zones+specs")
    if meta is None or api is not None:
        if api is None:
            cid, secret = load_credentials()
            api = WCL(cid, secret)
        meta = {"zones": get_zones(api), "specs": get_specs(api)}
        cache_put("meta", "zones+specs", meta)
    return meta


def pick_zone(zones, zone_id):
    if zone_id:
        return next((z for z in zones if z["id"] == zone_id), None)
    live = [z for z in zones if not z.get("frozen")] or zones
    return live[-1] if live else None


def dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def compact_cache(region=None):
    """Rewrite every saved log in the small format, and drop ones no longer in a top 10."""
    fights_dir = os.path.join(CACHE_DIR, "fights")
    if not os.path.isdir(fights_dir):
        log("No saved logs found.")
        return
    before = dir_size(CACHE_DIR)
    files = sorted(os.listdir(fights_dir))
    done = shrunk = 0
    for fn in files:
        p = os.path.join(fights_dir, fn)
        try:
            with open(p, "r", encoding="utf-8") as f:
                c = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(c, dict) and c.get("v") != 2:
            c = compact_fight(c.get("players"), c.get("events"))
            with open(p, "w", encoding="utf-8") as f:
                json.dump(c, f)
            shrunk += 1
        done += 1
        if done % 200 == 0:
            log(f"  {done}/{len(files)} logs checked...")

    # Drop logs nobody in the current top 10 uses any more
    meta = cache_get("meta", "zones+specs")
    removed = 0
    if meta:
        keep = set()
        for z in meta["zones"]:
            for b in z.get("encounters") or []:
                for sp in meta["specs"]:
                    for r in cache_get("rankings", rank_key(b["id"], sp, region)) or []:
                        rp = r.get("report") or {}
                        if rp.get("code") and rp.get("fightID") is not None:
                            keep.add(hashlib.sha1(f"{rp['code']}|{int(rp['fightID'])}".encode()).hexdigest()[:20] + ".json")
        if keep:
            for kind in ("fights", "talents", "potions", "potionsv2"):
                d = os.path.join(CACHE_DIR, kind)
                for fn in os.listdir(d) if os.path.isdir(d) else []:
                    if fn not in keep:
                        try:
                            os.remove(os.path.join(d, fn))
                            removed += 1
                        except OSError:
                            pass
    after = dir_size(CACHE_DIR)
    log(f"\nRewrote {shrunk} logs in the small format and removed {removed} unused ones.")
    log(f"Cache is now {after/1e6:.1f} MB (was {before/1e6:.1f} MB).")


def prune_cache(zone, specs, region):
    """Delete saved logs that no longer appear in anyone's top 10."""
    keep = set()
    for b in zone["encounters"]:
        for s in specs:
            for r in cache_get("rankings", rank_key(b["id"], s, region)) or []:
                rep = r.get("report") or {}
                if rep.get("code") and rep.get("fightID") is not None:
                    keep.add(hashlib.sha1(f"{rep['code']}|{int(rep['fightID'])}".encode()).hexdigest()[:20] + ".json")
    removed = 0
    for kind in ("fights", "talents", "potions", "potionsv2"):
        d = os.path.join(CACHE_DIR, kind)
        for fn in os.listdir(d) if os.path.isdir(d) else []:
            if fn not in keep:
                try:
                    os.remove(os.path.join(d, fn))
                    removed += 1
                except OSError:
                    pass
    log(f"Removed {removed} saved logs that have dropped out of the top 10.")


def build_from_cache(zone, specs, bosses, region, api=None, addon=False):
    """Build the page using only saved data. Bosses with no saved rankings are left out."""
    item_names = {int(k): v for k, v in (cache_get("items", "names") or {}).items()}
    result = {"zone": zone["name"], "region": region or "All regions",
              "generated": datetime.now(timezone.utc).isoformat(), "bosses": []}
    loaded = total = tal_loaded = 0
    for b in bosses:
        boss_out = {"id": b["id"], "name": b["name"], "specs": []}
        any_ranks = False
        for s in specs:
            lst = cache_get("rankings", rank_key(b["id"], s, region))
            if lst is None:
                lst = []
            else:
                any_ranks = True
            players = []
            for i, r in enumerate(lst):
                rep = r.get("report") or {}
                fight = None
                if rep.get("code") and rep.get("fightID") is not None:
                    fight = load_fight(rep["code"], int(rep["fightID"]))
                p = build_player(r, i, fight, item_names)
                total += 1
                loaded += 1 if p["loaded"] else 0
                tal_loaded += 1 if p["talents"] is not None else 0
                players.append(p)
            boss_out["specs"].append({
                "cls": s["classSlug"], "className": s["className"],
                "spec": s["specName"], "role": s["role"],
                "metric": "HPS" if s["role"] == "healer" else "DPS",
                "players": players,
            })
        if any_ranks:
            result["bosses"].append(boss_out)

    spell_names = {int(k): v for k, v in (cache_get("spells", "names") or {}).items()}
    if api is not None:
        ids = sorted({t["id"] for b in result["bosses"] for s in b["specs"]
                      for p in s["players"] for t in p["trinkets"]})
        try:
            fill_item_names(api, item_names, ids)
        except Exception:
            pass
        sids = sorted({(p["potion"] or {}).get("id") for b in result["bosses"] for s in b["specs"]
                       for p in s["players"] if isinstance(p.get("potion"), dict)})
        try:
            spell_names = fill_spell_names(api, [i for i in sids if i])
        except Exception:
            pass
    for b in result["bosses"]:
        for s in b["specs"]:
            for p in s["players"]:
                for t in p["trinkets"]:
                    t["name"] = t.get("name") or item_names.get(t["id"])
                if isinstance(p.get("potion"), dict) and p["potion"].get("id"):
                    p["potion"]["name"] = (p["potion"].get("name")
                                           or spell_names.get(p["potion"]["id"], ""))
    result["progress"] = {"loaded": loaded, "total": total, "talents": tal_loaded}
    if ADDON_DIR:
        result["addon"] = "MythicStats.zip"
    write_page(result, open_browser=False)
    if addon and ADDON_DIR:
        try:
            write_addon(result, ADDON_DIR, INTERFACE)
        except Exception as e:
            log(f"Couldn't write the addon: {e}")
    return loaded, total


def test_potions(api, args):
    """Try several ways of asking for potion casts on one cached log and show what comes back."""
    if args.test_report and args.test_fight and args.test_source:
        sample = (args.test_report, args.test_fight, args.test_source, "that player")
        return _run_potion_tests(api, *sample)
    meta = cache_get("meta", "zones+specs")
    if not meta:
        log("Run a normal fetch first so there's some saved data to test with.")
        return
    zone = pick_zone(meta["zones"], args.zone)
    sample = None
    for b in zone["encounters"]:
        for sp in meta["specs"]:
            for r in cache_get("rankings", rank_key(b["id"], sp, args.region)) or []:
                rep = r.get("report") or {}
                if rep.get("code") and rep.get("fightID") is not None:
                    fight = load_fight(rep["code"], int(rep["fightID"]))
                    actor = find_actor(fight, r.get("name", "")) if fight else None
                    if actor and actor.get("id"):
                        sample = (rep["code"], int(rep["fightID"]), actor["id"], r.get("name"))
                        break
            if sample:
                break
        if sample:
            break
    if not sample:
        log("No saved log with a matching player was found to test with.")
        return
    return _run_potion_tests(api, *sample)


def _run_potion_tests(api, code, fid, actor_id, who):
    log(f"Testing on log {code}, fight {fid}, player {who} (actor {actor_id}).\n")

    ids = potion_ids(api, [(code, fid)])
    log(f"  Potion auras being matched: {ids}\n")
    flt = "ability.id in (" + ", ".join(str(i) for i in ids) + ")"
    q = ('{ reportData { report(code: "%s") { events(fightIDs: [%d], dataType: Buffs, limit: 50, '
         'useAbilityIDs: false, filterExpression: "%s") { data } } } }') % (code, fid, flt)
    d, errors = api.query(q, allow_errors=True, with_errors=True)
    if errors:
        log("  Buff lookup rejected: " + "; ".join(e.get('message', '?') for e in errors)[:200])
    else:
        data = ((((d or {}).get("reportData") or {}).get("report") or {}).get("events") or {}).get("data") or []
        log(f"  Potion buff events found: {len(data)}")
        for ev in data[:5]:
            log(f"      {json.dumps(ev)[:220]}")

    log("\n  Buffs the player had (potions show up here as auras):")
    q = ('{ reportData { report(code: "%s") { table(fightIDs: [%d], dataType: Buffs, '
         'sourceID: %d) } } }') % (code, fid, actor_id)
    d, errors = api.query(q, allow_errors=True, with_errors=True)
    if errors:
        log("      rejected: " + "; ".join(e.get('message', '?') for e in errors)[:200])
    else:
        tbl = (((d or {}).get("reportData") or {}).get("report") or {}).get("table")
        tbl = unwrap(tbl or {}, "data")
        auras = (tbl or {}).get("auras") if isinstance(tbl, dict) else None
        if isinstance(auras, list):
            for a in auras[:25]:
                log(f"      {a.get('guid')}  {a.get('name')}  x{a.get('totalUses', a.get('totalUptime', '?'))}")
        else:
            log(f"      {json.dumps(tbl)[:600]}")

    log("\n  Unfiltered casts, to see what the fields look like:")
    q = ('{ reportData { report(code: "%s") { events(fightIDs: [%d], dataType: Casts, '
         'sourceID: %d, limit: 5) { data } } } }') % (code, fid, actor_id)
    d, errors = api.query(q, allow_errors=True, with_errors=True)
    if errors:
        log("      rejected: " + "; ".join(e.get('message', '?') for e in errors)[:200])
    else:
        data = ((((d or {}).get("reportData") or {}).get("report") or {}).get("events") or {}).get("data") or []
        for ev in data[:5]:
            log(f"      {json.dumps(ev)[:200]}")
    log("\n  Fight summary (it sometimes lists consumables directly):")
    q = ('{ reportData { report(code: "%s") { table(fightIDs: [%d], dataType: Summary, '
         'sourceID: %d) } } }') % (code, fid, actor_id)
    d, errors = api.query(q, allow_errors=True, with_errors=True)
    if errors:
        log("      rejected: " + "; ".join(e.get('message', '?') for e in errors)[:200])
    else:
        tbl = (((d or {}).get("reportData") or {}).get("report") or {}).get("table")
        text = json.dumps(unwrap(tbl or {}, "data"))
        for word in ("potion", "Potion"):
            i = text.find(word)
            if i > 0:
                log(f"      ...{text[max(0, i - 200):i + 300]}...")
                break
        else:
            log(f"      nothing mentioning potions. First part: {text[:400]}")
    log("\nSend these lines to Claude and the potion lookup can be fixed to match.")


def rebuild_offline(args):
    """Build the page from saved data alone. Used when the API can't be reached."""
    meta = cache_get("meta", "zones+specs")
    if not meta:
        return False
    zone = pick_zone(meta["zones"], args.zone)
    if not zone:
        return False
    try:
        loaded, total = build_from_cache(zone, meta["specs"], zone["encounters"], args.region, addon=True)
    except Exception:
        return False
    log(f"Page saved with {loaded} of {total} players loaded.")
    return True


def run(args):
    if args.test_potions:
        cid, secret = load_credentials()
        api = WCL(cid, secret)
        api.auth()
        test_potions(api, args)
        return

    if args.compact:
        compact_cache(args.region)
        return

    if args.limit:
        cid, secret = load_credentials()
        api = WCL(cid, secret)
        api.auth()
        rl = api.query("{ rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn } }")["rateLimitData"]
        left = rl["limitPerHour"] - rl["pointsSpentThisHour"]
        mins = int(rl["pointsResetIn"]) // 60
        log(f"Client ID {cid[:8]}...: {rl['pointsSpentThisHour']:.0f} of {rl['limitPerHour']} points used "
            f"this hour, {left:.0f} left, resets in {mins} min.")
        return

    if args.offline:
        meta = cache_get("meta", "zones+specs")
        if meta is None:
            log("Looking up the raid and spec list (one small request)...")
            meta = get_meta(None)
        zone = pick_zone(meta["zones"], args.zone)
        if not zone:
            log("Raid not found. Use --list-zones to see options.")
            return
        loaded, total = build_from_cache(zone, meta["specs"], zone["encounters"], args.region, addon=True)
        log(f"Page built with {loaded} of {total} players' stats loaded.")
        open_page()
        return

    cid, secret = load_credentials()
    api = WCL(cid, secret)
    log("Connecting to Warcraft Logs...")
    api.auth()
    meta = get_meta(api)
    zones, specs = meta["zones"], meta["specs"]

    if args.list_zones:
        for z in zones:
            log(f"  {z['id']:>5}  {z['name']}  ({(z.get('expansion') or {}).get('name', '')})"
                + ("  [old]" if z.get("frozen") else ""))
        return
    zone = pick_zone(zones, args.zone)
    if not zone:
        log(f"Zone {args.zone} not found. Use --list-zones to see options.")
        return
    log(f"Raid: {zone['name']}")
    bosses = choose_bosses(zone["encounters"], args.all)

    def rebuild():
        build_from_cache(zone, specs, zone["encounters"], args.region)

    opened = False
    attempt = 0
    while True:
        try:
            # Rankings for every chosen boss first, so the page lists all specs straight away
            ranks_by_boss = {}
            rstats = {"checked": 0, "new": 0, "specs_changed": 0}
            ttl = args.rank_age * 3600 if args.rank_age is not None else RANKINGS_TTL
            for b in bosses:
                log(f"{b['name']}: checking top 10 per spec...")
                ranks_by_boss[b["id"]] = get_rankings(api, b["id"], specs, args.region,
                                                      args.refresh, ttl, rstats)
            if rstats["checked"]:
                log(f"\nChecked {rstats['checked']} spec rankings: "
                    f"{rstats['new']} new entries across {rstats['specs_changed']} specs.")
            rebuild()
            if not opened:
                open_page()
                opened = True
                log("\nThe page is built and fills in as logs are read. "
                    "Refresh your browser to see new data.")

            # Then logs, highest-ranked players first across all bosses and specs
            best = {}
            names = {}
            for ranks in ranks_by_boss.values():
                for lst in ranks.values():
                    for i, r in enumerate(lst):
                        rep = r.get("report") or {}
                        if rep.get("code") and rep.get("fightID") is not None:
                            k = (rep["code"], int(rep["fightID"]))
                            best[k] = min(best.get(k, 99), i)
                            names.setdefault(k, set()).add(r.get("name", ""))
            fights = sorted(best, key=lambda k: best[k])
            process_fights(api, fights, names, on_batch=rebuild)
            if args.prune:
                prune_cache(zone, specs, args.region)
            loaded, total = build_from_cache(zone, specs, zone["encounters"], args.region, api, addon=True)
            log(f"\nDone: {loaded} of {total} players loaded ({api.calls} API requests).")
            log("Refresh the page in your browser to see everything.")
            return

        except (KeyboardInterrupt, TimeUp) as e:
            loaded, total = build_from_cache(zone, specs, zone["encounters"], args.region, addon=True)
            why = "Stopped." if isinstance(e, KeyboardInterrupt) else (
                "Used up this hour's API allowance." if STOP_AT_LIMIT else "Out of time for this run.")
            log(f"\n{why} Page saved with {loaded} of {total} players loaded.")
            log("Run the script again later to carry on from here.")
            if not opened:
                open_page()
            return

        except ApiDown as e:
            loaded, total = build_from_cache(zone, specs, zone["encounters"], args.region, addon=True)
            if not opened:
                open_page()
                opened = True
            log(f"\nWarcraft Logs isn't usable right now: {e}")
            log(f"Page saved with {loaded} of {total} players loaded.")
            attempt += 1
            wait = min(1800, 120 * 2 ** (attempt - 1))
            left = time_left()
            if left is not None and wait + 120 > left:
                log("No time left in this run to wait for it. Saving and stopping.")
                return
            if left is None and attempt > RETRY_TRIES:
                log(f"Gave up after {RETRY_TRIES} tries. Run the script again later.")
                return
            log(f"Waiting {wait // 60} min, then trying again (try {attempt}).")
            try:
                time.sleep(wait)
            except KeyboardInterrupt:
                log("\nStopped.")
                return
            log("Trying Warcraft Logs again...")


# --------------------------------------------------------------------------- #
# Demo data
# --------------------------------------------------------------------------- #

DEMO_CLASSES = {
    "DeathKnight": ("Death Knight", ["Blood", "Frost", "Unholy"]),
    "DemonHunter": ("Demon Hunter", ["Havoc", "Vengeance"]),
    "Druid": ("Druid", ["Balance", "Feral", "Guardian", "Restoration"]),
    "Evoker": ("Evoker", ["Augmentation", "Devastation", "Preservation"]),
    "Hunter": ("Hunter", ["Beast Mastery", "Marksmanship", "Survival"]),
    "Mage": ("Mage", ["Arcane", "Fire", "Frost"]),
    "Monk": ("Monk", ["Brewmaster", "Mistweaver", "Windwalker"]),
    "Paladin": ("Paladin", ["Holy", "Protection", "Retribution"]),
    "Priest": ("Priest", ["Discipline", "Holy", "Shadow"]),
    "Rogue": ("Rogue", ["Assassination", "Outlaw", "Subtlety"]),
    "Shaman": ("Shaman", ["Elemental", "Enhancement", "Restoration"]),
    "Warlock": ("Warlock", ["Affliction", "Demonology", "Destruction"]),
    "Warrior": ("Warrior", ["Arms", "Fury", "Protection"]),
}
DEMO_PRIMARY = {"DeathKnight": "Strength", "Warrior": "Strength", "DemonHunter": "Agility",
                "Hunter": "Agility", "Rogue": "Agility"}


def demo(args):
    rnd = random.Random(7)
    trinkets = [(219314, "Demo Trinket A"), (219917, "Demo Trinket B"), (220202, "Demo Trinket C"),
                (225577, "Demo Trinket D"), (219312, "Demo Trinket E"), (212683, "Demo Trinket F")]
    bosses = []
    for bname in ["Sample Boss One", "Sample Boss Two", "Sample Boss Three"]:
        specs = []
        for slug, (cname, sl) in DEMO_CLASSES.items():
            for sp in sl:
                key = (slug, sp)
                role = "healer" if key in HEALER_SPECS else "tank" if key in TANK_SPECS else "dps"
                prim = DEMO_PRIMARY.get(slug, "Intellect")
                if slug in ("Paladin",) and sp != "Holy":
                    prim = "Strength"
                if slug in ("Druid", "Monk", "Shaman") and sp in ("Feral", "Guardian", "Brewmaster", "Windwalker", "Enhancement"):
                    prim = "Agility"
                bias = [rnd.random() for _ in range(4)]
                abc = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
                builds = ["C" + "".join(rnd.choice(abc) for _ in range(118)) for _ in range(4)]
                base = 1000000 if role == "dps" else 600000 if role == "healer" else 450000
                pl = []
                amt = base * rnd.uniform(1.1, 1.5)
                for i in range(TOP_N):
                    amt *= rnd.uniform(0.95, 0.995)
                    w = [b * rnd.uniform(0.85, 1.15) + 0.3 for b in bias]
                    tot = 38000 * rnd.uniform(0.97, 1.03)
                    sw = sum(w)
                    t1, t2 = rnd.sample(trinkets[:4] if i < 6 else trinkets, 2)
                    pl.append({
                        "rank": i + 1, "name": f"Player{rnd.randint(100, 999)}",
                        "server": rnd.choice(["Tarren Mill", "Area 52", "Draenor", "Illidan"]),
                        "region": rnd.choice(["EU", "US"]),
                        "guild": rnd.choice(["Sample Guild", "Demo Raiders", "Test Team"]),
                        "amount": round(amt), "ilvl": round(rnd.uniform(668, 676), 1),
                        "duration": rnd.randint(280000, 520000),
                        "date": int(time.time() * 1000) - rnd.randint(0, 20) * 86400000,
                        "report": "demo", "fight": 1,
                        "stats": {"primaryName": prim, "primary": rnd.randint(40000, 46000),
                                  "stamina": rnd.randint(380000, 420000),
                                  "crit": round(tot * w[0] / sw), "haste": round(tot * w[1] / sw),
                                  "mastery": round(tot * w[2] / sw), "vers": round(tot * w[3] / sw),
                                  "leech": 0, "avoidance": 0, "speed": 0},
                        "talents": builds[min(i // 3, 3)] if i != 7 else None,
                        "trinkets": [{"id": t1[0], "ilvl": 678, "name": t1[1]},
                                     {"id": t2[0], "ilvl": 675, "name": t2[1]}],
                    })
                specs.append({"cls": slug, "className": cname, "spec": sp, "role": role,
                              "metric": "HPS" if role == "healer" else "DPS", "players": pl})
        bosses.append({"id": len(bosses) + 1, "name": bname, "specs": specs})
    write_page({"zone": "Sample Raid (made-up data)", "region": "All regions", "demo": True,
                "generated": datetime.now(timezone.utc).isoformat(), "bosses": bosses})
    log(f"Demo page written: {OUT_FILE}")


# --------------------------------------------------------------------------- #
# Page output
# --------------------------------------------------------------------------- #

def open_page():
    if not OPEN_BROWSER:
        log(f"Page written to {OUT_FILE}")
        return
    try:
        webbrowser.open("file://" + os.path.abspath(OUT_FILE))
    except Exception:
        pass


ADDON_ICON_TGA = "AAACAAAAAAAAAAAAQABAABgAHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBgVHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHRkXHBgVHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHRkXGxQPGAwFGAsFGAsFGAsFGAsFGAsFGAsFGAsFGAsFGAsFGAsFGAsFGAsFGAsFGAsFGAsFGAsFGAsFGAsFGAsFGAsFGAsFGAsFGAsFGAsFGAsFGAsFGAsFGAsF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KSIfKSIfKSIfKSIfKSIfKSQhJhkTNU1YNGJ2GAsFHRkXHBcUHBcUHBcUHBcUHBcUHBcUHRkWGQ0HKkRPPGh8JhcRKiQiKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSMgKB8cJxkUJxoUJxkUJxkUJxkUJxoUJxkTKB0ZKSMgKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSQhJhkTNU1YNGJ2GAsFHRkXHBcUHBcUHBcUHBcUHBcUHBcUHRkWGQ0HKkRPPGh8JhcRKiQiKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSMgKSQiKSQhKSQhKSQhKSQhKSQhKSQiKSMgKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSQhJhkTNU1YNGJ2GAsFHRkXHBcUHBcUHBcUHBcUHBcUHBcUHRkWGQ0HKkRPPGh8JhcRKiQiKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSQhJhkTNU1YNGJ2GAsFHRkXHBcUHBcUHBcUHBcUHBcUHBcUHRkWGQ0HKkRPPGh8JhcRKiQiKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSQhJhkTNU1YNGJ2GAsFHRkXHBcUHBcUHBcUHBcUHBcUHBcUHRkWGQ0HKkRPPGh8JhcRKiQiKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSQhJhkTNU1YNGJ2GAsFHRkXHBcUHBcUHBcUHBcUHBcUHBcUHRkWGQ0HKkNOPGh9JhcRKiQiKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSQhJhkTNU5ZNGF1GAsFHRkXHBcUHBcUHBcUHBcUHBcUHBcUHRkWGQ0GK0ZSO2Z5JhcRKiQiKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSQhJhoUNEpUNWR4GAsFHRkXHBcUHBcUHBcUHBcUHBcUHBcUHRkWGhALJjY9P3OLJxkTKiUjKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKSIfKiUjJhcROVxsMFVmGAwFHRkXHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBgVGhINPnyYMT1DJRQNKiUjKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiQiKiUjJxwXKSIfQoWjICQmGxMPHBgVHBcUHBcUHBcUHBcUHBcUHBcUHBcUHRgWGhEMIy0xQoOhMT1DJxkTJhcQJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhcRJhgRLC0tQXyXLElWGQ0HHRkXHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBkWGhEMIy0xPnyYPnOLO2Z5PGh9PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8PGh8O2h8O2d7PGyCQIKgKkJNGg8KHBkWHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHRkWGhEMGhINJjY9K0ZSKkNPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPKkRPK0VRKD5IHBkWGQ4IHBgWHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBgWHBgVGhALGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ0HGQ4JHBYTHRkWHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBgWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHRkWHBgWHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUHBcUAAAAAAAAAABUUlVFVklTSU9OLVhGSUxFLgA="

LUA_CLASS_FILE = ('MythicStatsDB = MythicStatsDB or {}\n'
                  'MythicStatsDB.classes = MythicStatsDB.classes or {}\n'
                  'MythicStatsDB.classes["%s"] = %s\n')


def lua_str(v):
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def lua_value(v, indent=0):
    pad = "  " * indent
    if isinstance(v, dict):
        parts = [f'{pad}  [{lua_str(k)}] = {lua_value(x, indent + 1)}' for k, x in v.items()]
        return "{\n" + ",\n".join(parts) + f"\n{pad}}}"
    if isinstance(v, (list, tuple)):
        parts = [f'{pad}  {lua_value(x, indent + 1)}' for x in v]
        return "{\n" + ",\n".join(parts) + f"\n{pad}}}"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return f"{v:.10g}"
    if v is None:
        return "nil"
    return lua_str(v)


def write_addon(data, addon_dir, interface):
    """Write a World of Warcraft addon folder with the same data as the page."""
    addon_dir = os.path.abspath(addon_dir)
    data_dir = os.path.join(addon_dir, "data")
    os.makedirs(data_dir, exist_ok=True)

    with open(os.path.join(addon_dir, "icon.tga"), "wb") as f:
        f.write(base64.b64decode(ADDON_ICON_TGA))

    core_src = os.path.join(HERE, "addon", "core.lua")
    core = open(core_src, encoding="utf-8").read() if os.path.exists(core_src) else ADDON_CORE_LUA
    with open(os.path.join(addon_dir, "core.lua"), "w", encoding="utf-8") as f:
        f.write(core)

    bosses = [b["name"] for b in data["bosses"]]
    gen = data["generated"][:16].replace("T", " ") + " UTC"
    header = ("MythicStatsDB = MythicStatsDB or {}\n"
              f'MythicStatsDB.zone = {lua_str(data["zone"])}\n'
              f'MythicStatsDB.updated = {lua_str(gen)}\n'
              f'MythicStatsDB.region = {lua_str(data["region"])}\n'
              f'MythicStatsDB.bosses = {lua_value(bosses)}\n')
    with open(os.path.join(data_dir, "_info.lua"), "w", encoding="utf-8") as f:
        f.write(header)

    # One file per class, so the game parses less at a time
    classes = {}
    for b in data["bosses"]:
        for sp in b["specs"]:
            c = classes.setdefault(sp["cls"], {"className": sp["className"], "specs": {}})
            entry = c["specs"].setdefault(sp["spec"], {"spec": sp["spec"], "role": sp["role"],
                                                       "metric": sp["metric"], "players": []})
            for p in sp["players"]:
                st = p.get("stats") or {}
                entry["players"].append({
                    "name": p["name"], "guild": p.get("guild") or "", "boss": b["name"],
                    "amount": round(p.get("amount") or 0), "ilvl": round(p.get("ilvl") or 0, 1),
                    "crit": round(st.get("crit") or 0) or None,
                    "haste": round(st.get("haste") or 0) or None,
                    "mastery": round(st.get("mastery") or 0) or None,
                    "vers": round(st.get("vers") or 0) or None,
                    "trinkets": [{"id": t["id"], "name": t.get("name") or "",
                                  "ilvl": round(t.get("ilvl") or 0)}
                                 for t in p.get("trinkets") or []],
                    "potion": (p.get("potion") or None) and
                              {"id": (p["potion"] or {}).get("id") or 0,
                               "name": (p["potion"] or {}).get("name") or ""},
                    "talents": p.get("talents") or "",
                })
    files = ["data\\_info.lua"]
    for cls, c in sorted(classes.items()):
        payload = {"className": c["className"], "specs": list(c["specs"].values())}
        with open(os.path.join(data_dir, cls + ".lua"), "w", encoding="utf-8") as f:
            f.write(LUA_CLASS_FILE % (cls, lua_value(payload)))
        files.append(f"data\\{cls}.lua")

    toc = [f"## Interface: {', '.join(x.strip() for x in str(interface).split(','))}",
           "## Title: Mythic Stat Sheet",
           f"## Notes: Top 10 Mythic logs per spec. Data from {gen}.",
           "## Version: 1.0",
           r"## IconTexture: Interface\AddOns\MythicStats\icon",
           ""] + files + ["core.lua", ""]
    with open(os.path.join(addon_dir, "MythicStats.toc"), "w", encoding="utf-8") as f:
        f.write("\n".join(toc))
    log(f"Addon written to {addon_dir}")
    return addon_dir


def write_page(data, open_browser=True):
    global OUT_FILE
    tpl_path = os.path.join(HERE, "stat_sheet_template.html")
    if os.path.exists(tpl_path):
        with open(tpl_path, "r", encoding="utf-8") as f:
            tpl = f.read()
    else:
        tpl = PAGE_TEMPLATE
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    html = tpl.replace("/*__DATA__*/null", payload)
    tmp = OUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, OUT_FILE)
    if open_browser:
        open_page()


ADDON_CORE_LUA = r"""-- MythicStats: top 10 Mythic logs per spec, with stats, trinkets, potions and talent codes.
-- Data lives in the data/*.lua files, written by wcl_mythic_stats.py.

MythicStatsDB = MythicStatsDB or {}
local DB = MythicStatsDB

local CLASS_ORDER = {
  "DeathKnight", "DemonHunter", "Druid", "Evoker", "Hunter", "Mage", "Monk",
  "Paladin", "Priest", "Rogue", "Shaman", "Warlock", "Warrior",
}
local QUESTION = "Interface\\Icons\\INV_Misc_QuestionMark"
local ROW_H, SIDE_W, SIDE_ROW = 62, 210, 32
local SIDE_PAD = 30            -- room for the scroll bar down the right of the list

local state = { key = nil, boss = 1 }
local specs, byKey, iconFor, classFileFor = {}, {}, {}, {}
local main, sideButtons, bossButtons, rows = nil, {}, {}, {}

----------------------------------------------------------------------
-- Lookups
----------------------------------------------------------------------
local function classColor(slug)
  local file = classFileFor[slug] or slug:upper()
  local c = RAID_CLASS_COLORS and RAID_CLASS_COLORS[file]
  return c or { r = 0.9, g = 0.9, b = 0.9 }
end

local function colorCode(slug)
  local c = classColor(slug)
  return string.format("|cff%02x%02x%02x",
    math.floor(c.r * 255 + 0.5), math.floor(c.g * 255 + 0.5), math.floor(c.b * 255 + 0.5))
end

-- Spec icons and class file names come from the game, matched up by name
local function buildLookups()
  local slugByFile = {}
  for _, slug in ipairs(CLASS_ORDER) do slugByFile[slug:upper()] = slug end
  local n = (GetNumClasses and GetNumClasses()) or 13
  for i = 1, n do
    local _, classFile, classID = GetClassInfo(i)
    local slug = classFile and slugByFile[classFile]
    if slug then
      classFileFor[slug] = classFile
      local count = (GetNumSpecializationsForClassID and GetNumSpecializationsForClassID(classID)) or 0
      for s = 1, count do
        local _, specName, _, icon = GetSpecializationInfoForClassID(classID, s)
        if specName and icon then iconFor[slug .. "|" .. specName] = icon end
      end
    end
  end
end

local function buildSpecList()
  specs, byKey = {}, {}
  for _, slug in ipairs(CLASS_ORDER) do
    local cd = DB.classes and DB.classes[slug]
    if cd then
      for _, sp in ipairs(cd.specs) do
        local entry = {
          key = slug .. "|" .. sp.spec, cls = slug, className = cd.className,
          spec = sp.spec, role = sp.role, metric = sp.metric, players = sp.players,
        }
        specs[#specs + 1] = entry
        byKey[entry.key] = entry
      end
    end
  end
end

local function playerSpecKey()
  local _, classFile = UnitClass("player")
  local slug
  for _, s in ipairs(CLASS_ORDER) do if s:upper() == classFile then slug = s end end
  if not slug then return nil end
  local idx = GetSpecialization and GetSpecialization()
  if idx then
    local _, specName = GetSpecializationInfo(idx)
    if specName and byKey[slug .. "|" .. specName] then return slug .. "|" .. specName end
  end
  for _, e in ipairs(specs) do if e.cls == slug then return e.key end end
end

----------------------------------------------------------------------
-- Formatting
----------------------------------------------------------------------
local function fmtAmount(v)
  if not v or v == 0 then return "-" end
  if v >= 1e6 then return string.format("%.2fM", v / 1e6) end
  if v >= 1e3 then return string.format("%.1fK", v / 1e3) end
  return tostring(math.floor(v))
end

local function comma(v)
  if not v or v == 0 then return "-" end
  local out = tostring(math.floor(v)):reverse():gsub("(%d%d%d)", "%1,"):reverse()
  return (out:gsub("^,", ""))
end

local function itemIcon(id)
  if C_Item and C_Item.GetItemIconByID then
    local icon = C_Item.GetItemIconByID(id)
    if icon then return icon end
  end
  if GetItemIcon then return GetItemIcon(id) end
end

local function itemName(id, fallback)
  if C_Item and C_Item.GetItemNameByID then
    local n = C_Item.GetItemNameByID(id)
    if n then return n end
  end
  if fallback and fallback ~= "" then return fallback end
  return "Item " .. id
end

local function spellName(id)
  if C_Spell and C_Spell.GetSpellInfo then
    local info = C_Spell.GetSpellInfo(id)
    if info and info.name then return info.name end
  end
  if GetSpellInfo then return (GetSpellInfo(id)) end
end

local function spellIcon(id)
  if C_Spell and C_Spell.GetSpellInfo then
    local info = C_Spell.GetSpellInfo(id)
    if info and info.iconID then return info.iconID end
  end
  if GetSpellTexture then return GetSpellTexture(id) end
end

----------------------------------------------------------------------
-- Copy window
----------------------------------------------------------------------
local copyFrame
local function showCopy(text, label)
  if not copyFrame then
    local f = CreateFrame("Frame", "MythicStatsCopyFrame", UIParent, "BasicFrameTemplateWithInset")
    f:SetSize(470, 150)
    f:SetPoint("CENTER")
    f:SetFrameStrata("DIALOG")
    f:SetMovable(true); f:EnableMouse(true); f:RegisterForDrag("LeftButton")
    f:SetScript("OnDragStart", f.StartMoving)
    f:SetScript("OnDragStop", f.StopMovingOrSizing)
    f.TitleText:SetText("Talent code")

    f.info = f:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
    f.info:SetPoint("TOPLEFT", 14, -32)
    f.info:SetPoint("TOPRIGHT", -14, -32)
    f.info:SetJustifyH("LEFT")

    local box = CreateFrame("EditBox", nil, f, "InputBoxTemplate")
    box:SetPoint("TOPLEFT", 18, -72)
    box:SetPoint("TOPRIGHT", -18, -72)
    box:SetHeight(24)
    box:SetAutoFocus(false)
    box:SetFontObject(ChatFontNormal)
    box:SetScript("OnEscapePressed", function() f:Hide() end)
    box:SetScript("OnEditFocusGained", function(self) self:HighlightText() end)
    box:SetScript("OnTextChanged", function(self, user)
      if user then self:SetText(self.value or ""); self:HighlightText() end
    end)
    f.box = box

    f.hint = f:CreateFontString(nil, "OVERLAY", "GameFontDisableSmall")
    f.hint:SetPoint("TOPLEFT", 18, -104)
    f.hint:SetPoint("TOPRIGHT", -18, -104)
    f.hint:SetJustifyH("LEFT")
    f.hint:SetText("Press Ctrl+C to copy, then open your talents, click the loadout dropdown and choose Import.")
    copyFrame = f
  end
  copyFrame.info:SetText(label or "")
  copyFrame.box.value = text
  copyFrame.box:SetText(text)
  copyFrame:Show()
  copyFrame.box:SetFocus()
  copyFrame.box:HighlightText()
end

----------------------------------------------------------------------
-- Icon plus name, used for trinkets and potions
----------------------------------------------------------------------
local function makeSlot(parent, x, y, width)
  local f = CreateFrame("Button", nil, parent)
  f:SetSize(width, 18)
  f:SetPoint("TOPLEFT", x, y)
  f.icon = f:CreateTexture(nil, "ARTWORK")
  f.icon:SetSize(16, 16)
  f.icon:SetPoint("LEFT", 0, 0)
  f.icon:SetTexCoord(0.08, 0.92, 0.08, 0.92)
  f.border = f:CreateTexture(nil, "BACKGROUND")
  f.border:SetPoint("TOPLEFT", f.icon, -1, 1)
  f.border:SetPoint("BOTTOMRIGHT", f.icon, 1, -1)
  f.border:SetColorTexture(0, 0, 0, 0.8)
  f.text = f:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
  f.text:SetPoint("LEFT", f.icon, "RIGHT", 6, 0)
  f.text:SetPoint("RIGHT", 0, 0)
  f.text:SetJustifyH("LEFT")
  f:SetScript("OnEnter", function(self)
    if not self.tipID or self.tipID == 0 then return end
    GameTooltip:SetOwner(self, "ANCHOR_RIGHT")
    if self.tipKind == "spell" then GameTooltip:SetSpellByID(self.tipID)
    else GameTooltip:SetItemByID(self.tipID) end
    GameTooltip:Show()
  end)
  f:SetScript("OnLeave", function() GameTooltip:Hide() end)
  f:Hide()
  return f
end

local function fillItemSlot(slot, entry)
  if not entry or not entry.id or entry.id == 0 then slot:Hide() return end
  slot.icon:SetTexture(itemIcon(entry.id) or QUESTION)
  local ilvl = (entry.ilvl and entry.ilvl > 0) and ("  |cff9d9d9d" .. entry.ilvl .. "|r") or ""
  slot.text:SetText(itemName(entry.id, entry.name) .. ilvl)
  slot.tipID, slot.tipKind = entry.id, "item"
  slot:Show()
end

local function fillPotionSlot(slot, potion, loaded)
  local name = potion and potion.name
  if (not name or name == "") and potion and potion.id and potion.id > 0 then
    name = spellName(potion.id)
  end
  if name and name ~= "" then
    slot.icon:SetTexture((potion.id and potion.id > 0 and spellIcon(potion.id)) or QUESTION)
    slot.text:SetText("|cff8fd6ffPotion:|r " .. name)
    slot.tipID, slot.tipKind = potion.id or 0, "spell"
  else
    slot.icon:SetTexture(QUESTION)
    slot.text:SetText(loaded and "|cff808080Potion: none cast in the fight|r"
      or "|cff808080Potion: not loaded yet|r")
    slot.tipID = nil
  end
  slot:Show()
end

----------------------------------------------------------------------
-- Content
----------------------------------------------------------------------
local function current() return state.key and byKey[state.key] end

local function playersFor(sp)
  if not sp then return {} end
  local bossName = DB.bosses and DB.bosses[state.boss]
  local out = {}
  for _, p in ipairs(sp.players or {}) do
    if state.boss == 0 or p.boss == bossName then out[#out + 1] = p end
  end
  table.sort(out, function(a, b) return (a.amount or 0) > (b.amount or 0) end)
  return out
end

local function updateRows()
  local sp = current()
  local players = playersFor(sp)
  for i, row in ipairs(rows) do
    local p = players[i]
    if p and sp then
      row.rank:SetText("#" .. i)
      row.name:SetText(colorCode(sp.cls) .. p.name .. "|r  |cff9d9d9d" .. (p.guild or "") .. "|r")
      row.top:SetText(string.format("%s %s   ilvl %.1f   |cff9d9d9d%s|r",
        fmtAmount(p.amount), sp.metric or "DPS", p.ilvl or 0, p.boss or ""))
      if p.crit then
        row.stats:SetText(string.format(
          "|cffff6b5bCrit|r %s   |cfff4c542Haste|r %s   |cffa98cffMastery|r %s   |cff3ecf9aVers|r %s",
          comma(p.crit), comma(p.haste), comma(p.mastery), comma(p.vers)))
      else
        row.stats:SetText("|cff808080No stats in this log|r")
      end
      fillItemSlot(row.t1, p.trinkets and p.trinkets[1])
      fillItemSlot(row.t2, p.trinkets and p.trinkets[2])
      fillPotionSlot(row.pot, p.potion, p.potion ~= nil)
      row.copy:SetShown(p.talents and p.talents ~= "")
      row.copy.code = p.talents
      row.copy.label = string.format("%s %s, rank %d on %s", sp.spec, sp.className, i, p.boss or "")
      row:Show()
    else
      row:Hide()
    end
  end
  if main then
    main.empty:SetShown(#players == 0)
    main.TitleText:SetText("Mythic Stat Sheet - " .. (DB.zone or ""))
    if sp then
      main.sub:SetText(string.format("%s%s %s|r   top 10 %s   |cff808080updated %s|r",
        colorCode(sp.cls), sp.spec, sp.className,
        state.boss == 0 and "across all bosses" or ("on " .. ((DB.bosses or {})[state.boss] or "")),
        DB.updated or "?"))
    end
  end
end

local function updateSide()
  for _, b in ipairs(sideButtons) do
    b.sel:SetShown(b.key == state.key)
  end
end

local function select(key)
  state.key = key
  updateSide(); updateRows()
end

local function buildSide(parent)
  for i, e in ipairs(specs) do
    local b = CreateFrame("Button", nil, parent)
    b:SetSize(SIDE_W - SIDE_PAD, SIDE_ROW)
    b:SetPoint("TOPLEFT", 0, -(i - 1) * SIDE_ROW)
    b.key = e.key

    b.sel = b:CreateTexture(nil, "BACKGROUND")
    b.sel:SetAllPoints()
    b.sel:SetColorTexture(1, 1, 1, 0.1)
    b.sel:Hide()
    b:SetHighlightTexture("Interface\\QuestFrame\\UI-QuestTitleHighlight", "ADD")

    b.icon = b:CreateTexture(nil, "ARTWORK")
    b.icon:SetSize(24, 24)
    b.icon:SetPoint("LEFT", 4, 0)
    b.icon:SetTexCoord(0.08, 0.92, 0.08, 0.92)
    b.icon:SetTexture(iconFor[e.key] or QUESTION)

    b.spec = b:CreateFontString(nil, "OVERLAY", "GameFontNormal")
    b.spec:SetPoint("TOPLEFT", b.icon, "TOPRIGHT", 7, 1)
    b.spec:SetText(e.spec)
    local c = classColor(e.cls)
    b.spec:SetTextColor(c.r, c.g, c.b)

    b.class = b:CreateFontString(nil, "OVERLAY", "GameFontDisableSmall")
    b.class:SetPoint("TOPLEFT", b.spec, "BOTTOMLEFT", 0, -1)
    b.class:SetText(e.className)

    b:SetScript("OnClick", function(self) select(self.key) end)
    sideButtons[#sideButtons + 1] = b
  end
end

local BOSS_TOP, BOSS_H, PER_LINE = -54, 23, 5

local function bossLines()
  return math.ceil((1 + #(DB.bosses or {})) / PER_LINE)
end

local function buildBossButtons()
  local names = { "All bosses" }
  for i, n in ipairs(DB.bosses or {}) do names[i + 1] = n end
  for i, n in ipairs(names) do
    local b = bossButtons[i]
    if not b then
      b = CreateFrame("Button", nil, main, "UIPanelButtonTemplate")
      b:SetSize(136, 20)
      local col, line = (i - 1) % PER_LINE, math.floor((i - 1) / PER_LINE)
      b:SetPoint("TOPLEFT", SIDE_W + 26 + col * 140, BOSS_TOP - line * BOSS_H)
      bossButtons[i] = b
    end
    b:SetText(n)
    b:SetScript("OnClick", function() state.boss = i - 1; updateRows(); buildBossButtons() end)
    b:SetEnabled(state.boss ~= i - 1)
    b:Show()
  end
end

local function createMain()
  local f = CreateFrame("Frame", "MythicStatsFrame", UIParent, "BasicFrameTemplateWithInset")
  f:SetSize(1000, 700)   -- height is worked out below, once the boss rows are known
  f:SetPoint("CENTER")
  f:SetMovable(true); f:EnableMouse(true); f:RegisterForDrag("LeftButton")
  f:SetScript("OnDragStart", f.StartMoving)
  f:SetScript("OnDragStop", f.StopMovingOrSizing)
  tinsert(UISpecialFrames, "MythicStatsFrame")
  main = f

  f.sub = f:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
  f.sub:SetPoint("TOPLEFT", SIDE_W + 26, -34)
  f.sub:SetJustifyH("LEFT")

  -- Scrolling list of every spec
  local scroll = CreateFrame("ScrollFrame", "MythicStatsSideScroll", f, "UIPanelScrollFrameTemplate")
  scroll:SetPoint("TOPLEFT", 12, -34)
  local child = CreateFrame("Frame", nil, scroll)
  child:SetSize(SIDE_W - SIDE_PAD, math.max(1, #specs * SIDE_ROW))
  scroll:SetScrollChild(child)
  buildSide(child)
  child:SetHeight(math.max(1, #specs * SIDE_ROW))
  if scroll.UpdateScrollChildRect then scroll:UpdateScrollChildRect() end

  buildBossButtons()

  f.empty = f:CreateFontString(nil, "OVERLAY", "GameFontDisable")
  f.empty:SetPoint("CENTER", SIDE_W / 2, 0)
  f.empty:SetText("No rankings for this spec on this boss.")

  local top = BOSS_TOP - bossLines() * BOSS_H - 10
  local height = math.abs(top) + 10 * (ROW_H + 3) + 22
  f:SetSize(1000, height)
  scroll:SetSize(SIDE_W - SIDE_PAD, height - 46)
  for i = 1, 10 do
    local row = CreateFrame("Frame", nil, f)
    row:SetSize(750, ROW_H)
    row:SetPoint("TOPLEFT", SIDE_W + 26, top - (i - 1) * (ROW_H + 3))

    row.bg = row:CreateTexture(nil, "BACKGROUND")
    row.bg:SetAllPoints()
    row.bg:SetColorTexture(1, 1, 1, i % 2 == 0 and 0.03 or 0.06)

    row.rank = row:CreateFontString(nil, "OVERLAY", "GameFontNormalLarge")
    row.rank:SetPoint("TOPLEFT", 6, -5)
    row.rank:SetWidth(36)

    row.name = row:CreateFontString(nil, "OVERLAY", "GameFontNormal")
    row.name:SetPoint("TOPLEFT", 46, -5)
    row.name:SetJustifyH("LEFT")

    row.top = row:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
    row.top:SetPoint("TOPRIGHT", -104, -5)
    row.top:SetJustifyH("RIGHT")

    row.stats = row:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
    row.stats:SetPoint("TOPLEFT", 46, -23)
    row.stats:SetJustifyH("LEFT")

    row.t1 = makeSlot(row, 46, -40, 210)
    row.t2 = makeSlot(row, 262, -40, 210)
    row.pot = makeSlot(row, 478, -40, 180)

    row.copy = CreateFrame("Button", nil, row, "UIPanelButtonTemplate")
    row.copy:SetSize(92, 22)
    row.copy:SetPoint("TOPRIGHT", -6, -22)
    row.copy:SetText("Talents")
    row.copy:SetScript("OnClick", function(self) showCopy(self.code, self.label) end)

    rows[i] = row
  end
  f:Hide()
end

----------------------------------------------------------------------
-- Open, close, and following your spec
----------------------------------------------------------------------
local function scrollToSelected()
  local scroll = MythicStatsSideScroll
  if not scroll then return end
  local index
  for i, b in ipairs(sideButtons) do
    if b.key == state.key then index = i break end
  end
  if not index then return end
  -- The scroll frame doesn't know its own range until it has been drawn once,
  -- so work it out first, then scroll on the next frame and clamp to the range.
  local function go()
    if scroll.UpdateScrollChildRect then scroll:UpdateScrollChildRect() end
    local want = math.max(0, (index - 4) * SIDE_ROW)
    local range = scroll:GetVerticalScrollRange() or 0
    if range > 0 and want > range then want = range end
    scroll:SetVerticalScroll(want)
  end
  go()
  if C_Timer and C_Timer.After then C_Timer.After(0, go) end
end

local function toggle()
  if not DB.classes then
    print("|cff3fc7ebMythicStats|r: no data found. Download a newer copy of the addon.")
    return
  end
  if not main then
    buildLookups(); buildSpecList()
    createMain()
    state.key = playerSpecKey() or (specs[1] and specs[1].key)
    select(state.key)
  end
  if main:IsShown() then
    main:Hide()
  else
    state.key = playerSpecKey() or state.key   -- follow the spec you're in now
    select(state.key)
    main:Show()
    scrollToSelected()
  end
end

SLASH_MYTHICSTATS1 = "/mythicstats"
SLASH_MYTHICSTATS2 = "/ms"
SlashCmdList["MYTHICSTATS"] = toggle

local loader = CreateFrame("Frame")
loader:RegisterEvent("PLAYER_LOGIN")
loader:RegisterEvent("PLAYER_SPECIALIZATION_CHANGED")
loader:SetScript("OnEvent", function(_, event, unit)
  if event == "PLAYER_LOGIN" then
    print("|cff3fc7ebMythicStats|r loaded. Type |cffffff00/ms|r for the top 10 per spec. Data from "
      .. (DB.updated or "?") .. ".")
  elseif unit == "player" and main and main:IsShown() then
    local key = playerSpecKey()
    if key then select(key); scrollToSelected() end
  end
end)
"""


PAGE_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Mythic Stat Sheet</title>
<link rel="icon" type="image/png" href="data:image/png;base64,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">
<link rel="apple-touch-icon" href="data:image/png;base64,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">
<script>const whTooltips = {colorLinks: true, iconizeLinks: true, renameLinks: true, iconSize: 'small'};</script>
<script src="https://wow.zamimg.com/js/tooltips.js"></script>
<style>
:root{
  color-scheme: light dark;
  --sans: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI Variable", "Segoe UI", Inter, system-ui, sans-serif;
  --crit:#ff5f52; --haste:#f0b400; --mastery:#8b5cf6; --vers:#00b37e;
  --r: 18px; --r-sm: 11px;
  --gap: clamp(14px, 1.5vw, 26px);
  --rail: clamp(210px, 17vw, 300px);
}
/* Light */
:root{
  --bg:#f2f2f6; --bg-2:#e8e8ef;
  --glass: rgba(255,255,255,.66);
  --glass-2: rgba(255,255,255,.82);
  --stroke: rgba(0,0,0,.08);
  --stroke-2: rgba(0,0,0,.12);
  --ink:#16161a; --dim:#65656e; --faint:#9a9aa3;
  --shadow: 0 1px 2px rgba(0,0,0,.04), 0 8px 24px rgba(0,0,0,.06);
  --hover: rgba(0,0,0,.04);
  --track: rgba(0,0,0,.08);
}
html[data-theme="dark"]{
  --bg:#0b0b0e; --bg-2:#14141a;
  --glass: rgba(28,28,34,.62);
  --glass-2: rgba(36,36,44,.76);
  --stroke: rgba(255,255,255,.09);
  --stroke-2: rgba(255,255,255,.16);
  --ink:#f2f2f5; --dim:#a0a0ab; --faint:#6e6e78;
  --shadow: 0 1px 2px rgba(0,0,0,.3), 0 12px 40px rgba(0,0,0,.4);
  --hover: rgba(255,255,255,.06);
  --track: rgba(255,255,255,.12);
}
*{box-sizing:border-box}
html{font-size:clamp(14px, .38vw + 10.2px, 18px)}
html,body{margin:0;height:100%}
body{
  background:
    radial-gradient(60rem 40rem at 12% -10%, color-mix(in srgb, var(--crit) 12%, transparent), transparent 60%),
    radial-gradient(52rem 34rem at 92% 4%, color-mix(in srgb, var(--vers) 13%, transparent), transparent 62%),
    radial-gradient(46rem 32rem at 60% 100%, color-mix(in srgb, var(--mastery) 10%, transparent), transparent 60%),
    linear-gradient(var(--bg), var(--bg-2));
  background-attachment: fixed;
  color:var(--ink); font-family:var(--sans); line-height:1.45;
  -webkit-font-smoothing:antialiased;
}
a{color:inherit}
button,select,input{font:inherit;color:inherit}
:focus-visible{outline:2px solid color-mix(in srgb, var(--cc, var(--ink)) 70%, transparent); outline-offset:2px}
.num{font-variant-numeric:tabular-nums}

.glass{
  background:var(--glass);
  -webkit-backdrop-filter:saturate(180%) blur(22px);
  backdrop-filter:saturate(180%) blur(22px);
  border:1px solid var(--stroke);
  box-shadow:var(--shadow);
  border-radius:var(--r);
}

/* Shell */
.app{display:grid;grid-template-rows:auto 1fr;height:100vh;height:100dvh}
.top{
  display:flex;align-items:center;gap:var(--gap);
  padding:.7rem clamp(12px,2vw,26px);
  background:var(--glass);
  -webkit-backdrop-filter:saturate(180%) blur(22px);
  backdrop-filter:saturate(180%) blur(22px);
  border-bottom:1px solid var(--stroke);
  min-width:0;
}
.raid{flex:0 0 auto;min-width:0}
.raid h1{margin:0;font-size:1.12rem;font-weight:650;letter-spacing:-.02em;white-space:nowrap}
.raid p{margin:.1rem 0 0;color:var(--dim);font-size:.76rem;display:flex;flex-wrap:wrap;gap:.2rem .55rem;align-items:center}
.stamp{display:inline-flex;align-items:center;gap:.35rem}
.stamp i{width:.42rem;height:.42rem;border-radius:50%;background:var(--vers)}
.stamp.old i{background:var(--haste)} .stamp.stale i{background:var(--crit)}
.warn{color:var(--haste)}
.dl{color:var(--dim);text-decoration:none;border-bottom:1px solid var(--stroke-2)}
.dl:hover{color:var(--ink)}

.bosses{display:flex;gap:.25rem;overflow-x:auto;min-width:0;flex:1;scrollbar-width:none}
.bosses::-webkit-scrollbar{display:none}
.bosses button{
  flex:0 0 auto;border:1px solid transparent;background:transparent;border-radius:999px;
  padding:.3rem .8rem;color:var(--dim);cursor:pointer;font-size:.86rem;white-space:nowrap;
  transition:background .15s, color .15s;
}
.bosses button:hover{background:var(--hover);color:var(--ink)}
.bosses button[aria-selected="true"]{background:var(--glass-2);border-color:var(--stroke);color:var(--ink);font-weight:550}
.theme{
  flex:0 0 auto;width:2rem;height:2rem;border-radius:50%;border:1px solid var(--stroke);
  background:var(--glass-2);cursor:pointer;display:grid;place-items:center;font-size:.9rem;
}
.theme:hover{background:var(--hover)}

.body{display:grid;grid-template-columns:var(--rail) minmax(0,1fr);min-height:0;gap:var(--gap);
  padding:var(--gap) clamp(12px,2vw,26px) var(--gap)}

/* Rail */
.rail{overflow:hidden;display:flex;flex-direction:column;min-height:0;padding:.7rem}
.rail-tools{display:grid;gap:.45rem;padding:.1rem .1rem .6rem}
.rail input{
  width:100%;background:var(--glass-2);border:1px solid var(--stroke);border-radius:999px;
  padding:.4rem .85rem;font-size:.86rem;
}
.rail input::placeholder{color:var(--faint)}
.roles{display:grid;grid-template-columns:repeat(4,1fr);background:var(--track);border-radius:999px;padding:2px}
.roles button{border:0;background:none;padding:.26rem 0;border-radius:999px;cursor:pointer;color:var(--dim);font-size:.78rem}
.roles button[aria-pressed="true"]{background:var(--glass-2);color:var(--ink);box-shadow:0 1px 2px rgba(0,0,0,.08)}
.rail-list{overflow-y:auto;min-height:0;padding-right:.2rem;scrollbar-width:thin}
.overview-link{
  display:flex;align-items:center;gap:.55rem;width:100%;border:0;background:none;text-align:left;
  padding:.45rem .55rem;cursor:pointer;border-radius:var(--r-sm);font-size:.88rem;font-weight:550;color:var(--ink);
}
.overview-link:hover{background:var(--hover)}
.overview-link[aria-current="true"]{background:var(--glass-2);box-shadow:inset 0 0 0 1px var(--stroke)}
.overview-link .grid-ico{display:grid;grid-template-columns:repeat(2,5px);gap:2px}
.overview-link .grid-ico i{width:5px;height:5px;border-radius:1px}
.cls{margin-top:.55rem}
.cls h3{margin:0;padding:.15rem .55rem;font-size:.66rem;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--faint)}
.spec-btn{
  display:grid;grid-template-columns:1fr auto;gap:.5rem;align-items:center;width:100%;
  border:0;background:none;text-align:left;padding:.3rem .55rem;cursor:pointer;border-radius:var(--r-sm);
  font-size:.88rem;color:var(--ink);
}
.spec-btn:hover{background:var(--hover)}
.spec-btn[aria-current="true"]{background:color-mix(in srgb, var(--cc) 16%, transparent);box-shadow:inset 0 0 0 1px color-mix(in srgb, var(--cc) 40%, transparent)}
.spec-btn .dot{width:.45rem;height:.45rem;border-radius:50%;background:var(--cc);display:inline-block;margin-right:.45rem;vertical-align:.04rem}
.spec-btn .mini{display:flex;width:clamp(34px,3vw,56px);height:5px;border-radius:3px;overflow:hidden;background:var(--track)}
.spec-btn .mini span{height:100%}
.spec-btn.nodata{color:var(--faint)}
.rail-empty{padding:.8rem .6rem;color:var(--dim);font-size:.86rem}

/* Main */
main{overflow-y:auto;min-height:0;container-type:inline-size;container-name:main;border-radius:var(--r)}
.pad{padding:clamp(14px,1.8vw,28px)}
.empty{padding:4rem 1rem;color:var(--dim);text-align:center}
.demo{display:inline-block;margin-bottom:1rem;padding:.15rem .7rem;border:1px solid var(--stroke-2);border-radius:999px;font-size:.78rem;color:var(--dim)}

/* Overview */
.ov-head{margin-bottom:1.1rem;max-width:62ch}
.ov-head h2{margin:0;font-size:clamp(1.5rem,2.4vw,2.1rem);font-weight:680;letter-spacing:-.025em}
.ov-head p{margin:.35rem 0 0;color:var(--dim);font-size:.84rem}
.heat-wrap{overflow-x:auto}
.heat{width:100%;border-collapse:separate;border-spacing:0 6px}
.heat .sh{display:none}
.heat th{
  font-weight:500;color:var(--dim);font-size:.74rem;text-align:left;padding:.2rem .7rem;
  cursor:pointer;white-space:nowrap;user-select:none;letter-spacing:.02em;
}
.heat th.on{color:var(--ink)}
.heat th .sw{display:inline-block;width:.5rem;height:.5rem;border-radius:2px;margin-right:.35rem}
.heat tr{cursor:pointer}
.heat td{padding:0 .7rem;height:2.7rem;background:var(--glass);border-top:1px solid var(--stroke);border-bottom:1px solid var(--stroke);white-space:nowrap;transition:background .15s}
.heat td:first-child{border-left:1px solid var(--stroke);border-radius:var(--r-sm) 0 0 var(--r-sm);position:relative;padding-left:1rem}
.heat td:first-child::before{content:"";position:absolute;left:.45rem;top:50%;transform:translateY(-50%);width:.4rem;height:.4rem;border-radius:50%;background:var(--cc)}
.heat td:last-child{border-right:1px solid var(--stroke);border-radius:0 var(--r-sm) var(--r-sm) 0}
.heat tbody tr:hover td{background:var(--glass-2)}
.heat .sp b{font-weight:600;font-size:.94rem;margin-right:.4rem}
.heat .sp small{color:var(--dim);font-size:.78rem}
.heat td.cell{padding:4px;width:10.5%}
.heat .cell div{height:calc(2.7rem - 8px);border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:.82rem;font-weight:600}
.heat .cell.hi div{box-shadow:inset 0 0 0 1.5px color-mix(in srgb, var(--ink) 45%, transparent)}
.heat .prio{color:var(--dim);font-size:.82rem}
.heat .tk{max-width:22ch;overflow:hidden;text-overflow:ellipsis;font-size:.82rem}
.heat .tk a{text-decoration:none}
.heat .nod{color:var(--faint);font-size:.8rem}
@container main (max-width:900px){ .heat .col-prio{display:none} }
@container main (max-width:720px){ .heat .col-tk{display:none} .heat .lg{display:none} .heat .sh{display:inline} }
@container main (max-width:520px){ .heat .col-il{display:none} .heat .sp small{display:none} .heat td.cell{width:auto} .heat td,.heat th{padding-left:.4rem;padding-right:.4rem} .heat .sw{display:none} }

/* Spec page */
.hero{
  padding:clamp(1.1rem,2vw,2rem) clamp(14px,1.8vw,28px);
  border-radius:var(--r) var(--r) 0 0;
  background:linear-gradient(140deg, color-mix(in srgb, var(--cc) 20%, transparent), transparent 62%);
  border-bottom:1px solid var(--stroke);
}
.hero-grid{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:1rem 2.4rem}
.hero h2{margin:0;font-size:clamp(2rem,4.4cqi,3.4rem);font-weight:700;letter-spacing:-.03em;line-height:1}
.hero .who{margin-top:.3rem;color:var(--dim);font-size:.9rem}
.facts{display:flex;flex-wrap:wrap;gap:.5rem clamp(1.1rem,2.2vw,2.6rem)}
.fact b{display:block;font-size:clamp(1.3rem,2.2cqi,1.9rem);font-weight:650;letter-spacing:-.02em;line-height:1.1}
.fact span{color:var(--dim);font-size:.76rem}
.split-big{margin-top:clamp(.9rem,1.6vw,1.5rem)}
.split-bar{display:flex;height:clamp(24px,2.2vw,34px);border-radius:10px;overflow:hidden;background:var(--track)}
.split-bar span{display:flex;align-items:center;padding:0 .6rem;min-width:0;overflow:hidden;white-space:nowrap;font-weight:600;font-size:.82rem}
.split-note{display:flex;flex-wrap:wrap;justify-content:space-between;gap:.3rem 1rem;margin-top:.45rem;font-size:.82rem;color:var(--dim)}
.split-note strong{color:var(--ink);font-weight:600}

.panels{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr));gap:var(--gap);margin-top:var(--gap)}
.panel{padding:1rem 1.1rem}
.panel h3{margin:0 0 .7rem;font-size:1rem;font-weight:620;letter-spacing:-.01em}
.panel h3 small{font-size:.76rem;color:var(--dim);margin-left:.4rem;font-weight:400;letter-spacing:0}

.tk-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:.2rem .8rem;align-items:center;padding:.3rem 0}
.tk-row .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.88rem}
.tk-row .nm a{text-decoration:none}
.tk-row .ct{color:var(--dim);font-size:.8rem}
.tk-row .bar{grid-column:1 / -1;height:4px;border-radius:2px;background:var(--track);overflow:hidden}
.tk-row .bar i{display:block;height:100%;background:var(--cc);border-radius:2px}

.rng{display:grid;grid-template-columns:5.4rem minmax(0,1fr) auto;gap:.7rem;align-items:center;padding:.4rem 0}
.rng .lb{font-size:.85rem;color:var(--dim)}
.rng .track{position:relative;height:16px}
.rng .track::before{content:"";position:absolute;left:0;right:0;top:7px;height:2px;background:var(--track);border-radius:2px}
.rng .dot{position:absolute;top:3px;width:9px;height:9px;margin-left:-4.5px;border-radius:50%;opacity:.8}
.rng .avg{position:absolute;top:0;width:2px;height:16px;margin-left:-1px;background:var(--ink);border-radius:2px;opacity:.75}
.rng .vals{text-align:right;font-size:.84rem}
.rng .vals small{display:block;color:var(--faint);font-size:.7rem;white-space:nowrap}

.players-head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:.6rem;margin:calc(var(--gap) * 1.2) 0 .7rem}
.players-head h3{margin:0;font-size:1.05rem;font-weight:620}
.players-head select{background:var(--glass-2);border:1px solid var(--stroke);border-radius:999px;padding:.3rem .8rem;font-size:.84rem}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,290px),1fr));gap:calc(var(--gap) * .7)}
.card{padding:.85rem .95rem;display:flex;flex-direction:column;gap:.6rem}
.card.first{box-shadow:var(--shadow), inset 0 0 0 1px color-mix(in srgb, var(--cc) 45%, transparent)}
.c-top{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:.6rem;align-items:start}
.rank{font-size:1.3rem;font-weight:660;line-height:1.1;color:var(--faint);min-width:1.4ch}
.card.first .rank{color:var(--cc)}
.nm2{min-width:0}
.nm2 b{display:block;font-weight:600;font-size:.95rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nm2 small{display:block;color:var(--dim);font-size:.74rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.out{text-align:right}
.out b{display:block;font-size:1.1rem;font-weight:650;letter-spacing:-.02em;line-height:1.1}
.out small{color:var(--dim);font-size:.7rem}
.sbars{display:grid;gap:.26rem}
.sb{display:grid;grid-template-columns:3.6rem minmax(0,1fr) 3.6rem 2.3rem;gap:.45rem;align-items:center;font-size:.8rem}
.sb .t{height:6px;border-radius:3px;background:var(--track);overflow:hidden}
.sb .t i{display:block;height:100%;border-radius:3px}
.sb .v{text-align:right}
.sb .p{text-align:right;color:var(--faint)}
.prim{display:flex;justify-content:space-between;font-size:.78rem;color:var(--dim)}
.prim b{color:var(--ink);font-weight:550}
.c-tk{display:grid;gap:.15rem;font-size:.84rem;border-top:1px solid var(--stroke);padding-top:.55rem}
.c-tk div{display:flex;justify-content:space-between;gap:.6rem;min-width:0}
.c-tk a{text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.c-tk small{color:var(--faint);flex:0 0 auto}
.c-tal{border-top:1px solid var(--stroke);padding-top:.55rem;display:grid;gap:.3rem}
.c-tal .h{font-size:.72rem;color:var(--faint);letter-spacing:.04em;text-transform:uppercase}
.tal{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:.35rem;align-items:center}
.tal code{
  display:block;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  font-family:ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;font-size:.72rem;color:var(--dim);
  background:var(--track);border-radius:7px;padding:.25rem .45rem;user-select:all;
}
.copy{border:1px solid var(--stroke);background:var(--glass-2);border-radius:999px;padding:.2rem .65rem;cursor:pointer;font-size:.78rem;font-weight:550}
.copy:hover{background:var(--hover)}
.copy.done{background:var(--vers);border-color:transparent;color:#fff}
.view{font-size:.78rem;color:var(--dim);white-space:nowrap;text-decoration:none}
.view:hover{color:var(--ink)}
.c-foot{display:flex;flex-wrap:wrap;gap:.15rem .8rem;font-size:.74rem;color:var(--faint);margin-top:auto}
.c-foot a{color:var(--dim)}
.pending{color:var(--faint);font-size:.82rem;padding:.3rem 0}
.build{padding:.5rem 0;border-bottom:1px solid var(--stroke);display:grid;gap:.3rem}
.build:last-child{border-bottom:0}
.build-top{display:flex;justify-content:space-between;align-items:baseline;gap:.7rem;font-size:.86rem}
.build-top b{font-weight:600}
.build-top .share{height:4px;flex:1;max-width:38%;border-radius:2px;background:var(--track);overflow:hidden;align-self:center}
.build-top .share i{display:block;height:100%;background:var(--cc)}
.build small{color:var(--faint);font-size:.74rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

@media (max-width:780px){
  .app{height:auto;display:block}
  .top{flex-wrap:wrap}
  .bosses{order:3;flex-basis:100%;padding-top:.3rem}
  .body{display:block;padding:var(--gap) 12px}
  .rail{margin-bottom:var(--gap)}
  .rail-list{display:flex;gap:.3rem;overflow-x:auto;padding-bottom:.2rem}
  .rail-list .cls{display:contents}
  .rail-list .cls h3{display:none}
  .spec-btn{flex:0 0 auto;width:auto;border:1px solid var(--stroke);border-radius:999px;background:var(--glass-2)}
  .spec-btn .mini{display:none}
  main{overflow:visible}
  .hero{border-radius:var(--r) var(--r) 0 0}
}
@media (prefers-reduced-motion:no-preference){
  .hero,.panels,.cards,.heat{animation:fade .22s ease-out}
  @keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
}
</style>
</head>
<body>
<div class="app">
  <div class="top">
    <div class="raid"><h1 id="title">Mythic Stat Sheet</h1><p id="sub"></p></div>
    <nav class="bosses" id="bosses" role="tablist" aria-label="Boss"></nav>
    <button class="theme" id="theme" title="Light or dark" aria-label="Switch between light and dark">◐</button>
  </div>
  <div class="body">
    <aside class="rail glass" aria-label="Specs">
      <div class="rail-tools">
        <input type="search" id="q" placeholder="Find a spec or player" aria-label="Find a spec or player">
        <div class="roles" id="roles" role="group" aria-label="Role">
          <button data-v="all" aria-pressed="true">All</button>
          <button data-v="tank" aria-pressed="false">Tank</button>
          <button data-v="healer" aria-pressed="false">Heal</button>
          <button data-v="dps" aria-pressed="false">DPS</button>
        </div>
      </div>
      <div class="rail-list" id="railList">
        <button class="overview-link" id="ovLink"><span class="grid-ico" aria-hidden="true">
          <i style="background:var(--crit)"></i><i style="background:var(--haste)"></i>
          <i style="background:var(--mastery)"></i><i style="background:var(--vers)"></i>
        </span>Compare all specs</button>
        <div id="rail"></div>
      </div>
    </aside>
    <main class="glass" id="main"></main>
  </div>
</div>

<script>
const DATA = /*__DATA__*/null;

const CLASS_COLORS = {
  DeathKnight:"#C41E3A", DemonHunter:"#A330C9", Druid:"#FF7C0A", Evoker:"#33937F",
  Hunter:"#8CBF4A", Mage:"#22A7D0", Monk:"#00A878", Paladin:"#E86FA0", Priest:"#7C7C85",
  Rogue:"#C9A227", Shaman:"#2D7FD6", Warlock:"#7C7DE0", Warrior:"#B08050"
};
const CLASS_COLORS_DARK = {
  DeathKnight:"#E0425C", DemonHunter:"#B84FDB", Druid:"#FF9333", Evoker:"#3FBFA3",
  Hunter:"#AAD372", Mage:"#3FC7EB", Monk:"#00E68A", Paladin:"#F48CBA", Priest:"#E8E8E8",
  Rogue:"#FFF468", Shaman:"#4E9BF5", Warlock:"#9A9BFF", Warrior:"#C69B6D"
};
const ROLE_NAMES = {tank:"Tank", healer:"Healer", dps:"Damage"};
const STATS = [
  {k:"crit", n:"Crit", full:"Critical Strike", c:"var(--crit)", hex:"#ff5f52"},
  {k:"haste", n:"Haste", full:"Haste", c:"var(--haste)", hex:"#f0b400"},
  {k:"mastery", n:"Mastery", full:"Mastery", c:"var(--mastery)", hex:"#8b5cf6"},
  {k:"vers", n:"Vers", full:"Versatility", c:"var(--vers)", hex:"#00b37e"}
];

const state = {boss: 0, spec: null, role: "all", q: "", ovSort: null, cardSort: "rank", theme: "auto"};
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const pct = v => (v*100).toFixed(0) + "%";
const isDark = () => document.documentElement.dataset.theme === "dark";
const classColor = cls => (isDark() ? CLASS_COLORS_DARK : CLASS_COLORS)[cls] || (isDark() ? "#bbb" : "#666");

function fmtAmt(v){ if(!v) return "–"; if(v>=1e6) return (v/1e6).toFixed(2)+"M"; if(v>=1e3) return (v/1e3).toFixed(1)+"K"; return Math.round(v)+""; }
function fmtInt(v){ return v ? Math.round(v).toLocaleString() : "–"; }
function fmtDur(ms){ if(!ms) return "–"; const s=Math.round(ms/1000); return Math.floor(s/60)+":"+String(s%60).padStart(2,"0"); }
function fmtDate(ms){ if(!ms) return "–"; return new Date(ms).toLocaleDateString(undefined,{day:"numeric",month:"short"}); }
function ago(ms){
  const s = (Date.now()-ms)/1000;
  if(s < 90) return "just now";
  if(s < 5400) return Math.round(s/60) + " min ago";
  if(s < 36*3600) return Math.round(s/3600) + " hours ago";
  return Math.round(s/86400) + " days ago";
}
function hexA(hex, a){ const n=parseInt(hex.slice(1),16); return `rgba(${n>>16},${(n>>8)&255},${n&255},${a})`; }

/* ---------- theme ---------- */
function applyTheme(){
  let t = state.theme;
  if(t === "auto") t = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  document.documentElement.dataset.theme = t;
  $("#theme").textContent = t === "dark" ? "☾" : "☀";
  try{ localStorage.setItem("mss-theme", state.theme); }catch(e){}
}
function initTheme(){
  try{ state.theme = localStorage.getItem("mss-theme") || "auto"; }catch(e){}
  applyTheme();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if(state.theme === "auto"){ applyTheme(); render(); }
  });
}

/* ---------- data shaping ---------- */
function specList(){
  const bosses = state.boss === "all" ? DATA.bosses : [DATA.bosses[state.boss]].filter(Boolean);
  const map = new Map();
  for(const b of bosses) for(const s of b.specs){
    const key = s.cls+"|"+s.spec;
    if(!map.has(key)) map.set(key, {...s, key, players: []});
    for(const p of s.players) map.get(key).players.push({...p, boss: b.name});
  }
  const out = [...map.values()];
  for(const s of out) s.sum = summarise(s);
  return out;
}

function summarise(s){
  const ws = s.players.filter(p => p.stats);
  const avg = {}, min = {}, max = {};
  for(const st of STATS){
    const vals = ws.map(p => p.stats[st.k]);
    avg[st.k] = vals.length ? vals.reduce((a,b)=>a+b,0)/vals.length : 0;
    min[st.k] = vals.length ? Math.min(...vals) : 0;
    max[st.k] = vals.length ? Math.max(...vals) : 0;
  }
  const tot = STATS.reduce((a,st)=>a+avg[st.k],0);
  const share = {}; for(const st of STATS) share[st.k] = tot ? avg[st.k]/tot : 0;
  const il = s.players.filter(p=>p.ilvl);
  const ilvl = il.length ? il.reduce((a,p)=>a+p.ilvl,0)/il.length : 0;
  const counts = new Map();
  let withTk = 0;
  for(const p of s.players){
    if(p.trinkets.length) withTk++;
    for(const t of p.trinkets){
      const c = counts.get(t.id) || {id:t.id, name:t.name, n:0, ilvl:0};
      c.n++; c.ilvl = Math.max(c.ilvl, t.ilvl||0); counts.set(t.id, c);
    }
  }
  const trinkets = [...counts.values()].sort((a,b)=>b.n-a.n);
  const prio = STATS.filter(st=>avg[st.k]>0).sort((a,b)=>avg[b.k]-avg[a.k]);
  const best = s.players.reduce((a,p)=>Math.max(a,p.amount||0),0);
  return {avg, min, max, share, ilvl, trinkets, withTk, prio, best, n: s.players.length, nStats: ws.length};
}

function visible(specs){
  const q = state.q.toLowerCase();
  return specs.filter(s=>{
    if(state.role !== "all" && s.role !== state.role) return false;
    if(!q) return true;
    if((s.spec+" "+s.className).toLowerCase().includes(q)) return true;
    return s.players.some(p => (p.name+" "+(p.guild||"")).toLowerCase().includes(q));
  });
}

function tkLink(t, ilvl){
  const wh = ilvl && t.ilvl ? ` data-wowhead="ilvl=${Math.round(t.ilvl)}"` : "";
  return `<a href="https://www.wowhead.com/item=${t.id}" target="_blank" rel="noopener"${wh}>${esc(t.name || "Item " + t.id)}</a>`;
}
function talentBox(code){
  return `<div class="tal"><code title="${esc(code)}">${esc(code)}</code>
    <button class="copy" data-code="${esc(code)}">Copy</button>
    <a class="view" href="https://www.wowhead.com/talent-calc/blizzard/${encodeURIComponent(code)}" target="_blank" rel="noopener">View</a></div>`;
}
function talentBuilds(players){
  const m = new Map();
  for(const p of players){ if(!p.talents) continue;
    const b = m.get(p.talents) || {code:p.talents, players:[]}; b.players.push(p); m.set(p.talents, b); }
  return [...m.values()].sort((a,b)=>b.players.length-a.players.length || Math.min(...a.players.map(p=>p.rank))-Math.min(...b.players.map(p=>p.rank)));
}
async function copyText(text){
  try{ await navigator.clipboard.writeText(text); return true; }
  catch(e){
    const ta = document.createElement("textarea");
    ta.value = text; ta.setAttribute("readonly",""); ta.style.position="fixed"; ta.style.opacity="0";
    document.body.appendChild(ta); ta.select();
    let ok = false; try{ ok = document.execCommand("copy"); }catch(_){}
    ta.remove(); return ok;
  }
}

/* ---------- rail ---------- */
function renderRail(specs){
  const vis = new Set(visible(specs).map(s=>s.key));
  const groups = new Map();
  for(const s of specs){
    if(!vis.has(s.key)) continue;
    if(!groups.has(s.cls)) groups.set(s.cls, {name: s.className, specs: []});
    groups.get(s.cls).specs.push(s);
  }
  $("#ovLink").setAttribute("aria-current", state.spec ? "false" : "true");
  if(!groups.size){ $("#rail").innerHTML = `<div class="rail-empty">No specs match.</div>`; return; }
  $("#rail").innerHTML = [...groups].map(([cls,g])=>`
    <div class="cls" style="--cc:${classColor(cls)}"><h3>${esc(g.name)}</h3>
      ${g.specs.map(s=>`<button class="spec-btn ${s.sum.n?"":"nodata"}" data-key="${esc(s.key)}" aria-current="${state.spec===s.key}">
        <span><span class="dot"></span>${esc(s.spec)}</span>
        <span class="mini" aria-hidden="true">${s.sum.nStats?STATS.map(st=>`<span style="width:${s.sum.share[st.k]*100}%;background:${st.c}"></span>`).join(""):""}</span>
      </button>`).join("")}
    </div>`).join("");
}

/* ---------- overview ---------- */
function renderOverview(specs){
  const vis = visible(specs);
  const bossName = state.boss === "all" ? "All bosses" : DATA.bosses[state.boss].name;
  const withData = vis.filter(s=>s.sum.nStats);
  const range = {};
  for(const st of STATS){
    const v = withData.map(s=>s.sum.share[st.k]);
    range[st.k] = [Math.min(...v), Math.max(...v)];
  }
  let rows = [...vis];
  const srt = state.ovSort;
  if(srt){
    const f = srt === "ilvl" ? s=>s.sum.ilvl : s=>s.sum.share[srt];
    rows.sort((a,b)=>f(b)-f(a));
  }
  const cell = (s, st) => {
    if(!s.sum.nStats) return `<td class="cell"><div style="background:var(--track);color:var(--faint)">–</div></td>`;
    const [lo,hi] = range[st.k];
    const t = hi>lo ? (s.sum.share[st.k]-lo)/(hi-lo) : .5;
    const top = s.sum.prio[0] && s.sum.prio[0].k === st.k;
    return `<td class="cell ${top?"hi":""}"><div class="num" style="background:${hexA(st.hex, .1 + t*.55)}" title="${st.full}: ${pct(s.sum.share[st.k])} of secondary stats, avg ${fmtInt(s.sum.avg[st.k])} rating">${pct(s.sum.share[st.k])}</div></td>`;
  };
  const th = (k,label,cls="") => `<th class="${cls} ${srt===k?"on":""}" data-sort="${k}" tabindex="0">${label}${srt===k?" ↓":""}</th>`;
  $("#main").innerHTML = `<div class="pad">
    ${DATA.demo?`<div class="demo">Preview with made-up players and items</div>`:""}
    <div class="ov-head"><h2>${esc(bossName)}</h2>
      <p>Each cell is that stat's share of the top 10's secondary stats. Deeper colour means more than other specs; the outlined cell is the spec's highest. Click a header to sort, or a row to open the spec.</p></div>
    ${vis.length ? `<div class="heat-wrap"><table class="heat">
      <thead><tr>${th("","Spec")}${STATS.map(st=>th(st.k,`<span class="sw" style="background:${st.c}"></span><span class="lg">${st.full}</span><span class="sh">${st.n}</span>`)).join("")}
        ${th("prio","Priority","col-prio")}${th("ilvl","Avg ilvl","col-il")}<th class="col-tk">Most used trinket</th></tr></thead>
      <tbody>${rows.map(s=>`<tr data-key="${esc(s.key)}" tabindex="0" style="--cc:${classColor(s.cls)}">
        <td class="sp"><b>${esc(s.spec)}</b><small>${esc(s.className)}</small></td>
        ${STATS.map(st=>cell(s,st)).join("")}
        <td class="prio col-prio">${s.sum.prio.map(x=>x.n).join(" › ") || `<span class="nod">No stats yet</span>`}</td>
        <td class="num col-il">${s.sum.ilvl?s.sum.ilvl.toFixed(1):"–"}</td>
        <td class="col-tk"><div class="tk">${s.sum.trinkets[0]?`${tkLink(s.sum.trinkets[0])} <span class="nod">${s.sum.trinkets[0].n}/${s.sum.n}</span>`:`<span class="nod">–</span>`}</div></td>
      </tr>`).join("")}</tbody></table></div>`
    : `<div class="empty">No specs match. Clear the search or pick another role.</div>`}
  </div>`;
  $("#main").querySelectorAll("th[data-sort]").forEach(h=>{
    const go = ()=>{ const k=h.dataset.sort; if(k==="prio") return; state.ovSort = (state.ovSort===k||!k) ? null : k; render(); };
    h.addEventListener("click", go);
    h.addEventListener("keydown", e=>{ if(e.key==="Enter"){ e.preventDefault(); go(); } });
  });
  $("#main").querySelectorAll("tbody tr").forEach(r=>{
    const go = ()=>openSpec(r.dataset.key);
    r.addEventListener("click", e=>{ if(!e.target.closest("a")) go(); });
    r.addEventListener("keydown", e=>{ if(e.key==="Enter"){ e.preventDefault(); go(); } });
  });
}

/* ---------- spec page ---------- */
const CARD_SORTS = {
  rank:["Rank", (a,b)=>a.rank-b.rank || b.amount-a.amount],
  amount:["Highest output", (a,b)=>b.amount-a.amount],
  ilvl:["Item level", (a,b)=>b.ilvl-a.ilvl],
  crit:["Most Crit", (a,b)=>(b.stats?.crit||0)-(a.stats?.crit||0)],
  haste:["Most Haste", (a,b)=>(b.stats?.haste||0)-(a.stats?.haste||0)],
  mastery:["Most Mastery", (a,b)=>(b.stats?.mastery||0)-(a.stats?.mastery||0)],
  vers:["Most Versatility", (a,b)=>(b.stats?.vers||0)-(a.stats?.vers||0)],
  duration:["Fastest kill", (a,b)=>(a.duration||9e9)-(b.duration||9e9)]
};

function card(p, s, allBosses, maxShare){
  const st = p.stats;
  const tot = st ? STATS.reduce((a,x)=>a+st[x.k],0) : 0;
  const pending = p.loaded === false;
  const statBlock = st ? `
    <div class="sbars">${STATS.map(x=>{
      const sh = tot ? st[x.k]/tot : 0;
      return `<div class="sb"><span style="color:var(--dim)">${x.n}</span><span class="t"><i style="width:${maxShare?Math.min(100,sh/maxShare*100):0}%;background:${x.c}"></i></span><span class="v num">${fmtInt(st[x.k])}</span><span class="p num">${pct(sh)}</span></div>`;
    }).join("")}</div>
    <div class="prim"><span>${esc(st.primaryName)} <b class="num">${fmtInt(st.primary)}</b></span><span>Stamina <b class="num">${fmtInt(st.stamina)}</b></span></div>`
    : `<div class="pending">${pending ? "Stats not loaded yet." : "This log has no stat data."}</div>`;
  const potName = p.potion && (p.potion.name || (p.potion.id ? "Potion " + p.potion.id : ""));
  const pot = potName
    ? `<div><span style="color:var(--faint)">Potion</span><span>${esc(potName)}</span></div>`
    : (p.potion === null || p.potion === undefined ? "" :
       `<div><span style="color:var(--faint)">Potion</span><span style="color:var(--faint)">none cast</span></div>`);
  const tks = p.trinkets.length
    ? p.trinkets.map(t=>`<div>${tkLink(t,true)}<small class="num">${t.ilvl?Math.round(t.ilvl):""}</small></div>`).join("")
    : `<div class="pending">${pending ? "Trinkets not loaded yet" : "No trinket data"}</div>`;
  const logLink = p.report && p.report !== "demo"
    ? `<a href="https://www.warcraftlogs.com/reports/${encodeURIComponent(p.report)}#fight=${p.fight}" target="_blank" rel="noopener">View log</a>` : "";
  return `<article class="card glass ${p.rank===1?"first":""}">
    <div class="c-top">
      <span class="rank num">${p.rank}</span>
      <div class="nm2"><b>${esc(p.name)}</b><small>${esc([p.guild, p.server && (p.server+(p.region?" ("+p.region+")":""))].filter(Boolean).join(", ")) || "&nbsp;"}</small></div>
      <div class="out"><b class="num">${fmtAmt(p.amount)}</b><small>${s.metric}, ilvl ${p.ilvl?p.ilvl.toFixed(1):"–"}</small></div>
    </div>
    ${statBlock}
    <div class="c-tk">${tks}${pot}</div>
    <div class="c-tal"><span class="h">Talents</span>${
      p.talents ? talentBox(p.talents)
      : `<div class="pending">${p.talents === "" ? "No talent code in this log" : "Not loaded yet"}</div>`}</div>
    <div class="c-foot">${allBosses?`<span>${esc(p.boss)}</span>`:""}<span>Kill ${fmtDur(p.duration)}</span><span>${fmtDate(p.date)}</span>${logLink}</div>
  </article>`;
}

function renderSpec(s){
  const sum = s.sum, cc = classColor(s.cls);
  const allBosses = state.boss === "all";
  const bossName = allBosses ? "all bosses" : DATA.bosses[state.boss].name;
  const players = [...s.players].sort((CARD_SORTS[state.cardSort]||CARD_SORTS.rank)[1]);
  const maxShare = Math.max(0.0001, ...s.players.filter(p=>p.stats).flatMap(p=>{
    const t = STATS.reduce((a,x)=>a+p.stats[x.k],0); return STATS.map(x=>t?p.stats[x.k]/t:0);
  }));

  const split = sum.nStats ? `<div class="split-big">
      <div class="split-bar">${STATS.map(st=>`<span style="width:${sum.share[st.k]*100}%;background:${st.c};color:#fff" title="${st.full} ${pct(sum.share[st.k])}">${sum.share[st.k]>.09?`${st.n} ${pct(sum.share[st.k])}`:""}</span>`).join("")}</div>
      <div class="split-note"><span>Stat priority from the top ${sum.nStats}: <strong>${sum.prio.map(x=>x.full).join(" › ")}</strong></span>
      ${sum.nStats<sum.n?`<span>${sum.n-sum.nStats} not loaded yet</span>`:""}</div>
    </div>` : "";

  const ranges = sum.nStats ? STATS.map(st=>{
    const lo = sum.min[st.k], hi = sum.max[st.k], span = hi-lo || 1;
    const pos = v => ((v-lo)/span*100).toFixed(2);
    return `<div class="rng"><span class="lb">${st.full}</span>
      <div class="track" title="Lowest ${fmtInt(lo)}, average ${fmtInt(sum.avg[st.k])}, highest ${fmtInt(hi)}">
        ${s.players.filter(p=>p.stats).map(p=>`<span class="dot" style="left:${pos(p.stats[st.k])}%;background:${st.c}"></span>`).join("")}
        <span class="avg" style="left:${pos(sum.avg[st.k])}%"></span>
      </div>
      <div class="vals num">${fmtInt(sum.avg[st.k])}<small>${fmtInt(lo)} to ${fmtInt(hi)}</small></div></div>`;
  }).join("") : `<div class="pending">No stats loaded for this spec yet.</div>`;

  const builds = talentBuilds(s.players);
  const withTal = s.players.filter(p=>p.talents).length;
  const buildsHtml = builds.length ? builds.slice(0,6).map((b,i)=>`
    <div class="build">
      <div class="build-top"><b>${i===0 && b.players.length>1 ? "Most used" : `Build ${i+1}`}</b>
        <span class="share"><i style="width:${b.players.length/withTal*100}%"></i></span>
        <span class="num" style="color:var(--dim);font-size:.8rem">${b.players.length} of ${withTal}</span></div>
      ${talentBox(b.code)}
      <small>${b.players.sort((x,y)=>x.rank-y.rank).map(p=>`#${p.rank} ${esc(p.name)}`).join(", ")}</small>
    </div>`).join("") + (builds.length>6?`<div class="pending">${builds.length-6} more on the cards below.</div>`:"")
    : `<div class="pending">No talents loaded for this spec yet.</div>`;

  const tkMax = sum.trinkets[0]?.n || 1;
  const trinkets = sum.trinkets.length ? sum.trinkets.slice(0,8).map(t=>`
    <div class="tk-row"><span class="nm">${tkLink(t)}</span><span class="ct num">${t.n} of ${sum.withTk}</span>
      <span class="bar"><i style="width:${t.n/tkMax*100}%"></i></span></div>`).join("")
    : `<div class="pending">No trinkets loaded for this spec yet.</div>`;

  const potCounts = new Map();
  for(const p of s.players){
    const nm = p.potion && (p.potion.name || (p.potion.id ? "Potion " + p.potion.id : ""));
    if(nm){ const c = potCounts.get(nm) || {name:nm, n:0}; c.n++; potCounts.set(nm, c); }
  }
  const pots = [...potCounts.values()].sort((a,b)=>b.n-a.n);
  const potsHtml = pots.length ? pots.map(x=>`<div class="tk-row"><span class="nm">${esc(x.name)}</span><span class="ct num">${x.n}</span>
      <span class="bar"><i style="width:${x.n/pots[0].n*100}%"></i></span></div>`).join("") : "";

  $("#main").innerHTML = `<div style="--cc:${cc}">
    <section class="hero">
      ${DATA.demo?`<div class="demo">Preview with made-up players and items</div>`:""}
      <div class="hero-grid">
        <div><h2>${esc(s.spec)}</h2><div class="who">${esc(s.className)} · ${ROLE_NAMES[s.role]||s.role} · top 10 on ${esc(bossName)}</div></div>
        <div class="facts">
          <div class="fact"><b class="num">${fmtAmt(sum.best)}</b><span>Best ${s.metric}</span></div>
          <div class="fact"><b class="num">${sum.ilvl?sum.ilvl.toFixed(1):"–"}</b><span>Average item level</span></div>
          <div class="fact"><b class="num">${sum.nStats}/${sum.n}</b><span>Players with stats</span></div>
        </div>
      </div>
      ${split}
    </section>
    <div class="pad">
      ${sum.n ? `
      <div class="panels">
        <section class="panel glass"><h3>Secondary stat ranges<small>Dots are players, the line is the average</small></h3>${ranges}</section>
        <section class="panel glass"><h3>Trinkets used<small>Across the top ${sum.withTk || sum.n}</small></h3>${trinkets}
          ${potsHtml ? `<h3 style="margin-top:1rem">Combat potions</h3>${potsHtml}` : ""}</section>
        <section class="panel glass"><h3>Talent builds<small>${withTal ? `${builds.length} across ${withTal} players` : ""}</small></h3>${buildsHtml}</section>
      </div>
      <div class="players-head"><h3>${allBosses ? "Top players by boss" : "Top 10 players"}</h3>
        <label><span style="color:var(--dim);font-size:.82rem;margin-right:.4rem">Sort by</span>
          <select id="cardSort">${Object.entries(CARD_SORTS).map(([k,[n]])=>`<option value="${k}" ${state.cardSort===k?"selected":""}>${n}</option>`).join("")}</select></label>
      </div>
      <div class="cards">${players.map(p=>card(p,s,allBosses,maxShare)).join("")}</div>`
      : `<div class="empty">No Mythic rankings for ${esc(s.spec)} ${esc(s.className)} on ${esc(bossName)} yet.</div>`}
    </div>
  </div>`;
  const cs = $("#cardSort");
  if(cs) cs.addEventListener("change", e=>{ state.cardSort = e.target.value; render(); });
}

/* ---------- routing ---------- */
function writeHash(){
  const h = `boss=${state.boss}` + (state.spec ? `&spec=${encodeURIComponent(state.spec)}` : "");
  history.replaceState(null, "", "#" + h);
}
function readHash(){
  const p = new URLSearchParams(location.hash.slice(1));
  const b = p.get("boss");
  if(b === "all" && DATA.bosses.length > 1) state.boss = "all";
  else if(b !== null && DATA.bosses[+b]) state.boss = +b;
  state.spec = p.get("spec") || null;
}
function openSpec(key){ state.spec = key; state.cardSort = "rank"; render(); $("#main").scrollTop = 0; window.scrollTo({top: 0}); }

function render(){
  writeHash();
  document.querySelectorAll("#bosses button").forEach(b=>b.setAttribute("aria-selected", String(b.dataset.v)===String(state.boss)));
  const specs = specList();
  renderRail(specs);
  const s = state.spec && specs.find(x=>x.key===state.spec);
  if(s) renderSpec(s); else { state.spec = null; renderOverview(specs); }
  if(window.$WowheadPower && $WowheadPower.refreshLinks) try{ $WowheadPower.refreshLinks(); }catch(e){}
}

function init(){
  initTheme();
  $("#theme").addEventListener("click", ()=>{
    state.theme = isDark() ? "light" : "dark";
    applyTheme(); render();
  });
  if(!DATA || !DATA.bosses || !DATA.bosses.length){
    $("#main").innerHTML = `<div class="empty">Nothing saved yet. Run wcl_mythic_stats.py to download rankings.</div>`;
    return;
  }
  $("#title").textContent = DATA.zone;
  document.title = DATA.zone + " · Mythic Stat Sheet";
  const gen = new Date(DATA.generated);
  const pr = DATA.progress;
  const partial = pr && pr.total && pr.loaded < pr.total;
  const age = Date.now() - gen.getTime();
  const cls = age > 3*86400e3 ? "stale" : age > 36*3600e3 ? "old" : "";
  $("#sub").innerHTML = `<span class="stamp ${cls}"><i></i>Updated ${esc(ago(gen.getTime()))}</span>`
    + `<span>Mythic · top 10 per spec · ${esc(DATA.region)}</span>`
    + (partial ? `<span class="warn">${pr.loaded} of ${pr.total} loaded</span>` : "")
    + (DATA.addon ? `<a class="dl" href="${esc(DATA.addon)}" download>Addon</a>` : "");

  const tabs = DATA.bosses.map((b,i)=>[i,b.name]);
  if(DATA.bosses.length > 1) tabs.push(["all","All bosses"]);
  $("#bosses").innerHTML = tabs.map(([v,n])=>`<button role="tab" data-v="${v}">${esc(n)}</button>`).join("");
  $("#bosses").addEventListener("click", e=>{
    const b = e.target.closest("button"); if(!b) return;
    state.boss = b.dataset.v === "all" ? "all" : +b.dataset.v;
    render();
  });
  $("#rail").addEventListener("click", e=>{ const b = e.target.closest(".spec-btn"); if(b) openSpec(b.dataset.key); });
  $("#ovLink").addEventListener("click", ()=>{ state.spec = null; render(); });
  $("#q").addEventListener("input", e=>{ state.q = e.target.value.trim(); render(); });
  $("#roles").addEventListener("click", e=>{
    const b = e.target.closest("button"); if(!b) return;
    state.role = b.dataset.v;
    document.querySelectorAll("#roles button").forEach(x=>x.setAttribute("aria-pressed", x===b));
    render();
  });
  $("#main").addEventListener("click", async e=>{
    const b = e.target.closest(".copy"); if(!b) return;
    const ok = await copyText(b.dataset.code);
    b.textContent = ok ? "Copied" : "Press Ctrl+C";
    if(ok) b.classList.add("done");
    else { const c = b.parentElement.querySelector("code"); const r = document.createRange(); r.selectNodeContents(c); const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r); }
    setTimeout(()=>{ b.textContent = "Copy"; b.classList.remove("done"); }, 1600);
  });
  readHash();
  render();
}
init();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="Warcraft Logs Mythic raid stat sheet (top 10 per spec)")
    ap.add_argument("--all", action="store_true", help="fetch every boss without asking")
    ap.add_argument("--refresh", action="store_true", help="ignore cached rankings")
    ap.add_argument("--zone", type=int, help="raid zone ID (see --list-zones)")
    ap.add_argument("--list-zones", action="store_true", help="list raid zones and exit")
    ap.add_argument("--region", choices=["US", "EU", "KR", "TW", "CN"], help="only one region")
    ap.add_argument("--demo", action="store_true", help="build a preview page with made-up data")
    ap.add_argument("--offline", action="store_true", help="rebuild the page from saved data only")
    ap.add_argument("--out", help="where to write the HTML page")
    ap.add_argument("--deadline", type=float, metavar="MINUTES",
                    help="stop fetching after this many minutes and save the page")
    ap.add_argument("--no-open", action="store_true", help="don't open a browser (for servers)")
    ap.add_argument("--addon", nargs="?", const="MythicStats", metavar="FOLDER",
                    help="also write the in-game addon into this folder")
    ap.add_argument("--interface", default="120100,120105",
                    help="addon Interface number(s), comma separated (default 120100,120105)")
    ap.add_argument("--rank-age", type=float, metavar="HOURS",
                    help="reuse saved rankings younger than this (default 12)")
    ap.add_argument("--max-new", type=int, metavar="N",
                    help="read at most N new logs this run")
    ap.add_argument("--stop-at-limit", action="store_true",
                    help="stop and save when the hourly API limit is hit, instead of waiting")
    ap.add_argument("--compact", action="store_true",
                    help="shrink the saved cache folder and drop logs that are no longer needed")
    ap.add_argument("--test-potions", action="store_true",
                    help="show what Warcraft Logs returns for potion casts and auras")
    ap.add_argument("--test-report", help="use this report code with --test-potions")
    ap.add_argument("--test-fight", type=int, help="fight ID to use with --test-report")
    ap.add_argument("--test-source", type=int, help="source/actor ID to use with --test-report")
    ap.add_argument("--limit", action="store_true",
                    help="show how many API points this key has used this hour, then exit")
    ap.add_argument("--prune", action="store_true",
                    help="after updating, delete saved logs that dropped out of the top 10")
    args = ap.parse_args()
    global OUT_FILE, DEADLINE, OPEN_BROWSER, MAX_NEW, STOP_AT_LIMIT, ADDON_DIR, INTERFACE
    if args.out:
        OUT_FILE = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(OUT_FILE) or ".", exist_ok=True)
    if args.deadline:
        DEADLINE = time.time() + args.deadline * 60
    if args.no_open:
        OPEN_BROWSER = False
    if args.max_new:
        MAX_NEW = args.max_new
    if args.stop_at_limit:
        STOP_AT_LIMIT = True
    if args.addon:
        ADDON_DIR = os.path.abspath(args.addon)
    INTERFACE = args.interface
    try:
        if args.demo:
            demo(args)
        else:
            run(args)
    except KeyboardInterrupt:
        log("\nStopped.")
    except TimeUp:
        log("\nOut of time for this run.")
    except ApiDown as e:
        log(f"\nWarcraft Logs isn't usable right now: {e}")
        if rebuild_offline(args):
            log("The page was rebuilt from saved data, so it still shows everything downloaded so far.")
        log("Nothing was lost; run the script again later.")
    except Exception as e:
        log(f"\nSomething went wrong: {e}")
        log("If it mentions a field or argument, the Warcraft Logs API may have changed. "
            "Send this message to Claude to get it fixed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
