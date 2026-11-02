/* Retina map. Vanilla JS, MapLibre GL, no build step. */
(function () {
  const CFG = window.RETINA_CONFIG;
  const TYPES = {
    project_nola: { label: "Project NOLA", color: "#c0392b" },
    nopd: { label: "NOPD / RTCC", color: "#2c5aa0" },
    traffic: { label: "City traffic", color: "#e67e22" },
    alpr: { label: "Flock/ALPR", color: "#8e44ad" },
    private: { label: "Private", color: "#16a085" },
    unknown: { label: "Unknown", color: "#7f8c8d" },
  };
  const MOUNT = { pole: "Pole", building: "Building", traffic_signal: "Traffic signal", other: "Other" };

  const state = {
    all: null,
    types: new Set(Object.keys(TYPES)),
    frOnly: false,
    photosOnly: false,
  };

  // --- filter UI -----------------------------------------------------------
  const typeBox = document.getElementById("type-filters");
  for (const [key, t] of Object.entries(TYPES)) {
    const id = "t-" + key;
    const label = document.createElement("label");
    label.className = "check";
    label.innerHTML = `<input type="checkbox" id="${id}" checked> <span class="swatch" style="background:${t.color}"></span> ${t.label} <span class="count" data-count="${key}"></span>`;
    typeBox.appendChild(label);
    label.querySelector("input").addEventListener("change", (e) => {
      e.target.checked ? state.types.add(key) : state.types.delete(key);
      applyFilters();
    });
  }
  document.getElementById("f-fr").addEventListener("change", (e) => { state.frOnly = e.target.checked; applyFilters(); });
  document.getElementById("f-photos").addEventListener("change", (e) => { state.photosOnly = e.target.checked; applyFilters(); });
  document.getElementById("about-btn").addEventListener("click", () => document.getElementById("about").showModal());

  // --- map -----------------------------------------------------------------
  const map = new maplibregl.Map({
    container: "map",
    style: CFG.BASEMAP_STYLE,
    center: CFG.CENTER,
    zoom: CFG.ZOOM,
    minZoom: CFG.MIN_ZOOM,
    maxBounds: CFG.MAX_BOUNDS,
    hash: true,
    attributionControl: { compact: true },
  });
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), "top-right");
  map.addControl(new maplibregl.GeolocateControl({ positionOptions: { enableHighAccuracy: true }, trackUserLocation: false }), "top-right");

  const colorExpr = ["match", ["get", "camera_type"], ...Object.entries(TYPES).flatMap(([k, t]) => [k, t.color]), "#7f8c8d"];

  map.on("load", async () => {
    map.addSource("cameras", {
      type: "geojson",
      data: { type: "FeatureCollection", features: [] },
      cluster: true,
      clusterRadius: 40,
      clusterMaxZoom: 15,
      promoteId: "id",
    });
    map.addLayer({
      id: "clusters", type: "circle", source: "cameras", filter: ["has", "point_count"],
      paint: {
        "circle-color": "#555", "circle-opacity": 0.85,
        "circle-radius": ["step", ["get", "point_count"], 14, 20, 18, 100, 24, 500, 30],
        "circle-stroke-width": 2, "circle-stroke-color": "#fff",
      },
    });
    map.addLayer({
      id: "cluster-count", type: "symbol", source: "cameras", filter: ["has", "point_count"],
      layout: { "text-field": ["get", "point_count_abbreviated"], "text-size": 12, "text-font": ["Noto Sans Bold"] },
      paint: { "text-color": "#fff" },
    });
    map.addLayer({
      id: "points", type: "circle", source: "cameras", filter: ["!", ["has", "point_count"]],
      paint: {
        "circle-color": colorExpr,
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 10, 4, 14, 7, 17, 10],
        "circle-stroke-width": ["case", ["get", "facial_recognition"], 3, 1.5],
        "circle-stroke-color": ["case", ["get", "facial_recognition"], "#ffd700", "#fff"],
      },
    });
    // Direction tick for cameras with a known bearing.
    map.addLayer({
      id: "direction", type: "symbol", source: "cameras",
      filter: ["all", ["!", ["has", "point_count"]], ["has", "direction"], ["!=", ["get", "direction"], null]],
      layout: {
        "text-field": "▲", "text-size": 14, "text-offset": [0, -1.1],
        "text-rotate": ["get", "direction"], "text-rotation-alignment": "map", "text-allow-overlap": true, "text-font": ["Noto Sans Regular"],
      },
      paint: { "text-color": colorExpr, "text-halo-color": "#fff", "text-halo-width": 1 },
      minzoom: 14,
    });

    map.on("click", "clusters", (e) => {
      const f = map.queryRenderedFeatures(e.point, { layers: ["clusters"] })[0];
      map.getSource("cameras").getClusterExpansionZoom(f.properties.cluster_id).then((z) => {
        map.easeTo({ center: f.geometry.coordinates, zoom: z });
      });
    });
    map.on("click", "points", (e) => openPopup(e.features[0]));
    for (const l of ["clusters", "points"]) {
      map.on("mouseenter", l, () => (map.getCanvas().style.cursor = "pointer"));
      map.on("mouseleave", l, () => (map.getCanvas().style.cursor = ""));
    }

    await loadData();
  });

  async function loadData() {
    const sources = [CFG.DATA_URL, (CFG.API_BASE || "") + "/api/cameras/"].filter(Boolean);
    let data = null;
    for (const url of sources) {
      try {
        const r = await fetch(url, { cache: "no-cache" });
        if (!r.ok) continue;
        data = await r.json();
        if (data && data.type === "FeatureCollection") break;
      } catch (_) { /* try next */ }
    }
    if (!data) {
      document.getElementById("count").textContent = "no data";
      return;
    }
    // MapLibre needs the id on the feature for promoteId; photos come as arrays.
    for (const f of data.features) {
      f.properties.id = f.id;
      f.properties.photo_count = (f.properties.photos || []).length;
    }
    state.all = data;
    document.getElementById("generated").textContent = data.generated_at ? "Updated " + new Date(data.generated_at).toLocaleString() : "";
    applyFilters();
  }

  function applyFilters() {
    if (!state.all) return;
    const perType = {};
    const feats = state.all.features.filter((f) => {
      const p = f.properties;
      perType[p.camera_type] = (perType[p.camera_type] || 0) + 1;
      if (!state.types.has(p.camera_type)) return false;
      if (state.frOnly && !p.facial_recognition) return false;
      if (state.photosOnly && !p.photo_count) return false;
      return true;
    });
    map.getSource("cameras").setData({ type: "FeatureCollection", features: feats });
    document.getElementById("count").textContent = `${feats.length} of ${state.all.features.length}`;
    for (const el of document.querySelectorAll("[data-count]")) {
      el.textContent = perType[el.dataset.count] ? `(${perType[el.dataset.count]})` : "";
    }
  }

  // --- popup ---------------------------------------------------------------
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

  function openPopup(feature) {
    const p = feature.properties;
    const [lng, lat] = feature.geometry.coordinates;
    const t = TYPES[p.camera_type] || TYPES.unknown;
    let photos = [];
    try { photos = typeof p.photos === "string" ? JSON.parse(p.photos) : p.photos || []; } catch (_) {}
    const where = p.cross_street || p.street_address || p.host_business || "";
    const rows = [
      ["Operator", p.operator], ["Host", p.host_business], ["Address", p.street_address],
      ["Maker", [p.manufacturer, p.model_name].filter(Boolean).join(" ")],
      ["Mount", MOUNT[p.mount]], ["Facing", p.direction != null && p.direction !== "" ? p.direction + "°" : ""],
      ["Source", { field_report: "Field report", admin: "Field report", city_traffic: "data.nola.gov", city_rtcc: "City RTCC camera map", osm: "OpenStreetMap", records_request: "Public records" }[p.source]],
      ["Vetted", p.vetted_at ? new Date(p.vetted_at).toLocaleDateString() : ""],
    ].filter(([, v]) => v);
    const gmaps = `https://www.google.com/maps?q=${lat.toFixed(6)},${lng.toFixed(6)}`;
    const sv = `https://www.google.com/maps?layer=c&cbll=${lat.toFixed(6)},${lng.toFixed(6)}`;
    const osm = p.osm_id ? `https://www.openstreetmap.org/node/${p.osm_id}` : null;
    const fix = `submit.html?lat=${lat.toFixed(6)}&lng=${lng.toFixed(6)}&correction=${encodeURIComponent(p.id)}`;
    const html = `
      <div class="popup">
        <h3><span class="badge" style="background:${t.color}">${esc(t.label)}</span>${p.facial_recognition ? '<span class="badge" style="background:#b8860b">Facial recognition</span>' : ""}</h3>
        <div><strong>${esc(where)}</strong></div>
        <dl>${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
        ${photos.length ? `<div class="photos">${photos.map((ph) => `<a href="${esc(ph.url)}" target="_blank" rel="noopener"><img src="${esc(ph.url)}" alt="${esc(ph.type)}" loading="lazy"></a>`).join("")}</div>` : ""}
        <div class="links">
          <a href="${gmaps}" target="_blank" rel="noopener">Open in Google Maps</a>
          <a href="${sv}" target="_blank" rel="noopener">Street View</a>
          ${osm ? `<a href="${osm}" target="_blank" rel="noopener">OSM</a>` : ""}
          <a href="${fix}">Suggest correction</a>
        </div>
      </div>`;
    new maplibregl.Popup({ maxWidth: "320px" }).setLngLat([lng, lat]).setHTML(html).addTo(map);
  }
})();
