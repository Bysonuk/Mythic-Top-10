# Mythic Stat Sheet

The top 10 Mythic raid logs for every spec in World of Warcraft, with each
player's secondary stats, trinkets and talent import code, so you don't have to
click through Warcraft Logs one ranking at a time.

Two ways to read it:

- **A website**, rebuilt and published automatically: <https://bysonuk.github.io/Mythic-Top-10/>
- **An in-game addon**, `/ms`, with a Copy button for every talent build

All data comes from public Warcraft Logs rankings.

---

## What it shows

For every spec, on every Mythic boss of the current raid:

- The top 10 players, with DPS or HPS, item level, guild, realm, kill time and a
  link to the log
- Critical Strike, Haste, Mastery and Versatility at the pull, as ratings and as
  a share of their total
- Both trinkets, with item levels
- The talent import code, ready to paste into the game

The website adds a "Compare all specs" grid showing every spec's stat split side
by side, per-spec stat ranges across the top 10, trinket usage counts, and talent
builds grouped so the common one stands out.

---

## How it runs

One Python script does everything: fetch, build the page, build the addon.

A GitHub Actions workflow runs it on a schedule, commits the results and
publishes the site to GitHub Pages. Downloaded logs are cached, so each run only
fetches entries that are new to the top 10, and logs are dropped from the cache
when players fall out of the rankings.

Warcraft Logs allows 3,600 API points an hour. A run either waits for the reset
or stops and publishes what it has, depending on the flags. Nothing is lost
either way: the next run carries on from the cache.

---

## Setting up your own copy

### 1. Create the repository

Make a **public** repo. GitHub Pages and unlimited Actions minutes are free on
public repos; private repos need a paid plan for Pages.

Upload `wcl_mythic_stats.py`, `cf_upload.py`, `README.md` and `.gitignore`, then
create `.github/workflows/update-stat-sheet.yml` and paste the workflow in.

### 2. Add your Warcraft Logs key

Create an API client at <https://www.warcraftlogs.com/api/clients>. Redirect URL
`http://localhost`, and leave "Public Client" unticked.

In **Settings → Secrets and variables → Actions**, add:

| Name                | Value              |
| ------------------- | ------------------ |
| `WCL_CLIENT_ID`     | your Client ID     |
| `WCL_CLIENT_SECRET` | your Client Secret |

### 3. Turn on Pages

**Settings → Pages → Source → GitHub Actions**.

### 4. Run it

**Actions → Update stat sheet → Run workflow.**

The first fill takes several runs, because there are a couple of thousand logs to
read and the hourly API limit caps how fast that can go. Each run publishes what
it has, so the site fills in as it goes. Once it's caught up, a run is just the
rankings check plus any new entries.

---

## Publishing the addon to CurseForge

`cf_upload.py` uploads the built zip through CurseForge's API, at most once a day
and only when the addon has changed. Add two more secrets:

| Name            | Value                                                  |
| --------------- | ------------------------------------------------------ |
| `CF_API_TOKEN`  | from <https://legacy.curseforge.com/account/api-tokens> |
| `CF_PROJECT_ID` | the numeric project ID on your CurseForge project page  |

Without them the step does nothing, so the rest still works. Leave CurseForge's
own **Automatic Packaging** set to "No automatic packaging": it reacts to GitHub
releases, not to files in the repo.

---

## Running it on your own PC

```
python wcl_mythic_stats.py --all          # fetch everything and open the page
python wcl_mythic_stats.py --offline      # rebuild the page from saved data
python wcl_mythic_stats.py --offline --addon   # also build the addon folder
python wcl_mythic_stats.py --limit        # how much API allowance is left
python wcl_mythic_stats.py --compact      # shrink the cache folder
python wcl_mythic_stats.py --list-zones   # raid zone IDs
```

Needs Python 3.8 or newer and nothing else. On the first run it asks for your
Client ID and Secret and saves them to `wcl_credentials.json` next to the script.
Never commit that file; `.gitignore` blocks it.

Useful flags: `--region EU`, `--zone <id>`, `--rank-age <hours>`,
`--max-new <n>`, `--deadline <minutes>`, `--stop-at-limit`, `--prune`,
`--out <path>`, `--interface <number>`, `--no-open`.

---

## Installing the addon

Download `MythicStats.zip` from the site's header link, or from CurseForge, and
extract it into:

```
World of Warcraft\_retail_\Interface\AddOns\
```

You should end up with `AddOns\MythicStats\MythicStats.toc`. Type `/ms` in game.

Pick a class with the arrows, a spec below them, and a boss in the middle column.
Each row has a **Talents** button: click it, press **Ctrl+C**, then open your
talent window, click the loadout dropdown and choose **Import**. Addons aren't
allowed to change talents for you, so that step is Blizzard's.

---

## Links

- Site: <https://bysonuk.github.io/Mythic-Top-10/>
- Repo: <https://github.com/Bysonuk/Mythic-Top-10>
- Addon downloads: the Releases panel on the repo, or the download link in the site header

## Notes

- Retail only, Mythic difficulty. Healers are ranked by HPS and everyone else by
  DPS, the same as on Warcraft Logs.
- Scheduled workflows on public repos stop after 60 days without activity. Each
  run commits something, so this keeps itself awake.
- If Warcraft Logs is down or the key is rejected, the run still publishes the
  page from saved data and retries.
- Not affiliated with Warcraft Logs or Blizzard.
