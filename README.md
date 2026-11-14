# Salt Lake Air

A one-page, phone-friendly air quality dashboard for the Salt Lake Valley (set up for Murray / Holladay). It answers "is it bad now, and is it going to get better or worse later today?"

A GitHub Action runs every hour, pulls the data below into `data.json`, and commits it. GitHub Pages serves `index.html`, which reads that file.

## Sources (free, no keys)

| What | Source | Why |
|---|---|---|
| Measured now and the past 24 h | Utah DAQ regulatory monitors (`air.utah.gov/csvFeed.php`) | Most accurate instruments in the valley |
| Official daily forecast | Utah DAQ forecasters (`rssFeed.php?id=slc`) | People who know inversions |
| Hourly PM2.5, ozone, and smoke forecast | NOAA/NWS National Air Quality Forecast Capability (bias-corrected PM2.5 when available) | Official US model, hourly out to about 48–72 h |
| Second-opinion hourly forecast | CAMS global model via Open-Meteo | Backup if NOAA is down |
| Mixing height and wind | NOAA GFS/HRRR via Open-Meteo | Hourly estimate of the Clearing Index (mixing depth × wind) for the trapped-air strip |
| Official Clearing Index | NWS Salt Lake City Smoke Management Forecast (`api.weather.gov`) | Utah's own ventilation measure: under 500 is poor and open burning is restricted (R307-202) |

## Change the location

Edit `LAT`, `LON`, `LOCATION_NAME`, and the `STATIONS` order (the first station with data is the "now" station) at the top of `fetch_data.py`. Set `CLEARING_AIRSHED` to your NWS airshed number (map at weather.gov/slc/ClearingIndex).

## Run locally

```
python3 fetch_data.py data.json
python3 -m http.server   # then open http://localhost:8000
```

If a source fails, the run still publishes, and the failure appears under "About this data → Status" on the page.
