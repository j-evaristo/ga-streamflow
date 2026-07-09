"""
Build viewer data files from downloaded USGS CSVs.

Outputs:
  data/sites_index.js       - metadata for all sites + GA boundary (loaded by viewer at startup)
  data/sites/USGS_<no>.js   - compact per-site daily series (loaded on demand via <script>)
  data/sites_metadata.csv   - consolidated station metadata table
"""

import csv
import json
import math
import os
from datetime import date

ROOT = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(ROOT, "data", "raw")
ASSETS = os.path.join(ROOT, "assets")
CSV_DIR = os.path.join(ROOT, "data", "csv")
SITES_DIR = os.path.join(ROOT, "data", "sites")
os.makedirs(SITES_DIR, exist_ok=True)

EPOCH = date(1970, 1, 1)
GAP_DAYS = 45  # gaps longer than this start a new segment

STAT_PREF = ["00003", "00021", "00022", "00023", "00024", "00001", "00002"]


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
            if parts[0] and parts[0][-1] in "sdn" and parts[0][:-1].isdigit():
                continue
            rows.append(dict(zip(header, parts)))
    return rows


def epoch_day(iso):
    y, m, d = int(iso[0:4]), int(iso[5:7]), int(iso[8:10])
    return (date(y, m, d) - EPOCH).days


def load_counties():
    lookup = {}
    with open(os.path.join(ASSETS, "national_county.txt"), encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split(",")
            if len(parts) >= 4:
                st_abbr, st_fips, cty_fips, name = parts[0], parts[1], parts[2], parts[3]
                lookup[(st_fips, cty_fips)] = f"{name}, {st_abbr}"
    return lookup


def clean_num(v):
    """int if whole, else float rounded to 6 significant digits."""
    if v is None:
        return None
    if v == int(v) and abs(v) < 1e15:
        return int(v)
    if v != 0:
        mag = int(math.floor(math.log10(abs(v))))
        v = round(v, max(0, 5 - mag))
        if v == int(v):
            return int(v)
    return v


def build_segments(day_values):
    """day_values: sorted list of (epoch_day, value). Returns segment list."""
    segs = []
    cur_start = None
    cur_vals = []
    prev_day = None
    for d, v in day_values:
        if prev_day is None:
            cur_start, cur_vals = d, [v]
        else:
            gap = d - prev_day
            if gap > GAP_DAYS:
                segs.append([cur_start, cur_vals])
                cur_start, cur_vals = d, [v]
            else:
                cur_vals.extend([None] * (gap - 1))
                cur_vals.append(v)
        prev_day = d
    if cur_vals:
        segs.append([cur_start, cur_vals])
    return segs


def process_site_csv(site_no):
    path = os.path.join(CSV_DIR, f"USGS_{site_no}.csv")
    if not os.path.exists(path):
        return None
    per_stat = {}   # stat -> {epoch_day: value}
    quals = {"P": 0, "e": 0}
    with open(path, encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            stat = row["stat_cd"]
            raw = row["discharge_cfs"]
            if raw == "":
                v = None
            else:
                try:
                    v = float(raw)
                except ValueError:
                    v = None
            d = epoch_day(row["date"])
            bucket = per_stat.setdefault(stat, {})
            # dedup across method blocks: keep first non-null
            if d not in bucket or (bucket[d] is None and v is not None):
                bucket[d] = v
            q = row["qualifiers"]
            if "P" in q.split():
                quals["P"] += 1
            if "e" in q.split():
                quals["e"] += 1
    series = {}
    for stat, days in per_stat.items():
        pts = sorted((d, clean_num(v)) for d, v in days.items() if v is not None)
        if not pts:
            continue
        series[stat] = {
            "segs": build_segments(pts),
            "n": len(pts),
            "b": pts[0][0],
            "e": pts[-1][0],
        }
    return series, quals


def main():
    counties = load_counties()
    sites = parse_rdb(os.path.join(RAW, "ga_sites_expanded.rdb"))
    meta = {s["site_no"]: s for s in sites}

    ga = json.load(open(os.path.join(ASSETS, "ga_boundary.json"), encoding="utf-8"))
    boundary = [[round(x, 3), round(y, 3)] for x, y in ga["geometry"]["coordinates"][0]]

    index = []
    meta_rows = []
    total_vals = 0
    for site_no in sorted(meta):
        result = process_site_csv(site_no)
        if result is None:
            print(f"WARN: no CSV for {site_no}")
            continue
        series, quals = result
        if not series:
            print(f"WARN: no data series for {site_no}")
            continue
        pref = next((s for s in STAT_PREF if s in series), sorted(series)[0])
        m = meta[site_no]
        out = {"series": {}}
        stats_idx = {}
        for stat, s in series.items():
            out["series"][stat] = {"segs": s["segs"]}
            stats_idx[stat] = [s["b"], s["e"], s["n"]]
            total_vals += s["n"]
        with open(os.path.join(SITES_DIR, f"USGS_{site_no}.js"), "w", encoding="utf-8") as f:
            f.write("window.__GAGE_DATA=window.__GAGE_DATA||{};")
            f.write(f"window.__GAGE_DATA[{json.dumps(site_no)}]=")
            f.write(json.dumps(out, separators=(",", ":")))
            f.write(";if(window.__onGageData)window.__onGageData(" + json.dumps(site_no) + ");")

        def fnum(key):
            v = m.get(key, "").strip()
            try:
                return float(v)
            except ValueError:
                return None

        county = counties.get((m.get("state_cd", ""), m.get("county_cd", "")), "")
        entry = {
            "no": site_no,
            "nm": m.get("station_nm", "").strip(),
            "lat": fnum("dec_lat_va"),
            "lon": fnum("dec_long_va"),
            "cty": county,
            "huc": m.get("huc_cd", "").strip(),
            "da": fnum("drain_area_va"),
            "cda": fnum("contrib_drain_area_va"),
            "alt": fnum("alt_va"),
            "altd": m.get("alt_datum_cd", "").strip(),
            "tp": m.get("site_tp_cd", "").strip(),
            "stats": stats_idx,
            "pref": pref,
            "nP": quals["P"],
            "nE": quals["e"],
        }
        index.append(entry)

        pref_s = series[pref]
        meta_rows.append({
            "site_no": site_no,
            "station_nm": entry["nm"],
            "site_tp_cd": entry["tp"],
            "dec_lat_va": m.get("dec_lat_va", "").strip(),
            "dec_long_va": m.get("dec_long_va", "").strip(),
            "coord_datum": m.get("dec_coord_datum_cd", "").strip(),
            "county": county,
            "state_cd": m.get("state_cd", "").strip(),
            "huc_cd": entry["huc"],
            "basin_cd": m.get("basin_cd", "").strip(),
            "alt_va": m.get("alt_va", "").strip(),
            "alt_datum_cd": entry["altd"],
            "drain_area_sqmi": m.get("drain_area_va", "").strip(),
            "contrib_drain_area_sqmi": m.get("contrib_drain_area_va", "").strip(),
            "begin_date": (EPOCH.fromordinal(EPOCH.toordinal() + pref_s["b"])).isoformat(),
            "end_date": (EPOCH.fromordinal(EPOCH.toordinal() + pref_s["e"])).isoformat(),
            "n_daily_values": pref_s["n"],
            "preferred_stat_cd": pref,
            "all_stat_cds": " ".join(sorted(series)),
            "n_provisional_days": quals["P"],
            "n_estimated_days": quals["e"],
        })

    index_obj = {
        "generated": date.today().isoformat(),
        "state": "GA",
        "parameter": "00060 Discharge, cubic feet per second",
        "boundary": boundary,
        "sites": index,
    }
    with open(os.path.join(ROOT, "data", "sites_index.js"), "w", encoding="utf-8") as f:
        f.write("window.__GAGE_INDEX=")
        f.write(json.dumps(index_obj, separators=(",", ":")))
        f.write(";")

    with open(os.path.join(ROOT, "data", "sites_metadata.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(meta_rows[0].keys()))
        w.writeheader()
        w.writerows(meta_rows)

    print(f"index: {len(index)} sites, {total_vals} values across all series")
    sizes = sorted(
        (os.path.getsize(os.path.join(SITES_DIR, fn)), fn) for fn in os.listdir(SITES_DIR)
    )
    print(f"largest site file: {sizes[-1][1]} {sizes[-1][0]/1e6:.1f} MB")
    print(f"total sites dir: {sum(s for s, _ in sizes)/1e6:.1f} MB")
    print(f"index size: {os.path.getsize(os.path.join(ROOT,'data','sites_index.js'))/1e3:.0f} KB")


if __name__ == "__main__":
    main()
