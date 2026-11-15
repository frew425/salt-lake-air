#!/usr/bin/env python3
"""
Fetch Salt Lake Valley air quality data and write data.json for the web page.

Sources (all free, no API keys):
  - Utah DAQ regulatory monitors (hourly observations)      air.utah.gov/csvFeed.php
  - Utah DAQ official daily forecast (human forecasters)     air.utah.gov/rssFeed.php
  - NOAA/NWS National Air Quality Forecast Capability        mapservices.weather.noaa.gov
      hourly PM2.5 (bias-corrected when available), ozone, smoke
  - CAMS global model via Open-Meteo (backup hourly forecast)
  - Mixing height / wind from NOAA GFS+HRRR via Open-Meteo (inversion indicator)
  - NWS Salt Lake City Smoke Management Forecast (official daily Clearing Index)

Stdlib only so it runs on a bare GitHub Actions runner.
Every source is optional: failures are recorded in data["errors"] and the page
shows whatever succeeded.

Usage: python3 fetch_data.py [output.json]
"""
import csv
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- config ----
TZ = ZoneInfo("America/Denver")

# Monitors people can choose between. All are Utah DAQ regulatory monitors in airshed 8
# (S. Wasatch Front). "id" is the DAQ feed id (https://air.utah.gov/airapi.html); "zone" is
# the forecast point used for the model data; "county" picks the DAQ forecast.
# A monitor is offered in the picker only while its feed is reporting PM2.5.
STATIONS = [
    {"id": "slc", "name": "Hawthorne", "area": "Salt Lake City · Liberty Wells", "county": "slc", "zone": "slc-north", "lat": 40.7344, "lon": -111.8721},
    {"id": "rp", "name": "Rose Park", "area": "Salt Lake City · northwest", "county": "slc", "zone": "slc-north", "lat": 40.7956, "lon": -111.9310},
    {"id": "ut", "name": "Utah Tech Center", "area": "Salt Lake City · west side", "county": "slc", "zone": "slc-north", "lat": 40.7771, "lon": -111.9450},
    {"id": "lp", "name": "West Valley", "area": "West Valley City · Lake Park", "county": "slc", "zone": "slc-west", "lat": 40.7098, "lon": -112.0088},
    {"id": "nr", "name": "Near Road", "area": "Murray · beside I-15", "county": "slc", "zone": "slc-south", "lat": 40.6620, "lon": -111.9020},
    {"id": "cv", "name": "Copper View", "area": "Midvale", "county": "slc", "zone": "slc-south", "lat": 40.5981, "lon": -111.8942},
    {"id": "ln", "name": "Lindon", "area": "Lindon · Utah County", "county": "utah", "zone": "utah-north", "lat": 40.3396, "lon": -111.7135},
    {"id": "sf", "name": "Spanish Fork", "area": "Spanish Fork · Utah County", "county": "utah", "zone": "utah-south", "lat": 40.1363, "lon": -111.6579},
]
# Forecast points. The models are coarse (NOAA about 12 km, CAMS about 40 km), so nearby
# monitors share one point; this also keeps the hourly run short.
ZONES = {
    "slc-north": {"name": "Salt Lake City", "lat": 40.765, "lon": -111.915},
    "slc-west": {"name": "West Valley", "lat": 40.710, "lon": -112.009},
    "slc-south": {"name": "Murray and Midvale", "lat": 40.630, "lon": -111.898},
    "utah-north": {"name": "Lindon and Orem", "lat": 40.340, "lon": -111.714},
    "utah-south": {"name": "Spanish Fork", "lat": 40.136, "lon": -111.658},
}
# Utah DAQ's official forecast is issued per county.
COUNTIES = {
    "slc": {"name": "Salt Lake County", "rss": "https://air.utah.gov/rssFeed.php?id=slc", "csv": "slc"},
    "utah": {"name": "Utah County", "rss": "https://air.utah.gov/rssFeed.php?id=ln", "csv": "utah"},
}
# All counties in one file (Day, County, Severity, Action, Message); used when a county's RSS is empty.
DAQ_FORECAST_CSV = "https://air.utah.gov/csvFeed.php?id=forecast"
NOAA_ROOT = "https://mapservices.weather.noaa.gov/raster/rest/services/air_quality"

# NWS Salt Lake City Smoke Management Forecast: the official daily Clearing Index.
# Airshed numbers are on the map at https://www.weather.gov/slc/ClearingIndex
NWS_SMF_LIST = "https://api.weather.gov/products/types/SMF/locations/SLC"
CLEARING_AIRSHED = 8  # S. Wasatch Front (Salt Lake Valley); map: smokemgt.utah.gov/static/pdf/Airsheds_NAAclass1.pdf

OUT = "data.json"
UA = "slc-air-dashboard/1.0 (personal, non-commercial; github pages)"

errors = []


def log(msg):
    print(msg, file=sys.stderr)


RUN_START = time.monotonic()
RUN_BUDGET_S = 330  # stop retrying slow sources after ~5.5 min so the run always finishes


def get(url, params=None, timeout=25, tries=2):
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    last = None
    for i in range(tries):
        if time.monotonic() - RUN_START > RUN_BUDGET_S:
            raise RuntimeError("skipped: run time budget used up by slow sources")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            last = e
            if i + 1 < tries:
                # 429 = rate limited: wait as long as the server asks (capped), not just 2 s
                wait = 2
                if e.code == 429:
                    try:
                        wait = min(30, max(5, int(e.headers.get("Retry-After") or 15)))
                    except ValueError:
                        wait = 15
                time.sleep(wait)
        except Exception as e:  # noqa: BLE001
            last = e
            if i + 1 < tries:
                time.sleep(2)
    raise RuntimeError(f"GET failed {url[:140]}: {last}")


def fnum(x):
    try:
        if x is None:
            return None
        s = str(x).strip()
        if s == "" or s.lower() in ("nan", "null", "none", "-"):
            return None
        return float(s)
    except ValueError:
        return None


def iso(dt):
    return dt.astimezone(TZ).isoformat(timespec="minutes")


def record_error(source, e):
    msg = f"{source}: {e}"
    log("ERROR " + msg)
    errors.append({"source": source, "error": str(e)[:400]})


# ------------------------------------------------------------ DAQ stations ----
def parse_daq_time(s, now):
    s = s.strip()
    cands = []
    for fmt in ("%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            cands.append(datetime.strptime(s, fmt).replace(tzinfo=TZ))
        except ValueError:
            pass
    if not cands:
        return None
    # choose the interpretation closest to now (handles m/d vs d/m ambiguity)
    return min(cands, key=lambda d: abs((d - now).total_seconds()))


def parse_daq_csv(text, now):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    hdr_i = next((i for i, ln in enumerate(lines) if "dtstamp" in ln.lower()), None)
    if hdr_i is None:
        raise ValueError("no header row with 'dtstamp'")
    body = "\n".join(lines[hdr_i:])
    delim = "\t" if "\t" in lines[hdr_i] else ","
    rows = []
    for row in csv.DictReader(io.StringIO(body), delimiter=delim):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        t = parse_daq_time(row.get("dtstamp", ""), now)
        if not t:
            continue
        o3 = fnum(row.get("ozone"))
        o3_8 = fnum(row.get("ozone_8hr_avg"))
        rows.append({
            "t": iso(t),
            "pm25": fnum(row.get("pm25")),
            "pm25_24h": fnum(row.get("pm25_24hr_avg")),
            # DAQ reports ozone in ppm; convert to ppb
            "o3": round(o3 * 1000, 1) if o3 is not None else None,
            "o3_8h": round(o3_8 * 1000, 1) if o3_8 is not None else None,
            "no2": fnum(row.get("no2")),
            "temp_f": fnum(row.get("temp")),
            "rh": fnum(row.get("rh")),
            "wind_mph": fnum(row.get("ws")),
            "wind_dir": fnum(row.get("wd")),
        })
    rows.sort(key=lambda r: r["t"])
    return rows


def fetch_stations(now):
    out = []
    for st in STATIONS:
        meta = dict(st)
        try:
            text = get(f"https://air.utah.gov/csvFeed.php?id={st['id']}")
            rows = parse_daq_csv(text, now)
            if not rows:
                raise ValueError("no rows parsed")
            cutoff = now - timedelta(hours=72)
            rows = [r for r in rows if datetime.fromisoformat(r["t"]) >= cutoff]
            out.append(dict(meta, series=rows))
        except Exception as e:  # noqa: BLE001
            record_error(f"DAQ station {st['name']}", e)
            out.append(dict(meta, series=[], error=str(e)[:200]))
    return out


# ------------------------------------------------------------ DAQ forecast ----
def strip_html(s):
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&nbsp;", " ", s)
    s = re.sub(r"[ \t]+", " ", s)
    return "\n".join(ln.strip() for ln in s.splitlines() if ln.strip())


def fetch_daq_forecast_csv():
    """{county code: [rss-style items]} from DAQ's all-county forecast file."""
    text = get(DAQ_FORECAST_CSV)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    delim = "\t" if lines and "\t" in lines[0] else ","
    rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delim))
    if not rows:
        return {}
    hdr = [h.strip().lower() for h in rows[0]]
    ix = {k: hdr.index(k) for k in ("day", "county", "severity", "action") if k in hdr}
    if len(ix) < 4:
        raise ValueError(f"unexpected header {rows[0]}")
    out = {}
    for r in rows[1:]:
        if len(r) <= max(ix.values()):
            continue
        try:
            d = datetime.strptime(r[ix["day"]].strip(), "%Y-%m-%d")
        except ValueError:
            continue
        out.setdefault(r[ix["county"]].strip().lower(), []).append({
            "title": f"Forecast for {d.month:02d}/{d.day:02d}/{d.year}",
            "description": f"Health: {r[ix['severity']].strip().title()} Action: {r[ix['action']].strip().title()}",
            "pubDate": "",
        })
    for items in out.values():
        items.sort(key=lambda it: it["title"][-4:] + it["title"][-10:-5])
    return out


def fetch_daq_forecast():
    """Official forecast per county: {"slc": {"rss": [...]}, "utah": {"rss": [...]}}.
    Each county's RSS first; DAQ's all-county CSV fills any county whose RSS is empty or down."""
    out, need_csv, rss_err = {}, [], {}
    for key, c in COUNTIES.items():
        res = {"name": c["name"], "rss": []}
        try:
            root = ET.fromstring(get(c["rss"]))
            for item in root.iter("item"):
                res["rss"].append({
                    "title": strip_html(item.findtext("title", "")),
                    "description": strip_html(item.findtext("description", "")),
                    "pubDate": item.findtext("pubDate", ""),
                })
            if not res["rss"]:
                raise ValueError("no forecast items")
        except Exception as e:  # noqa: BLE001
            need_csv.append(key)
            rss_err[key] = e
        out[key] = res
    if need_csv:
        try:
            table = fetch_daq_forecast_csv()
        except Exception as e:  # noqa: BLE001
            table = {}
            log(f"DAQ forecast CSV failed: {e}")
        for key in need_csv:
            items = table.get(COUNTIES[key]["csv"], [])
            if items:
                out[key]["rss"] = items
                out[key]["source"] = "csv"
            else:
                record_error(f"DAQ forecast ({COUNTIES[key]['name']})", rss_err[key])
    return out


# ------------------------------------------------------------------- NOAA ----
def noaa_services():
    data = json.loads(get(NOAA_ROOT, {"f": "json"}))
    return [s["name"].split("/")[-1] for s in data.get("services", []) if s.get("type") == "ImageServer"]


NOAA_CAP = 20  # the server returns at most this many samples per getSamples call


def _noaa_window(base, common, t0, t1, variant, depth=0):
    """Samples for [t0, t1] ms; splits the window when the server cap is hit."""
    params = dict(common, time=f"{t0},{t1}")
    if variant == 0:
        params["mosaicRule"] = json.dumps({"mosaicMethod": "esriMosaicNone"})
    data = json.loads(get(base, params))
    if "error" in data:
        raise RuntimeError(json.dumps(data["error"])[:300])
    samples = data.get("samples", [])
    if len(samples) >= NOAA_CAP and t1 - t0 > 3600000 and depth < 6:
        mid = t0 + (t1 - t0) // 2 // 3600000 * 3600000
        return (_noaa_window(base, common, t0, mid, variant, depth + 1)
                + _noaa_window(base, common, mid + 1, t1, variant, depth + 1))
    return samples


def noaa_point_series(service, now, lat, lon):
    """Hourly values at one point from a time-enabled NDGD ImageServer.

    The service caps each response at ~20 samples, so we walk the forecast in
    small time windows and, for every valid hour, keep the newest model run.
    """
    base = f"{NOAA_ROOT}/{service}/ImageServer/getSamples"
    geom = json.dumps({"x": lon, "y": lat, "spatialReference": {"wkid": 4326}})
    common = {
        "geometry": geom,
        "geometryType": "esriGeometryPoint",
        "returnFirstValueOnly": "false",
        "outFields": "idp_validtime,idp_issueddate,idp_fcst_hour",
        "f": "json",
    }
    h0 = int((now - timedelta(hours=2)).timestamp() // 3600 * 3600 * 1000)
    h_end = int((now + timedelta(hours=78)).timestamp() * 1000)
    step = 6 * 3600000
    samples, variant, errs = [], None, []
    for w0 in range(h0, h_end, step):
        w1 = w0 + step - 1
        for v in ([variant] if variant is not None else [0, 1]):
            try:
                samples += _noaa_window(base, common, w0, w1, v)
                variant = v
                break
            except Exception as e:  # noqa: BLE001
                errs.append(str(e)[:120])
                if "timed out" in str(e).lower() or "budget" in str(e):
                    break
        if variant is None and len(errs) >= 2:
            raise RuntimeError(f"{service}: {' | '.join(errs[:2])}")
    best = {}
    for s in samples:
        a = {k.lower(): v for k, v in (s.get("attributes") or {}).items()}
        vt = a.get("idp_validtime")
        issued = a.get("idp_issueddate") or 0
        raw = s.get("value")
        v = fnum(str(raw).split()[0]) if raw not in (None, "") else None
        if vt is None or v is None or v < 0 or v > 5000:
            continue
        hour = int(vt) // 3600000 * 3600000
        if hour not in best or issued >= best[hour][0]:
            best[hour] = (issued, v)
    if not best:
        raise RuntimeError(f"{service}: no usable samples ({len(samples)} raw; {' | '.join(errs[:2])})")
    latest = max(i for i, _ in best.values())
    series = [{"t": iso(datetime.fromtimestamp(ms / 1000, tz=timezone.utc)), "v": round(v, 1)}
              for ms, (_, v) in sorted(best.items())]
    return {"service": service,
            "issued": iso(datetime.fromtimestamp(latest / 1000, tz=timezone.utc)) if latest else None,
            "samples": len(samples), "series": series}


NOAA_WANTED = {
    # bias-corrected hourly PM2.5 first, raw model second
    "pm25": [r"apm25_hr01_bc", r"pm25.*1hr.*bc", r"apm25_hr01$", r"pm25.*1hr"],
    "o3": [r"ozone_1hr.*bc", r"ozone.*1hr", r"o3.*hr01"],
    "smoke": [r"smoke_sfc_1hr"],
}
NOAA_FALLBACKS = {
    "pm25": ["ndgd_apm25_hr01_bc", "ndgd_apm25_hr01"],
    "o3": ["ndgd_ozone_1hr_avg_time"],
    "smoke": ["ndgd_smoke_sfc_1hr_avg_time"],
}


def fetch_noaa_point(now, names, lat, lon):
    """One forecast point. Returns ({pm25, o3, smoke}, {key: error text})."""
    res, fails = {}, {}
    for key, pats in NOAA_WANTED.items():
        cands = []
        for p in pats:
            for n in names:
                if re.search(p, n) and n not in cands:
                    cands.append(n)
        for n in NOAA_FALLBACKS[key]:
            if n not in cands:
                cands.append(n)
        errs = []
        for svc in cands:
            try:
                res[key] = noaa_point_series(svc, now, lat, lon)
                res[key]["bias_corrected"] = svc.endswith("_bc")
                break
            except Exception as e:  # noqa: BLE001
                errs.append(str(e)[:160])
        if key not in res:
            fails[key] = " | ".join(errs) or "no service found"
    return res, fails


def fetch_noaa(now):
    """All zones, in parallel: NOAA is the slowest source and each point needs dozens of calls."""
    try:
        names = noaa_services()
    except Exception as e:  # noqa: BLE001
        record_error("NOAA service list", e)
        names = []
    with ThreadPoolExecutor(max_workers=len(ZONES)) as pool:
        futs = {zid: pool.submit(fetch_noaa_point, now, names, z["lat"], z["lon"]) for zid, z in ZONES.items()}
        got = {}
        for zid, f in futs.items():
            try:
                got[zid] = f.result()
            except Exception as e:  # noqa: BLE001
                got[zid] = ({}, {k: str(e)[:160] for k in NOAA_WANTED})
    for key in NOAA_WANTED:  # one error line per pollutant, not one per zone
        bad = [zid for zid, (_, fails) in got.items() if key in fails]
        if bad:
            record_error(f"NOAA {key}", f"failed for {len(bad)} of {len(ZONES)} areas ({', '.join(bad)}): {got[bad[0]][1][key]}")
    return {zid: res for zid, (res, _) in got.items()}


# -------------------------------------------------------------- Open-Meteo ----
ZONE_IDS = list(ZONES)
ZONE_LATS = ",".join(str(ZONES[z]["lat"]) for z in ZONE_IDS)
ZONE_LONS = ",".join(str(ZONES[z]["lon"]) for z in ZONE_IDS)


def openmeteo_all(base, params):
    """One Open-Meteo request for every zone (comma-separated coordinates), so a run makes
    two requests to Open-Meteo instead of ten; their free tier rate-limits shared CI IPs."""
    data = json.loads(get(base, dict(params, latitude=ZONE_LATS, longitude=ZONE_LONS), tries=3))
    if isinstance(data, dict):
        data = [data]
    if len(data) != len(ZONE_IDS):
        raise RuntimeError(f"expected {len(ZONE_IDS)} locations, got {len(data)}")
    return dict(zip(ZONE_IDS, data))


def fetch_cams_all():
    try:
        got = openmeteo_all("https://air-quality-api.open-meteo.com/v1/air-quality", {
            "hourly": "pm2_5,ozone,us_aqi",
            "timezone": "America/Denver", "past_days": 1, "forecast_days": 4,
        })
    except Exception as e:  # noqa: BLE001
        record_error("CAMS / Open-Meteo air quality", e)
        return {z: None for z in ZONE_IDS}
    out = {}
    for zid, data in got.items():
        h = data["hourly"]
        ser = []
        for i, t in enumerate(h["time"]):
            dt = datetime.fromisoformat(t).replace(tzinfo=TZ)
            o3 = h["ozone"][i]
            ser.append({
                "t": iso(dt),
                "pm25": h["pm2_5"][i],
                # µg/m³ -> ppb (approx. at 25 °C, 1 atm)
                "o3": round(o3 / 1.96, 1) if o3 is not None else None,
                "aqi": h["us_aqi"][i],
            })
        out[zid] = {"model": "CAMS global (via Open-Meteo)", "series": ser}
    return out


def fetch_met_all():
    base = "https://api.open-meteo.com/v1/forecast"
    params = {
        "hourly": "boundary_layer_height,wind_speed_10m,wind_speed_80m,temperature_2m,precipitation_probability",
        "timezone": "America/Denver", "past_days": 1, "forecast_days": 4,
        "wind_speed_unit": "mph", "temperature_unit": "fahrenheit",
    }
    plain = dict(params, hourly=params["hourly"].replace("wind_speed_80m,", ""))
    last = None
    # 80 m wind is closer to the transport wind used by the Clearing Index; if a
    # model does not serve it, fall back to the original request.
    for prm, model in ((params, "gfs_seamless"), (plain, "gfs_seamless"), (params, None)):
        try:
            got = openmeteo_all(base, dict(prm, models=model) if model else prm)
            out = {}
            for zid, data in got.items():
                h = data["hourly"]
                if all(v is None for v in h.get("boundary_layer_height", [])):
                    raise RuntimeError("boundary_layer_height all null")
                n = len(h["time"])
                precip = h.get("precipitation_probability") or [None] * n
                wind80 = h.get("wind_speed_80m") or [None] * n
                out[zid] = {"model": model or "best match", "series": [{
                    "t": iso(datetime.fromisoformat(t).replace(tzinfo=TZ)),
                    "blh_m": h["boundary_layer_height"][i],
                    "wind_mph": h["wind_speed_10m"][i],
                    "wind80_mph": wind80[i],
                    "temp_f": h["temperature_2m"][i],
                    "precip_pct": precip[i],
                } for i, t in enumerate(h["time"])]}
            return out
        except Exception as e:  # noqa: BLE001
            last = e
            if "429" in str(e):
                break  # rate limited: other variants hit the same server
    record_error("Mixing height / Open-Meteo weather", last)
    return {z: None for z in ZONE_IDS}


# --------------------------------------------------------- Clearing Index ----
WEEKDAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]


def parse_clearing(text, airshed, issued):
    """Pull one airshed's daily Clearing Index out of the NWS Smoke Management Forecast text."""
    m = re.search(r"\.\.\.AIR ?SHED %d\.\.\.(.*?)(?=\.\.\.AIR ?SHED \d+\.\.\.|\$\$|\Z)" % airshed, text, re.S | re.I)
    if not m:
        raise RuntimeError(f"airshed {airshed} not found in product")
    days, cur, seen = [], None, set()
    for line in m.group(1).splitlines():
        s = line.strip()
        d = re.match(r"^\.?([A-Z][A-Z ]*?)\.\.\.$", s)
        if d:
            label = d.group(1).strip().upper()
            if label in ("TODAY", "TONIGHT", "REST OF TODAY", "THIS AFTERNOON"):
                date = issued.date()
            elif label == "TOMORROW":
                date = issued.date() + timedelta(days=1)
            elif label in WEEKDAYS:
                ahead = (WEEKDAYS.index(label) - issued.weekday()) % 7 or 7
                date = issued.date() + timedelta(days=ahead)
            else:
                cur = None
                continue
            cur = {"date": date.isoformat(), "label": label.title()}
            continue
        if cur is None:
            continue
        k = re.match(r"^(Afternoon Mixing Height|Transport Winds|Clearing Index)\.+\s*(.+)$", s, re.I)
        if not k:
            continue
        key, val = k.group(1).lower(), k.group(2).strip()
        if key.startswith("afternoon"):
            cur["mixing_ft_asl"] = fnum(val.replace(",", ""))
        elif key.startswith("transport"):
            cur["transport_wind"] = val
        else:
            n = re.match(r"^(\d+)\s*(\+?)", val)
            if n and cur["date"] not in seen:
                cur["index"] = int(n.group(1))
                cur["plus"] = bool(n.group(2))
                seen.add(cur["date"])
                days.append(cur)
            cur = None
    if not days:
        raise RuntimeError(f"no Clearing Index values for airshed {airshed}")
    return days


def fetch_clearing(now):
    try:
        listing = json.loads(get(NWS_SMF_LIST))
        items = listing.get("@graph") or listing.get("features") or []
        if not items:
            raise RuntimeError("no Smoke Management Forecast products listed")
        first = items[0]
        url = first.get("@id") or first.get("id")
        if not str(url).startswith("http"):
            url = "https://api.weather.gov/products/" + str(first.get("id"))
        prod = json.loads(get(url))
        text = prod.get("productText") or ""
        stamp = prod.get("issuanceTime") or first.get("issuanceTime")
        issued = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(TZ) if stamp else now
        return {
            "airshed": CLEARING_AIRSHED,
            "issued": iso(issued),
            "source": "NWS Salt Lake City Smoke Management Forecast",
            "days": parse_clearing(text, CLEARING_AIRSHED, issued),
        }
    except Exception as e:  # noqa: BLE001
        record_error("Clearing Index / NWS", e)
        return None


# ------------------------------------------------------------ resilience ----
def reuse_previous(data, now):
    """If a source failed this run, keep last run's copy while it still has future hours."""
    try:
        with open(OUT) as f:
            prev = json.load(f)
    except Exception:  # noqa: BLE001
        return
    stamp = prev.get("generated_at")

    def still_useful(block):
        ser = (block or {}).get("series") or []
        return any(datetime.fromisoformat(p["t"]) > now + timedelta(hours=6) for p in ser)

    pz = prev.get("zones") or {}
    for zid, z in data["zones"].items():
        old = pz.get(zid) or {}
        for key in ("cams", "met"):
            if not z.get(key) and still_useful(old.get(key)):
                z[key] = dict(old[key], stale_from=stamp)
        for key in ("pm25", "o3", "smoke"):
            if key not in z["noaa"] and still_useful((old.get("noaa") or {}).get(key)):
                z["noaa"][key] = dict(old["noaa"][key], stale_from=stamp)
    pc = prev.get("clearing")
    if not data.get("clearing") and pc and any(d.get("date", "") >= now.date().isoformat() for d in pc.get("days", [])):
        data["clearing"] = dict(pc, stale_from=stamp)
    pf = prev.get("daq_forecast") or {}
    for key, f in data["daq_forecast"].items():
        old = pf.get(key) if isinstance(pf.get(key), dict) else None
        if not f["rss"] and old and old.get("rss"):
            data["daq_forecast"][key] = dict(old, stale_from=stamp)


def calibrate_cams(data):
    """CAMS is a ~40 km global model and often reads far off in the valley.
    For each monitor, work out how CAMS compared with it over the last 24 h.
    The page multiplies CAMS PM2.5 by this factor for whichever monitor is chosen."""
    for st in data["stations"]:
        cams = (data["zones"].get(st["zone"]) or {}).get("cams")
        if not cams or not st["series"]:
            continue
        obs = {r["t"][:13]: r["pm25"] for r in st["series"][-24:] if r["pm25"] is not None}
        pairs = [(obs[p["t"][:13]], p["pm25"]) for p in cams["series"]
                 if p["t"][:13] in obs and p["pm25"] is not None]
        if len(pairs) < 8:
            continue
        ratios = sorted((o + 1) / (c + 1) for o, c in pairs)
        factor = max(0.15, min(2.0, ratios[len(ratios) // 2]))
        st["cams_cal"] = {"factor": round(factor, 2), "hours": len(pairs)}


# ------------------------------------------------------------------- main ----
def main():
    now = datetime.now(TZ)
    data = {
        "generated_at": iso(now),
        "format": 2,
        "stations": fetch_stations(now),
        "daq_forecast": fetch_daq_forecast(),
        "clearing": fetch_clearing(now),
    }
    cams = fetch_cams_all()
    met = fetch_met_all()
    noaa = fetch_noaa(now)  # slowest source goes last
    data["zones"] = {zid: dict(z, cams=cams[zid], met=met[zid], noaa=noaa[zid]) for zid, z in ZONES.items()}
    reuse_previous(data, now)
    calibrate_cams(data)
    data["errors"] = errors
    with open(OUT, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    ok = sum(1 for s in data["stations"] if s["series"])
    n_noaa = sum(1 for z in data["zones"].values() if z["noaa"].get("pm25"))
    n_cams = sum(1 for z in data["zones"].values() if z["cams"])
    n_met = sum(1 for z in data["zones"].values() if z["met"])
    log(f"wrote {OUT}: {ok}/{len(STATIONS)} stations; zones with noaa pm25 {n_noaa}, cams {n_cams}, "
        f"met {n_met} of {len(ZONES)}; errors={len(errors)}")
    # Fail the run only if nothing worked, so a partial outage still publishes.
    if ok == 0 and n_noaa == 0 and n_cams == 0:
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        OUT = sys.argv[1]
    main()
