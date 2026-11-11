#!/usr/bin/env python3
"""Prepare one US farm with real USDA CSB field boundaries for Assignment 1.

This is an operational helper layered on top of the committed data-pipeline
source. It does four things, in order:

1. Creates the canonical farm shell with ``run_farm_pipeline.py --structure-test``.
2. Computes the county bounding box from the shared geoadmin L2 county layer and
   calls ``ingest/download_csb_fields.py`` for a surplus of candidate polygons.
3. Validates every candidate against the target county polygon and county name,
   then keeps the largest in-county fields up to ``--max-fields``. Original CSB
   ``field_id`` values and ``csb_id`` provenance columns are preserved.
4. Writes ``state_fips`` / ``county_fips`` (needed by the CDL routing step),
   rewrites the farm boundary, per-field boundaries and inventory, and records
   the full selection decision in ``manifests/csb_farm_prep.json``.

The helper never downloads farm imagery, weather, soil or DEM data; it only
writes boundary metadata under ``DATA_PIPELINE_DATA_ROOT``. Generated files are
runtime data and must stay outside Git.

Usage:
    export DATA_PIPELINE_DATA_ROOT=~/my-farm-advisor-runtime
    "$DATA_PIPELINE_DATA_ROOT/data-pipeline/.venv/bin/python" \
        my-farm-advisor/data-pipeline/scripts/prepare_csb_farm.py \
        --grower-slug il-dekalb-grower --farm-slug dekalb-north-farm \
        --farm-name "DeKalb North Farm" \
        --county-name DeKalb --state IL --state-fips 17 --county-fips 037 \
        --field-prefix il-r --max-fields 5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

# Shared county GeoJSON files are multi-megabyte multipolygons; GDAL's default
# object-size guard rejects them unless disabled.
os.environ.setdefault("OGR_GEOJSON_MAX_OBJ_SIZE", "0")

import geopandas as gpd  # noqa: E402
import pandas as pd  # noqa: E402

CSB_SOURCE_URL = "https://data.source.coop/fiboa/us-usda-cropland/us_usda_cropland.parquet"
CSB_SOURCE_LABEL = "FIBOA us-usda-cropland (USDA NASS Crop Sequence Boundaries)"
CSB_LICENSE = "CC-BY-4.0 (FIBOA); USDA public (CSB)"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_geometry(geometry) -> str:
    return hashlib.sha256(bytes(geometry.wkb)).hexdigest()


def _normalize_county_name(value: str) -> str:
    cleaned = str(value).strip().lower()
    if cleaned.endswith(" county"):
        cleaned = cleaned[: -len(" county")]
    return " ".join(cleaned.split())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--grower-slug", required=True)
    parser.add_argument("--farm-slug", required=True)
    parser.add_argument("--farm-name", required=True)
    parser.add_argument("--county-name", required=True, help="County name, e.g. DeKalb")
    parser.add_argument("--state", required=True, help="Two-letter state code written to boundaries, e.g. IL")
    parser.add_argument("--state-fips", required=True, help="Two-digit state FIPS, e.g. 17")
    parser.add_argument("--county-fips", required=True, help="Three-digit county FIPS, e.g. 037")
    parser.add_argument("--field-prefix", required=True, help="CSB field_id prefix, e.g. il-r")
    parser.add_argument("--max-fields", type=int, default=5)
    parser.add_argument("--candidate-fields", type=int, default=12)
    parser.add_argument("--min-area-ha", type=float, default=40.0)
    parser.add_argument("--crop-year", type=int, default=2024, help="CSB crop year written to boundaries")
    parser.add_argument(
        "--data-root",
        default=None,
        help="Runtime data root. Defaults to DATA_PIPELINE_DATA_ROOT.",
    )
    parser.add_argument(
        "--reuse-existing",
        action="store_true",
        help="Skip the CSB query and finalize the existing farm boundary instead",
    )
    return parser.parse_args()


def _resolve_runtime(data_root: str | None) -> tuple[Path, Path]:
    raw = data_root or os.environ.get("DATA_PIPELINE_DATA_ROOT", "")
    if not raw:
        raise SystemExit("DATA_PIPELINE_DATA_ROOT or --data-root is required")
    root = Path(raw).expanduser().resolve(strict=False)
    if not root.is_absolute():
        raise SystemExit("runtime data root must be absolute")
    runtime_base = root / "data-pipeline"
    runtime_src = runtime_base / "src"
    if not (runtime_src / "scripts" / "run_farm_pipeline.py").exists():
        raise SystemExit(f"runtime source is missing run_farm_pipeline.py: {runtime_src}")
    return runtime_base, runtime_src


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    print("run:", " ".join(command), flush=True)
    subprocess.run(command, cwd=str(cwd), env=env, check=True)


def _load_target_county(runtime_base: Path, state_fips: str, county_fips: str, county_name: str):
    counties_path = runtime_base / "shared" / "geoadmin" / "l2_counties" / "counties_usa.geojson"
    if not counties_path.exists():
        raise SystemExit(
            f"shared county layer is missing: {counties_path}. "
            "Run scripts/init_shared_data.py first."
        )
    counties = gpd.read_file(counties_path)
    state_series = counties["state_fips"].astype(str).str.zfill(2)
    county_series = counties["county_fips"].astype(str).str.zfill(3)
    match = counties[(state_series == state_fips) & (county_series == county_fips)].copy()
    if match.empty:
        raise SystemExit(
            f"no county found for state_fips={state_fips} county_fips={county_fips}"
        )
    row = match.iloc[0]
    actual_name = str(row.get("county_name", ""))
    if _normalize_county_name(actual_name) != _normalize_county_name(county_name):
        raise SystemExit(
            f"county name mismatch: requested {county_name!r}, "
            f"layer has {actual_name!r} for FIPS {state_fips}{county_fips}"
        )
    geometry = row.geometry
    if geometry is None or geometry.is_empty:
        raise SystemExit("target county geometry is empty")
    return geometry, actual_name


def _bbox_from_geometry(geometry) -> tuple[float, float, float, float]:
    minx, miny, maxx, maxy = geometry.bounds
    # CSB expects S W N E.
    return (float(miny), float(minx), float(maxy), float(maxx))


def _prepare_shell(args: argparse.Namespace, runtime_src: Path, env: dict[str, str]) -> None:
    _run(
        [
            sys.executable,
            str(runtime_src / "scripts" / "run_farm_pipeline.py"),
            "--structure-test",
            "--grower-slug",
            args.grower_slug,
            "--farm-slug",
            args.farm_slug,
            "--farm-name",
            args.farm_name,
        ],
        cwd=runtime_src,
        env=env,
    )


def _download_csb(args: argparse.Namespace, runtime_src: Path, env: dict[str, str], bbox) -> None:
    south, west, north, east = bbox
    _run(
        [
            sys.executable,
            str(runtime_src / "scripts" / "ingest" / "download_csb_fields.py"),
            "--bbox",
            f"{south:.8f}",
            f"{west:.8f}",
            f"{north:.8f}",
            f"{east:.8f}",
            "--min-area-ha",
            str(args.min_area_ha),
            "--max-fields",
            str(args.candidate_fields),
            "--field-prefix",
            args.field_prefix,
            "--country",
            "US",
            "--state",
            args.state,
            "--year",
            str(args.crop_year),
        ],
        cwd=runtime_src,
        env=env,
    )


def _finalize(
    args: argparse.Namespace,
    runtime_base: Path,
    county_geometry,
    actual_county_name: str,
    bbox,
    candidate_count: int,
) -> dict:
    state_fips = str(args.state_fips).zfill(2)
    county_fips = str(args.county_fips).zfill(3)
    farm_dir = runtime_base / "growers" / args.grower_slug / "farms" / args.farm_slug
    boundary_path = farm_dir / "boundary" / "field_boundaries.geojson"
    inventory_path = farm_dir / "manifests" / "field-inventory.csv"
    if not boundary_path.exists():
        raise SystemExit(f"farm boundary is missing after CSB ingest: {boundary_path}")

    fields = gpd.read_file(boundary_path)
    if fields.empty:
        raise SystemExit("CSB ingest produced no field polygons")

    records: list[dict] = []
    for row in fields.itertuples(index=False):
        geometry = row.geometry
        if geometry is None or geometry.is_empty:
            continue
        if not geometry.is_valid:
            geometry = geometry.buffer(0)
        if geometry.is_empty:
            continue
        point = geometry.representative_point()
        spatially_in_county = bool(county_geometry.contains(point) or county_geometry.intersects(geometry))
        property_county = _normalize_county_name(str(getattr(row, "county", "")))
        property_matches = property_county == _normalize_county_name(actual_county_name)
        records.append(
            {
                "field_id": str(getattr(row, "field_id")),
                "csb_id": str(getattr(row, "csb_id", "")),
                "area_ha_est": float(getattr(row, "area_ha_est", 0.0) or 0.0),
                "crop": str(getattr(row, "crop", "")),
                "county": str(getattr(row, "county", "")),
                "spatially_in_county": spatially_in_county,
                "property_matches_county": property_matches,
                "geometry": geometry,
            }
        )

    if not records:
        raise SystemExit("no valid CSB polygons remained after geometry validation")

    in_county = [r for r in records if r["spatially_in_county"]]
    if not in_county:
        raise SystemExit("no CSB polygons intersect the target county polygon")
    in_county.sort(key=lambda r: (-r["area_ha_est"], r["field_id"]))
    kept = in_county[: args.max_fields]
    if len(kept) < 3:
        raise SystemExit(
            f"only {len(kept)} in-county fields available; at least 3 are required"
        )
    kept_ids = {r["field_id"] for r in kept}

    kept_frame = gpd.GeoDataFrame(
        [
            {
                "field_id": r["field_id"],
                "country": "US",
                "state": args.state,
                "state_fips": state_fips,
                "county_fips": county_fips,
                "county_name": actual_county_name,
                "year": str(args.crop_year),
                "area_ha_est": round(r["area_ha_est"], 1),
                "crop": r["crop"],
                "area_acres": round(r["area_ha_est"] * 2.47105, 1),
                "source": "usda_csb_fiboa",
                "csb_id": r["csb_id"],
                "county": r["county"],
                "geometry": r["geometry"],
            }
            for r in sorted(kept, key=lambda item: item["field_id"])
        ],
        geometry="geometry",
        crs=fields.crs,
    )

    boundary_path.parent.mkdir(parents=True, exist_ok=True)
    kept_frame.to_file(boundary_path, driver="GeoJSON")

    inventory_rows = [{"field_id": r["field_id"], "field_slug": r["field_id"]} for r in kept]
    pd.DataFrame(inventory_rows, columns=["field_id", "field_slug"]).sort_values("field_id").to_csv(
        inventory_path, index=False
    )

    fields_root = farm_dir / "fields"
    for candidate in records:
        field_dir = fields_root / candidate["field_id"]
        if candidate["field_id"] in kept_ids:
            single = gpd.GeoDataFrame(
                kept_frame[kept_frame["field_id"] == candidate["field_id"]].copy(),
                geometry="geometry",
                crs=kept_frame.crs,
            )
            target = field_dir / "boundary" / "field_boundary.geojson"
            target.parent.mkdir(parents=True, exist_ok=True)
            single.to_file(target, driver="GeoJSON")
        elif field_dir.exists():
            shutil.rmtree(field_dir)

    farm_manifest_path = farm_dir / "farm.json"
    if farm_manifest_path.exists():
        payload = json.loads(farm_manifest_path.read_text(encoding="utf-8"))
        payload["state"] = args.state
        payload["country"] = "US"
        payload["display_name"] = args.farm_name
        farm_manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    csb_provenance_path = farm_dir / "manifests" / "csb_fields_provenance.json"
    csb_provenance: dict = {}
    if csb_provenance_path.exists():
        try:
            csb_provenance = json.loads(csb_provenance_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            csb_provenance = {}

    dropped = [r for r in records if r["field_id"] not in kept_ids]
    summary = {
        "status": "ok",
        "generated_at": _iso_now(),
        "grower_slug": args.grower_slug,
        "farm_slug": args.farm_slug,
        "farm_name": args.farm_name,
        "county": {
            "name": actual_county_name,
            "state": args.state,
            "state_fips": state_fips,
            "county_fips": county_fips,
            "fips": f"{state_fips}{county_fips}",
            "bbox_s_w_n_e": [round(value, 8) for value in bbox],
        },
        "source": {
            "label": CSB_SOURCE_LABEL,
            "url": CSB_SOURCE_URL,
            "license": CSB_LICENSE,
            "csb_provenance_file": "manifests/csb_fields_provenance.json",
            "csb_row_groups_read": csb_provenance.get("row_groups_read"),
            "csb_total_row_groups": csb_provenance.get("total_row_groups"),
            "csb_accessed_at": csb_provenance.get("accessed_at"),
        },
        "selection": {
            "candidate_fields_requested": args.candidate_fields,
            "candidate_fields_written": candidate_count,
            "min_area_ha": args.min_area_ha,
            "max_fields": args.max_fields,
            "rank_rule": "area_ha_est descending, then field_id ascending",
            "county_rule": "representative point inside target county polygon (county name cross-check recorded)",
        },
        "kept_fields": [
            {
                "field_id": r["field_id"],
                "csb_id": r["csb_id"],
                "area_ha_est": round(r["area_ha_est"], 1),
                "crop": r["crop"],
                "county": r["county"],
                "spatially_in_county": r["spatially_in_county"],
                "property_matches_county": r["property_matches_county"],
                "geometry_sha256": _sha256_geometry(r["geometry"]),
            }
            for r in sorted(kept, key=lambda item: item["field_id"])
        ],
        "dropped_fields": [
            {
                "field_id": r["field_id"],
                "csb_id": r["csb_id"],
                "area_ha_est": round(r["area_ha_est"], 1),
                "county": r["county"],
                "reason": (
                    "outside_target_county"
                    if not r["spatially_in_county"]
                    else "not_in_top_ranked_selection"
                ),
            }
            for r in sorted(dropped, key=lambda item: item["field_id"])
        ],
        "outputs": {
            "farm_boundary": str(boundary_path.relative_to(runtime_base)),
            "farm_boundary_sha256": _sha256_file(boundary_path),
            "inventory": str(inventory_path.relative_to(runtime_base)),
            "inventory_sha256": _sha256_file(inventory_path),
        },
    }
    summary_path = farm_dir / "manifests" / "csb_farm_prep.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    args = parse_args()
    state_fips = str(args.state_fips).zfill(2)
    county_fips = str(args.county_fips).zfill(3)
    if len(state_fips) != 2 or not state_fips.isdigit():
        raise SystemExit("--state-fips must be a two-digit code")
    if len(county_fips) != 3 or not county_fips.isdigit():
        raise SystemExit("--county-fips must be a three-digit code")
    if len(args.state) != 2 or not args.state.isalpha():
        raise SystemExit("--state must be a two-letter code")

    runtime_base, runtime_src = _resolve_runtime(args.data_root)
    env = os.environ.copy()
    env["DATA_PIPELINE_DATA_ROOT"] = str(runtime_base.parent)
    env["AG_GROWER_SLUG"] = args.grower_slug
    env["AG_FARM_SLUG"] = args.farm_slug
    env["AG_FARM_NAME"] = args.farm_name

    county_geometry, actual_county_name = _load_target_county(
        runtime_base, state_fips, county_fips, args.county_name
    )
    bbox = _bbox_from_geometry(county_geometry)
    print(f"target county: {actual_county_name} FIPS {state_fips}{county_fips}")
    print(f"county bbox (S W N E): {bbox}")

    _prepare_shell(args, runtime_src, env)
    if not args.reuse_existing:
        _download_csb(args, runtime_src, env, bbox)

    farm_dir = runtime_base / "growers" / args.grower_slug / "farms" / args.farm_slug
    boundary_path = farm_dir / "boundary" / "field_boundaries.geojson"
    candidate_count = 0
    if boundary_path.exists():
        candidate_count = len(gpd.read_file(boundary_path))

    summary = _finalize(
        args,
        runtime_base,
        county_geometry,
        actual_county_name,
        bbox,
        candidate_count,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
