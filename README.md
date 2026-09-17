# Mythic Stat Sheet

Top 10 Mythic raid logs for every spec in World of Warcraft, with each player's
secondary stats, trinkets and talent import code. Data comes from Warcraft Logs.

The page rebuilds itself once a day through GitHub Actions and is served free on
GitHub Pages.

---

## Setting it up (about 10 minutes)

### 1. Create the repository

On github.com, click **New repository**.

- Name it something like `mythic-stats`.
- Set it to **Public**. GitHub Pages and unlimited Actions minutes are free on
  public repos; private repos need a paid plan for Pages.
- Tick **Add a README file** so the repo isn't empty, then click **Create**.

Anyone with the link will be able to see the page. All of the data on it is
already public on Warcraft Logs, so this is usually fine. If you want it private,
see "Keeping it private" at the bottom.

### 2. Add the files

In the repo, click **Add file**, then **Upload files**, and upload:

- `wcl_mythic_stats.py`
- `.gitignore`
- this `README.md` (replacing the one GitHub made)

Then create the workflow file. Click **Add file**, **Create new file**, and type
this exact name in the box:

```
.github/workflows/update-stat-sheet.yml
```

Paste in the contents of `update-stat-sheet.yml`, then click **Commit changes**.

### 3. Add your Warcraft Logs key

Go to **Settings**, then **Secrets and variables**, then **Actions**, and click
**New repository secret** twice:

| Name                | Value                            |
| ------------------- | -------------------------------- |
| `WCL_CLIENT_ID`     | your Client ID                   |
| `WCL_CLIENT_SECRET` | your Client Secret               |

Secrets are hidden from anyone viewing the repo and are not printed in the logs.
Create a fresh pair at <https://www.warcraftlogs.com/api/clients> if you'd rather
not reuse an old one.

### 4. Turn on Pages

Go to **Settings**, then **Pages**, and under **Source** choose
**GitHub Actions**.

### 5. Run it

Go to the **Actions** tab, click **Update stat sheet**, then **Run workflow**.

The first run does the heavy lifting and will probably stop after about five
hours with part of the raid loaded, because Warcraft Logs limits how much can be
fetched per hour. That's expected. Run it again (or wait for the next morning)
and it carries on from where it stopped. Once it's caught up, daily runs are
quick, because only new logs are fetched.

When a run finishes, your page is at:

```
https://<your-username>.github.io/<repo-name>/
```

---

## Speeding up the first load

If you've already been running the script on your PC, upload your `wcl_cache`
folder to the repo. Everything in it is reused, so the first run online has far
less to do.

The cache only stores what the page needs, so it stays small: roughly a few
kilobytes per log.

---

## Changing things

- **Update time:** edit the `cron` line in the workflow. It's in UTC, so
  `0 6 * * *` is 7am UK time in summer and 6am in winter.
- **One region only:** add `--region EU` to the python line in the workflow.
- **A different raid:** add `--zone <id>`. Run
  `python wcl_mythic_stats.py --list-zones` on your PC to see the IDs.
- **Run it by hand at any time:** Actions tab, **Update stat sheet**,
  **Run workflow**.

## Keeping it private

GitHub Pages needs a paid plan to serve a private repo. Free alternatives:

- **Cloudflare Pages** works with private GitHub repos on its free plan. Connect
  the repo, set the build output folder to `site`, and use a Cron Trigger for the
  daily rebuild.
- Or keep the repo private with no Pages at all, and download
  `site/index.html` from the Actions artifact when you want to look at it.

## Notes

- Scheduled workflows on public repos are switched off after 60 days with no
  activity. The daily commit counts as activity, so this keeps itself alive as
  long as the data keeps changing.
- If a run fails, open it in the Actions tab and read the last few lines. Errors
  mentioning a field or argument usually mean Warcraft Logs changed their API.
- Never commit `wcl_credentials.json`. The included `.gitignore` blocks it.
