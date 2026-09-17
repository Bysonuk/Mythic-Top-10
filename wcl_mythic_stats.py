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


class TimeUp(Exception):
    """Raised when the run has used its allotted time (see --deadline)."""

CRED_FILE = os.path.join(HERE, "wcl_credentials.json")
CACHE_DIR = os.path.join(HERE, "wcl_cache")
OUT_FILE = os.path.join(HERE, "mythic_stat_sheet.html")
OPEN_BROWSER = True

TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
API_URL = "https://www.warcraftlogs.com/api/v2/client"

MYTHIC = 5
TOP_N = 10
RANKINGS_TTL = 12 * 3600
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
            if e.code in (400, 401):
                log("\nWarcraft Logs rejected the Client ID or Secret.")
                log(f"Check them, or delete {CRED_FILE} and run again to re-enter them.")
                sys.exit(1)
            raise

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
                    self.auth()
                    continue
                if e.code == 429:
                    self.wait_for_reset(force=True)
                    continue
                if e.code >= 500 and attempt < 5:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
            except urllib.error.URLError:
                if attempt < 5:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
            if res.get("errors") and not allow_errors:
                msgs = "; ".join(e.get("message", "?") for e in res["errors"])
                if "rate limit" in msgs.lower():
                    self.wait_for_reset(force=True)
                    continue
                raise RuntimeError("Warcraft Logs API error: " + msgs)
            if with_errors:
                return res.get("data") or {}, res.get("errors") or []
            return res.get("data") or {}
        raise RuntimeError("Warcraft Logs API kept failing, try again later.")

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


def get_rankings(api, enc_id, specs, region, refresh):
    out = {}
    todo = []
    for s in specs:
        key = rank_key(enc_id, s, region)
        cached = None if refresh else cache_get("rankings", key, RANKINGS_TTL)
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


def process_fights(api, fights, names_by_fight, on_batch=None):
    """fights: list of (code, fightID), most important first.
    Reads stats/trinkets for new logs, then talent codes for the ranked players."""
    def needs(key):
        fight = load_fight(key[0], key[1])
        if fight is None:
            return True
        if not api.talents_ok:
            return False
        have = cache_get("talents", f"{key[0]}|{key[1]}") or {}
        for nm in names_by_fight.get(key, ()):
            a = find_actor(fight, nm)
            if a and a.get("id") is not None and str(a["id"]) not in have:
                return True
        return False

    todo = [k for k in fights if needs(k)]
    total = len(todo)
    if not total:
        log("  Everything is already loaded.")
        return
    log(f"Reading {total} logs for stats, trinkets and talents...")
    done = 0
    for i in range(0, total, FIGHTS_PER_QUERY):
        chunk = todo[i:i + FIGHTS_PER_QUERY]
        new = [k for k in chunk if load_fight(k[0], k[1]) is None]
        if new:
            fetch_stats(api, new)
        if api.talents_ok:
            wanted = {}
            for k in chunk:
                fight = load_fight(k[0], k[1])
                if fight is None:
                    continue
                have = cache_get("talents", f"{k[0]}|{k[1]}") or {}
                ids = []
                for nm in names_by_fight.get(k, ()):
                    a = find_actor(fight, nm)
                    if a and a.get("id") is not None and str(a["id"]) not in have and a["id"] not in ids:
                        ids.append(a["id"])
                if ids:
                    wanted[k] = ids
            if wanted:
                fetch_talents(api, wanted)
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
    }
    if not fight:
        return p

    detail = find_actor(fight, name)
    if detail is None:
        p["talents"] = ""
    actor_id = detail.get("id") if detail else None
    if actor_id is not None and rep.get("code"):
        tal = cache_get("talents", f"{rep['code']}|{int(rep.get('fightID') or 0)}") or {}
        if str(actor_id) in tal:
            p["talents"] = tal[str(actor_id)]  # "" means the log had no talent code

    if detail:
        p["stats"] = detail.get("stats")
        p["trinkets"] = [dict(t) for t in detail.get("trinkets") or []]
        for t in p["trinkets"]:
            if t.get("id") and t.get("name"):
                item_names[int(t["id"])] = t["name"]

    return p


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


def build_from_cache(zone, specs, bosses, region, api=None):
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

    if api is not None:
        ids = sorted({t["id"] for b in result["bosses"] for s in b["specs"]
                      for p in s["players"] for t in p["trinkets"]})
        try:
            fill_item_names(api, item_names, ids)
        except Exception:
            pass
    for b in result["bosses"]:
        for s in b["specs"]:
            for p in s["players"]:
                for t in p["trinkets"]:
                    t["name"] = t.get("name") or item_names.get(t["id"])
    result["progress"] = {"loaded": loaded, "total": total, "talents": tal_loaded}
    write_page(result, open_browser=False)
    return loaded, total


def run(args):
    if args.offline:
        meta = cache_get("meta", "zones+specs")
        if meta is None:
            log("Looking up the raid and spec list (one small request)...")
            meta = get_meta(None)
        zone = pick_zone(meta["zones"], args.zone)
        if not zone:
            log("Raid not found. Use --list-zones to see options.")
            return
        loaded, total = build_from_cache(zone, meta["specs"], zone["encounters"], args.region)
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
    try:
        # Rankings for every chosen boss first, so the page lists all specs straight away
        ranks_by_boss = {}
        for b in bosses:
            log(f"{b['name']}: fetching top 10 per spec...")
            ranks_by_boss[b["id"]] = get_rankings(api, b["id"], specs, args.region, args.refresh)
        rebuild()
        open_page()
        opened = True
        log(f"\nThe page is open and fills in as logs are read. Refresh your browser to see new data.")

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
        loaded, total = build_from_cache(zone, specs, zone["encounters"], args.region, api)
        log(f"\nDone: {loaded} of {total} players loaded ({api.calls} API requests).")
        log("Refresh the page in your browser to see everything.")
    except (KeyboardInterrupt, TimeUp) as e:
        loaded, total = build_from_cache(zone, specs, zone["encounters"], args.region)
        why = "Out of time for this run." if isinstance(e, TimeUp) else "Stopped."
        log(f"\n{why} Page saved with {loaded} of {total} players loaded.")
        log("Run the script again later to carry on from here.")
        if not opened:
            open_page()


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


PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mythic Stat Sheet</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Saira+Extra+Condensed:wght@500;700&family=Saira+Semi+Condensed:wght@400;500;600&display=swap" rel="stylesheet">
<script>const whTooltips = {colorLinks: true, iconizeLinks: true, renameLinks: true, iconSize: 'small'};</script>
<script src="https://wow.zamimg.com/js/tooltips.js"></script>
<style>
:root{
  --bg:#1a1e26; --surface:#232833; --raised:#2c3240; --line:#373e4e;
  --ink:#eef0f4; --dim:#9aa3b5; --faint:#6c7589;
  --crit:#ff6b5b; --haste:#f4c542; --mastery:#a98cff; --vers:#3ecf9a;
  --accent:#c9d2e3;
  --display:"Saira Extra Condensed", "Arial Narrow", "Roboto Condensed", sans-serif;
  --ui:"Saira Semi Condensed", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  --rail:clamp(210px, 17vw, 330px);
  --gap:clamp(12px, 1.3vw, 28px);
  --radius:8px;
}
*{box-sizing:border-box}
html{font-size:clamp(14px, 0.42vw + 9.5px, 19px)}
html,body{margin:0;height:100%;background:var(--bg);color:var(--ink);font-family:var(--ui);line-height:1.4}
a{color:inherit}
button,select,input{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.num{font-variant-numeric:tabular-nums}

/* Shell: top bar, then rail + main filling the viewport */
.app{display:grid;grid-template-rows:auto 1fr;height:100vh;height:100dvh}
.top{display:flex;align-items:flex-end;gap:var(--gap);padding:.9rem var(--gap) 0;border-bottom:1px solid var(--line);min-width:0}
.raid{flex:0 0 auto;padding-bottom:.7rem;min-width:0}
.raid h1{margin:0;font-family:var(--display);font-weight:700;font-size:clamp(1.6rem, 2.2vw, 2.6rem);line-height:.95;white-space:nowrap}
.raid p{margin:.2rem 0 0;color:var(--dim);font-size:.8rem}
.raid .warn{color:var(--haste)}
.bosses{display:flex;gap:.2rem;overflow-x:auto;min-width:0;flex:1;scrollbar-width:none;align-self:flex-end}
.bosses::-webkit-scrollbar{display:none}
.bosses button{flex:0 0 auto;background:none;border:0;border-bottom:3px solid transparent;padding:.5rem .8rem .6rem;cursor:pointer;color:var(--dim);font-family:var(--display);font-weight:500;font-size:1.15rem;white-space:nowrap}
.bosses button:hover{color:var(--ink)}
.bosses button[aria-selected="true"]{color:var(--ink);border-bottom-color:var(--ink)}

.body{display:grid;grid-template-columns:var(--rail) minmax(0,1fr);min-height:0}

/* Rail */
.rail{border-right:1px solid var(--line);overflow-y:auto;padding:var(--gap) 0 2rem;min-height:0}
.rail-tools{padding:0 var(--gap) .8rem;display:grid;gap:.5rem}
.rail input{width:100%;background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:.45rem .7rem}
.roles{display:grid;grid-template-columns:repeat(4,1fr);background:var(--surface);border:1px solid var(--line);border-radius:6px;overflow:hidden}
.roles button{border:0;background:none;padding:.35rem 0;cursor:pointer;color:var(--dim);font-size:.82rem}
.roles button[aria-pressed="true"]{background:var(--raised);color:var(--ink)}
.overview-link{display:flex;align-items:center;gap:.6rem;width:100%;border:0;background:none;text-align:left;padding:.55rem var(--gap);cursor:pointer;font-weight:600}
.overview-link[aria-current="true"]{background:var(--raised)}
.overview-link .grid-ico{display:grid;grid-template-columns:repeat(4,6px);gap:2px}
.overview-link .grid-ico i{width:6px;height:6px;border-radius:1px}
.cls{margin-top:.7rem}
.cls h3{margin:0;padding:.2rem var(--gap);font-size:.75rem;font-weight:500;color:var(--cc)}
.spec-btn{display:grid;grid-template-columns:1fr auto;gap:.5rem;align-items:center;width:100%;border:0;background:none;text-align:left;padding:.32rem var(--gap);cursor:pointer;border-left:3px solid transparent}
.spec-btn:hover{background:var(--surface)}
.spec-btn[aria-current="true"]{background:var(--raised);border-left-color:var(--cc)}
.spec-btn .mini{display:flex;width:clamp(40px,3.6vw,70px);height:6px;border-radius:3px;overflow:hidden;background:var(--line)}
.spec-btn .mini span{height:100%}
.spec-btn.nodata{color:var(--faint)}
.rail-empty{padding:1rem var(--gap);color:var(--dim)}

/* Main */
main{overflow-y:auto;min-height:0;container-type:inline-size;container-name:main}
.pad{padding:var(--gap) var(--gap) 3rem}
.empty{padding:4rem var(--gap);color:var(--dim);text-align:center}
.demo{display:inline-block;margin-bottom:1rem;padding:.2rem .6rem;border:1px solid var(--haste);color:var(--haste);border-radius:4px;font-size:.8rem}

/* Overview heatmap */
.ov-head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:.5rem 1.5rem;margin-bottom:1rem}
.ov-head h2{margin:0;font-family:var(--display);font-weight:700;font-size:clamp(1.8rem,2.6vw,3rem);line-height:1}
.ov-head p{margin:0;color:var(--dim);font-size:.85rem;max-width:60ch}
.heat-wrap{overflow-x:auto}
.heat{width:100%;border-collapse:separate;border-spacing:0 3px}
.heat .sh{display:none}
.heat th{font-weight:500;color:var(--dim);font-size:.8rem;text-align:left;padding:.3rem .6rem;cursor:pointer;white-space:nowrap;user-select:none}
.heat th.on{color:var(--ink)}
.heat th .sw{display:inline-block;width:.6rem;height:.6rem;border-radius:2px;margin-right:.35rem}
.heat td{padding:0 .6rem;height:2.6rem;background:var(--surface);white-space:nowrap}
.heat tr{cursor:pointer}
.heat tbody tr:hover td{background:var(--raised)}
.heat td:first-child{border-left:4px solid var(--cc);border-radius:var(--radius) 0 0 var(--radius)}
.heat td:last-child{border-radius:0 var(--radius) var(--radius) 0}
.heat .sp b{font-family:var(--display);font-weight:700;font-size:1.25rem;color:var(--cc);margin-right:.4rem}
.heat .sp small{color:var(--dim)}
.heat td.cell{padding:3px;width:11%}
.heat .cell div{height:calc(2.6rem - 6px);border-radius:5px;display:flex;align-items:center;justify-content:center;font-weight:600;color:#fff;text-shadow:0 1px 2px rgba(0,0,0,.45)}
.heat .cell.hi div{box-shadow:inset 0 0 0 2px rgba(255,255,255,.85)}
.heat .prio{color:var(--dim);font-size:.85rem}
.heat .tk{max-width:22ch;overflow:hidden;text-overflow:ellipsis;font-size:.85rem}
.heat .tk a{text-decoration:none}
.heat .nod{color:var(--faint);font-size:.85rem}
@container main (max-width:900px){ .heat .col-prio{display:none} }
@container main (max-width:720px){ .heat .col-tk{display:none} .heat .lg{display:none} .heat .sh{display:inline} }
@container main (max-width:520px){ .heat .col-il{display:none} .heat .sp small{display:none} .heat td.cell{width:auto} .heat .sp b{font-size:1.05rem} .heat td,.heat th{padding-left:.35rem;padding-right:.35rem} .heat .sw{display:none} }

/* Spec page */
.hero{position:relative;padding:clamp(1rem,2vw,2.2rem) var(--gap);border-bottom:1px solid var(--line);
  background:linear-gradient(100deg, color-mix(in srgb, var(--cc) 22%, var(--bg)) 0%, var(--bg) 70%)}
.hero-grid{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:1rem 2.5rem}
.hero h2{margin:0;font-family:var(--display);font-weight:700;font-size:clamp(2.8rem,6cqi,6rem);line-height:.85;color:var(--cc)}
.hero .who{margin-top:.35rem;color:var(--ink);opacity:.8}
.facts{display:flex;flex-wrap:wrap;gap:.5rem clamp(1.2rem,2.5vw,3rem)}
.fact b{display:block;font-family:var(--display);font-weight:700;font-size:clamp(1.6rem,2.8cqi,2.6rem);line-height:1}
.fact span{color:var(--dim);font-size:.8rem}

.split-big{margin-top:clamp(1rem,2vw,1.8rem)}
.split-bar{display:flex;height:clamp(28px,2.6vw,44px);border-radius:6px;overflow:hidden;background:var(--line)}
.split-bar span{display:flex;align-items:center;padding:0 .6rem;min-width:0;overflow:hidden;white-space:nowrap;font-weight:600;color:#fff;text-shadow:0 1px 2px rgba(0,0,0,.45);font-size:.9rem}
.split-note{display:flex;flex-wrap:wrap;justify-content:space-between;gap:.3rem 1rem;margin-top:.45rem;font-size:.85rem;color:var(--dim)}
.split-note strong{color:var(--ink);font-weight:600}

.panels{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,380px),1fr));gap:var(--gap);margin-top:var(--gap)}
.panel{background:var(--surface);border-radius:var(--radius);padding:1rem 1.1rem}
.panel h3{margin:0 0 .8rem;font-family:var(--display);font-weight:500;font-size:1.35rem}
.panel h3 small{font-family:var(--ui);font-size:.78rem;color:var(--dim);margin-left:.4rem;font-weight:400}

.tk-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:.2rem .8rem;align-items:center;padding:.3rem 0}
.tk-row .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tk-row .nm a{text-decoration:none}
.tk-row .ct{color:var(--dim);font-size:.85rem}
.tk-row .bar{grid-column:1 / -1;height:4px;border-radius:2px;background:var(--line);overflow:hidden}
.tk-row .bar i{display:block;height:100%;background:var(--cc)}

.rng{display:grid;grid-template-columns:5.5rem minmax(0,1fr) auto;gap:.8rem;align-items:center;padding:.45rem 0}
.rng .lb{font-size:.9rem}
.rng .track{position:relative;height:18px}
.rng .track::before{content:"";position:absolute;left:0;right:0;top:8px;height:2px;background:var(--line)}
.rng .dot{position:absolute;top:4px;width:10px;height:10px;margin-left:-5px;border-radius:50%;opacity:.75}
.rng .avg{position:absolute;top:0;width:3px;height:18px;margin-left:-1.5px;background:var(--ink);border-radius:2px}
.rng .vals{text-align:right;font-size:.85rem}
.rng .vals small{display:block;white-space:nowrap;color:var(--dim);font-size:.72rem}

.players-head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:.6rem;margin:calc(var(--gap) * 1.4) 0 .8rem}
.players-head h3{margin:0;font-family:var(--display);font-weight:700;font-size:1.7rem}
.players-head select{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:.35rem .6rem}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,300px),1fr));gap:calc(var(--gap) * .7)}
.card{background:var(--surface);border-radius:var(--radius);padding:.9rem 1rem;display:flex;flex-direction:column;gap:.7rem;border-top:3px solid transparent}
.card.first{border-top-color:var(--cc)}
.c-top{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:.7rem;align-items:start}
.rank{font-family:var(--display);font-weight:700;font-size:2rem;line-height:.9;color:var(--faint);min-width:1.6ch}
.card.first .rank{color:var(--cc)}
.nm2{min-width:0}
.nm2 b{display:block;font-weight:600;font-size:1.05rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nm2 small{display:block;color:var(--dim);font-size:.78rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.out{text-align:right}
.out b{display:block;font-family:var(--display);font-weight:700;font-size:1.6rem;line-height:.95}
.out small{color:var(--dim);font-size:.75rem}
.sbars{display:grid;gap:.3rem}
.sb{display:grid;grid-template-columns:4.2rem minmax(0,1fr) 3.9rem 2.6rem;gap:.5rem;align-items:center;font-size:.85rem}
.sb .t{height:7px;border-radius:4px;background:var(--line);overflow:hidden}
.sb .t i{display:block;height:100%;border-radius:4px}
.sb .v{text-align:right}
.sb .p{text-align:right;color:var(--dim)}
.prim{display:flex;justify-content:space-between;font-size:.82rem;color:var(--dim)}
.prim b{color:var(--ink);font-weight:500}
.c-tk{display:grid;gap:.2rem;font-size:.88rem;border-top:1px solid var(--line);padding-top:.6rem}
.c-tk div{display:flex;justify-content:space-between;gap:.6rem;min-width:0}
.c-tk a{text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.c-tk small{color:var(--dim);flex:0 0 auto}
.c-foot{display:flex;flex-wrap:wrap;gap:.2rem 1rem;font-size:.78rem;color:var(--dim);margin-top:auto}
.c-foot a{color:var(--ink)}
.pending{color:var(--faint);font-style:italic;font-size:.88rem;padding:.4rem 0}

/* Talents */
.tal{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:.4rem;align-items:center}
.tal code{display:block;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:Consolas,"Cascadia Mono","Courier New",monospace;font-size:.78rem;color:var(--dim);background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:.3rem .45rem;cursor:text;user-select:all}
.copy{border:1px solid var(--line);background:var(--raised);border-radius:5px;padding:.25rem .7rem;cursor:pointer;font-size:.82rem;font-weight:600;white-space:nowrap}
.copy:hover{border-color:var(--cc,var(--ink))}
.copy.done{background:var(--vers);border-color:var(--vers);color:#0d2a20}
.view{font-size:.82rem;color:var(--dim);white-space:nowrap}
.c-tal{border-top:1px solid var(--line);padding-top:.6rem;display:grid;gap:.35rem}
.c-tal .h{font-size:.78rem;color:var(--dim)}
.build{padding:.55rem 0;border-bottom:1px solid var(--line);display:grid;gap:.35rem}
.build:last-child{border-bottom:0}
.build-top{display:flex;justify-content:space-between;align-items:baseline;gap:.8rem}
.build-top b{font-weight:600}
.build-top .share{height:4px;flex:1;max-width:40%;border-radius:2px;background:var(--line);overflow:hidden;align-self:center}
.build-top .share i{display:block;height:100%;background:var(--cc)}
.build small{color:var(--dim);font-size:.78rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* Narrow screens: rail becomes a top strip */
@media (max-width:760px){
  .app{height:auto;display:block}
  .top{flex-wrap:wrap;align-items:flex-start}
  .raid{padding-bottom:0}
  .bosses{flex-basis:100%}
  .body{display:block}
  .rail{border-right:0;border-bottom:1px solid var(--line);overflow:visible;padding-bottom:.6rem}
  .rail-list{display:flex;gap:.3rem;overflow-x:auto;padding:0 var(--gap) .3rem}
  .rail-list .cls{display:contents}
  .rail-list .cls h3{display:none}
  .spec-btn{flex:0 0 auto;width:auto;border-left:0;border-bottom:3px solid transparent;border-radius:6px;background:var(--surface)}
  .spec-btn[aria-current="true"]{border-bottom-color:var(--cc)}
  .spec-btn .mini{display:none}
  .overview-link{padding:.5rem var(--gap)}
  main{overflow:visible}
  .rng{grid-template-columns:4.5rem minmax(0,1fr) auto}
}
@media (prefers-reduced-motion:no-preference){
  .hero, .panels, .cards{animation:rise .25s ease-out}
  @keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
}
</style>
</head>
<body>
<div class="app">
  <div class="top">
    <div class="raid"><h1 id="title">Mythic Stat Sheet</h1><p id="sub"></p></div>
    <nav class="bosses" id="bosses" role="tablist" aria-label="Boss"></nav>
  </div>
  <div class="body">
    <aside class="rail" aria-label="Specs">
      <div class="rail-tools">
        <input type="search" id="q" placeholder="Find a spec or player" aria-label="Find a spec or player">
        <div class="roles" id="roles" role="group" aria-label="Role">
          <button data-v="all" aria-pressed="true">All</button>
          <button data-v="tank" aria-pressed="false">Tank</button>
          <button data-v="healer" aria-pressed="false">Heal</button>
          <button data-v="dps" aria-pressed="false">DPS</button>
        </div>
      </div>
      <button class="overview-link" id="ovLink"><span class="grid-ico" aria-hidden="true">
        <i style="background:var(--crit)"></i><i style="background:var(--haste)"></i><i style="background:var(--mastery)"></i><i style="background:var(--vers)"></i>
        <i style="background:var(--haste)"></i><i style="background:var(--vers)"></i><i style="background:var(--crit)"></i><i style="background:var(--mastery)"></i>
      </span>Compare all specs</button>
      <div class="rail-list" id="rail"></div>
    </aside>
    <main id="main"></main>
  </div>
</div>

<script>
const DATA = /*__DATA__*/null;

const CLASS_COLORS = {
  DeathKnight:"#E0425C", DemonHunter:"#B84FDB", Druid:"#FF7C0A", Evoker:"#3AAE96",
  Hunter:"#AAD372", Mage:"#3FC7EB", Monk:"#00E68A", Paladin:"#F48CBA", Priest:"#E8E8E8",
  Rogue:"#FFF468", Shaman:"#3A8EF0", Warlock:"#9A9BFF", Warrior:"#C69B6D"
};
const ROLE_NAMES = {tank:"Tank", healer:"Healer", dps:"Damage"};
const STATS = [
  {k:"crit", n:"Crit", full:"Critical Strike", c:"var(--crit)", hex:"#ff6b5b"},
  {k:"haste", n:"Haste", full:"Haste", c:"var(--haste)", hex:"#f4c542"},
  {k:"mastery", n:"Mastery", full:"Mastery", c:"var(--mastery)", hex:"#a98cff"},
  {k:"vers", n:"Vers", full:"Versatility", c:"var(--vers)", hex:"#3ecf9a"}
];

const state = {boss: 0, spec: null, role: "all", q: "", ovSort: null, cardSort: "rank"};
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const pct = v => (v*100).toFixed(0) + "%";

function fmtAmt(v){ if(!v) return "–"; if(v>=1e6) return (v/1e6).toFixed(2)+"M"; if(v>=1e3) return (v/1e3).toFixed(1)+"K"; return Math.round(v)+""; }
function fmtInt(v){ return v ? Math.round(v).toLocaleString() : "–"; }
function fmtDur(ms){ if(!ms) return "–"; const s=Math.round(ms/1000); return Math.floor(s/60)+":"+String(s%60).padStart(2,"0"); }
function fmtDate(ms){ if(!ms) return "–"; return new Date(ms).toLocaleDateString(undefined,{day:"numeric",month:"short"}); }
function hexA(hex, a){ const n=parseInt(hex.slice(1),16); return `rgba(${n>>16},${(n>>8)&255},${n&255},${a})`; }

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
  if(!groups.size){ $("#rail").innerHTML = `<div class="rail-empty">No specs match. Clear the search or pick another role.</div>`; return; }
  $("#rail").innerHTML = [...groups].map(([cls,g])=>`
    <div class="cls" style="--cc:${CLASS_COLORS[cls]||"#ccc"}"><h3>${esc(g.name)}</h3>
      ${g.specs.map(s=>`<button class="spec-btn ${s.sum.n?"":"nodata"}" data-key="${esc(s.key)}" aria-current="${state.spec===s.key}">
        <span>${esc(s.spec)}</span>
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
    if(!s.sum.nStats) return `<td class="cell"><div style="background:var(--raised);color:var(--faint);text-shadow:none">–</div></td>`;
    const [lo,hi] = range[st.k];
    const t = hi>lo ? (s.sum.share[st.k]-lo)/(hi-lo) : .5;
    const top = s.sum.prio[0] && s.sum.prio[0].k === st.k;
    return `<td class="cell ${top?"hi":""}"><div class="num" style="background:${hexA(st.hex, .14 + t*.78)}" title="${st.full}: ${pct(s.sum.share[st.k])} of secondary stats, avg ${fmtInt(s.sum.avg[st.k])} rating">${pct(s.sum.share[st.k])}</div></td>`;
  };
  const th = (k,label,cls="") => `<th class="${cls} ${srt===k?"on":""}" data-sort="${k}" tabindex="0">${label}${srt===k?" ▼":""}</th>`;
  $("#main").innerHTML = `<div class="pad">
    ${DATA.demo?`<div class="demo">Preview with made-up players and items</div>`:""}
    <div class="ov-head"><h2>${esc(bossName)}</h2>
      <p>Each cell is that stat's share of the top 10's secondary stats. Brighter means more than other specs; the outlined cell is the spec's highest stat. Click a header to sort, or a row to open the spec.</p></div>
    ${vis.length ? `<div class="heat-wrap"><table class="heat">
      <thead><tr>${th("","Spec")}${STATS.map(st=>th(st.k,`<span class="sw" style="background:${st.c}"></span><span class="lg">${st.full}</span><span class="sh">${st.n}</span>`)).join("")}
        ${th("prio","Priority","col-prio")}${th("ilvl","Avg ilvl","col-il")}<th class="col-tk">Most used trinket</th></tr></thead>
      <tbody>${rows.map(s=>`<tr data-key="${esc(s.key)}" tabindex="0" style="--cc:${CLASS_COLORS[s.cls]||"#ccc"}">
        <td class="sp"><b>${esc(s.spec)}</b><small>${esc(s.className)}</small></td>
        ${STATS.map(st=>cell(s,st)).join("")}
        <td class="prio col-prio">${s.sum.prio.map(x=>x.n).join(" > ") || `<span class="nod">No stats yet</span>`}</td>
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
      return `<div class="sb"><span>${x.n}</span><span class="t"><i style="width:${maxShare?Math.min(100,sh/maxShare*100):0}%;background:${x.c}"></i></span><span class="v num">${fmtInt(st[x.k])}</span><span class="p num">${pct(sh)}</span></div>`;
    }).join("")}</div>
    <div class="prim"><span>${esc(st.primaryName)} <b class="num">${fmtInt(st.primary)}</b></span><span>Stamina <b class="num">${fmtInt(st.stamina)}</b></span></div>`
    : `<div class="pending">${pending ? "Stats not loaded yet. Run the script again to fill this in." : "This log has no stat data."}</div>`;
  const tks = p.trinkets.length
    ? p.trinkets.map(t=>`<div>${tkLink(t,true)}<small class="num">${t.ilvl?Math.round(t.ilvl):""}</small></div>`).join("")
    : `<div class="pending">${pending ? "Trinkets not loaded yet" : "No trinket data"}</div>`;
  const logLink = p.report && p.report !== "demo"
    ? `<a href="https://www.warcraftlogs.com/reports/${encodeURIComponent(p.report)}#fight=${p.fight}" target="_blank" rel="noopener">View log</a>` : "";
  return `<article class="card ${p.rank===1?"first":""}">
    <div class="c-top">
      <span class="rank num">${p.rank}</span>
      <div class="nm2"><b>${esc(p.name)}</b><small>${esc([p.guild, p.server && (p.server+(p.region?" ("+p.region+")":""))].filter(Boolean).join(", ")) || "&nbsp;"}</small></div>
      <div class="out"><b class="num">${fmtAmt(p.amount)}</b><small>${s.metric}, ilvl ${p.ilvl?p.ilvl.toFixed(1):"–"}</small></div>
    </div>
    ${statBlock}
    <div class="c-tk">${tks}</div>
    <div class="c-tal"><span class="h">Talents</span>${
      p.talents ? talentBox(p.talents)
      : `<div class="pending">${p.talents === "" ? "This log has no talent code" : "Talents not loaded yet"}</div>`}</div>
    <div class="c-foot">${allBosses?`<span>${esc(p.boss)}</span>`:""}<span>Kill ${fmtDur(p.duration)}</span><span>${fmtDate(p.date)}</span>${logLink}</div>
  </article>`;
}

function renderSpec(s){
  const sum = s.sum, cc = CLASS_COLORS[s.cls] || "#ccc";
  document.documentElement.style.setProperty("--accent", cc);
  const allBosses = state.boss === "all";
  const bossName = allBosses ? "all bosses" : DATA.bosses[state.boss].name;
  const players = [...s.players].sort((CARD_SORTS[state.cardSort]||CARD_SORTS.rank)[1]);
  const maxShare = Math.max(0.0001, ...s.players.filter(p=>p.stats).flatMap(p=>{
    const t = STATS.reduce((a,x)=>a+p.stats[x.k],0); return STATS.map(x=>t?p.stats[x.k]/t:0);
  }));

  const split = sum.nStats ? `<div class="split-big">
      <div class="split-bar">${STATS.map(st=>`<span style="width:${sum.share[st.k]*100}%;background:${st.c}" title="${st.full} ${pct(sum.share[st.k])}">${sum.share[st.k]>.09?`${st.n} ${pct(sum.share[st.k])}`:""}</span>`).join("")}</div>
      <div class="split-note"><span>Stat priority from the top ${sum.nStats}: <strong>${sum.prio.map(x=>x.full).join(" > ")}</strong></span>
      ${sum.nStats<sum.n?`<span>${sum.n-sum.nStats} player${sum.n-sum.nStats>1?"s":""} not loaded yet</span>`:""}</div>
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
      <div class="build-top"><b>${i===0 && b.players.length>1 ? "Most used build" : `Build ${i+1}`}</b>
        <span class="share"><i style="width:${b.players.length/withTal*100}%"></i></span>
        <span class="ct num" style="color:var(--dim);font-size:.85rem">${b.players.length} of ${withTal}</span></div>
      ${talentBox(b.code)}
      <small>${b.players.sort((x,y)=>x.rank-y.rank).map(p=>`#${p.rank} ${esc(p.name)}`).join(", ")}</small>
    </div>`).join("") + (builds.length>6?`<div class="pending">${builds.length-6} more builds are on the player cards below.</div>`:"")
    : `<div class="pending">No talents loaded for this spec yet. Run the script again to fetch them.</div>`;
  const tkMax = sum.trinkets[0]?.n || 1;
  const trinkets = sum.trinkets.length ? sum.trinkets.slice(0,8).map(t=>`
    <div class="tk-row"><span class="nm">${tkLink(t)}</span><span class="ct num">${t.n} of ${sum.withTk}</span>
      <span class="bar"><i style="width:${t.n/tkMax*100}%"></i></span></div>`).join("")
    : `<div class="pending">No trinkets loaded for this spec yet.</div>`;

  $("#main").innerHTML = `<div style="--cc:${cc}">
    <section class="hero">
      ${DATA.demo?`<div class="demo">Preview with made-up players and items</div>`:""}
      <div class="hero-grid">
        <div><h2>${esc(s.spec)}</h2><div class="who">${esc(s.className)}, ${ROLE_NAMES[s.role]||s.role}, top 10 on ${esc(bossName)}</div></div>
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
        <section class="panel"><h3>Secondary stat ranges<small>Dots are players, the white line is the average</small></h3>${ranges}</section>
        <section class="panel"><h3>Trinkets used<small>Across the top ${sum.withTk || sum.n}</small></h3>${trinkets}</section>
        <section class="panel"><h3>Talent builds<small>${withTal ? `${builds.length} different across ${withTal} players` : ""}</small></h3>${buildsHtml}</section>
      </div>
      <div class="players-head"><h3>${allBosses ? "Top players by boss" : "Top 10 players"}</h3>
        <label><span class="sr" style="color:var(--dim);font-size:.85rem;margin-right:.4rem">Sort by</span>
          <select id="cardSort">${Object.entries(CARD_SORTS).map(([k,[n]])=>`<option value="${k}" ${state.cardSort===k?"selected":""}>${n}</option>`).join("")}</select></label>
      </div>
      <div class="cards">${players.map(p=>card(p,s,allBosses,maxShare)).join("")}</div>`
      : `<div class="empty">No Mythic rankings for ${esc(s.spec)} ${esc(s.className)} on ${esc(bossName)} yet. Try another boss.</div>`}
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
  if(s) renderSpec(s);
  else { state.spec = null; document.documentElement.style.setProperty("--accent", "#c9d2e3"); renderOverview(specs); }
  if(window.$WowheadPower && $WowheadPower.refreshLinks) try{ $WowheadPower.refreshLinks(); }catch(e){}
}

function init(){
  if(!DATA || !DATA.bosses || !DATA.bosses.length){
    $("#main").innerHTML = `<div class="empty">Nothing saved yet. Run wcl_mythic_stats.py to download rankings.</div>`;
    return;
  }
  $("#title").textContent = DATA.zone;
  document.title = DATA.zone + " stat sheet";
  const gen = new Date(DATA.generated);
  const pr = DATA.progress;
  const partial = pr && pr.total && pr.loaded < pr.total;
  $("#sub").innerHTML = `Mythic, top 10 per spec, ${esc(DATA.region)}. Updated ${esc(gen.toLocaleString(undefined,{dateStyle:"medium",timeStyle:"short"}))}.`
    + (partial ? ` <span class="warn">${pr.loaded} of ${pr.total} players loaded.</span>` : "")
    + (pr && pr.total && pr.talents !== undefined && pr.talents < pr.total ? ` <span class="warn">Talents for ${pr.talents} of ${pr.total}.</span>` : "");

  const tabs = DATA.bosses.map((b,i)=>[i,b.name]);
  if(DATA.bosses.length > 1) tabs.push(["all","All bosses"]);
  $("#bosses").innerHTML = tabs.map(([v,n])=>`<button role="tab" data-v="${v}">${esc(n)}</button>`).join("");
  $("#bosses").addEventListener("click", e=>{
    const b = e.target.closest("button"); if(!b) return;
    state.boss = b.dataset.v === "all" ? "all" : +b.dataset.v;
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
  $("#rail").addEventListener("click", e=>{ const b = e.target.closest(".spec-btn"); if(b) openSpec(b.dataset.key); });
  $("#ovLink").addEventListener("click", ()=>{ state.spec = null; render(); });
  $("#q").addEventListener("input", e=>{ state.q = e.target.value.trim(); render(); });
  $("#roles").addEventListener("click", e=>{
    const b = e.target.closest("button"); if(!b) return;
    state.role = b.dataset.v;
    document.querySelectorAll("#roles button").forEach(x=>x.setAttribute("aria-pressed", x===b));
    render();
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
    args = ap.parse_args()
    global OUT_FILE, DEADLINE, OPEN_BROWSER
    if args.out:
        OUT_FILE = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(OUT_FILE) or ".", exist_ok=True)
    if args.deadline:
        DEADLINE = time.time() + args.deadline * 60
    if args.no_open:
        OPEN_BROWSER = False
    try:
        if args.demo:
            demo(args)
        else:
            run(args)
    except KeyboardInterrupt:
        log("\nStopped.")
    except TimeUp:
        log("\nOut of time for this run.")
    except Exception as e:
        log(f"\nSomething went wrong: {e}")
        log("If it mentions a field or argument, the Warcraft Logs API may have changed. "
            "Send this message to Claude to get it fixed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
