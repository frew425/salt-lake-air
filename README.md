# Salt Lake Air

A one-page, phone-friendly air quality dashboard for the Salt Lake and Utah valleys. Visitors choose the Utah DAQ air monitoring station closest to them from a map; their location is never requested. It answers "is it bad now, and is it going to get better or worse later today?"

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

## Stations and areas

Everything on the page follows the station a visitor picks. The choice is saved in their browser.

- `STATIONS` in `fetch_data.py` lists the stations: DAQ feed id, name, area, county, map position and forecast zone. A station only appears in the picker while its feed has a PM2.5 reading from the last 8 hours.
- `ZONES` are the points the model forecasts (NOAA, CAMS, weather) are fetched for. Nearby stations share a zone because the models are coarse.
- `COUNTIES` sets which Utah DAQ forecast feed each county uses.
- `CLEARING_AIRSHED` is the NWS airshed number (map at smokemgt.utah.gov/static/pdf/Airsheds_NAAclass1.pdf). All current stations are in airshed 8.
- To add a station, add it to `STATIONS`, then add its name, area, county and coordinates to `META` in `index.html` so it can be drawn on the map.

## Link parameters

- `?station=cv` opens on a station: `slc` Hawthorne, `rp` Rose Park, `ut` Utah Tech Center, `lp` West Valley, `nr` Near Road, `cv` Copper View, `ln` Lindon, `sf` Spanish Fork.
- `&chart=ozone` opens the 3-day outlook on the ozone tab. Older links with `&chart=inversion` still work and open the PM2.5 tab; the inversion outlook now lives in the valley section with its own 3-day slider.

A link's station is used for that visit only; it does not replace the visitor's saved choice. The share button in the header copies a link to the current view.

## Run locally

```
python3 fetch_data.py data.json
python3 -m http.server   # then open http://localhost:8000
```

If a source fails, the run still publishes, and the failure appears under "About this data → Status" on the page.
