import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "my-farm-advisor" / "data-pipeline" / "grower-web-map" / "src" / "grower_web_map.py"
PYTHON = os.environ.get(
    "DATA_PIPELINE_VENV_PYTHON",
    os.path.expanduser("~/my-farm-advisor-runtime/data-pipeline/.venv/bin/python"),
)


def _make_runtime(tmp: Path) -> Path:
    root = tmp / "runtime"
    data_root = root / "data-pipeline"
    for grower, farm, state, coords in (
        ("test-grower", "alpha-farm", "IL", [(-88.8, 41.8), (-88.7, 41.8), (-88.7, 41.9), (-88.8, 41.9)]),
        ("test-grower", "beta-farm", "IA", [(-93.5, 41.9), (-93.4, 41.9), (-93.4, 42.0), (-93.5, 42.0)]),
    ):
        farm_dir = data_root / "growers" / grower / "farms" / farm
        boundary_dir = farm_dir / "boundary"
        boundary_dir.mkdir(parents=True, exist_ok=True)
        (farm_dir / "farm.json").write_text(
            json.dumps(
                {
                    "grower_slug": grower,
                    "farm_slug": farm,
                    "display_name": farm.replace("-", " ").title(),
                    "state": "IA",
                    "country": "US",
                }
            ),
            encoding="utf-8",
        )
        coords = coords + [coords[0]]
        feature = {
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [[ [lon, lat] for lon, lat in coords ]]},
            "properties": {
                "field_id": f"{farm[:2]}-r1",
                "state": state,
                "state_fips": "17" if state == "IL" else "19",
                "county_fips": "037" if state == "IL" else "169",
                "county_name": "DeKalb" if state == "IL" else "Story",
                "area_ha_est": 50.0,
                "crop": "Corn" if state == "IL" else "Soybeans",
                "csb_id": f"csb-{farm}",
            },
        }
        (boundary_dir / "field_boundaries.geojson").write_text(
            json.dumps({"type": "FeatureCollection", "features": [feature]}),
            encoding="utf-8",
        )
    return root


class GrowerWebMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = _make_runtime(Path(self.tmp.name))
        self.env = os.environ.copy()
        self.env["DATA_PIPELINE_DATA_ROOT"] = str(self.runtime)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PYTHON, str(SCRIPT), "--data-root", str(self.runtime), *args],
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
        )

    def test_generation_and_verification(self) -> None:
        result = self._run("--grower-slug", "test-grower", "--fix-farm-state")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["results"][0]["status"], "ok")
        html = self.runtime / "data-pipeline/growers/test-grower/web-map/test-grower_field_map.html"
        manifest_path = self.runtime / "data-pipeline/growers/test-grower/web-map/web_map_manifest.json"
        self.assertTrue(html.exists())
        self.assertTrue(manifest_path.exists())
        text = html.read_text(encoding="utf-8")
        self.assertLess(len(text.encode("utf-8")), 300_000)
        self.assertNotIn("data:image", text)
        self.assertNotIn(".tif", text.lower())
        self.assertIn("server.arcgisonline.com", text)
        self.assertIn("field-nav", text)
        self.assertIn("field-label", text)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["totals"]["farms"], 2)
        self.assertEqual(manifest["totals"]["fields"], 2)
        self.assertEqual(len(manifest["metadata_fixes"]), 1)
        # farm.json state corrected from boundary evidence
        alpha = json.loads(
            (self.runtime / "data-pipeline/growers/test-grower/farms/alpha-farm/farm.json").read_text()
        )
        self.assertEqual(alpha["state"], "IL")
        self.assertTrue(
            (self.runtime / "data-pipeline/growers/test-grower/farms/alpha-farm/manifests/farm_metadata_state_fix.json").exists()
        )

    def test_verify_mode(self) -> None:
        self.assertEqual(self._run("--grower-slug", "test-grower").returncode, 0)
        verify = self._run("--grower-slug", "test-grower", "--verify")
        self.assertEqual(verify.returncode, 0, verify.stderr)
        payload = json.loads(verify.stdout)
        self.assertEqual(payload["results"][0]["status"], "ok")
        self.assertEqual(payload["results"][0]["fields"], 2)

    def test_all_growers(self) -> None:
        result = self._run("--all-growers")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
