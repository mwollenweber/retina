// Runtime configuration for the static frontend. Edit per deployment.
window.RETINA_CONFIG = {
  // Where the published GeoJSON lives. Relative path works when the frontend
  // and data/ are in the same bucket. Falls back to API_BASE + /api/cameras/.
  DATA_URL: "data/cameras.geojson",
  // Django API origin. Empty string = same origin (production behind CloudFront).
  // For local dev with `python manage.py runserver`, use "http://localhost:8000".
  API_BASE: "",
  // MapLibre style. Default is OpenFreeMap (free, no key). For a fully
  // self-hosted basemap, point this at your own style.json that reads a
  // PMTiles archive (see README "Self-hosting the basemap").
  BASEMAP_STYLE: "https://tiles.openfreemap.org/styles/liberty",
  CENTER: [-90.07, 29.96],
  ZOOM: 11.5,
  MIN_ZOOM: 9,
  MAX_BOUNDS: [[-90.45, 29.70], [-89.45, 30.30]],
  CORRECTION_EMAIL: "",
};
