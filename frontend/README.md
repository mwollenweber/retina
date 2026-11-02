# Frontend

Static site: MapLibre GL JS, vanilla JS, no build step.

- `index.html` / `app.js`: the map. Reads `data/cameras.geojson` (published by the backend or the
  `publish_geojson` Lambda) and falls back to `API_BASE/api/cameras/`.
- `submit.html`: public report form. Posts multipart to `API_BASE/api/reports/`.
- `config.js`: per-deployment settings (data URL, API origin, basemap style).

## Local dev

```sh
# backend running on :8000 (see root README), then:
cd frontend
sed -i '' 's|API_BASE: ""|API_BASE: "http://localhost:8000"|' config.js   # or edit by hand
python3 -m http.server 5173
open http://localhost:5173
```

Publish data locally with `python manage.py publish_geojson` (writes `frontend/data/cameras.geojson`).

## Self-hosting the basemap

The default style is OpenFreeMap (free, no API key, no account). To remove that dependency:

1. Build a PMTiles extract for the New Orleans area from Protomaps' daily planet builds:
   `pmtiles extract https://build.protomaps.com/<date>.pmtiles nola.pmtiles --bbox=-90.45,29.70,-89.45,30.30`
2. Upload `nola.pmtiles` to the site bucket under `data/`.
3. Create a `style.json` that uses the `pmtiles://` protocol (see the Protomaps basemaps docs for a
   ready-made style) and set `BASEMAP_STYLE` in `config.js` to it. Add the PMTiles protocol script
   to `index.html` and `submit.html`.
