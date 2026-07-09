# Publishing the explorer with automatic daily updates

The explorer is a static page, so it can be hosted anywhere. To keep it
**automatically up to date**, a scheduled job re-downloads the USGS data and
redeploys the page every morning — no manual intervention, ever. USGS publishes
daily values with roughly a one-day lag, so a daily rebuild keeps the page
exactly as current as USGS itself.

Everything needed is already in this folder. Two options:

---

## Option A (recommended): GitHub Pages + GitHub Actions — free, zero servers

The included workflow (`.github/workflows/update-data.yml`) runs **every day at
~6:17 am Eastern**: it re-downloads all 499 stations from USGS (~3–5 min),
rebuilds the viewer data, and deploys the site. If USGS is down, the run fails
gracefully and yesterday's site stays live. New gages USGS commissions are
picked up automatically because the site catalog is refreshed on every run.

### One-time setup (~10 minutes)

1. **Create a GitHub repository** (e.g. `ga-streamflow`) at github.com → New
   repository (public is fine and required for free Pages).

2. **Push this folder** (from this directory; the `.gitignore` already excludes
   the 600 MB `data/` folder — only code and small assets are committed):

   ```
   git init
   git add .
   git commit -m "Georgia Streamflow Explorer"
   git branch -M main
   git remote add origin https://github.com/YOUR-USERNAME/ga-streamflow.git
   git push -u origin main
   ```

3. **Enable Pages**: repo → Settings → Pages → "Build and deployment" →
   Source: **GitHub Actions**.

4. **First deploy**: repo → Actions → "Update GA streamflow data and deploy" →
   Run workflow (or just wait — the push in step 2 already triggered it).

Your explorer is now live at
`https://YOUR-USERNAME.github.io/ga-streamflow/` and refreshes itself daily.
The footer's "Retrieved" date updates automatically so you can always confirm
freshness.

### Putting it on your website

- **Link to it** directly, or **embed it** in any lab/department page:

  ```html
  <iframe src="https://YOUR-USERNAME.github.io/ga-streamflow/"
          style="width:100%; height:90vh; border:0;"
          title="Georgia Streamflow Explorer" loading="lazy"></iframe>
  ```

- **Custom domain** (e.g. `streamflow.yourlab.org`): add a CNAME DNS record
  pointing to `YOUR-USERNAME.github.io`, then set the domain in repo →
  Settings → Pages → Custom domain. Deep links like
  `.../#site=02336000` keep working either way.

### Changing the schedule

Edit the `cron:` line in `.github/workflows/update-data.yml`
(times are UTC): `"17 10 * * *"` = daily 10:17 UTC. Twice daily:
`"17 10,22 * * *"`. Weekly (Mondays): `"17 10 * * 1"`.

Note: GitHub disables cron schedules on repos with no activity for 60 days —
GitHub emails you first, and one click re-enables it. Any commit also resets
the clock.

---

## Option B: your own web server (e.g. UGA hosting) + cron

If the lab has a Linux web server, run the same two scripts on a cron schedule
and copy the results into the web root:

```
# /etc/cron.d/ga-streamflow  (daily at 06:17)
17 6 * * * youruser cd /opt/ga-streamflow && python3 download_ga_discharge.py && python3 build_viewer_data.py && \
  rsync -a --delete ga_streamflow_explorer.html data/sites_index.js data/sites_metadata.csv data/sites /var/www/html/streamflow/
```

(rename `ga_streamflow_explorer.html` to `index.html` in the destination if you
want a clean URL). Same behavior, just on your infrastructure.

---

## What gets published

Only what the viewer needs (~25 MB): `index.html`, `data/sites_index.js`,
`data/sites/*.js` (499 files), and `data/sites_metadata.csv`. The bulk archives
(per-station CSVs, raw JSON responses) are rebuilt on each run but not
published; add `cp -r data/csv site/data/csv` to the "Assemble site" step if
you also want the CSVs downloadable from the site.

## If USGS retires the legacy service

The scripts use `waterservices.usgs.gov` (the classic NWIS REST services). USGS
is gradually migrating to `api.waterdata.usgs.gov`. If a scheduled run ever
starts failing with 410/redirect errors, the fix is confined to the two URL
constants at the top of `download_ga_discharge.py` (`BASE`, `SITE_SERVICE`).
