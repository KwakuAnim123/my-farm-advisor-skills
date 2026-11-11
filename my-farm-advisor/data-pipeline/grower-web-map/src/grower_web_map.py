#!/usr/bin/env python3
"""Render a lightweight interactive field map for one or more growers.

The generator reads real field boundaries from the My Farm Advisor runtime and
writes a single self-contained HTML file per grower. Field polygons are the only
spatial payload embedded in the HTML: no satellite rasters, no NDVI GeoTIFFs,
and no raw data tables are inlined. The Esri World Imagery basemap is loaded as
XYZ tiles by the browser at view time.

Runtime layout::

    ${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower>/farms/<farm>/
        boundary/field_boundaries.geojson
        farm.json

Output layout::

    ${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower>/web-map/
        <grower>_field_map.html
        web_map_manifest.json

The script never writes outside the runtime data root. Run it with the runtime
virtualenv so ``geopandas``/``shapely`` match the pipeline environment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

GENERATOR_NAME = "grower-web-map"
GENERATOR_VERSION = "1.0.0"
DEFAULT_MAX_HTML_BYTES = 1_000_000

ESRI_IMAGERY_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Imagery/MapServer/tile/{z}/{y}/{x}"
)
ESRI_IMAGERY_ATTRIBUTION = (
    "Tiles (c) Esri - Source: Esri, Maxar, Earthstar Geographics, "
    "and the GIS User Community"
)
OSM_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_ATTRIBUTION = "OpenStreetMap contributors"

FARM_COLORS = [
    "#2E7D32",
    "#1565C0",
    "#EF6C00",
    "#6A1B9A",
    "#00838F",
    "#C62828",
    "#558B2F",
    "#4527A0",
    "#AD1457",
    "#00695C",
]


def iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prettify_slug(slug: str) -> str:
    parts = [part for part in re.split(r"[-_]+", str(slug)) if part]
    words: list[str] = []
    for part in parts:
        if len(part) == 2 and part.isalpha():
            words.append(part.upper())
        elif part.isupper():
            words.append(part)
        else:
            words.append(part[:1].upper() + part[1:])
    return " ".join(words)


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def clean_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return value


def round_coords(value: Any, digits: int = 6) -> Any:
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, (list, tuple)):
        return [round_coords(item, digits) for item in value]
    return value


def field_key(farm_slug: str, field_id: str) -> str:
    return f"{farm_slug}:{field_id}"


def _load_geopandas():
    try:
        import geopandas as gpd  # noqa: F401
    except ImportError as exc:  # pragma: no cover - environment guard
        raise SystemExit(
            "geopandas is required. Run this script with the data-pipeline "
            f"runtime virtualenv, not {sys.executable}."
        ) from exc
    return gpd


def _area_hectares(row_area_ha: Any, projected_area_ha: float | None) -> float:
    for candidate in (row_area_ha, projected_area_ha):
        if candidate is None:
            continue
        try:
            value = float(candidate)
        except (TypeError, ValueError):
            continue
        if value > 0 and not math.isnan(value):
            return value
    return 0.0


def _boundary_state_values(frame) -> list[str]:
    if "state" not in frame.columns:
        return []
    values = {
        str(value).strip().upper()
        for value in frame["state"].tolist()
        if str(value).strip()
    }
    return sorted(values)


def _maybe_fix_farm_state(
    *,
    farm_dir: Path,
    farm_meta: dict[str, Any],
    boundary_states: list[str],
    state_fips: list[str],
    fixes: list[dict[str, Any]],
) -> None:
    if len(boundary_states) != 1:
        return
    correct_state = boundary_states[0]
    previous = str(farm_meta.get("state", "")).strip().upper()
    if not previous or previous == correct_state:
        return
    farm_meta["state"] = correct_state
    write_json_atomic(farm_dir / "farm.json", farm_meta)
    record = {
        "farm_dir": str(farm_dir),
        "field": "state",
        "previous": previous,
        "corrected": correct_state,
        "evidence": {
            "boundary_field_states": boundary_states,
            "boundary_state_fips": state_fips,
        },
        "upstream_issue": (
            "ensure_canonical_data_tree writes farm.json with a hardcoded "
            "IA default; field boundaries carry the authoritative state."
        ),
        "corrected_at": iso_now(),
    }
    write_json_atomic(farm_dir / "manifests" / "farm_metadata_state_fix.json", record)
    fixes.append(record)


def load_farms(
    grower_dir: Path,
    *,
    farm_slugs: set[str] | None,
    fix_farm_state: bool,
    warnings: list[str],
    fixes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[float], dict[str, Any]]:
    gpd = _load_geopandas()
    from shapely.geometry import mapping
    from shapely.validation import make_valid

    farms_root = grower_dir / "farms"
    if not farms_root.exists():
        raise SystemExit(f"grower has no farms directory: {farms_root}")

    farm_dirs = sorted(path for path in farms_root.iterdir() if path.is_dir())
    if farm_slugs:
        farm_dirs = [path for path in farm_dirs if path.name in farm_slugs]
    if not farm_dirs:
        raise SystemExit(f"no matching farms under {farms_root}")

    farms: list[dict[str, Any]] = []
    features: list[dict[str, Any]] = []
    all_bounds = [math.inf, math.inf, -math.inf, -math.inf]

    for color_index, farm_dir in enumerate(farm_dirs):
        boundary_path = farm_dir / "boundary" / "field_boundaries.geojson"
        if not boundary_path.exists():
            warnings.append(f"{farm_dir.name}: missing {boundary_path.name}; farm skipped")
            continue

        farm_meta = read_json(farm_dir / "farm.json")
        farm_name = str(farm_meta.get("display_name") or "").strip() or prettify_slug(farm_dir.name)

        try:
            frame = gpd.read_file(boundary_path)
        except Exception as exc:
            warnings.append(f"{farm_dir.name}: could not read boundary ({exc}); farm skipped")
            continue

        if frame.empty:
            warnings.append(f"{farm_dir.name}: boundary has no features; farm skipped")
            continue

        if frame.crs is None:
            frame = frame.set_crs(4326, allow_override=True)
        elif frame.crs.to_epsg() != 4326:
            frame = frame.to_crs(4326)

        boundary_states = _boundary_state_values(frame)
        state_fips = []
        if "state_fips" in frame.columns:
            state_fips = sorted({str(value).strip().zfill(2) for value in frame["state_fips"] if str(value).strip()})

        stored_state = str(farm_meta.get("state", "")).strip().upper()
        if len(boundary_states) == 1 and stored_state and stored_state != boundary_states[0]:
            warnings.append(
                f"{farm_dir.name}: farm.json state {stored_state!r} disagrees with "
                f"boundary state {boundary_states[0]!r}"
            )
            if fix_farm_state:
                _maybe_fix_farm_state(
                    farm_dir=farm_dir,
                    farm_meta=farm_meta,
                    boundary_states=boundary_states,
                    state_fips=state_fips,
                    fixes=fixes,
                )
                warnings.append(
                    f"{farm_dir.name}: farm.json state corrected to {boundary_states[0]} "
                    "from boundary evidence"
                )

        try:
            projected = frame.to_crs(frame.estimate_utm_crs())
            projected_areas: list[float | None] = [
                float(value) if value is not None else None
                for value in (projected.geometry.area / 10_000).tolist()
            ]
        except Exception:
            projected_areas = [None] * len(frame)

        color = FARM_COLORS[color_index % len(FARM_COLORS)]
        farm_field_count = 0
        farm_acres = 0.0

        for index, row in frame.iterrows():
            geometry = row.geometry
            if geometry is None or geometry.is_empty:
                warnings.append(f"{farm_dir.name}: empty geometry skipped")
                continue
            if not geometry.is_valid:
                geometry = make_valid(geometry)
            if geometry.is_empty:
                warnings.append(f"{farm_dir.name}: invalid geometry skipped after make_valid")
                continue

            field_id = str(clean_value(row.get("field_id", ""))).strip()
            if not field_id:
                warnings.append(f"{farm_dir.name}: feature without field_id skipped")
                continue

            projected_area = (
                projected_areas[list(frame.index).index(index)]
                if list(frame.index).index(index) < len(projected_areas)
                else None
            )
            area_ha = _area_hectares(row.get("area_ha_est", row.get("area_ha")), projected_area)
            area_acres = area_ha * 2.471053814671653 if area_ha else 0.0
            farm_field_count += 1
            farm_acres += area_acres

            minx, miny, maxx, maxy = geometry.bounds
            all_bounds = [
                min(all_bounds[0], minx),
                min(all_bounds[1], miny),
                max(all_bounds[2], maxx),
                max(all_bounds[3], maxy),
            ]

            properties = {
                "field_id": field_id,
                "field_slug": str(clean_value(row.get("field_slug", field_id))) or field_id,
                "farm_slug": farm_dir.name,
                "farm_name": farm_name,
                "grower_slug": grower_dir.name,
                "area_acres": round(area_acres, 1),
                "area_ha": round(area_ha, 1),
                "crop": str(clean_value(row.get("crop", ""))),
                "county": str(clean_value(row.get("county_name", row.get("county", "")))),
                "state": str(clean_value(row.get("state", ""))),
                "csb_id": str(clean_value(row.get("csb_id", ""))),
                "farm_color": color,
                "field_key": field_key(farm_dir.name, field_id),
            }
            geometry_payload = mapping(geometry)
            geometry_payload["coordinates"] = round_coords(geometry_payload["coordinates"])
            features.append(
                {
                    "type": "Feature",
                    "geometry": geometry_payload,
                    "properties": properties,
                }
            )

        if farm_field_count:
            farms.append(
                {
                    "slug": farm_dir.name,
                    "name": farm_name,
                    "color": color,
                    "field_count": farm_field_count,
                    "acres": round(farm_acres, 1),
                    "state": boundary_states[0] if len(boundary_states) == 1 else "",
                    "source_boundary": str(boundary_path),
                    "source_sha256": sha256_file(boundary_path),
                }
            )
        else:
            warnings.append(f"{farm_dir.name}: no usable field polygons; farm omitted")

    if not features:
        raise SystemExit("no usable field polygons found")

    totals = {
        "farms": len(farms),
        "fields": len(features),
        "acres": round(sum(float(feature["properties"]["area_acres"]) for feature in features), 1),
    }
    return farms, {"type": "FeatureCollection", "features": features}, all_bounds, totals


def html_template() -> str:
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
      integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="" />
<style>
:root {
  --ink: #0f172a;
  --muted: #64748b;
  --line: #e2e8f0;
  --accent: #2e7d32;
  --accent-dark: #1b5e20;
  --sidebar: #0b1220;
  --sidebar-2: #111b2e;
  --sidebar-ink: #e2e8f0;
  --sidebar-muted: #94a3b8;
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  color: var(--ink);
  background: #f8fafc;
  -webkit-font-smoothing: antialiased;
}
#app { display: flex; height: 100vh; width: 100vw; overflow: hidden; }
#sidebar {
  width: 370px;
  flex: 0 0 370px;
  background: linear-gradient(180deg, var(--sidebar), var(--sidebar-2));
  color: var(--sidebar-ink);
  display: flex;
  flex-direction: column;
  border-right: 1px solid rgba(255,255,255,.06);
  z-index: 900;
}
.brand { padding: 22px 22px 0; }
.eyebrow {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: .16em;
  text-transform: uppercase;
  color: #86efac;
}
.eyebrow::before {
  content: "";
  width: 7px; height: 7px; border-radius: 50%;
  background: #22c55e;
  box-shadow: 0 0 0 4px rgba(34,197,94,.16);
}
.grower-title { margin: 12px 0 4px; font-size: 25px; line-height: 1.15; letter-spacing: -.02em; }
.grower-sub { margin: 0; font-size: 12.5px; color: var(--sidebar-muted); }
.stats {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
  padding: 16px 22px 0;
}
.stat {
  background: rgba(255,255,255,.045);
  border: 1px solid rgba(255,255,255,.07);
  border-radius: 10px;
  padding: 9px 10px;
}
.stat b { display: block; font-size: 16px; font-weight: 700; letter-spacing: -.01em; }
.stat span { display: block; margin-top: 2px; font-size: 10px; text-transform: uppercase; letter-spacing: .08em; color: var(--sidebar-muted); }
.search-wrap { padding: 16px 22px 10px; }
#field-search {
  width: 100%;
  border: 1px solid rgba(255,255,255,.12);
  background: rgba(255,255,255,.05);
  color: var(--sidebar-ink);
  border-radius: 9px;
  padding: 10px 12px;
  font-size: 13px;
  outline: none;
}
#field-search::placeholder { color: #7c8aa5; }
#field-search:focus { border-color: #22c55e; box-shadow: 0 0 0 3px rgba(34,197,94,.14); }
.nav-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 2px 22px 10px;
}
.nav-head span { font-size: 10.5px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; color: var(--sidebar-muted); }
#nav-toggle {
  border: 0; background: transparent; color: #86efac; font-size: 11.5px;
  font-weight: 600; cursor: pointer; padding: 2px 0;
}
.nav { flex: 1; overflow-y: auto; padding: 0 14px 18px; scrollbar-width: thin; }
.farm-group { margin-bottom: 14px; }
.farm-head {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 9px 6px;
  color: #cbd5e1;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .06em;
  text-transform: uppercase;
}
.farm-swatch { width: 8px; height: 8px; border-radius: 2px; flex: 0 0 8px; }
.farm-count { margin-left: auto; color: #7c8aa5; font-weight: 600; letter-spacing: 0; text-transform: none; }
.field-item {
  display: flex; align-items: center; gap: 10px; width: 100%;
  padding: 9px 10px;
  border: 0; border-radius: 8px;
  background: transparent; color: inherit;
  text-align: left; cursor: pointer;
  transition: background .12s ease;
}
.field-item:hover, .field-item.hover { background: rgba(255,255,255,.06); }
.field-item.active { background: rgba(34,197,94,.14); box-shadow: inset 3px 0 0 #22c55e; }
.field-dot { width: 9px; height: 9px; border-radius: 50%; flex: 0 0 9px; box-shadow: 0 0 0 3px rgba(255,255,255,.06); }
.field-text { min-width: 0; flex: 1; }
.field-name { display: block; font-size: 13px; font-weight: 650; letter-spacing: .01em; }
.field-meta { display: block; margin-top: 2px; font-size: 11.5px; color: var(--sidebar-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.chev { color: #64748b; font-size: 16px; line-height: 1; }
.empty { padding: 18px 12px; color: var(--sidebar-muted); font-size: 12.5px; }
.sidebar-footer {
  padding: 12px 22px 16px;
  border-top: 1px solid rgba(255,255,255,.07);
  color: #7c8aa5;
  font-size: 10.5px;
  line-height: 1.5;
}
.sidebar-footer b { color: #cbd5e1; font-weight: 600; }
#sidebar.nav-collapsed .nav, #sidebar.nav-collapsed .search-wrap { display: none; }
#map-wrap { position: relative; flex: 1; min-width: 0; }
#map { position: absolute; inset: 0; background: #dbeafe; }
.map-topbar {
  position: absolute; top: 14px; right: 14px; z-index: 600;
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  justify-content: flex-end;
}
.seg {
  display: flex; background: #fff; border-radius: 10px; overflow: hidden;
  box-shadow: 0 8px 24px rgba(15,23,42,.16), 0 1px 2px rgba(15,23,42,.08);
  border: 1px solid rgba(15,23,42,.06);
}
.seg button {
  border: 0; background: #fff; color: #334155; cursor: pointer;
  padding: 9px 13px; font-size: 12px; font-weight: 650;
}
.seg button + button { border-left: 1px solid #e2e8f0; }
.seg button.active { background: var(--accent); color: #fff; }
.chip-btn, .toggle-chip {
  display: inline-flex; align-items: center; gap: 7px;
  background: #fff; color: #334155;
  border: 1px solid rgba(15,23,42,.06);
  border-radius: 10px;
  box-shadow: 0 8px 24px rgba(15,23,42,.16), 0 1px 2px rgba(15,23,42,.08);
  padding: 9px 12px; font-size: 12px; font-weight: 650; cursor: pointer;
}
.chip-btn:hover, .toggle-chip:hover { color: var(--accent-dark); }
.legend {
  position: absolute; left: 14px; bottom: 30px; z-index: 600;
  background: rgba(255,255,255,.96);
  border: 1px solid rgba(15,23,42,.06);
  border-radius: 10px;
  box-shadow: 0 8px 24px rgba(15,23,42,.14);
  padding: 10px 12px;
  font-size: 11px;
  color: #334155;
}
.legend-title { font-weight: 700; text-transform: uppercase; letter-spacing: .08em; font-size: 9.5px; color: #64748b; margin-bottom: 6px; }
.legend-row { display: flex; align-items: center; gap: 7px; margin-top: 4px; }
.legend-swatch { width: 10px; height: 10px; border-radius: 2px; }
.field-label {
  background: rgba(15,23,42,.84);
  border: 0; box-shadow: none;
  color: #fff; font-size: 11px; font-weight: 700;
  padding: 2px 7px; border-radius: 999px;
  letter-spacing: .01em;
}
.field-label::before { display: none; }
.hide-field-labels .field-label { display: none; }
.leaflet-popup-content-wrapper { border-radius: 12px; box-shadow: 0 18px 50px rgba(15,23,42,.28); }
.leaflet-popup-content { margin: 0; }
.popup { padding: 14px 16px 13px; min-width: 230px; }
.popup-kicker { font-size: 10px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; color: var(--accent); }
.popup h3 { margin: 5px 0 2px; font-size: 18px; letter-spacing: -.01em; }
.popup .popup-farm { margin: 0 0 10px; font-size: 12px; color: var(--muted); }
.popup dl { display: grid; grid-template-columns: auto 1fr; gap: 5px 14px; margin: 0; }
.popup dt { color: var(--muted); font-size: 11.5px; }
.popup dd { margin: 0; font-size: 11.5px; font-weight: 600; text-align: right; }
.popup .popup-foot { margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--line); font-size: 10px; color: #94a3b8; }
.leaflet-control-attribution { font-size: 9.5px; }
@media (max-width: 860px) {
  #app { flex-direction: column; }
  #sidebar { width: 100%; flex: 0 0 auto; max-height: 46vh; }
  #sidebar.nav-collapsed { max-height: none; }
  #sidebar.nav-collapsed .stats { display: none; }
  .nav { max-height: 26vh; }
  .map-topbar { top: 10px; right: 10px; }
  .legend { bottom: 20px; left: 10px; }
}
</style>
</head>
<body>
<div id="app">
  <aside id="sidebar" aria-label="Grower and field navigation">
    <div class="brand">
      <div class="eyebrow">My Farm Advisor</div>
      <h1 class="grower-title" id="grower-title">Grower</h1>
      <p class="grower-sub" id="grower-sub">Field map</p>
    </div>
    <div class="stats" aria-label="Farm summary">
      <div class="stat"><b id="stat-farms">0</b><span>Farms</span></div>
      <div class="stat"><b id="stat-fields">0</b><span>Fields</span></div>
      <div class="stat"><b id="stat-acres">0</b><span>Acres</span></div>
    </div>
    <div class="search-wrap">
      <input id="field-search" type="search" autocomplete="off" spellcheck="false"
             placeholder="Search fields, farms, crops..." aria-label="Search fields" />
    </div>
    <div class="nav-head">
      <span>Fields</span>
      <button id="nav-toggle" type="button" aria-expanded="true">Hide list</button>
    </div>
    <nav class="nav" id="field-nav" aria-label="Field list"></nav>
    <div class="sidebar-footer">
      <b>Boundaries</b> USDA CSB / FIBOA &middot;
      <b>Imagery</b> Esri World Imagery<br />
      Generated __GENERATED_AT__ &middot; __GENERATOR_NAME__ v__GENERATOR_VERSION__
    </div>
  </aside>
  <main id="map-wrap">
    <div class="map-topbar">
      <div class="seg" role="group" aria-label="Basemap">
        <button type="button" data-basemap="satellite" class="active" aria-pressed="true">Satellite</button>
        <button type="button" data-basemap="street" aria-pressed="false">Street</button>
      </div>
      <label class="toggle-chip">
        <input id="labels-toggle" type="checkbox" checked />
        Field labels
      </label>
      <button id="reset-view" class="chip-btn" type="button">Reset view</button>
    </div>
    <div id="map"></div>
    <div class="legend" id="legend" aria-label="Farm legend"></div>
  </main>
</div>
<script id="map-data" type="application/json">__PAYLOAD__</script>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<script>
(function () {
  "use strict";

  var DATA = JSON.parse(document.getElementById("map-data").textContent);
  var ESRI_URL = __ESRI_URL__;
  var ESRI_ATTR = __ESRI_ATTR__;
  var OSM_URL = __OSM_URL__;
  var OSM_ATTR = __OSM_ATTR__;

  var map = L.map("map", {
    zoomControl: false,
    preferCanvas: true,
    minZoom: 3,
    maxZoom: 19
  });

  var satellite = L.tileLayer(ESRI_URL, { maxZoom: 19, attribution: ESRI_ATTR });
  var street = L.tileLayer(OSM_URL, { maxZoom: 19, attribution: OSM_ATTR });
  var activeBase = satellite;
  activeBase.addTo(map);

  L.control.zoom({ position: "bottomright" }).addTo(map);
  L.control.scale({ position: "bottomright", imperial: true, metric: true }).addTo(map);

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function formatNumber(value) {
    var number = Number(value);
    if (!isFinite(number)) { return "0"; }
    return number.toLocaleString(undefined, { maximumFractionDigits: 0 });
  }

  function popupHtml(props) {
    var rows = [
      ["Grower", props.grower_slug],
      ["Farm", props.farm_name],
      ["Field", props.field_id],
      ["Area", formatNumber(props.area_acres) + " ac / " + formatNumber(props.area_ha) + " ha"],
      ["Crop (CSB year)", props.crop || "Not recorded"],
      ["County, State", [props.county, props.state].filter(Boolean).join(", ")],
      ["CSB ID", props.csb_id || "Not recorded"]
    ];
    var body = rows.map(function (row) {
      return "<dt>" + escapeHtml(row[0]) + "</dt><dd>" + escapeHtml(row[1]) + "</dd>";
    }).join("");
    return '<div class="popup">' +
      '<div class="popup-kicker">Field boundary</div>' +
      "<h3>" + escapeHtml(props.field_id) + "</h3>" +
      '<p class="popup-farm">' + escapeHtml(props.farm_name) + " &middot; " + escapeHtml(props.grower_slug) + "</p>" +
      "<dl>" + body + "</dl>" +
      '<div class="popup-foot">USDA CSB / FIBOA boundary &middot; Esri World Imagery basemap</div>' +
      "</div>";
  }

  function baseStyle(feature) {
    var props = feature.properties || {};
    return {
      color: props.farm_color || "#2E7D32",
      weight: 2.5,
      opacity: 1,
      fillColor: props.farm_color || "#2E7D32",
      fillOpacity: 0.18
    };
  }

  var layersByKey = {};
  var itemsByKey = {};

  var geoLayer = L.geoJSON(DATA.fields, {
    style: baseStyle,
    onEachFeature: function (feature, layer) {
      var props = feature.properties || {};
      var key = props.field_key;

      layer.bindTooltip(String(props.field_id || "Field"), {
        permanent: true,
        direction: "center",
        className: "field-label",
        opacity: 1
      });
      layer.bindPopup(popupHtml(props), { maxWidth: 340, closeButton: true });

      layer.on("mouseover", function () {
        layer.setStyle({ weight: 4, fillOpacity: 0.32 });
        if (itemsByKey[key]) { itemsByKey[key].classList.add("hover"); }
      });
      layer.on("mouseout", function () {
        geoLayer.resetStyle(layer);
        if (itemsByKey[key]) { itemsByKey[key].classList.remove("hover"); }
      });
      layer.on("click", function () {
        setActive(key);
      });

      layersByKey[key] = layer;
    }
  }).addTo(map);

  if (!geoLayer.getLayers().length) { return; }

  var initialBounds = geoLayer.getBounds();
  map.fitBounds(initialBounds, { padding: [34, 34] });

  document.getElementById("grower-title").textContent = DATA.grower.name;
  document.getElementById("grower-sub").textContent =
    "Grower field map \u00b7 " + DATA.totals.farms + " farm" + (DATA.totals.farms === 1 ? "" : "s") +
    " \u00b7 " + DATA.totals.fields + " fields";
  document.getElementById("stat-farms").textContent = DATA.totals.farms;
  document.getElementById("stat-fields").textContent = DATA.totals.fields;
  document.getElementById("stat-acres").textContent = formatNumber(DATA.totals.acres);

  var nav = document.getElementById("field-nav");
  var legend = document.getElementById("legend");
  var allItems = [];

  DATA.farms.forEach(function (farm) {
    var farmFields = DATA.fields.features.filter(function (feature) {
      return feature.properties.farm_slug === farm.slug;
    });

    var legendRow = document.createElement("div");
    legendRow.className = "legend-row";
    var swatch = document.createElement("span");
    swatch.className = "legend-swatch";
    swatch.style.background = farm.color;
    var label = document.createElement("span");
    label.textContent = farm.name;
    legendRow.appendChild(swatch);
    legendRow.appendChild(label);
    legend.appendChild(legendRow);

    var group = document.createElement("section");
    group.className = "farm-group";

    var head = document.createElement("div");
    head.className = "farm-head";
    var farmSwatch = document.createElement("span");
    farmSwatch.className = "farm-swatch";
    farmSwatch.style.background = farm.color;
    var farmName = document.createElement("span");
    farmName.textContent = farm.name;
    var count = document.createElement("span");
    count.className = "farm-count";
    count.textContent = farm.field_count;
    head.appendChild(farmSwatch);
    head.appendChild(farmName);
    head.appendChild(count);

    var list = document.createElement("div");
    farmFields.forEach(function (feature) {
      var props = feature.properties;
      var item = document.createElement("button");
      item.type = "button";
      item.className = "field-item";
      item.dataset.key = props.field_key;
      item.dataset.search = [
        props.field_id, props.field_slug, props.farm_name, props.farm_slug,
        props.crop, props.county, props.state
      ].join(" ").toLowerCase();

      var dot = document.createElement("span");
      dot.className = "field-dot";
      dot.style.background = farm.color;

      var text = document.createElement("span");
      text.className = "field-text";
      var name = document.createElement("span");
      name.className = "field-name";
      name.textContent = props.field_id;
      var meta = document.createElement("span");
      meta.className = "field-meta";
      meta.textContent = formatNumber(props.area_acres) + " ac \u00b7 " +
        (props.crop || "Crop n/a") + " \u00b7 " + [props.county, props.state].filter(Boolean).join(", ");
      text.appendChild(name);
      text.appendChild(meta);

      var chev = document.createElement("span");
      chev.className = "chev";
      chev.textContent = "\u203a";

      item.appendChild(dot);
      item.appendChild(text);
      item.appendChild(chev);

      item.addEventListener("click", function () {
        var layer = layersByKey[props.field_key];
        if (!layer) { return; }
        setActive(props.field_key);
        map.flyToBounds(layer.getBounds(), { padding: [90, 90], maxZoom: 16, duration: 0.7 });
        window.setTimeout(function () { layer.openPopup(); }, 750);
      });
      item.addEventListener("mouseenter", function () {
        var layer = layersByKey[props.field_key];
        if (layer) { layer.setStyle({ weight: 4, fillOpacity: 0.32 }); }
      });
      item.addEventListener("mouseleave", function () {
        var layer = layersByKey[props.field_key];
        if (layer) { geoLayer.resetStyle(layer); }
      });

      itemsByKey[props.field_key] = item;
      allItems.push(item);
      list.appendChild(item);
    });

    group.appendChild(head);
    group.appendChild(list);
    nav.appendChild(group);
  });

  if (!allItems.length) {
    var empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "No fields found for this grower.";
    nav.appendChild(empty);
  }

  var activeKey = null;
  function setActive(key) {
    if (activeKey && itemsByKey[activeKey]) {
      itemsByKey[activeKey].classList.remove("active");
    }
    activeKey = key;
    if (itemsByKey[key]) {
      itemsByKey[key].classList.add("active");
      itemsByKey[key].scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  var search = document.getElementById("field-search");
  function applyFilter() {
    var query = search.value.trim().toLowerCase();
    allItems.forEach(function (item) {
      var match = !query || item.dataset.search.indexOf(query) !== -1;
      item.hidden = !match;
      var layer = layersByKey[item.dataset.key];
      if (!layer) { return; }
      if (match) {
        geoLayer.resetStyle(layer);
      } else {
        layer.setStyle({ opacity: 0.12, fillOpacity: 0.02 });
      }
    });
    Array.prototype.forEach.call(nav.querySelectorAll(".farm-group"), function (group) {
      var visible = Array.prototype.some.call(group.querySelectorAll(".field-item"), function (item) {
        return !item.hidden;
      });
      group.hidden = !visible;
    });
  }
  search.addEventListener("input", applyFilter);

  function setBasemap(name) {
    var next = name === "street" ? street : satellite;
    if (next === activeBase) { return; }
    map.removeLayer(activeBase);
    activeBase = next;
    activeBase.addTo(map);
    Array.prototype.forEach.call(document.querySelectorAll("[data-basemap]"), function (button) {
      var active = button.dataset.basemap === name;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }
  Array.prototype.forEach.call(document.querySelectorAll("[data-basemap]"), function (button) {
    button.addEventListener("click", function () { setBasemap(button.dataset.basemap); });
  });

  var labelsToggle = document.getElementById("labels-toggle");
  function setLabels(enabled) {
    document.getElementById("map").classList.toggle("hide-field-labels", !enabled);
  }
  labelsToggle.addEventListener("change", function () { setLabels(labelsToggle.checked); });
  setLabels(labelsToggle.checked && DATA.meta.labels_default !== false);

  document.getElementById("reset-view").addEventListener("click", function () {
    map.closePopup();
    map.fitBounds(initialBounds, { padding: [34, 34] });
  });

  var navToggle = document.getElementById("nav-toggle");
  navToggle.addEventListener("click", function () {
    var sidebar = document.getElementById("sidebar");
    var collapsed = sidebar.classList.toggle("nav-collapsed");
    navToggle.textContent = collapsed ? "Show list" : "Hide list";
    navToggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
    window.setTimeout(function () { map.invalidateSize(); }, 180);
  });

  window.addEventListener("resize", function () { map.invalidateSize(); });
})();
</script>
</body>
</html>
"""


def render_html(
    *,
    grower_slug: str,
    grower_name: str,
    farms: list[dict[str, Any]],
    fields: dict[str, Any],
    totals: dict[str, Any],
    bbox: list[float],
    title: str,
    labels_default: bool,
) -> str:
    payload = {
        "grower": {"slug": grower_slug, "name": grower_name},
        "farms": farms,
        "fields": fields,
        "totals": totals,
        "bbox": [round(value, 6) for value in bbox],
        "meta": {
            "generator": GENERATOR_NAME,
            "generator_version": GENERATOR_VERSION,
            "generated_at": iso_now(),
            "labels_default": labels_default,
            "basemaps": {
                "satellite": {
                    "name": "Esri World Imagery",
                    "url": ESRI_IMAGERY_URL,
                    "attribution": "Tiles (c) Esri - Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community",
                },
                "street": {
                    "name": "OpenStreetMap",
                    "url": OSM_URL,
                    "attribution": "OpenStreetMap contributors",
                },
            },
        },
    }
    payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    payload_json = payload_json.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")

    template = html_template()
    return (
        template.replace("__TITLE__", escape(title))
        .replace("__GENERATED_AT__", iso_now())
        .replace("__GENERATOR_NAME__", GENERATOR_NAME)
        .replace("__GENERATOR_VERSION__", GENERATOR_VERSION)
        .replace("__ESRI_URL__", json.dumps(ESRI_IMAGERY_URL))
        .replace("__ESRI_ATTR__", json.dumps("Tiles (c) Esri - Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community"))
        .replace("__OSM_URL__", json.dumps(OSM_URL))
        .replace("__OSM_ATTR__", json.dumps("OpenStreetMap contributors"))
        .replace("__PAYLOAD__", payload_json)
    )


def discover_growers(growers_root: Path) -> list[str]:
    if not growers_root.exists():
        raise SystemExit(f"growers directory not found: {growers_root}")
    growers: list[str] = []
    for path in sorted(growers_root.iterdir()):
        if not path.is_dir():
            continue
        if any(path.glob("farms/*/boundary/field_boundaries.geojson")):
            growers.append(path.name)
    return growers


def output_paths(grower_dir: Path, output_dir: Path | None, grower_slug: str, multiple: bool) -> tuple[Path, Path]:
    directory = output_dir if output_dir else grower_dir / "web-map"
    if output_dir and multiple:
        directory = output_dir / grower_slug
    return directory / f"{grower_slug}_field_map.html", directory / "web_map_manifest.json"


def generate_grower(args: argparse.Namespace, grower_slug: str, *, multiple: bool) -> dict[str, Any]:
    growers_root = args.data_root / "data-pipeline" / "growers"
    grower_dir = growers_root / grower_slug
    if not grower_dir.exists():
        raise SystemExit(f"grower directory not found: {grower_dir}")

    warnings: list[str] = []
    fixes: list[dict[str, Any]] = []
    farms, fields, bbox, totals = load_farms(
        grower_dir,
        farm_slugs=set(args.farm_slug) if args.farm_slug else None,
        fix_farm_state=args.fix_farm_state,
        warnings=warnings,
        fixes=fixes,
    )

    grower_name = args.grower_name or prettify_slug(grower_slug)
    title = args.title or f"{grower_name} Field Map | My Farm Advisor"
    labels_default = not args.no_labels

    html_text = render_html(
        grower_slug=grower_slug,
        grower_name=grower_name,
        farms=farms,
        fields=fields,
        totals=totals,
        bbox=bbox,
        title=title,
        labels_default=labels_default,
    )

    html_path, manifest_path = output_paths(
        grower_dir, args.output_dir, grower_slug, multiple
    )
    html_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = html_path.with_name(html_path.name + ".tmp")
    tmp.write_text(html_text, encoding="utf-8")
    tmp.replace(html_path)

    manifest = {
        "status": "ok",
        "generator": GENERATOR_NAME,
        "generator_version": GENERATOR_VERSION,
        "generated_at": iso_now(),
        "data_root": str(args.data_root),
        "grower": {"slug": grower_slug, "name": grower_name},
        "outputs": {
            "html": str(html_path),
            "html_relative": str(html_path.relative_to(args.data_root)),
            "html_bytes": html_path.stat().st_size,
            "html_sha256": sha256_file(html_path),
            "embedded_fields_sha256": sha256_text(
                json.dumps(fields, separators=(",", ":"), sort_keys=True)
            ),
        },
        "farms": [
            {
                "farm_slug": farm["slug"],
                "farm_name": farm["name"],
                "state": farm["state"],
                "field_count": farm["field_count"],
                "acres": farm["acres"],
                "source_boundary": farm["source_boundary"],
                "source_sha256": farm["source_sha256"],
            }
            for farm in farms
        ],
        "totals": totals,
        "bbox_wgs84": [round(value, 6) for value in bbox],
        "basemap": {
            "default": "esri-world-imagery",
            "url": ESRI_IMAGERY_URL,
            "street_alternate": OSM_URL,
        },
        "labels_default": labels_default,
        "metadata_fixes": fixes,
        "warnings": warnings,
    }
    write_json_atomic(manifest_path, manifest)
    return manifest


def verify_grower(args: argparse.Namespace, grower_slug: str, *, multiple: bool) -> dict[str, Any]:
    growers_root = args.data_root / "data-pipeline" / "growers"
    grower_dir = growers_root / grower_slug
    html_path, manifest_path = output_paths(grower_dir, args.output_dir, grower_slug, multiple)
    problems: list[str] = []

    if not html_path.exists():
        problems.append(f"missing HTML: {html_path}")
    if not manifest_path.exists():
        problems.append(f"missing manifest: {manifest_path}")
    if problems:
        return {"grower": grower_slug, "status": "failed", "problems": problems}

    manifest = read_json(manifest_path)
    html_text = html_path.read_text(encoding="utf-8")
    size = html_path.stat().st_size
    if size > args.max_html_bytes:
        problems.append(f"HTML exceeds {args.max_html_bytes} bytes: {size}")
    if size < 2_000:
        problems.append(f"HTML suspiciously small: {size} bytes")
    if "data:image" in html_text:
        problems.append("HTML contains embedded data:image raster payload")
    if ".tif" in html_text.lower():
        problems.append("HTML references a GeoTIFF")
    if ESRI_IMAGERY_URL.split("/tile")[0] not in html_text:
        problems.append("Esri World Imagery URL missing")
    if "field-label" not in html_text:
        problems.append("field label class missing")
    if "field-nav" not in html_text:
        problems.append("field navigation container missing")
    if "popupHtml" not in html_text:
        problems.append("popup renderer missing")

    match = re.search(
        r'<script id="map-data" type="application/json">(.*?)</script>',
        html_text,
        re.S,
    )
    if not match:
        problems.append("embedded map-data payload missing")
        return {"grower": grower_slug, "status": "failed", "problems": problems}

    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        problems.append(f"embedded JSON is invalid: {exc}")
        return {"grower": grower_slug, "status": "failed", "problems": problems}

    embedded_features = payload.get("fields", {}).get("features", [])
    expected_ids: list[str] = []
    for boundary_path in sorted(grower_dir.glob("farms/*/boundary/field_boundaries.geojson")):
        try:
            boundary = json.loads(boundary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            problems.append(f"unreadable boundary: {boundary_path}")
            continue
        for feature in boundary.get("features", []):
            field_id = str((feature.get("properties") or {}).get("field_id", "")).strip()
            if field_id:
                expected_ids.append(field_id)

    embedded_ids = [str(feature.get("properties", {}).get("field_id", "")) for feature in embedded_features]
    if sorted(embedded_ids) != sorted(expected_ids):
        missing = sorted(set(expected_ids) - set(embedded_ids))
        extra = sorted(set(embedded_ids) - set(expected_ids))
        problems.append(f"embedded fields do not match boundaries (missing={missing}, extra={extra})")

    if len(embedded_ids) != len(set(embedded_ids)):
        problems.append("embedded field IDs are not unique")

    if manifest.get("outputs", {}).get("html_sha256") != sha256_file(html_path):
        problems.append("manifest HTML sha256 does not match the current file")

    if payload.get("totals", {}).get("fields") != len(embedded_features):
        problems.append("payload totals.fields does not match embedded feature count")

    return {
        "grower": grower_slug,
        "status": "ok" if not problems else "failed",
        "problems": problems,
        "html": str(html_path),
        "html_bytes": size,
        "fields": len(embedded_features),
        "farms": len(payload.get("farms", [])),
        "sha256": sha256_file(html_path),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--grower-slug", action="append", default=[], help="Grower slug; repeatable")
    parser.add_argument("--all-growers", action="store_true", help="Render every grower with farms")
    parser.add_argument("--farm-slug", action="append", default=[], help="Limit to specific farms")
    parser.add_argument(
        "--data-root",
        default=os.environ.get("DATA_PIPELINE_DATA_ROOT", ""),
        help="Runtime data root. Defaults to DATA_PIPELINE_DATA_ROOT.",
    )
    parser.add_argument("--output-dir", default=None, help="Optional output directory override")
    parser.add_argument("--grower-name", default=None, help="Display name override")
    parser.add_argument("--title", default=None, help="HTML title override")
    parser.add_argument("--basemap", choices=("satellite", "street"), default="satellite")
    parser.add_argument("--no-labels", action="store_true", help="Start with field labels hidden")
    parser.add_argument(
        "--fix-farm-state",
        action="store_true",
        help="Correct farm.json state from boundary evidence and record the fix",
    )
    parser.add_argument("--verify", action="store_true", help="Verify existing outputs instead of rendering")
    parser.add_argument("--max-html-bytes", type=int, default=DEFAULT_MAX_HTML_BYTES)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.data_root:
        raise SystemExit("DATA_PIPELINE_DATA_ROOT or --data-root is required")
    args.data_root = Path(args.data_root).expanduser().resolve(strict=False)
    if not args.data_root.is_absolute():
        raise SystemExit("data root must be absolute")
    if args.output_dir:
        args.output_dir = Path(args.output_dir).expanduser().resolve(strict=False)

    growers_root = args.data_root / "data-pipeline" / "growers"
    if args.all_growers:
        grower_slugs = discover_growers(growers_root)
    else:
        grower_slugs = [slug for slug in args.grower_slug if slug]
    if not grower_slugs:
        raise SystemExit("pass --grower-slug or --all-growers")

    multiple = len(grower_slugs) > 1
    results: list[dict[str, Any]] = []
    failed = False
    for grower_slug in grower_slugs:
        if args.verify:
            result = verify_grower(args, grower_slug, multiple=multiple)
        else:
            result = generate_grower(args, grower_slug, multiple=multiple)
        results.append(result)
        if result.get("status") != "ok":
            failed = True

    print(json.dumps({"generator": GENERATOR_NAME, "version": GENERATOR_VERSION, "results": results}, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
