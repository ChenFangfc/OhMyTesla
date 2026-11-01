/* Maps: MapLibre GL with OpenFreeMap vector tiles — no API key, crisp at any
 * zoom, and styled to sit in a dark UI. (CARTO's raster tiles, used before,
 * now render "API KEY REQUIRED" watermarks.)
 *
 * The database stores WGS-84. Map sources in mainland China use GCJ-02, so
 * every coordinate goes through OmtMap.ll(map, lat, lon) / OmtMap.line(map,
 * points) on its way onto a map, never converted at rest.
 *
 * MapLibre is ~900 KB, so it is loaded on first use, not on every page.
 */
const OmtMap = (() => {
  let loading = null;
  function load() {
    if (window.maplibregl) return Promise.resolve();
    if (loading) return loading;
    loading = new Promise((res, rej) => {
      const css = document.createElement("link");
      css.rel = "stylesheet"; css.href = "/static/vendor/maplibre/maplibre-gl.css";
      document.head.append(css);
      const s = document.createElement("script");
      s.src = "/static/vendor/maplibre/maplibre-gl.js";
      s.onload = () => res(); s.onerror = rej;
      document.head.append(s);
    });
    return loading;
  }

  function dark() {
    const forced = document.documentElement.dataset.theme;
    if (forced) return forced === "dark";
    return !matchMedia("(prefers-color-scheme: light)").matches;
  }

  async function styleFor() {
    // A configured tile source (TILE_URL, or the China default) wins: those
    // are raster XYZ templates, wrapped into a minimal style.
    let cfg = {};
    try { cfg = await apiGet("/api/config"); } catch (e) { /* defaults */ }
    if (cfg.tile_url && !/basemaps\.cartocdn\.com/.test(cfg.tile_url)) {
      const datum = cfg.tile_datum || "wgs84";
      const subs = /autonavi|amap/.test(cfg.tile_url) ? ["1", "2", "3", "4"] : ["a", "b", "c"];
      const tiles = cfg.tile_url.includes("{s}") ? subs.map((s) => cfg.tile_url.replace("{s}", s)) : [cfg.tile_url];
      // Raster tiles are drawn for a light page. In the dark theme, invert
      // their brightness and turn the hues back so roads and water keep
      // their colours.
      const paint = dark() ? { "raster-brightness-min": 1, "raster-brightness-max": 0.1,
                               "raster-hue-rotate": 180, "raster-saturation": -0.35 } : {};
      return { datum, style: {
        version: 8,
        sources: { r: { type: "raster", tiles, tileSize: 256, maxzoom: 18,
                        attribution: t(datum === "gcj02" ? "drives.map.attrib_amap" : "drives.map.attrib_osm") } },
        layers: [{ id: "r", type: "raster", source: "r", paint }] } };
    }
    return { datum: "wgs84",
             style: `https://tiles.openfreemap.org/styles/${dark() ? "dark" : "positron"}` };
  }

  /* create(el, {center:[lat,lon], zoom, interactive}) → maplibre Map */
  async function create(el, opts = {}) {
    await load();
    const { style, datum } = await styleFor();
    const map = new maplibregl.Map({
      container: el, style, zoom: opts.zoom ?? 13,
      center: [0, 20],
      interactive: opts.interactive !== false, attributionControl: { compact: true },
      fadeDuration: 0, pitchWithRotate: false,
    });
    map.omtDatum = datum;
    // OpenFreeMap's styles name a few POI icons their sprite doesn't carry;
    // stand in a blank so the console stays clean.
    map.on("styleimagemissing", (e) => {
      if (!map.hasImage(e.id)) map.addImage(e.id, { width: 1, height: 1, data: new Uint8Array(4) });
    });
    if (opts.center) map.setCenter(ll(map, opts.center[0], opts.center[1]));
    if (opts.interactive !== false) map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    return map;
  }

  /* WGS-84 lat/lon → [lng, lat] in the map's own datum. */
  function ll(map, lat, lon) {
    if (map && map.omtDatum === "gcj02" && window.omtGeo) {
      const [la, lo] = omtGeo.wgs2gcj(lat, lon);
      return [lo, la];
    }
    return [lon, lat];
  }
  /* [[lat, lon], …] (WGS-84) → [[lng, lat], …] in the map's datum. */
  function line(map, points) {
    return points.map(([lat, lon]) => ll(map, lat, lon));
  }

  function ready(map) {
    return map.isStyleLoaded() ? Promise.resolve(map) : new Promise((res) => map.once("load", () => res(map)));
  }

  /* lngLat must already be in the map's datum (use ll()). */
  function carMarker(map, lngLat, heading) {
    const el = document.createElement("div");
    el.style.cssText = "width:18px;height:18px;border-radius:50%;background:#3e6ae1;border:3px solid #fff;box-shadow:0 0 0 6px rgba(62,106,225,.25)";
    return new maplibregl.Marker({ element: el, rotation: heading || 0 }).setLngLat(lngLat).addTo(map);
  }

  /* Draw a route; coords = [[lng,lat],...] in the map's datum. Returns the source id. */
  function route(map, id, coords, color = "#3e6ae1", width = 4) {
    const data = { type: "Feature", geometry: { type: "LineString", coordinates: coords } };
    const add = () => {
      if (map.getSource(id)) { map.getSource(id).setData(data); return; }
      map.addSource(id, { type: "geojson", data });
      map.addLayer({ id, type: "line", source: id, layout: { "line-cap": "round", "line-join": "round" },
                     paint: { "line-color": color, "line-width": width, "line-opacity": 0.95 } });
    };
    if (map.isStyleLoaded()) add(); else map.once("load", add);
    return id;
  }

  function fit(map, coords, pad = 40) {
    if (!coords.length) return;
    const b = coords.reduce((bb, c) => bb.extend(c), new maplibregl.LngLatBounds(coords[0], coords[0]));
    map.fitBounds(b, { padding: pad, duration: 0, maxZoom: 16 });
  }

  return { load, create, ll, line, ready, carMarker, route, fit };
})();
