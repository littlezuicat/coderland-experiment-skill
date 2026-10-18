import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

script_path = Path(__file__).resolve().parents[1] / "coderland-experiment" / "scripts" / "coderland.py"
spec = importlib.util.spec_from_file_location("coderland_skill", script_path)
coderland = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coderland)


class ProfileLayoutTests(unittest.TestCase):
    def test_no_reference_archive_keeps_input_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / "no-reference.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("input/image.png", b"synthetic image")

            entries = coderland._collect_entries(archive_path)
            profile = {"id": "no-reference-restoration", "_limits": {}}
            summary, _, _ = coderland.validate_entries(entries, profile["id"], profile)

            self.assertEqual(entries[0]["name"], "input/image.png")
            self.assertEqual(summary["image_count"], 1)

    def test_no_reference_archive_strips_only_an_outer_wrapper(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / "wrapped.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("dataset/input/image.png", b"synthetic image")

            entries = coderland._collect_entries(archive_path)
            profile = {"id": "no-reference-restoration", "_limits": {}}
            summary, _, _ = coderland.validate_entries(entries, profile["id"], profile)

            self.assertEqual(entries[0]["name"], "input/image.png")
            self.assertEqual(summary["image_count"], 1)


class ApiPathTests(unittest.TestCase):
    def test_rejects_literal_and_encoded_dot_segments(self):
        for path in (
            "/api/v1/experiments/../users",
            "/api/v1/experiments/%2e%2e/users",
            "/api/v1/experiments/items/%2E%2E/users",
        ):
            with self.subTest(path=path), self.assertRaises(coderland.CoderLandError):
                coderland.api_path(path)

    def test_rejects_backslash_path_separators(self):
        with self.assertRaises(coderland.CoderLandError):
            coderland.api_path("/api/v1/experiments/items\\..\\users")

    def test_maps_legacy_experiment_route(self):
        self.assertEqual(
            coderland.api_path("/api/experiments/datasets?limit=5"),
            "/api/v1/experiments/datasets?limit=5",
        )


class RequestJsonTests(unittest.TestCase):
    def test_raw_bytes_are_forwarded_to_request(self):
        raw_body = b"synthetic-grant\\nsynthetic-chunk"
        with patch.object(coderland, "_request", return_value=(b'{\"received\": 31}', {})) as request:
            result = coderland._request_json(
                "POST", "/api/v1/experiments/datasets/upload-chunk", raw=raw_body,
            )

        self.assertEqual(result, {"received": 31})
        request.assert_called_once_with(
            "POST", "/api/v1/experiments/datasets/upload-chunk",
            base=None, api_key=None, body=None, raw=raw_body, headers=None, timeout=120,
        )


if __name__ == "__main__":
    unittest.main()
