#!/usr/bin/env python3
"""
Mythic+ comps, from the Raider.IO API.

Stage one: which specs appear in the top keys, who they're brought with, and at
what key levels. Stats, trinkets and talents come later, from Warcraft Logs.

    python mplus.py --probe                 # show what the API returns, and stop
    python mplus.py --probe --season X      # probe a particular season slug
    python mplus.py --pages 10              # build the page from the top runs
    python mplus.py --out site/mplus.html   # where to write it

Set RIO_API_KEY to raise the rate limit; without it, Raider.IO allows 200
requests a minute unauthenticated, which is plenty for this.

Data from Raider.IO. Any page built from it must credit and link back to them,
which the generated page does.
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
            body = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"{e.code} on {path}: {body}")
        except urllib.error.URLError:
            if attempt < 4:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"Gave up on {path}")


# --------------------------------------------------------------------------- #
# Probe: show what the API actually returns
# --------------------------------------------------------------------------- #

def shape(obj, depth=0, prefix=""):
    """Print the structure of a JSON blob, a couple of levels deep."""
    pad = "  " * depth
    if isinstance(obj, dict):
        for k, v in list(obj.items())[:25]:
            if isinstance(v, (dict, list)):
                log(f"{pad}{prefix}{k}: {type(v).__name__}")
                if depth < 3:
                    shape(v, depth + 1)
            else:
                val = str(v)[:60]
                log(f"{pad}{prefix}{k} = {val}")
    elif isinstance(obj, list):
        log(f"{pad}[{len(obj)} items]")
        if obj and depth < 3:
            shape(obj[0], depth + 1, prefix="[0] ")


def probe(args):
    log("=== expansions and seasons ===")
    for exp in (args.expansion, 11, 12, 10):
        if not exp:
            continue
        try:
            d = get("/mythic-plus/static-data", expansion_id=exp)
        except Exception as e:
            log(f"  expansion {exp}: {e}")
            continue
        seasons = d.get("seasons") or []
        dungeons = d.get("dungeons") or []
        log(f"  expansion {exp}: {len(seasons)} seasons, {len(dungeons)} dungeons")
        for s in seasons[:6]:
            log(f"    season slug: {s.get('slug')}  starts: {str(s.get('starts'))[:40]}")
        for dg in dungeons[:12]:
            log(f"    dungeon: {dg.get('slug')}  ({dg.get('name')})  id={dg.get('id')}")
        if seasons and not args.season:
            args.season = seasons[0].get("slug")

    season = args.season or "season-tww-3"
    log(f"\n=== top runs, season {season} ===")
    try:
        d = get("/mythic-plus/runs", season=season, region="world",
                dungeon="all", affixes="all", page=0)
    except Exception as e:
        log(f"  failed: {e}")
        log("  Try --season with a slug from the list above.")
        return
    log("  top-level keys: " + ", ".join(d.keys()))
    rankings = d.get("rankings") or []
    log(f"  rankings: {len(rankings)}")
    if rankings:
        log("\n=== shape of one ranking ===")
        shape(rankings[0])
        log("\n=== raw first ranking (trimmed) ===")
        log(json.dumps(rankings[0])[:2500])
    log("\nSend this output to Claude to finish the Mythic+ section.")


# --------------------------------------------------------------------------- #
# Build: spec presence, pairings and key levels
# --------------------------------------------------------------------------- #

def dig(obj, *names):
    """Pull the first matching key out of a dict, at the top level or one down."""
    if not isinstance(obj, dict):
        return None
    for n in names:
        if n in obj:
            return obj[n]
    for v in obj.values():
        if isinstance(v, dict):
            for n in names:
                if n in v:
                    return v[n]
    return None


def roster_specs(run):
    """Best-effort: pull (class, spec) for each of the five players."""
    roster = dig(run, "roster", "members", "characters") or []
    out = []
    for member in roster:
        ch = member.get("character", member) if isinstance(member, dict) else {}
        spec = ch.get("spec") or member.get("spec") or {}
        cls = ch.get("class") or member.get("class") or {}
        spec_name = spec.get("name") if isinstance(spec, dict) else spec
        class_name = cls.get("name") if isinstance(cls, dict) else cls
        if spec_name and class_name:
            out.append((str(class_name), str(spec_name)))
    return out


def fetch_runs(season, pages, dungeon="all", region="world"):
    runs = []
    for page in range(pages):
        d = get("/mythic-plus/runs", season=season, region=region,
                dungeon=dungeon, affixes="all", page=page)
        rankings = d.get("rankings") or []
        if not rankings:
            break
        for r in rankings:
            run = r.get("run", r)
            runs.append(run)
        log(f"  page {page + 1}: {len(rankings)} runs")
        time.sleep(0.4)          # stay well inside 200 requests a minute
    return runs


def build(args):
    season = args.season
    if not season:
        d = get("/mythic-plus/static-data", expansion_id=args.expansion or 11)
        seasons = d.get("seasons") or []
        season = (seasons[0] or {}).get("slug") if seasons else None
        if not season:
            log("Couldn't work out the current season. Pass --season.")
            return
    log(f"Season: {season}")

    log("Fetching top runs...")
    runs = fetch_runs(season, args.pages, region=args.region)
    log(f"{len(runs)} runs")
    if not runs:
        log("Nothing came back. Run --probe to see what the API is returning.")
        return

    presence = Counter()
    pairs = defaultdict(Counter)
    levels = defaultdict(list)
    by_dungeon = defaultdict(Counter)
    dungeon_names = {}
    total_by_dungeon = Counter()

    for run in runs:
        specs = roster_specs(run)
        if not specs:
            continue
        dg = dig(run, "dungeon") or {}
        dg_name = dg.get("name") if isinstance(dg, dict) else str(dg)
        dg_slug = dg.get("slug") if isinstance(dg, dict) else str(dg)
        dungeon_names[dg_slug] = dg_name
        total_by_dungeon[dg_slug] += 1
        level = dig(run, "mythic_level", "keystone_level", "level")
        seen = set()
        for cls, spec in specs:
            key = f"{spec} {cls}"
            if key in seen:
                continue           # count a spec once per run, not twice for doubles
            seen.add(key)
            presence[key] += 1
            by_dungeon[dg_slug][key] += 1
            if isinstance(level, (int, float)):
                levels[key].append(level)
        for a in seen:
            for b in seen:
                if a != b:
                    pairs[a][b] += 1

    total = sum(1 for r in runs if roster_specs(r))
    data = {
        "season": season,
        "region": args.region,
        "generated": datetime.now(timezone.utc).isoformat(),
        "runs": total,
        "dungeons": [{"slug": s, "name": dungeon_names.get(s, s), "runs": total_by_dungeon[s],
                      "specs": by_dungeon[s].most_common()} for s in total_by_dungeon],
        "specs": [{
            "name": k,
            "runs": n,
            "share": n / total if total else 0,
            "levels": sorted(levels[k]),
            "with": pairs[k].most_common(6),
        } for k, n in presence.most_common()],
    }

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(PAGE.replace("/*__DATA__*/null",
                             json.dumps(data, separators=(",", ":")).replace("</", "<\\/")))
    log(f"Written to {out}")


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mythic+ comps</title>
<style>
:root{--bg:#1a1e26;--surface:#232833;--line:#373e4e;--ink:#eef0f4;--dim:#9aa3b5;--gold:#c9a14a}
*{box-sizing:border-box}
html{font-size:clamp(14px,0.42vw + 9.5px,19px)}
body{margin:0;background:var(--bg);color:var(--ink);font-family:"Saira Semi Condensed","Segoe UI",Arial,sans-serif;line-height:1.4}
.wrap{max-width:1100px;margin:0 auto;padding:clamp(14px,2vw,32px)}
h1{font-size:clamp(1.8rem,3vw,2.6rem);margin:0}
.sub{color:var(--dim);font-size:.85rem;margin-top:.3rem}
table{width:100%;border-collapse:separate;border-spacing:0 3px;margin-top:1.4rem}
th{text-align:left;color:var(--dim);font-weight:500;font-size:.8rem;padding:.3rem .6rem}
td{background:var(--surface);padding:.5rem .6rem;white-space:nowrap}
td:first-child{border-radius:6px 0 0 6px}
td:last-child{border-radius:0 6px 6px 0;white-space:normal}
.bar{height:8px;border-radius:4px;background:var(--line);overflow:hidden;min-width:80px}
.bar i{display:block;height:100%;background:var(--gold)}
.num{font-variant-numeric:tabular-nums}
footer{margin-top:2rem;color:var(--dim);font-size:.8rem}
footer a{color:var(--ink)}
</style>
</head>
<body>
<div class="wrap">
  <h1 id="title">Mythic+ comps</h1>
  <div class="sub" id="sub"></div>
  <table id="tbl"></table>
  <footer>Data from <a href="https://raider.io" target="_blank" rel="noopener">Raider.IO</a>.</footer>
</div>
<script>
const DATA = /*__DATA__*/null;
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function median(a){ if(!a.length) return null; const m = Math.floor(a.length/2);
  return a.length % 2 ? a[m] : (a[m-1]+a[m])/2; }
if(!DATA){ document.getElementById("tbl").innerHTML = "<tr><td>No data yet.</td></tr>"; }
else {
  document.getElementById("sub").textContent =
    `${DATA.runs} top runs, ${DATA.season}, ${DATA.region}. Updated ${new Date(DATA.generated).toLocaleString()}.`;
  const top = DATA.specs[0] ? DATA.specs[0].share : 1;
  document.getElementById("tbl").innerHTML =
    `<thead><tr><th>Spec</th><th>In runs</th><th></th><th>Key level</th><th>Usually with</th></tr></thead><tbody>` +
    DATA.specs.map(s => {
      const lv = s.levels.length ? `${s.levels[0]}–${s.levels[s.levels.length-1]} (median ${median(s.levels)})` : "–";
      return `<tr><td>${esc(s.name)}</td>
        <td class="num">${(s.share*100).toFixed(0)}%</td>
        <td><span class="bar"><i style="width:${(s.share/top*100).toFixed(1)}%"></i></span></td>
        <td class="num">${lv}</td>
        <td>${s.with.map(w => esc(w[0])).join(", ")}</td></tr>`;
    }).join("") + `</tbody>`;
}
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="Mythic+ comps from Raider.IO")
    ap.add_argument("--probe", action="store_true", help="show what the API returns, then stop")
    ap.add_argument("--season", help="season slug, e.g. season-tww-3")
    ap.add_argument("--expansion", type=int, help="expansion ID for static data")
    ap.add_argument("--region", default="world", help="world, us, eu, kr, tw")
    ap.add_argument("--pages", type=int, default=10, help="pages of top runs to read")
    ap.add_argument("--out", default="site/mplus.html", help="where to write the page")
    args = ap.parse_args()
    try:
        if args.probe:
            probe(args)
        else:
            build(args)
    except Exception as e:
        log(f"Something went wrong: {e}")
        log("Run with --probe and send Claude the output.")
        sys.exit(1)


if __name__ == "__main__":
    main()
