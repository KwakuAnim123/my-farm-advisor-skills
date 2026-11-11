#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SUBSKILL_DIR=$(CDPATH='' cd -- "${SCRIPT_DIR}/.." && pwd)
SOURCE_SCRIPT="${SUBSKILL_DIR}/src/grower_web_map.py"

DATA_ROOT="${DATA_PIPELINE_DATA_ROOT:-}"
GROWER_SLUGS=()
ALL_GROWERS=0
VERIFY=0
NO_RUN=0
EXTRA_ARGS=()

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Stage the grower-web-map generator into the runtime and generate maps.

Options:
  --data-root <abs-path>     Runtime data root. Overrides DATA_PIPELINE_DATA_ROOT.
  --grower-slug <slug>      Grower to render. Repeatable.
  --all-growers             Render every grower with field boundaries.
  --verify                  Verify existing outputs instead of regenerating.
  --stage-only              Stage the generator without running it.
  --fix-farm-state          Pass --fix-farm-state to the generator.
  --no-run                  Stage only; do not generate maps.
  -- <args...>              Extra arguments forwarded to the generator.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --data-root)
      DATA_ROOT="$2"
      shift 2
      ;;
    --grower-slug)
      GROWER_SLUGS+=("$2")
      shift 2
      ;;
    --all-growers)
      ALL_GROWERS=1
      shift
      ;;
    --verify)
      VERIFY=1
      shift
      ;;
    --stage-only|--no-run)
      NO_RUN=1
      shift
      ;;
    --fix-farm-state)
      EXTRA_ARGS+=("--fix-farm-state")
      shift
      ;;
    --)
      shift
      EXTRA_ARGS+=("$@")
      break
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "${DATA_ROOT}" ]]; then
  echo "DATA_PIPELINE_DATA_ROOT or --data-root is required" >&2
  exit 2
fi
if [[ "${DATA_ROOT}" != /* ]]; then
  echo "data root must be absolute: ${DATA_ROOT}" >&2
  exit 2
fi

PYTHON="${DATA_ROOT}/data-pipeline/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  echo "runtime virtualenv python not found: ${PYTHON}" >&2
  exit 2
fi
if [[ ! -f "${SOURCE_SCRIPT}" ]]; then
  echo "source script missing: ${SOURCE_SCRIPT}" >&2
  exit 2
fi

TOOLS_DIR="${DATA_ROOT}/data-pipeline/tools/grower-web-map"
mkdir -p "${TOOLS_DIR}"
cp "${SOURCE_SCRIPT}" "${TOOLS_DIR}/grower_web_map.py"
chmod 0644 "${TOOLS_DIR}/grower_web_map.py"

SHA256=$(sha256sum "${TOOLS_DIR}/grower_web_map.py" | awk '{print $1}')
SOURCE_SHA256=$(sha256sum "${SOURCE_SCRIPT}" | awk '{print $1}')
cat > "${TOOLS_DIR}/install_manifest.json" <<EOF
{
  "generator": "grower-web-map",
  "version": "1.0.0",
  "staged_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "source_script": "${SOURCE_SCRIPT}",
  "source_sha256": "${SOURCE_SHA256}",
  "staged_script": "${TOOLS_DIR}/grower_web_map.py",
  "staged_sha256": "${SHA256}",
  "data_root": "${DATA_ROOT}"
}
EOF

echo "Staged generator: ${TOOLS_DIR}/grower_web_map.py"

if [[ "${NO_RUN}" -eq 1 ]]; then
  exit 0
fi

RUN_ARGS=("--data-root" "${DATA_ROOT}")
if [[ "${ALL_GROWERS}" -eq 1 ]]; then
  RUN_ARGS+=("--all-growers")
fi
for slug in "${GROWER_SLUGS[@]}"; do
  RUN_ARGS+=("--grower-slug" "${slug}")
done
if [[ "${VERIFY}" -eq 1 ]]; then
  RUN_ARGS+=("--verify")
fi
RUN_ARGS+=("${EXTRA_ARGS[@]}")

"${PYTHON}" "${TOOLS_DIR}/grower_web_map.py" "${RUN_ARGS[@]}"
