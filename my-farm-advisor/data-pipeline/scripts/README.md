# Data-pipeline operational scripts

This directory holds installer and helper scripts that are run from the skill
checkout. They are **not** copied into the runtime source tree by
`install.sh`; runtime execution uses the copy under
`${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src/scripts`.

## `prepare_csb_farm.py`

Prepare one US farm with real USDA CSB field boundaries. See the script's
`--help` for the full usage. It writes only runtime data under
`DATA_PIPELINE_DATA_ROOT` and never modifies the skill checkout.

### Assignment 1 fixed targets

| Grower | Farm | County | FIPS | Field prefix | Fields |
|---|---|---|---|---|---|
| `il-dekalb-grower` | `dekalb-north-farm` | DeKalb County, IL | 17037 | `il-r` | 5 |
| `ia-story-grower` | `story-east-farm` | Story County, IA | 19169 | `ia-r` | 5 |

### Boundary source and provenance

- Source: `https://data.source.coop/fiboa/us-usda-cropland/us_usda_cropland.parquet`
- License: CC-BY-4.0 (FIBOA); USDA public (CSB).
- Selection: candidates with `area_ha_est >= 40` in the county window, sorted
  by area descending then `field_id`, keeping the first five that intersect the
  county polygon.
- Original CSB `field_id` and `csb_id` values are preserved and recorded in
  `manifests/csb_farm_prep.json` together with the county bbox, candidate
  count, dropped candidates, polygon geometry sha256 values, and the shared
  CSB provenance record.
- `state_fips` and `county_fips` are added because the CDL routing step reads
  `state_fips` from the boundary.
- Shared county data must exist first; run `scripts/init_shared_data.py` before
  this helper.
