# Grower Web Map

A reusable subskill of `my-farm-advisor/data-pipeline` that renders one lightweight, self-contained interactive HTML map per grower from the real runtime field boundaries.

## What it produces

- One HTML file per grower: `<grower>_field_map.html`
- One manifest per grower: `web_map_manifest.json`
- Output location: `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower>/web-map/`

The HTML embeds only the field polygons and small attributes (field id, farm name, area, crop, county, state, CSB id). The Esri World Imagery basemap is loaded as XYZ tiles by the browser at view time, so the HTML stays small and does not contain rasters, NDVI GeoTIFFs, or raw data tables.

## Design

- Basemap: Esri World Imagery, with OpenStreetMap as an alternate.
- Controls: zoom, scale bar, basemap switcher, field-label toggle, reset view.
- Field labels: permanent `field_id` tooltips, on by default.
- Popups: grower, farm, field, area (acres and hectares), crop, county/state, CSB id.
- Sidebar: grower title, farm subtitle, stat chips (farms, fields, acres), searchable field navigation grouped by farm, provenance footer.
- Clicking a sidebar entry flies to the field and opens its popup.
- Responsive layout for tablets and phones.

## Installation into an existing runtime

No pipeline source refresh is required. The generator uses geopandas, shapely, and pyproj from the existing runtime virtualenv.

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
my-farm-advisor/data-pipeline/grower-web-map/scripts/install.sh --all-growers
```

`install.sh` stages `src/grower_web_map.py` into `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/tools/grower-web-map/` with an install manifest, then runs generation. The `tools/` directory is a sibling of `src/`, so the installer's source-drift checks remain unaffected.

## Direct usage

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  my-farm-advisor/data-pipeline/grower-web-map/src/grower_web_map.py \
  --grower-slug il-dekalb-grower \
  --data-root "$DATA_PIPELINE_DATA_ROOT" \
  --fix-farm-state
```

Useful options:

- `--grower-slug` (repeatable) or `--all-growers`
- `--farm-slug` to limit to specific farms
- `--output-dir` to override the output location
- `--grower-name`, `--title` for branding
- `--basemap street` to default to OpenStreetMap
- `--no-labels` to start with labels hidden
- `--fix-farm-state` to correct `farm.json` state from boundary evidence
- `--verify` to validate existing outputs instead of regenerating
- `--max-html-bytes` to change the size cap (default 1,000,000)

## Browser testing

After generation, open the HTML files directly in a browser:

- Illinois: `file:///home/coder/my-farm-advisor-runtime/data-pipeline/growers/il-dekalb-grower/web-map/il-dekalb-grower_field_map.html`
- Iowa: `file:///home/coder/my-farm-advisor-runtime/data-pipeline/growers/ia-story-grower/web-map/ia-story-grower_field_map.html`

Check the following:

1. The basemap loads as Esri World Imagery and can be switched to OpenStreetMap.
2. All five field polygons appear in the correct county locations.
3. Field labels (`il-r1` ... `il-r5`, `ia-r1` ... `ia-r5`) are visible and can be toggled.
4. Clicking a polygon opens a popup with grower, farm, field, area, crop, county/state, and CSB id.
5. The sidebar lists all fields grouped by farm and clicking one flies to the field and opens its popup.
6. The search box filters the list and dims non-matching polygons.
7. Reset view returns to the full grower extent.
8. The layout works on a narrow window.

## Validation

Repository validation:

```bash
cd /home/coder/workspace/my-farm-advisor-skills-assignment-1
./scripts/validate.sh
```

Unit tests:

```bash
cd /home/coder/workspace/my-farm-advisor-skills-assignment-1
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" -m unittest discover \
  -s my-farm-advisor/data-pipeline/grower-web-map/tests -v
```

Output verification:

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/tools/grower-web-map/grower_web_map.py" \
  --grower-slug il-dekalb-grower --data-root "$DATA_PIPELINE_DATA_ROOT" --verify
```

`--verify` checks file existence, size cap, embedded field id/count match against boundaries, manifest hash, and absence of embedded raster markers.

## Reuse for other growers

The skill is not hardcoded to Illinois or Iowa. Any grower with farms under `growers/<grower>/farms/<farm>/boundary/field_boundaries.geojson` can be rendered with `--grower-slug` or `--all-growers`. Farm display names come from `farm.json` with slug fallback, and grower display names come from `--grower-name` or a prettified slug.

## Known runtime issue: Illinois farm metadata

`ensure_canonical_data_tree` writes `farm.json` with a hardcoded `IA` state. The DeKalb farm boundaries carry `state = IL` and `state_fips = 17`, so the generator uses boundary evidence for all state-dependent behavior. The generator can optionally correct the runtime metadata with `--fix-farm-state`, recording the old value, new value, and evidence in `manifests/farm_metadata_state_fix.json`. This does not rerun the pipeline.

## Non-goals

- No embedded satellite rasters, NDVI GeoTIFFs, or raw data tables.
- No changes to pipeline source, shared data, or farm reports.
- No new dependencies; uses the existing runtime virtualenv. Leaflet is loaded from CDN at view time.
