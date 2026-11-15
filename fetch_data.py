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
]
DAQ_FORECAST_RSS = "https://air.utah.gov/rssFeed.php?id=slc"
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


def noaa_point_series(service, now):
    """Hourly values at LAT/LON from a time-enabled NDGD ImageServer.

    The service caps each response at ~20 samples, so we walk the forecast in
    small time windows and, for every valid hour, keep the newest model run.
    """
    base = f"{NOAA_ROOT}/{service}/ImageServer/getSamples"
    geom = json.dumps({"x": LON, "y": LAT, "spatialReference": {"wkid": 4326}})
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
        "hourly": "boundary_layer_height,wind_speed_10m,wind_speed_80m,temperature_2m,precipitation_probability",
        "timezone": "America/Denver", "past_days": 1, "forecast_days": 4,
        "wind_speed_unit": "mph", "temperature_unit": "fahrenheit",
    }
    plain = dict(params, hourly=params["hourly"].replace("wind_speed_80m,", ""))
    last = None
    # 80 m wind is closer to the transport wind used by the Clearing Index; if a
    # model does not serve it, fall back to the original request.
    for prm, model in ((params, "gfs_seamless"), (plain, "gfs_seamless"), (params, None), (plain, None)):
        try:
            p = dict(prm, models=model) if model else prm
            h = json.loads(get(base, p))["hourly"]
            if all(v is None for v in h.get("boundary_layer_height", [])):
                raise RuntimeError("boundary_layer_height all null")
            n = len(h["time"])
            precip = h.get("precipitation_probability") or [None] * n
            wind80 = h.get("wind_speed_80m") or [None] * n
            out = []
            for i, t in enumerate(h["time"]):
                out.append({
                    "t": iso(datetime.fromisoformat(t).replace(tzinfo=TZ)),
                    "blh_m": h["boundary_layer_height"][i],
                    "wind_mph": h["wind_speed_10m"][i],
                    "wind80_mph": wind80[i],
                    "temp_f": h["temperature_2m"][i],
                    "precip_pct": precip[i],
                })
            return {"model": model or "best match", "series": out}
        except Exception as e:  # noqa: BLE001
            last = e
    record_error("Mixing height / Open-Meteo weather", last)
    return None


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

    for key in ("cams", "met"):
        if not data.get(key) and still_useful(prev.get(key)):
            data[key] = dict(prev[key], stale_from=stamp)
    pc = prev.get("clearing")
    if not data.get("clearing") and pc and any(d.get("date", "") >= now.date().isoformat() for d in pc.get("days", [])):
        data["clearing"] = dict(pc, stale_from=stamp)
    for key in ("pm25", "o3", "smoke"):
        if key not in data["noaa"] and still_useful((prev.get("noaa") or {}).get(key)):
            data["noaa"][key] = dict(prev["noaa"][key], stale_from=stamp)


def calibrate_cams(data):
    """CAMS is a ~40 km global model and often reads far off in the valley.
    Scale its PM2.5 by how it compared with the home monitor over the last 24 h."""
    cams = data.get("cams")
    home = next((s for s in data["stations"] if s["series"]), None)
    if not cams or not home:
        return
    obs = {r["t"][:13]: r["pm25"] for r in home["series"][-24:] if r["pm25"] is not None}
    pairs = [(obs[p["t"][:13]], p["pm25"]) for p in cams["series"]
             if p["t"][:13] in obs and p["pm25"] is not None]
    if len(pairs) < 8:
        return
    ratios = sorted((o + 1) / (c + 1) for o, c in pairs)
    factor = max(0.15, min(2.0, ratios[len(ratios) // 2]))
    for p in cams["series"]:
        if p["pm25"] is not None:
            p["pm25_adj"] = round(p["pm25"] * factor, 1)
    cams["calibration"] = {"factor": round(factor, 2), "hours": len(pairs), "station": home["name"]}


# ------------------------------------------------------------------- main ----
def main():
    now = datetime.now(TZ)
    data = {
        "generated_at": iso(now),
        "location": {"name": LOCATION_NAME, "lat": LAT, "lon": LON},
        "stations": fetch_stations(now),
        "daq_forecast": fetch_daq_forecast(),
        "cams": fetch_cams(),
        "met": fetch_met(),
        "clearing": fetch_clearing(now),
    }
    data["noaa"] = fetch_noaa(now)  # slowest source goes last
    reuse_previous(data, now)
    calibrate_cams(data)
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
