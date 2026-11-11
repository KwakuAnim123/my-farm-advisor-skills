# Grower Web Map Local Instructions

## Purpose

This subskill renders one lightweight interactive HTML field map per grower from runtime field boundaries. It is a documentation-and-tooling subskill of `my-farm-advisor/data-pipeline`; it does not run the farm pipeline.

## Safe edit scope

Edit only this directory and its children. Do not change parent `SKILL.md`, sibling workflows, or root policy from this subskill task unless explicitly requested.

## Runtime contract

- `DATA_PIPELINE_DATA_ROOT` is required and must be absolute.
- Read boundaries from `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower>/farms/<farm>/boundary/field_boundaries.geojson`.
- Read farm display names from `farm.json` but derive state from the boundary, because `farm.json` can carry a stale hardcoded state.
- Write only under `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower>/web-map/` and `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/tools/grower-web-map/`.
- Never embed satellite rasters, NDVI GeoTIFFs, or raw data tables in the HTML.
- Keep generated outputs outside Git.

## Testing

```bash
cd /home/coder/workspace/my-farm-advisor-skills-assignment-1
./scripts/validate.sh
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" -m unittest discover \
  -s my-farm-advisor/data-pipeline/grower-web-map/tests -v
```

## Upstream issue

`ensure_canonical_data_tree` writes `farm.json` with a hardcoded `IA` state. Document and handle this through boundary evidence; do not rerun the pipeline to fix metadata.
