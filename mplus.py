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
    d = get("/mythic-plus/static-data", expansion_id=expansion)
    seasons = [s for s in (d.get("seasons") or []) if s.get("slug")]
    dungeons = [{"slug": x["slug"], "name": x.get("name", x["slug"]),
                 "short": x.get("short_name", "")} for x in (d.get("dungeons") or [])]
    # Side seasons ("break-the-meta", "cutoffs", "remix") aren't the live ladder
    main = [s for s in seasons if s["slug"].count("-") <= 2]
    season = (main or seasons)[0]["slug"]
    return season, dungeons


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

def build(args):
    season = args.season
    dungeons = []
    if not season or not args.dungeon:
        season_found, dungeons = current_season(args.expansion)
        season = season or season_found
    log(f"Season {season}, region {args.region}")

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
            for a in seen:
                for b in seen:
                    if a != b:
                        pairs[a][b] += 1
            comp = tuple(sorted(seen, key=lambda k: (ROLE_ORDER.get(roles.get(k, "dps"), 2), k)))
            comps[comp] += 1
        return presence, pairs, levels, comps, builds, roles, counted

    presence, pairs, levels, comps, builds, roles, counted = summarise(all_runs)

    def spec_rows(presence, total, pairs, levels, builds, roles):
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
                "builds": [{"code": c, "n": k} for c, k in builds[key].most_common(3)],
            })
        return rows

    data = {
        "season": season,
        "region": args.region,
        "generated": datetime.now(timezone.utc).isoformat(),
        "runs": counted,
        "affixes": [a for a, _ in affixes.most_common(4)],
        "specs": spec_rows(presence, counted, pairs, levels, builds, roles),
        "comps": [{"specs": list(c), "n": n, "share": n / counted if counted else 0}
                  for c, n in comps.most_common(12)],
        "dungeons": [],
    }
    for dg, runs in per_dungeon:
        p, pr, lv, cp, bl, rl, cnt = summarise(runs)
        data["dungeons"].append({
            "slug": dg["slug"], "name": dg["name"], "short": dg.get("short", ""),
            "runs": cnt,
            "specs": spec_rows(p, cnt, pr, lv, bl, rl),
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
<title>Mythic+ comps</title>
<link href="https://fonts.googleapis.com/css2?family=Saira+Extra+Condensed:wght@500;700&family=Saira+Semi+Condensed:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#1a1e26; --surface:#232833; --raised:#2c3240; --line:#373e4e;
  --ink:#eef0f4; --dim:#9aa3b5; --faint:#6c7589; --gold:#c9a14a;
  --tank:#6ea8ff; --healer:#3ecf9a; --dps:#ff6b5b;
  --display:"Saira Extra Condensed","Arial Narrow",sans-serif;
  --ui:"Saira Semi Condensed","Segoe UI",Arial,sans-serif;
}
*{box-sizing:border-box}
html{font-size:clamp(14px,0.42vw + 9.5px,19px)}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--ui);line-height:1.4}
a{color:inherit}
button,select{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--gold);outline-offset:2px}
.num{font-variant-numeric:tabular-nums}
.wrap{max-width:1180px;margin:0 auto;padding:clamp(14px,2vw,34px) clamp(12px,2vw,28px) 4rem}

header h1{font-family:var(--display);font-weight:700;font-size:clamp(2rem,4vw,3.2rem);margin:0;line-height:1}
.sub{color:var(--dim);font-size:.85rem;margin-top:.4rem;display:flex;flex-wrap:wrap;gap:.3rem .8rem;align-items:center}
.pill{background:var(--surface);border:1px solid var(--line);border-radius:999px;padding:.1rem .6rem;color:var(--ink)}

nav{display:flex;gap:.2rem;overflow-x:auto;border-bottom:1px solid var(--line);margin-top:1.2rem;scrollbar-width:none}
nav::-webkit-scrollbar{display:none}
nav button{flex:0 0 auto;background:none;border:0;border-bottom:3px solid transparent;padding:.5rem .8rem;color:var(--dim);cursor:pointer;font-family:var(--display);font-size:1.15rem;font-weight:500;white-space:nowrap}
nav button:hover{color:var(--ink)}
nav button[aria-selected="true"]{color:var(--ink);border-bottom-color:var(--gold)}

.controls{display:flex;flex-wrap:wrap;gap:.6rem;align-items:center;margin:1rem 0}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}
.seg button{background:none;border:0;padding:.35rem .8rem;color:var(--dim);cursor:pointer}
.seg button+button{border-left:1px solid var(--line)}
.seg button[aria-pressed="true"]{background:var(--raised);color:var(--ink)}

.panels{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:clamp(12px,1.6vw,26px);margin-top:.6rem}
.panel{background:var(--surface);border-radius:8px;padding:1rem 1.1rem}
.panel h2{font-family:var(--display);font-weight:500;font-size:1.5rem;margin:0 0 .2rem}
.panel p.note{color:var(--dim);font-size:.8rem;margin:0 0 .8rem}

.tscroll{overflow-x:auto}
table{width:100%;border-collapse:separate;border-spacing:0 3px;min-width:520px}
th{text-align:left;color:var(--dim);font-weight:500;font-size:.78rem;padding:.2rem .5rem}
td{background:var(--raised);padding:.45rem .5rem;white-space:nowrap}
td:first-child{border-radius:6px 0 0 6px;border-left:3px solid var(--rc,transparent)}
td:last-child{border-radius:0 6px 6px 0;white-space:normal;max-width:16rem}
tr.spec{cursor:pointer}
tr.spec:hover td{background:#333a4a}
.role{display:inline-block;width:.5rem;height:.5rem;border-radius:50%;margin-right:.45rem;vertical-align:.05rem}
.bar{height:8px;border-radius:4px;background:var(--line);overflow:hidden;min-width:60px}
.bar i{display:block;height:100%;background:var(--gold)}
.detail td{background:var(--surface);white-space:normal}
.builds{display:grid;gap:.4rem;margin-top:.4rem}
.build{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:.5rem;align-items:center}
.build code{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.75rem;color:var(--dim);background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:.25rem .45rem;user-select:all}
.copy{border:1px solid var(--line);background:var(--raised);border-radius:5px;padding:.2rem .7rem;cursor:pointer;font-size:.8rem;font-weight:600}
.copy.done{background:var(--healer);border-color:var(--healer);color:#0d2a20}
.small{color:var(--dim);font-size:.8rem}

.comp{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:.6rem;align-items:center;padding:.45rem 0;border-bottom:1px solid var(--line)}
.comp:last-child{border-bottom:0}
.comp .n{font-family:var(--display);font-size:1.3rem;color:var(--gold);min-width:2.4rem}
.comp .who{display:flex;flex-wrap:wrap;gap:.3rem}
.comp .who span{background:var(--raised);border-radius:4px;padding:.1rem .45rem;font-size:.82rem}
footer{margin-top:2.5rem;color:var(--dim);font-size:.8rem}
footer a{color:var(--ink)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Mythic+ comps</h1>
    <div class="sub" id="sub"></div>
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
    <section class="panel">
      <h2 id="specTitle">Specs in the top keys</h2>
      <p class="note">How often each spec appears in a run, and the key levels it's showing up at.</p>
      <div class="tscroll"><table id="specs"></table></div>
    </section>
    <section class="panel">
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
<script>
const DATA = /*__DATA__*/null;
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const state = {dungeon: "all", role: "all", open: null};

const CLASS_COLORS = {
  "Death Knight":"#E0425C","Demon Hunter":"#B84FDB","Druid":"#FF7C0A","Evoker":"#3AAE96",
  "Hunter":"#AAD372","Mage":"#3FC7EB","Monk":"#00E68A","Paladin":"#F48CBA","Priest":"#E8E8E8",
  "Rogue":"#FFF468","Shaman":"#3A8EF0","Warlock":"#9A9BFF","Warrior":"#C69B6D"
};
const roleColor = r => r === "tank" ? "var(--tank)" : r === "healer" ? "var(--healer)" : "var(--dps)";

function view(){
  if(state.dungeon === "all") return {specs: DATA.specs, comps: DATA.comps, runs: DATA.runs};
  const d = DATA.dungeons.find(x => x.slug === state.dungeon);
  return d ? {specs: d.specs, comps: d.comps, runs: d.runs} : {specs: [], comps: [], runs: 0};
}

async function copyText(t){
  try{ await navigator.clipboard.writeText(t); return true; }
  catch(e){
    const ta = document.createElement("textarea");
    ta.value = t; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    let ok = false; try{ ok = document.execCommand("copy"); }catch(_){}
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
    `<thead><tr><th>Spec</th><th>In runs</th><th></th><th>Key level</th><th>Brought with</th></tr></thead><tbody>` +
    (specs.length ? specs.map(s => {
      const lvl = s.low == null ? "–" : (s.low === s.high ? s.low : `${s.low}–${s.high}`);
      const open = state.open === s.name;
      const builds = s.builds.length
        ? s.builds.map((b, i) => `<div class="build">
             <code title="${esc(b.code)}">${esc(b.code)}</code>
             <span class="small num">${b.n} run${b.n > 1 ? "s" : ""}</span>
             <button class="copy" data-code="${esc(b.code)}">Copy</button></div>`).join("")
        : `<div class="small">No talent strings in these runs.</div>`;
      return `<tr class="spec" data-name="${esc(s.name)}" style="--rc:${CLASS_COLORS[s.cls] || "#888"}">
          <td><span class="role" style="background:${roleColor(s.role)}"></span>${esc(s.name)}</td>
          <td class="num">${(s.share * 100).toFixed(0)}%</td>
          <td><span class="bar"><i style="width:${(s.share / top * 100).toFixed(1)}%"></i></span></td>
          <td class="num">${lvl}${s.median != null ? ` <span class="small">med ${s.median}</span>` : ""}</td>
          <td>${s.with.map(w => esc(w[0])).slice(0, 3).join(", ") || "–"}</td>
        </tr>` + (open ? `<tr class="detail"><td colspan="5">
          <div class="small">Most used talent builds, from the runs above.</div>
          <div class="builds">${builds}</div></td></tr>` : "");
    }).join("") : `<tr><td colspan="5">Nothing here yet.</td></tr>`) + `</tbody>`;

  $("#comps").innerHTML = v.comps.length ? v.comps.map(c => `
    <div class="comp">
      <div class="n num">${(c.share * 100).toFixed(0)}%</div>
      <div class="who">${c.specs.map(s => `<span>${esc(s)}</span>`).join("")}</div>
      <div class="small num">${c.n}</div>
    </div>`).join("") : `<div class="small">Nothing here yet.</div>`;
}

function init(){
  if(!DATA){ document.body.innerHTML = "<p style='padding:2rem'>No data yet. Run mplus.py.</p>"; return; }
  $("#sub").innerHTML = `<span class="pill">${esc(DATA.season)}</span>`
    + `<span class="pill">${esc(DATA.region)}</span>`
    + DATA.affixes.map(a => `<span class="pill">${esc(a)}</span>`).join("")
    + `<span>${DATA.runs} runs, updated ${esc(new Date(DATA.generated).toLocaleString())}</span>`;

  $("#tabs").innerHTML = [["all", "All dungeons"]].concat(DATA.dungeons.map(d => [d.slug, d.name]))
    .map(([v, n]) => `<button role="tab" data-v="${esc(v)}" aria-selected="${v === state.dungeon}">${esc(n)}</button>`).join("");
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
