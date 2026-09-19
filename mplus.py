#!/usr/bin/env python3
"""
Mythic+ comps, from the Raider.IO API.

Reads the top keys of the current season and works out which specs are being
brought, who they're brought with, the groups that keep appearing, the key levels
they're run at, and the talent builds they use. Raider.IO includes each player's
talent loadout string in the roster, so the builds are copy-and-paste ready.

    python mplus.py                          # build site/mplus.html
    python mplus.py --pages 8                # 20 runs per page, per dungeon
    python mplus.py --season season-mn-2     # pin a season
    python mplus.py --region eu              # world, us, eu, kr, tw
    python mplus.py --probe                  # show what the API returns, then stop

Set RIO_API_KEY to raise the rate limit. Unauthenticated is 200 requests a
minute, which is comfortably more than this needs.

Data from Raider.IO; the page credits and links back to them, as their terms ask.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone

API = "https://raider.io/api/v1"
HERE = os.path.dirname(os.path.abspath(__file__))
ROLE_ORDER = {"tank": 0, "healer": 1, "dps": 2}


def log(m):
    print(m, flush=True)


def get(path, **params):
    key = os.environ.get("RIO_API_KEY")
    if key:
        params["access_key"] = key
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "MythicTop10/1.0"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            if e.code >= 500 and attempt < 4:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"{e.code} on {path}: {e.read().decode('utf-8', 'replace')[:200]}")
        except urllib.error.URLError:
            if attempt < 4:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"Gave up on {path}")


# --------------------------------------------------------------------------- #
# Reading runs
# --------------------------------------------------------------------------- #

def current_season(expansion):
    """The live season slug, and the dungeons actually being run in it.

    The expansion's static data lists every dungeon the expansion has, which is
    wider than the current rotation, so the dungeon list comes from the season's
    own entry when it has one, and otherwise from the leaderboard itself.
    """
    d = get("/mythic-plus/static-data", expansion_id=expansion)
    seasons = [s for s in (d.get("seasons") or []) if s.get("slug")]
    if not seasons:
        raise RuntimeError(f"No seasons for expansion {expansion}")
    # Side seasons ("break-the-meta", "cutoffs", "remix") aren't the live ladder
    main = [s for s in seasons if s["slug"].count("-") <= 2]
    chosen = (main or seasons)[0]
    season = chosen["slug"]

    def clean(lst):
        return [{"slug": x["slug"], "name": x.get("name", x["slug"]),
                 "short": x.get("short_name", "")} for x in lst if x.get("slug")]

    dungeons = clean(chosen.get("dungeons") or [])
    if dungeons:
        return season, dungeons
    return season, dungeons_in_play(season)


def dungeons_in_play(season, region="world", pages=8, want=8):
    """Read the top runs and see which dungeons this season actually uses."""
    found = {}
    for page in range(pages):
        if len(found) >= want and page >= 4:
            break
        d = get("/mythic-plus/runs", season=season, region=region,
                dungeon="all", affixes="all", page=page)
        for r in d.get("rankings") or []:
            dg = (r.get("run") or {}).get("dungeon") or {}
            if dg.get("slug") and dg["slug"] not in found:
                found[dg["slug"]] = {"slug": dg["slug"], "name": dg.get("name", dg["slug"]),
                                     "short": dg.get("short_name", "")}
        time.sleep(0.35)
    return sorted(found.values(), key=lambda x: x["name"])


def players(run):
    """(class, spec, role, loadout) for each of the five."""
    out = []
    for member in run.get("roster") or []:
        ch = member.get("character") or {}
        cls = (ch.get("class") or {}).get("name")
        spec = (ch.get("spec") or {}).get("name")
        if not cls or not spec:
            continue
        out.append({
            "cls": cls, "spec": spec,
            "role": (member.get("role") or "dps").lower(),
            "loadout": member.get("loadout") or "",
            "name": ch.get("name", ""),
        })
    return out


def fetch_dungeon(season, dungeon, pages, region):
    runs = []
    for page in range(pages):
        d = get("/mythic-plus/runs", season=season, region=region,
                dungeon=dungeon, affixes="all", page=page)
        rankings = d.get("rankings") or []
        if not rankings:
            break
        runs.extend(r.get("run") or {} for r in rankings)
        time.sleep(0.35)         # stay well inside the rate limit
    return runs


# --------------------------------------------------------------------------- #
# Working out the meta
# --------------------------------------------------------------------------- #

def hero_lookup():
    """Returns a function code -> hero tree, or one that always gives None."""
    try:
        import talents
        index = talents.load_index(os.path.join(HERE, "wcl_cache"))
    except Exception as e:
        log(f"  Hero trees unavailable ({e}).")
        return lambda code: None
    seen = {}

    def look(code):
        if not code:
            return None
        if code not in seen:
            seen[code] = talents.hero_tree(code, os.path.join(HERE, "wcl_cache"), index)
        return seen[code]
    return look


def build(args):
    season = args.season
    dungeons = []
    if not season or not args.dungeon:
        season_found, dungeons = current_season(args.expansion)
        season = season or season_found
    log(f"Season {season}, region {args.region}")
    if dungeons:
        log(f"Dungeons in rotation: {', '.join(d['name'] for d in dungeons)}")

    if args.dungeons:
        wanted = [x.strip() for x in args.dungeons.split(",") if x.strip()]
        known = {d["slug"]: d for d in dungeons}
        dungeons = [known.get(w, {"slug": w, "name": w.replace("-", " ").title(), "short": ""})
                    for w in wanted]
    if args.dungeon:
        dungeons = [d for d in dungeons if d["slug"] == args.dungeon] or \
                   [{"slug": args.dungeon, "name": args.dungeon, "short": ""}]

    all_runs = []
    per_dungeon = []
    for dg in dungeons:
        log(f"  {dg['name']}...")
        runs = fetch_dungeon(season, dg["slug"], args.pages, args.region)
        for r in runs:
            r["_dungeon"] = dg
        log(f"    {len(runs)} runs")
        all_runs.extend(runs)
        per_dungeon.append((dg, runs))
    if not all_runs:
        log("No runs came back. Try --probe.")
        return

    affixes = Counter()
    for r in all_runs:
        for m in r.get("weekly_modifiers") or []:
            if m.get("name"):
                affixes[m["name"]] += 1

    def summarise(runs):
        presence, pairs, levels = Counter(), defaultdict(Counter), defaultdict(list)
        comps, builds, roles = Counter(), defaultdict(Counter), {}
        heroes, hero_meta = defaultdict(Counter), {}
        counted = 0
        for r in runs:
            ps = players(r)
            if len(ps) < 5:
                continue
            counted += 1
            lvl = r.get("mythic_level")
            seen = []
            for p in ps:
                key = f"{p['spec']} {p['cls']}"
                roles[key] = p["role"]
                if key not in seen:
                    seen.append(key)
                    presence[key] += 1
                    if isinstance(lvl, (int, float)):
                        levels[key].append(int(lvl))
                if p["loadout"]:
                    builds[key][p["loadout"]] += 1
                    h = look_hero(p["loadout"])
                    if h and h.get("name"):
                        heroes[key][h["name"]] += 1
                        hero_meta[h["name"]] = h.get("icon", "")
            for a in seen:
                for b in seen:
                    if a != b:
                        pairs[a][b] += 1
            comp = tuple(sorted(seen, key=lambda k: (ROLE_ORDER.get(roles.get(k, "dps"), 2), k)))
            comps[comp] += 1
        return presence, pairs, levels, comps, builds, roles, counted, heroes, hero_meta

    look_hero = hero_lookup()
    presence, pairs, levels, comps, builds, roles, counted, heroes, hero_meta = summarise(all_runs)

    def spec_rows(presence, total, pairs, levels, builds, roles, heroes=None, hero_meta=None):
        rows = []
        for key, n in presence.most_common():
            lv = sorted(levels[key])
            rows.append({
                "name": key,
                "cls": key.split(" ", 1)[1] if " " in key else key,
                "spec": key.split(" ", 1)[0],
                "role": roles.get(key, "dps"),
                "runs": n,
                "share": n / total if total else 0,
                "low": lv[0] if lv else None,
                "high": lv[-1] if lv else None,
                "median": lv[len(lv) // 2] if lv else None,
                "with": [[w, c] for w, c in pairs[key].most_common(5)],
                "builds": [{"code": c, "n": k, "hero": (look_hero(c) or {}).get("name", "")}
                           for c, k in builds[key].most_common(3)],
                "heroes": [{"name": h, "n": k, "icon": (hero_meta or {}).get(h, "")}
                           for h, k in (heroes or {}).get(key, Counter()).most_common(3)],
            })
        return rows

    data = {
        "season": season,
        "region": args.region,
        "generated": datetime.now(timezone.utc).isoformat(),
        "runs": counted,
        "affixes": [a for a, _ in affixes.most_common(4)],
        "specs": spec_rows(presence, counted, pairs, levels, builds, roles, heroes, hero_meta),
        "comps": [{"specs": list(c), "n": n, "share": n / counted if counted else 0}
                  for c, n in comps.most_common(12)],
        "dungeons": [],
    }
    for dg, runs in per_dungeon:
        p, pr, lv, cp, bl, rl, cnt, hs, hm = summarise(runs)
        data["dungeons"].append({
            "slug": dg["slug"], "name": dg["name"], "short": dg.get("short", ""),
            "runs": cnt,
            "specs": spec_rows(p, cnt, pr, lv, bl, rl, hs, hm),
            "comps": [{"specs": list(c), "n": n, "share": n / cnt if cnt else 0}
                      for c, n in cp.most_common(6)],
        })

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(PAGE.replace("/*__DATA__*/null",
                             json.dumps(data, separators=(",", ":")).replace("</", "<\\/")))
    log(f"\n{counted} runs across {len(data['dungeons'])} dungeons -> {out}")
    top = data["specs"][:5]
    for s in top:
        log(f"  {s['name']}: in {s['share'] * 100:.0f}% of runs")


# --------------------------------------------------------------------------- #
# Probe
# --------------------------------------------------------------------------- #

def probe(args):
    season, dungeons = current_season(args.expansion)
    log(f"Season: {season}")
    log("Dungeons being run this season:")
    for d in dungeons:
        log(f"  {d['slug']}  ({d['name']})")
    d = get("/mythic-plus/runs", season=args.season or season, region=args.region,
            dungeon="all", affixes="all", page=0)
    rankings = d.get("rankings") or []
    log(f"\n{len(rankings)} rankings on page 0")
    if rankings:
        run = rankings[0].get("run") or {}
        log(f"  level {run.get('mythic_level')} {((run.get('dungeon') or {}).get('name'))}")
        for p in players(run):
            log(f"    {p['role']:<6} {p['spec']} {p['cls']}  loadout {'yes' if p['loadout'] else 'no'}")


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Mythic+ comps</title>
<style>
:root{
  color-scheme: light dark;
  --sans: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI Variable", "Segoe UI", Inter, system-ui, sans-serif;
  --tank:#3b82f6; --healer:#00b37e; --dps:#ff5f52; --accent:#8b5cf6;
  --r:18px; --r-sm:11px; --gap:clamp(14px,1.6vw,26px);
  --bg:#f2f2f6; --bg-2:#e8e8ef;
  --glass:rgba(255,255,255,.66); --glass-2:rgba(255,255,255,.82);
  --stroke:rgba(0,0,0,.08); --stroke-2:rgba(0,0,0,.12);
  --ink:#16161a; --dim:#65656e; --faint:#9a9aa3;
  --shadow:0 1px 2px rgba(0,0,0,.04), 0 8px 24px rgba(0,0,0,.06);
  --hover:rgba(0,0,0,.04); --track:rgba(0,0,0,.08);
}
html[data-theme="dark"]{
  --bg:#0b0b0e; --bg-2:#14141a;
  --glass:rgba(28,28,34,.62); --glass-2:rgba(36,36,44,.76);
  --stroke:rgba(255,255,255,.09); --stroke-2:rgba(255,255,255,.16);
  --ink:#f2f2f5; --dim:#a0a0ab; --faint:#6e6e78;
  --shadow:0 1px 2px rgba(0,0,0,.3), 0 12px 40px rgba(0,0,0,.4);
  --hover:rgba(255,255,255,.06); --track:rgba(255,255,255,.12);
}
*{box-sizing:border-box}
html{font-size:clamp(14px,.38vw + 10.2px,18px)}
body{
  margin:0;min-height:100%;
  background:
    radial-gradient(56rem 38rem at 10% -8%, color-mix(in srgb, var(--dps) 12%, transparent), transparent 60%),
    radial-gradient(50rem 32rem at 94% 2%, color-mix(in srgb, var(--healer) 13%, transparent), transparent 62%),
    linear-gradient(var(--bg), var(--bg-2));
  background-attachment:fixed;
  color:var(--ink);font-family:var(--sans);line-height:1.45;-webkit-font-smoothing:antialiased;
}
a{color:inherit}
button,select{font:inherit;color:inherit}
.num{font-variant-numeric:tabular-nums}
.glass{background:var(--glass);-webkit-backdrop-filter:saturate(180%) blur(22px);backdrop-filter:saturate(180%) blur(22px);
  border:1px solid var(--stroke);box-shadow:var(--shadow);border-radius:var(--r)}
.wrap{max-width:1180px;margin:0 auto;padding:clamp(16px,2.4vw,40px) clamp(12px,2vw,28px) 4rem}

header{display:flex;align-items:flex-start;justify-content:space-between;gap:1rem}
h1{font-size:clamp(1.7rem,3vw,2.4rem);font-weight:700;letter-spacing:-.03em;margin:0}
.sub{color:var(--dim);font-size:.8rem;margin-top:.45rem;display:flex;flex-wrap:wrap;gap:.3rem .5rem;align-items:center}
.pill{background:var(--glass-2);border:1px solid var(--stroke);border-radius:999px;padding:.1rem .6rem;color:var(--ink)}
.theme{width:2rem;height:2rem;border-radius:50%;border:1px solid var(--stroke);background:var(--glass-2);cursor:pointer;flex:0 0 auto}

nav{display:flex;flex-wrap:wrap;gap:.3rem;margin-top:1.2rem;padding-bottom:.2rem}
nav button{flex:0 0 auto;border:1px solid transparent;background:transparent;border-radius:999px;padding:.3rem .85rem;
  color:var(--dim);cursor:pointer;font-size:.86rem;white-space:nowrap}
nav button:hover{background:var(--hover);color:var(--ink)}
nav button[aria-selected="true"]{background:var(--glass-2);border-color:var(--stroke);color:var(--ink);font-weight:550}

.controls{display:flex;flex-wrap:wrap;gap:.7rem;align-items:center;margin:1rem 0 .4rem}
.seg{display:inline-flex;background:var(--track);border-radius:999px;padding:2px}
.seg button{background:none;border:0;padding:.26rem .85rem;border-radius:999px;color:var(--dim);cursor:pointer;font-size:.82rem}
.seg button[aria-pressed="true"]{background:var(--glass-2);color:var(--ink);box-shadow:0 1px 2px rgba(0,0,0,.08)}
.small{color:var(--dim);font-size:.8rem}

.panels{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:var(--gap);margin-top:.6rem}
.panel{padding:1rem 1.1rem}
.panel h2{font-size:1.05rem;font-weight:620;letter-spacing:-.01em;margin:0}
.panel p.note{color:var(--dim);font-size:.8rem;margin:.2rem 0 .8rem}

.tscroll{overflow-x:auto}
table{width:100%;border-collapse:separate;border-spacing:0 5px;min-width:520px}
th{text-align:left;color:var(--dim);font-weight:500;font-size:.74rem;padding:.15rem .55rem}
td{background:var(--glass-2);padding:.45rem .55rem;white-space:nowrap;border-top:1px solid var(--stroke);border-bottom:1px solid var(--stroke)}
td:first-child{border-radius:var(--r-sm) 0 0 var(--r-sm);border-left:1px solid var(--stroke)}
td:last-child{border-radius:0 var(--r-sm) var(--r-sm) 0;border-right:1px solid var(--stroke);white-space:normal;max-width:16rem}
tr.spec{cursor:pointer}
tr.spec:hover td{background:var(--hover)}
.role{display:inline-block;width:.45rem;height:.45rem;border-radius:50%;margin-right:.45rem;vertical-align:.05rem}
.bar{height:6px;border-radius:3px;background:var(--track);overflow:hidden;min-width:60px}
.bar i{display:block;height:100%;background:var(--rc,var(--accent));border-radius:3px}
.detail td{background:var(--glass);white-space:normal}
.builds{display:grid;gap:.4rem;margin-top:.4rem}
.build{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:.5rem;align-items:center}
.build code{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:.72rem;color:var(--dim);background:var(--track);border-radius:7px;padding:.25rem .45rem;user-select:all}
.copy{border:1px solid var(--stroke);background:var(--glass-2);border-radius:999px;padding:.2rem .7rem;cursor:pointer;font-size:.78rem;font-weight:550}
.copy.done{background:var(--healer);border-color:transparent;color:#fff}

.comp{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:.6rem;align-items:center;padding:.5rem 0;border-bottom:1px solid var(--stroke)}
.comp:last-child{border-bottom:0}
.comp .n{font-size:1.05rem;font-weight:640;color:var(--accent);min-width:2.6rem}
.comp .who{display:flex;flex-wrap:wrap;gap:.3rem}
.comp .who span{background:var(--track);border-radius:999px;padding:.1rem .55rem;font-size:.8rem}
.hero{display:inline-flex;align-items:center;gap:.25rem;background:var(--track);border-radius:999px;padding:.05rem .5rem;font-size:.74rem;margin-right:.2rem}
.hicon{border-radius:3px}
.fb-btn{position:fixed;right:clamp(12px,2vw,26px);bottom:clamp(12px,2vw,26px);z-index:30;
  border:1px solid var(--stroke);background:var(--glass-2);-webkit-backdrop-filter:saturate(180%) blur(22px);
  backdrop-filter:saturate(180%) blur(22px);box-shadow:var(--shadow);border-radius:999px;
  padding:.5rem 1rem;cursor:pointer;font-size:.86rem;font-weight:550}
dialog{border:1px solid var(--stroke);border-radius:var(--r);padding:0;color:var(--ink);background:var(--glass-2);
  -webkit-backdrop-filter:saturate(180%) blur(28px);backdrop-filter:saturate(180%) blur(28px);
  box-shadow:var(--shadow);width:min(30rem,92vw)}
dialog::backdrop{background:rgba(0,0,0,.35)}
.fb{padding:1.1rem 1.2rem;display:grid;gap:.7rem}
.fb h3{margin:0;font-size:1.05rem;font-weight:620}
.fb p{margin:0;color:var(--dim);font-size:.82rem}
.fb .kinds{display:flex;flex-wrap:wrap;gap:.35rem}
.fb .kinds button{border:1px solid var(--stroke);background:transparent;border-radius:999px;padding:.25rem .75rem;
  cursor:pointer;font-size:.82rem;color:var(--dim)}
.fb .kinds button[aria-pressed="true"]{background:var(--glass);color:var(--ink);font-weight:550}
.fb textarea{width:100%;min-height:7rem;resize:vertical;border:1px solid var(--stroke);border-radius:var(--r-sm);
  background:var(--glass);padding:.6rem .7rem;font:inherit;font-size:.88rem;color:var(--ink)}
.fb .row{display:flex;justify-content:space-between;align-items:center;gap:.6rem}
.fb .go{border:0;background:var(--ink);color:var(--bg);border-radius:999px;padding:.4rem 1.1rem;cursor:pointer;font-weight:600;font-size:.86rem}
.fb .go[disabled]{opacity:.45;cursor:default}
.fb .cancel{border:0;background:none;color:var(--dim);cursor:pointer;font-size:.84rem}
footer{margin-top:2.4rem;color:var(--faint);font-size:.78rem}
footer a{color:var(--dim)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Mythic+ comps</h1>
      <div class="sub" id="sub"></div>
    </div>
    <button class="theme" id="theme" aria-label="Switch between light and dark">\u25d0</button>
  </header>
  <nav id="tabs" role="tablist" aria-label="Dungeon"></nav>
  <div class="controls">
    <div class="seg" id="roles" role="group" aria-label="Role">
      <button data-v="all" aria-pressed="true">All roles</button>
      <button data-v="tank" aria-pressed="false">Tanks</button>
      <button data-v="healer" aria-pressed="false">Healers</button>
      <button data-v="dps" aria-pressed="false">DPS</button>
    </div>
    <span class="small">Click a spec for its talent builds.</span>
  </div>
  <div class="panels">
    <section class="panel glass">
      <h2 id="specTitle">Specs in the top keys</h2>
      <p class="note">How often each spec appears in a run, and the key levels it's showing up at.</p>
      <div class="tscroll"><table id="specs"></table></div>
    </section>
    <section class="panel glass">
      <h2>Groups that keep appearing</h2>
      <p class="note">The five-spec line-ups that show up most, tank first.</p>
      <div id="comps"></div>
    </section>
  </div>
  <footer>
    Data from <a href="https://raider.io" target="_blank" rel="noopener">Raider.IO</a>.
    Top keys are pushed by a small group of players, so read this as what's fashionable at the top, not as a rule.
  </footer>
</div>
<button class="fb-btn" id="fbOpen">Feedback</button>
<dialog id="fbDialog">
  <form class="fb" method="dialog">
    <h3>Send feedback</h3>
    <p>This opens a pre-filled issue on GitHub, where you click Submit. A free GitHub account is needed.</p>
    <div class="kinds" id="fbKinds">
      <button type="button" data-k="Bug" aria-pressed="true">Something's broken</button>
      <button type="button" data-k="Data" aria-pressed="false">Data looks wrong</button>
      <button type="button" data-k="Idea" aria-pressed="false">Idea</button>
    </div>
    <textarea id="fbText" placeholder="What happened, or what would you like to see?"></textarea>
    <div class="row">
      <button type="button" class="cancel" id="fbCancel">Cancel</button>
      <button type="button" class="go" id="fbGo" disabled>Open on GitHub</button>
    </div>
  </form>
</dialog>
<script>
const DATA = /*__DATA__*/null;
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"\']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","\'":"&#39;"}[c]));
const state = {dungeon:"all", role:"all", open:null, theme:"auto"};

const CLASS_LIGHT = {"Death Knight":"#C41E3A","Demon Hunter":"#A330C9","Druid":"#D2691E","Evoker":"#2E8B7A",
  "Hunter":"#6B8E23","Mage":"#1E90C8","Monk":"#00A878","Paladin":"#DB6FA0","Priest":"#6C6C75",
  "Rogue":"#B8901F","Shaman":"#2D7FD6","Warlock":"#6C6DD4","Warrior":"#9C7050"};
const CLASS_DARK = {"Death Knight":"#E0425C","Demon Hunter":"#B84FDB","Druid":"#FF9333","Evoker":"#3FBFA3",
  "Hunter":"#AAD372","Mage":"#3FC7EB","Monk":"#00E68A","Paladin":"#F48CBA","Priest":"#E8E8E8",
  "Rogue":"#FFF468","Shaman":"#4E9BF5","Warlock":"#9A9BFF","Warrior":"#C69B6D"};
const isDark = () => document.documentElement.dataset.theme === "dark";
const classColor = c => (isDark() ? CLASS_DARK : CLASS_LIGHT)[c] || (isDark() ? "#bbb" : "#666");
const roleColor = r => r === "tank" ? "var(--tank)" : r === "healer" ? "var(--healer)" : "var(--dps)";

function applyTheme(){
  let t = state.theme;
  if(t === "auto") t = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  document.documentElement.dataset.theme = t;
  $("#theme").textContent = t === "dark" ? "\u263e" : "\u2600";
  try{ localStorage.setItem("mplus-theme", state.theme); }catch(e){}
}

const UPDATE_HOUR_UTC = 0;
function nextUpdate(){
  const now = new Date();
  const next = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), UPDATE_HOUR_UTC, 0, 0));
  if(next <= now) next.setUTCDate(next.getUTCDate() + 1);
  return next;
}
function tickCountdown(){
  const el = document.getElementById("next");
  if(!el) return;
  const ms = nextUpdate() - Date.now();
  const h = Math.floor(ms / 3600000), m = Math.floor(ms % 3600000 / 60000);
  el.textContent = "Next update in " + (h >= 1 ? h + "h " + m + "m" : m >= 1 ? m + "m" : "any moment");
  el.title = "Runs daily at " + String(UPDATE_HOUR_UTC).padStart(2,"0") + ":00 UTC";
}

function view(){
  if(state.dungeon === "all") return {specs:DATA.specs, comps:DATA.comps, runs:DATA.runs};
  const d = DATA.dungeons.find(x => x.slug === state.dungeon);
  return d ? {specs:d.specs, comps:d.comps, runs:d.runs} : {specs:[], comps:[], runs:0};
}

async function copyText(t){
  try{ await navigator.clipboard.writeText(t); return true; }
  catch(e){
    const ta = document.createElement("textarea");
    ta.value = t; ta.style.position="fixed"; ta.style.opacity="0";
    document.body.appendChild(ta); ta.select();
    let ok=false; try{ ok = document.execCommand("copy"); }catch(_){}
    ta.remove(); return ok;
  }
}

function render(){
  const v = view();
  const specs = v.specs.filter(s => state.role === "all" || s.role === state.role);
  const top = specs.length ? specs[0].share : 1;
  $("#specTitle").textContent = state.dungeon === "all"
    ? `Specs across ${v.runs} top runs` : `Specs in ${v.runs} top runs here`;
  $("#specs").innerHTML =
    `<thead><tr><th>Spec</th><th>In runs</th><th></th><th>Key level</th><th>Hero talents</th><th>Brought with</th></tr></thead><tbody>` +
    (specs.length ? specs.map(s => {
      const lvl = s.low == null ? "\u2013" : (s.low === s.high ? s.low : `${s.low}\u2013${s.high}`);
      const open = state.open === s.name;
      const builds = s.builds.length
        ? s.builds.map(b => `<div class="build">
             <code title="${esc(b.code)}">${b.hero ? esc(b.hero) + " \u00b7 " : ""}${esc(b.code)}</code>
             <span class="small num">${b.n} run${b.n > 1 ? "s" : ""}</span>
             <button class="copy" data-code="${esc(b.code)}">Copy</button></div>`).join("")
        : `<div class="small">No talent strings in these runs.</div>`;
      return `<tr class="spec" data-name="${esc(s.name)}" style="--rc:${classColor(s.cls)}">
          <td><span class="role" style="background:${roleColor(s.role)}"></span>${esc(s.name)}</td>
          <td class="num">${(s.share*100).toFixed(0)}%</td>
          <td><span class="bar"><i style="width:${(s.share/top*100).toFixed(1)}%"></i></span></td>
          <td class="num">${lvl}${s.median != null ? ` <span class="small">med ${s.median}</span>` : ""}</td>
          <td>${(s.heroes && s.heroes.length) ? s.heroes.map(h => `<span class="hero">${h.icon ? `<img class="hicon" src="https://wow.zamimg.com/images/wow/icons/medium/${esc(h.icon)}.jpg" width="14" height="14" alt="" onerror="this.remove()">` : ""}${esc(h.name)} ${h.n}</span>`).join(" ") : "\u2013"}</td>
          <td>${s.with.map(w => esc(w[0])).slice(0,3).join(", ") || "\u2013"}</td>
        </tr>` + (open ? `<tr class="detail"><td colspan="6">
          <div class="small">Most used talent builds, from the runs above.</div>
          <div class="builds">${builds}</div></td></tr>` : "");
    }).join("") : `<tr><td colspan="6">Nothing here yet.</td></tr>`) + `</tbody>`;

  $("#comps").innerHTML = v.comps.length ? v.comps.map(c => `
    <div class="comp">
      <div class="n num">${(c.share*100).toFixed(0)}%</div>
      <div class="who">${c.specs.map(s => `<span>${esc(s)}</span>`).join("")}</div>
      <div class="small num">${c.n}</div>
    </div>`).join("") : `<div class="small">Nothing here yet.</div>`;
}

const REPO = "Bysonuk/Mythic-Top-10";
function initFeedback(){
  const dlg = $("#fbDialog"), text = $("#fbText"), go = $("#fbGo");
  let kind = "Bug";
  $("#fbOpen").addEventListener("click", () => dlg.showModal());
  $("#fbCancel").addEventListener("click", () => dlg.close());
  $("#fbKinds").addEventListener("click", e => {
    const b = e.target.closest("button"); if(!b) return;
    kind = b.dataset.k;
    document.querySelectorAll("#fbKinds button").forEach(x => x.setAttribute("aria-pressed", x === b));
  });
  text.addEventListener("input", () => { go.disabled = text.value.trim().length < 5; });
  go.addEventListener("click", () => {
    const title = "[" + kind + "] " + text.value.trim().split("\n")[0].slice(0, 70);
    const body = [text.value.trim(), "", "---", "Page: Mythic+ comps",
      "Dungeon: " + state.dungeon, "Season: " + (DATA ? DATA.season : "?"),
      "Generated: " + (DATA ? DATA.generated : "?"), "Browser: " + navigator.userAgent].join("\n");
    window.open("https://github.com/" + REPO + "/issues/new?title=" + encodeURIComponent(title)
      + "&body=" + encodeURIComponent(body) + "&labels=" + encodeURIComponent(kind.toLowerCase()),
      "_blank", "noopener");
    dlg.close(); text.value = ""; go.disabled = true;
  });
}

function init(){
  initFeedback();
  try{ state.theme = localStorage.getItem("mplus-theme") || "auto"; }catch(e){}
  applyTheme();
  $("#theme").addEventListener("click", () => {
    state.theme = isDark() ? "light" : "dark"; applyTheme(); render();
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if(state.theme === "auto"){ applyTheme(); render(); }
  });
  if(!DATA){ document.body.innerHTML = "<p style=\'padding:2rem\'>No data yet. Run mplus.py.</p>"; return; }
  $("#sub").innerHTML = `<span class="pill">${esc(DATA.season)}</span>`
    + `<span class="pill">${esc(DATA.region)}</span>`
    + DATA.affixes.map(a => `<span class="pill">${esc(a)}</span>`).join("")
    + `<span>${DATA.runs} runs \u00b7 updated ${esc(new Date(DATA.generated).toLocaleString())}</span>`
    + `<span id="next" style="color:var(--faint)"></span>`;
  tickCountdown();
  setInterval(tickCountdown, 30000);
  $("#tabs").innerHTML = [["all","All dungeons"]].concat(DATA.dungeons.map(d => [d.slug, d.name]))
    .map(([v,n]) => `<button role="tab" data-v="${esc(v)}" aria-selected="${v === state.dungeon}">${esc(n)}</button>`).join("");
  $("#tabs").addEventListener("click", e => {
    const b = e.target.closest("button"); if(!b) return;
    state.dungeon = b.dataset.v; state.open = null;
    document.querySelectorAll("#tabs button").forEach(x => x.setAttribute("aria-selected", x === b));
    render();
  });
  $("#roles").addEventListener("click", e => {
    const b = e.target.closest("button"); if(!b) return;
    state.role = b.dataset.v;
    document.querySelectorAll("#roles button").forEach(x => x.setAttribute("aria-pressed", x === b));
    render();
  });
  $("#specs").addEventListener("click", async e => {
    const copy = e.target.closest(".copy");
    if(copy){
      const ok = await copyText(copy.dataset.code);
      copy.textContent = ok ? "Copied" : "Select and copy";
      if(ok) copy.classList.add("done");
      setTimeout(() => { copy.textContent = "Copy"; copy.classList.remove("done"); }, 1500);
      return;
    }
    const row = e.target.closest("tr.spec"); if(!row) return;
    state.open = state.open === row.dataset.name ? null : row.dataset.name;
    render();
  });
  render();
}
init();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="Mythic+ comps from Raider.IO")
    ap.add_argument("--probe", action="store_true", help="show what the API returns, then stop")
    ap.add_argument("--season", help="season slug, e.g. season-mn-2")
    ap.add_argument("--expansion", type=int, default=11, help="expansion ID (default 11)")
    ap.add_argument("--dungeon", help="one dungeon slug only")
    ap.add_argument("--dungeons", help="comma-separated slugs, if the automatic list is wrong")
    ap.add_argument("--region", default="world", help="world, us, eu, kr, tw")
    ap.add_argument("--pages", type=int, default=5, help="pages per dungeon, 20 runs each")
    ap.add_argument("--out", default="site/mplus.html", help="where to write the page")
    args = ap.parse_args()
    try:
        probe(args) if args.probe else build(args)
    except Exception as e:
        log(f"Something went wrong: {e}")
        log("Run with --probe and send Claude the output.")
        sys.exit(1)


if __name__ == "__main__":
    main()
