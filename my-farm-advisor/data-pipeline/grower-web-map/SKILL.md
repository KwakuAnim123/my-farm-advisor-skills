---
name: grower-web-map
description: Generate a lightweight, self-contained interactive HTML field map for any grower in the My Farm Advisor runtime. Reads real USDA CSB field polygons and renders them over an Esri World Imagery basemap with field labels, clickable popups, a searchable field navigation list, and farm summary statistics. Use for grower demos, field review, and assignment walkthroughs.
version: 1.0.0
author: Kwaku Anim
metadata:
  skill-version: 1.0.0
  skill-author: Kwaku Anim
---

# Grower Web Map

Use this skill when the user wants an interactive, browser-viewable map of a grower's fields rather than the heavy embedded-raster farm report. It produces one lightweight HTML file per grower from the runtime field boundaries.

## When to use

- The user asks for a map, interactive view, satellite view, field labels, popups, or field navigation for one or more growers.
- The user wants a shareable HTML artifact that can be opened directly in any browser.
- The user wants to demonstrate field boundaries over satellite imagery for a video or presentation.

## When not to use

- The user wants the full farm report with NDVI, soil, weather, and CDL charts; use the data-pipeline reporting scripts instead.
- The user wants to edit, create, or download field boundaries; use the CSB preparation helper first.
- The user wants to start the interactive web-map admin skill; that is a separate skill under `admin/interactive-web-map`.

## Runtime contract

- `DATA_PIPELINE_DATA_ROOT` is required and must be absolute.
- Runtime growers live under `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower>/farms/<farm>/boundary/field_boundaries.geojson`.
- Run the generator with `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python`.
- Generated maps are runtime data and stay outside Git.

## Quick start

Generate maps for every grower in the runtime:

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
my-farm-advisor/data-pipeline/grower-web-map/scripts/install.sh --all-growers
```

Generate one map and verify it:

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/tools/grower-web-map/grower_web_map.py" \
  --grower-slug il-dekalb-grower \
  --data-root "$DATA_PIPELINE_DATA_ROOT" \
  --verify
```

## Outputs

- `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower>/web-map/<grower>_field_map.html`
- `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower>/web-map/web_map_manifest.json`

One HTML file is produced per grower. The HTML contains only field polygons plus small attributes; the Esri World Imagery basemap is loaded by the browser at view time.

## Verification

- `python -m unittest discover -s my-farm-advisor/data-pipeline/grower-web-map/tests`
- `./scripts/validate.sh` from the repository root after documentation changes

## References

- [README.md](README.md) for design, installation, browser testing, and reuse details.
- [AGENTS.md](AGENTS.md) for local edit scope and safety rules.
- [scripts/install.sh](scripts/install.sh) for staging and generation.
- [tests/test_grower_web_map.py](tests/test_grower_web_map.py) for the synthetic fixture test.

## Known runtime issue

`ensure_canonical_data_tree` writes `farm.json` with a hardcoded `IA` state. The generator derives state from field boundaries and can optionally correct `farm.json` with `--fix-farm-state`, recording the change in `manifests/farm_metadata_state_fix.json`.
