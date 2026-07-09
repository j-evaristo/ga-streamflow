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
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

BASE = "https://waterservices.usgs.gov/nwis/dv/"
SITE_SERVICE = "https://waterservices.usgs.gov/nwis/site/"
ROOT = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(ROOT, "data", "raw")
CSV_DIR = os.path.join(ROOT, "data", "csv")
JSON_DIR = os.path.join(RAW, "json")
END_DT = date.today().isoformat()
HEADERS = {"User-Agent": "GA-discharge-research/1.0 (python urllib)"}

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
    ]
    for name, url in targets:
        path = os.path.join(RAW, name)
        try:
            text = fetch(url)
            if text is None or not text.lstrip().startswith("#"):
                raise RuntimeError("unexpected response from site service")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"refreshed {name}", flush=True)
        except Exception as e:
            if os.path.exists(path):
                print(f"WARN: could not refresh {name} ({e}); using existing copy", flush=True)
            else:
                raise


def build_site_plan():
    cat = parse_rdb(os.path.join(RAW, "ga_series_catalog.rdb"))
    q = [r for r in cat if r["parm_cd"] == "00060" and r["data_type_cd"] == "dv"]
    plan = {}
    for r in q:
        s = plan.setdefault(r["site_no"], {"begin": r["begin_date"], "stats": set()})
        s["begin"] = min(s["begin"], r["begin_date"])
        s["stats"].add(r["stat_cd"])
    return plan


def fetch(url, tries=4):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read().decode("utf-8")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            last = e
            code = getattr(e, "code", None)
            if code is not None and code == 404:
                return None  # no data
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"failed after {tries} tries: {url} ({last})")


def download_site(site_no, info):
    begin = info["begin"]
    url = (f"{BASE}?format=json&sites={site_no}&parameterCd=00060"
           f"&startDT={begin}&endDT={END_DT}")
    text = fetch(url)
    if text is None:
        return site_no, 0, "404"
    with open(os.path.join(JSON_DIR, f"{site_no}.json"), "w", encoding="utf-8") as f:
        f.write(text)
    data = json.loads(text)
    ts_list = data.get("value", {}).get("timeSeries", [])
    n = 0
    with open(os.path.join(CSV_DIR, f"USGS_{site_no}.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["site_no", "date", "stat_cd", "discharge_cfs", "qualifiers"])
        for ts in ts_list:
            var = ts.get("variable", {})
            if var.get("variableCode", [{}])[0].get("value") != "00060":
                continue
            stat = (var.get("options", {}).get("option", [{}])[0]
                    .get("optionCode", ""))
            nodata = var.get("noDataValue", -999999)
            for block in ts.get("values", []):
                for v in block.get("value", []):
                    raw = v.get("value")
                    quals = " ".join(v.get("qualifiers", []))
                    try:
                        num = float(raw)
                    except (TypeError, ValueError):
                        num = None
                    if num is not None and num == nodata:
                        num = None
                    w.writerow([site_no, v["dateTime"][:10], stat,
                                "" if num is None else raw, quals])
                    n += 1
    return site_no, n, "ok"


def main():
    fetch_catalogs()
    plan = build_site_plan()
    print(f"{len(plan)} sites to download through {END_DT}", flush=True)
    if "--dry-run" in sys.argv:
        return
    results = []
    done = 0
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(download_site, s, info): s for s, info in plan.items()}
        for fut in as_completed(futures):
            site = futures[fut]
            try:
                site_no, n, status = fut.result()
            except Exception as e:
                site_no, n, status = site, 0, f"error: {e}"
            results.append((site_no, n, status))
            done += 1
            if done % 25 == 0 or status != "ok":
                print(f"[{done}/{len(plan)}] {site_no}: {status} ({n} rows)", flush=True)
    with open(os.path.join(ROOT, "data", "download_log.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["site_no", "rows", "status"])
        for row in sorted(results):
            w.writerow(row)
    ok = sum(1 for _, _, s in results if s == "ok")
    total = sum(n for _, n, _ in results)
    print(f"DONE: {ok}/{len(plan)} sites ok, {total} total rows", flush=True)
    bad = [(s, st) for s, _, st in results if st not in ("ok", "404")]
    if bad:
        print("FAILURES:", bad, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
