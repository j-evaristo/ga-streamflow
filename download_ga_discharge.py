"""
Download all USGS daily discharge (parameter 00060) data for Georgia.

Reads the site list + series catalog (RDB files already fetched into data/raw/),
then downloads the full period of record of daily values for every site via the
USGS Daily Values REST service. Outputs:

  data/csv/USGS_<site>.csv     - long-format CSV: site_no, date, stat_cd, discharge_cfs, qualifiers
  data/raw/json/<site>.json    - raw service responses (for provenance)
  data/download_log.csv        - per-site download status
"""

import csv
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time
import http.client
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

BASE = "https://waterservices.usgs.gov/nwis/dv/"
SITE_SERVICE = "https://waterservices.usgs.gov/nwis/site/"
# Georgia-Florida border strip (west,south,east,north). Shared border rivers
# (St Marys, Suwannee, Apalachicola, Ochlockonee, Withlacoochee, Alapaha...)
# have gages that USGS files under Florida even where the stream IS the state
# line - e.g. 02228500 St Marys at Moniac - plus near-border downstream gages.
BORDER_BBOX = "-85.30,30.20,-81.40,31.00"
ROOT = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(ROOT, "data", "raw")
CSV_DIR = os.path.join(ROOT, "data", "csv")
JSON_DIR = os.path.join(RAW, "json")
END_DT = date.today().isoformat()
HEADERS = {"User-Agent": "GA-discharge-research/1.0 (python urllib)"}

# waterservices brownouts (bursts of 503s and truncated responses) last
# minutes, far longer than one request's retries. Sites that fail the main pass
# are retried in later rounds after cool-downs; any still failing when the
# budget runs out keep the previous night's CSV from the dataset archive, so a
# brownout costs those stations one day of freshness instead of failing the
# whole update.
DV_RETRY_BUDGET_MIN = float(os.environ.get("DV_RETRY_BUDGET_MIN", "12") or 0)
RETRY_COOLDOWNS = [60, 180, 300]   # seconds before each retry round; the last repeats
MAX_STALE_FRACTION = 0.10          # more failures than this means USGS is down: fail
STALE = "stale: previous night's copy"
ABSENT = "skipped: not in previous archive"

os.makedirs(CSV_DIR, exist_ok=True)
os.makedirs(JSON_DIR, exist_ok=True)


def parse_rdb(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        header = None
        for line in f:
            if line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if header is None:
                header = parts
                continue
            # skip the column-format row (e.g. "5s", "15s")
            if parts[0] and parts[0][-1] in "sdn" and parts[0][:-1].isdigit():
                continue
            rows.append(dict(zip(header, parts)))
    return rows


def fetch_catalogs():
    """Refresh the GA site list and series catalog so newly commissioned gages
    are picked up automatically. Falls back to the existing copy if USGS is
    unreachable and a previous copy exists."""
    targets = [
        ("ga_sites_expanded.rdb",
         SITE_SERVICE + "?format=rdb&stateCd=ga&parameterCd=00060"
         "&hasDataTypeCd=dv&siteStatus=all&siteOutput=expanded"),
        ("ga_series_catalog.rdb",
         SITE_SERVICE + "?format=rdb&stateCd=ga&parameterCd=00060"
         "&outputDataTypeCd=dv&siteStatus=all&seriesCatalogOutput=true"),
        ("fl_border_sites_expanded.rdb",
         SITE_SERVICE + "?format=rdb&bBox=" + BORDER_BBOX + "&parameterCd=00060"
         "&hasDataTypeCd=dv&siteStatus=all&siteOutput=expanded"),
        ("fl_border_series_catalog.rdb",
         SITE_SERVICE + "?format=rdb&bBox=" + BORDER_BBOX + "&parameterCd=00060"
         "&outputDataTypeCd=dv&siteStatus=all&seriesCatalogOutput=true"),
    ]
    for name, url in targets:
        refresh_catalog(name, url)


def build_site_plan():
    cat = parse_rdb(os.path.join(RAW, "ga_series_catalog.rdb"))
    border = os.path.join(RAW, "fl_border_series_catalog.rdb")
    if os.path.exists(border):
        cat = cat + parse_rdb(border)
    q = [r for r in cat if r["parm_cd"] == "00060" and r["data_type_cd"] == "dv"]
    plan = {}
    for r in q:
        s = plan.setdefault(r["site_no"], {"begin": r["begin_date"], "stats": set()})
        s["begin"] = min(s["begin"], r["begin_date"])
        s["stats"].add(r["stat_cd"])
    return plan


def fetch(url, tries=6):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read().decode("utf-8")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
                http.client.HTTPException, ValueError) as e:
            # HTTPException/ValueError cover truncated or malformed chunked
            # responses seen during waterservices brownouts - retry them too
            last = e
            code = getattr(e, "code", None)
            if code is not None and code == 404:
                return None  # no data
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"failed after {tries} tries: {url} ({last})")


def note(msg):
    """Warning that also shows up as an annotation on the GitHub Actions run."""
    prefix = "::warning::" if os.environ.get("GITHUB_ACTIONS") else "WARN: "
    print(prefix + msg, flush=True)


_ARCHIVE = []   # memo: [path to the previous dataset archive, or None]


def previous_archive():
    """Download the previous night's dataset archive once; None if unavailable
    (e.g. a local run outside GitHub Actions)."""
    if _ARCHIVE:
        return _ARCHIVE[0]
    path = None
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if repo:
        url = f"https://github.com/{repo}/releases/download/dataset/dataset.tar.gz"
        dest = os.path.join(tempfile.gettempdir(), "previous_dataset.tar.gz")
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=600) as resp, open(dest, "wb") as f:
                    shutil.copyfileobj(resp, f)
                path = dest
                break
            except Exception as e:
                print(f"WARN: previous dataset archive unavailable ({e})", flush=True)
                time.sleep(10 * (attempt + 1))
    _ARCHIVE.append(path)
    return path


def restore_from_archive(members):
    """Copy {archive member: destination path} out of the previous archive.
    Returns the set of members restored, or None when there is no archive."""
    path = previous_archive()
    if path is None:
        return None
    restored = set()
    try:
        with tarfile.open(path, "r:gz") as tar:
            for m in tar:
                name = m.name[2:] if m.name.startswith("./") else m.name
                dest = members.get(name)
                if dest and m.isfile():
                    with tar.extractfile(m) as src, open(dest, "wb") as out:
                        shutil.copyfileobj(src, out)
                    restored.add(name)
    except (tarfile.TarError, OSError, EOFError) as e:
        print(f"WARN: could not read the previous dataset archive ({e})", flush=True)
        return None
    return restored


def refresh_catalog(name, url):
    """Fetch one site-service file with extra patience. If USGS stays down,
    reuse an existing local copy or the previous night's copy from the dataset
    archive - a day-old catalog loses nothing."""
    path = os.path.join(RAW, name)
    err = None
    for i, wait in enumerate((0, 60, 120, 180)):
        if i == 2:
            if os.path.exists(path):
                print(f"WARN: could not refresh {name} ({err}); using existing copy", flush=True)
                return
            if restore_from_archive({f"raw/{name}": path}):
                note(f"could not refresh {name} ({err}); using the previous night's copy")
                return
        time.sleep(wait)
        try:
            text = fetch(url)
            if text is None or not text.lstrip().startswith("#"):
                raise RuntimeError("unexpected response from site service")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"refreshed {name}", flush=True)
            return
        except Exception as e:
            err = e
    raise RuntimeError(f"could not fetch {name}: {err}")


def download_all(sites, plan, download_fn, workers, every):
    """Run download_fn over sites in parallel; returns {site_no: (rows, status)}."""
    results = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(download_fn, s, plan[s]): s for s in sites}
        for fut in as_completed(futures):
            site = futures[fut]
            try:
                _, n, status = fut.result()
            except Exception as e:
                n, status = 0, f"error: {e}"
            results[site] = (n, status)
            if len(results) % every == 0 or status not in ("ok", "404"):
                print(f"[{len(results)}/{len(futures)}] {site}: {status} ({n} rows)", flush=True)
    return results


def failed_sites(results):
    return sorted(s for s, (_, st) in results.items() if st not in ("ok", "404"))


def retry_failed(results, plan, download_fn, pass_ended):
    """Retry failed sites in rounds, each after a cool-down measured from the
    end of the previous pass, until all succeed or DV_RETRY_BUDGET_MIN is spent."""
    deadline = time.time() + DV_RETRY_BUDGET_MIN * 60
    rnd = 0
    while True:
        failed = failed_sites(results)
        if not failed:
            return
        cooldown = RETRY_COOLDOWNS[min(rnd, len(RETRY_COOLDOWNS) - 1)]
        wait = max(0.0, cooldown - (time.time() - pass_ended))
        if time.time() + wait >= deadline:
            print(f"retry budget spent with {len(failed)} sites still failing", flush=True)
            return
        rnd += 1
        print(f"retry round {rnd}: {len(failed)} sites, after a {wait:.0f}s pause", flush=True)
        time.sleep(wait)

        def attempt(site_no, info):
            if time.time() >= deadline:
                return site_no, results[site_no][0], results[site_no][1]
            return download_fn(site_no, info)

        results.update(download_all(failed, plan, attempt, workers=4, every=10**9))
        pass_ended = time.time()
        still = len(failed_sites(results))
        print(f"retry round {rnd}: recovered {len(failed) - still} of {len(failed)}", flush=True)


def fall_back_to_archive(results, plan):
    """Give sites that never came back their previous-night CSV. Returns the
    number restored. Sites missing from the archive were not published last
    night either, so they are skipped rather than failing the run."""
    failed = failed_sites(results)
    if not failed:
        return 0
    if len(failed) > MAX_STALE_FRACTION * len(plan):
        print(f"{len(failed)} sites failed - too many to patch from the archive", flush=True)
        return 0
    members = {f"csv/USGS_{s}.csv": os.path.join(CSV_DIR, f"USGS_{s}.csv") for s in failed}
    restored = restore_from_archive(members)
    if restored is None:
        return 0
    for s in failed:
        results[s] = (0, STALE if f"csv/USGS_{s}.csv" in restored else ABSENT)
    kept = [s for s in failed if results[s][1] == STALE]
    skipped = [s for s in failed if results[s][1] == ABSENT]
    if kept:
        note(f"USGS did not serve {len(kept)} of {len(plan)} sites; kept their "
             f"previous-night data: {' '.join(kept)}")
    if skipped:
        note(f"skipped {len(skipped)} unreachable sites not in the previous archive: "
             f"{' '.join(skipped)}")
    return len(kept)


def tolerated(status):
    return status in ("ok", "404", STALE, ABSENT)


def download_site(site_no, info):
    begin = info["begin"]
    url = (f"{BASE}?format=json&sites={site_no}&parameterCd=00060"
           f"&startDT={begin}&endDT={END_DT}")
    text = fetch(url)
    if text is None:
        return site_no, 0, "404"
    data = json.loads(text)
    with open(os.path.join(JSON_DIR, f"{site_no}.json"), "w", encoding="utf-8") as f:
        f.write(text)
    ts_list = data.get("value", {}).get("timeSeries", [])
    n = 0
    with open(os.path.join(CSV_DIR, f"USGS_{site_no}.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["site_no", "date", "stat_cd", "discharge_cfs", "qualifiers",
                    "method_id", "method_desc"])
        seen = set()
        for ts in ts_list:
            var = ts.get("variable", {})
            if var.get("variableCode", [{}])[0].get("value") != "00060":
                continue
            stat = (var.get("options", {}).get("option", [{}])[0]
                    .get("optionCode", ""))
            nodata = var.get("noDataValue", -999999)
            for block in ts.get("values", []):
                m = (block.get("method") or [{}])[0]
                mid = str(m.get("methodID") or "")
                mdesc = (m.get("methodDescription") or "").strip()
                for v in block.get("value", []):
                    raw = v.get("value")
                    quals = " ".join(v.get("qualifiers", []))
                    try:
                        num = float(raw)
                    except (TypeError, ValueError):
                        num = None
                    if num is not None and num == nodata:
                        num = None
                    day = v["dateTime"][:10]
                    key = (stat, day, raw, mid)
                    if key in seen:      # some sites repeat an identical block
                        continue
                    seen.add(key)
                    w.writerow([site_no, day, stat,
                                "" if num is None else raw, quals, mid, mdesc])
                    n += 1
    return site_no, n, "ok"


def main():
    fetch_catalogs()
    plan = build_site_plan()
    print(f"{len(plan)} sites to download through {END_DT}", flush=True)
    if "--dry-run" in sys.argv:
        return
    results = download_all(list(plan), plan, download_site, workers=10, every=25)
    retry_failed(results, plan, download_site, time.time())
    stale = fall_back_to_archive(results, plan)
    with open(os.path.join(ROOT, "data", "download_log.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["site_no", "rows", "status"])
        for s in sorted(results):
            w.writerow([s, *results[s]])
    ok = sum(1 for _, st in results.values() if st == "ok")
    total = sum(n for n, _ in results.values())
    print(f"DONE: {ok}/{len(plan)} sites ok, {stale} kept from the previous night, "
          f"{total} total rows", flush=True)
    bad = [(s, st) for s, (_, st) in sorted(results.items()) if not tolerated(st)]
    if bad:
        print("FAILURES:", bad, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
