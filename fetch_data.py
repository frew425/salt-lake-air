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
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- config ----
LOCATION_NAME = "Murray / Holladay"
LAT, LON = 40.655, -111.845
TZ = ZoneInfo("America/Denver")

# DAQ feed ids (see https://air.utah.gov/airapi.html). First one with data is the "now" station.
STATIONS = [
    ("slc", "Hawthorne"),
    ("cv", "Copper View (Midvale)"),
    ("ut", "Utah Tech Center"),
    ("nr", "Near Road I-15"),
    ("rp", "Rose Park"),
    ("lp", "West Valley"),
    ("ip", "Inland Port"),
]
DAQ_FORECAST_RSS = "https://air.utah.gov/rssFeed.php?id=slc"
DAQ_FORECAST_CSV = "https://air.utah.gov/csvFeed.php?id=forecast"
NOAA_ROOT = "https://mapservices.weather.noaa.gov/raster/rest/services/air_quality"

OUT = "data.json"
UA = "slc-air-dashboard/1.0 (personal, non-commercial; github pages)"

errors = []


def log(msg):
    print(msg, file=sys.stderr)


def get(url, params=None, timeout=40, tries=3):
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
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
    for sid, name in STATIONS:
        try:
            text = get(f"https://air.utah.gov/csvFeed.php?id={sid}")
            rows = parse_daq_csv(text, now)
            if not rows:
                raise ValueError("no rows parsed")
            cutoff = now - timedelta(hours=72)
            rows = [r for r in rows if datetime.fromisoformat(r["t"]) >= cutoff]
            out.append({"id": sid, "name": name, "series": rows})
        except Exception as e:  # noqa: BLE001
            record_error(f"DAQ station {name}", e)
            out.append({"id": sid, "name": name, "series": [], "error": str(e)[:200]})
    return out


# ------------------------------------------------------------ DAQ forecast ----
def strip_html(s):
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&nbsp;", " ", s)
    s = re.sub(r"[ \t]+", " ", s)
    return "\n".join(ln.strip() for ln in s.splitlines() if ln.strip())


def fetch_daq_forecast():
    res = {"rss": [], "csv": None}
    try:
        root = ET.fromstring(get(DAQ_FORECAST_RSS))
        for item in root.iter("item"):
            res["rss"].append({
                "title": strip_html(item.findtext("title", "")),
                "description": strip_html(item.findtext("description", "")),
                "pubDate": item.findtext("pubDate", ""),
            })
    except Exception as e:  # noqa: BLE001
        record_error("DAQ forecast RSS", e)
    try:
        text = get(DAQ_FORECAST_CSV)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        delim = "\t" if lines and "\t" in lines[0] else ","
        rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delim))
        if rows:
            header = [h.strip() for h in rows[0]]
            keep = [[c.strip() for c in r] for r in rows[1:]
                    if any("salt lake" in c.lower() for c in r)]
            res["csv"] = {"header": header, "rows": keep or [[c.strip() for c in r] for r in rows[1:8]]}
    except Exception as e:  # noqa: BLE001
        record_error("DAQ forecast CSV", e)
    return res


# ------------------------------------------------------------------- NOAA ----
def noaa_services():
    data = json.loads(get(NOAA_ROOT, {"f": "json"}))
    return [s["name"].split("/")[-1] for s in data.get("services", []) if s.get("type") == "ImageServer"]


def noaa_point_series(service, now):
    """Hourly values at LAT/LON from a time-enabled NDGD ImageServer, latest model run only."""
    base = f"{NOAA_ROOT}/{service}/ImageServer/getSamples"
    geom = json.dumps({"x": LON, "y": LAT, "spatialReference": {"wkid": 4326}})
    t0 = int((now - timedelta(hours=6)).timestamp() * 1000)
    t1 = int((now + timedelta(hours=84)).timestamp() * 1000)
    common = {
        "geometry": geom,
        "geometryType": "esriGeometryPoint",
        "returnFirstValueOnly": "false",
        "outFields": "idp_validtime,idp_issueddate,idp_fcst_hour",
        "f": "json",
    }
    attempts = [
        dict(common, time=f"{t0},{t1}", mosaicRule=json.dumps({"mosaicMethod": "esriMosaicNone"})),
        dict(common, time=f"{t0},{t1}"),
        dict(common),
    ]
    last_err = None
    for params in attempts:
        try:
            data = json.loads(get(base, params))
            if "error" in data:
                raise RuntimeError(json.dumps(data["error"])[:300])
            samples = data.get("samples", [])
            by_issue = {}
            for s in samples:
                a = {k.lower(): v for k, v in (s.get("attributes") or {}).items()}
                vt = a.get("idp_validtime")
                issued = a.get("idp_issueddate") or 0
                raw = s.get("value")
                v = fnum(str(raw).split()[0]) if raw not in (None, "") else None
                if vt is None or v is None or v < 0 or v > 5000:
                    continue
                by_issue.setdefault(issued, {})[int(vt)] = v
            if not by_issue:
                raise RuntimeError(f"no usable samples (got {len(samples)})")
            issued = max(by_issue)
            pts = sorted(by_issue[issued].items())
            series = [{"t": iso(datetime.fromtimestamp(ms / 1000, tz=timezone.utc)), "v": round(v, 1)}
                      for ms, v in pts]
            issued_s = iso(datetime.fromtimestamp(issued / 1000, tz=timezone.utc)) if issued else None
            return {"service": service, "issued": issued_s, "series": series}
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"{service}: {last_err}")


def fetch_noaa(now):
    res = {}
    try:
        names = noaa_services()
    except Exception as e:  # noqa: BLE001
        record_error("NOAA service list", e)
        names = []
    wanted = {
        # bias-corrected hourly PM2.5 first, raw model second
        "pm25": [r"apm25_hr01_bc", r"pm25.*1hr.*bc", r"apm25_hr01$", r"pm25.*1hr"],
        "o3": [r"ozone_1hr.*bc", r"ozone.*1hr", r"o3.*hr01"],
        "smoke": [r"smoke_sfc_1hr"],
    }
    fallbacks = {
        "pm25": ["ndgd_apm25_hr01_bc", "ndgd_apm25_hr01"],
        "o3": ["ndgd_ozone_1hr_avg_time"],
        "smoke": ["ndgd_smoke_sfc_1hr_avg_time"],
    }
    for key, pats in wanted.items():
        cands = []
        for p in pats:
            for n in names:
                if re.search(p, n) and n not in cands:
                    cands.append(n)
        for n in fallbacks[key]:
            if n not in cands:
                cands.append(n)
        errs = []
        for svc in cands:
            try:
                res[key] = noaa_point_series(svc, now)
                res[key]["bias_corrected"] = svc.endswith("_bc")
                break
            except Exception as e:  # noqa: BLE001
                errs.append(str(e)[:160])
        if key not in res:
            record_error(f"NOAA {key}", " | ".join(errs) or "no service found")
    res["available_services"] = names[:80]
    return res


# -------------------------------------------------------------- Open-Meteo ----
def fetch_cams():
    try:
        data = json.loads(get("https://air-quality-api.open-meteo.com/v1/air-quality", {
            "latitude": LAT, "longitude": LON,
            "hourly": "pm2_5,ozone,us_aqi",
            "timezone": "America/Denver", "past_days": 1, "forecast_days": 4,
        }))
        h = data["hourly"]
        out = []
        for i, t in enumerate(h["time"]):
            dt = datetime.fromisoformat(t).replace(tzinfo=TZ)
            o3 = h["ozone"][i]
            out.append({
                "t": iso(dt),
                "pm25": h["pm2_5"][i],
                # µg/m³ -> ppb (approx. at 25 °C, 1 atm)
                "o3": round(o3 / 1.96, 1) if o3 is not None else None,
                "aqi": h["us_aqi"][i],
            })
        return {"model": "CAMS global (via Open-Meteo)", "series": out}
    except Exception as e:  # noqa: BLE001
        record_error("CAMS / Open-Meteo air quality", e)
        return None


def fetch_met():
    base = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": LAT, "longitude": LON,
        "hourly": "boundary_layer_height,wind_speed_10m,temperature_2m,precipitation_probability",
        "timezone": "America/Denver", "past_days": 1, "forecast_days": 4,
        "wind_speed_unit": "mph", "temperature_unit": "fahrenheit",
    }
    last = None
    for model in ("gfs_seamless", None):
        try:
            p = dict(params, models=model) if model else params
            h = json.loads(get(base, p))["hourly"]
            if all(v is None for v in h.get("boundary_layer_height", [])):
                raise RuntimeError("boundary_layer_height all null")
            n = len(h["time"])
            precip = h.get("precipitation_probability") or [None] * n
            out = []
            for i, t in enumerate(h["time"]):
                out.append({
                    "t": iso(datetime.fromisoformat(t).replace(tzinfo=TZ)),
                    "blh_m": h["boundary_layer_height"][i],
                    "wind_mph": h["wind_speed_10m"][i],
                    "temp_f": h["temperature_2m"][i],
                    "precip_pct": precip[i],
                })
            return {"model": model or "best match", "series": out}
        except Exception as e:  # noqa: BLE001
            last = e
    record_error("Mixing height / Open-Meteo weather", last)
    return None


# ------------------------------------------------------------------- main ----
def main():
    now = datetime.now(TZ)
    data = {
        "generated_at": iso(now),
        "location": {"name": LOCATION_NAME, "lat": LAT, "lon": LON},
        "stations": fetch_stations(now),
        "daq_forecast": fetch_daq_forecast(),
        "noaa": fetch_noaa(now),
        "cams": fetch_cams(),
        "met": fetch_met(),
    }
    data["errors"] = errors
    with open(OUT, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    ok = sum(1 for s in data["stations"] if s["series"])
    noaa_keys = [k for k in data["noaa"] if k != "available_services"]
    log(f"wrote {OUT}: {ok}/{len(STATIONS)} stations, noaa={noaa_keys}, "
        f"cams={'yes' if data['cams'] else 'no'}, met={'yes' if data['met'] else 'no'}, errors={len(errors)}")
    # Fail the run only if nothing worked, so a partial outage still publishes.
    if ok == 0 and not data["noaa"].get("pm25") and not data["cams"]:
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        OUT = sys.argv[1]
    main()
