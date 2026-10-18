import io
import importlib.util
import json
import os
import stat
import tempfile
import unittest
import warnings
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

script_path = Path(__file__).resolve().parents[1] / "coderland-experiment" / "scripts" / "coderland.py"
spec = importlib.util.spec_from_file_location("coderland_skill", script_path)
coderland = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coderland)


KEYPOINT_NAMES = [
    "nose", "left_eye", "right_eye", "left_ear", "right_ear", "left_shoulder", "right_shoulder",
    "left_elbow", "right_elbow", "left_wrist", "right_wrist", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle",
]


def _sample_archive(path, profile_id):
    image_bytes = b"\x89PNG\r\n\x1a\nsynthetic image bytes\x00" + profile_id.encode("ascii")
    gt_bytes = b"\x89PNG\r\n\x1a\nsynthetic ground-truth bytes\x00"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        if profile_id in {"paired-restoration", "segmentation"}:
            archive.writestr("input/image.png", image_bytes)
            archive.writestr("gt/image.png", gt_bytes)
        elif profile_id == "no-reference-restoration":
            archive.writestr("input/image.png", image_bytes)
        elif profile_id == "coco-detection":
            archive.writestr("input/image.png", image_bytes)
            archive.writestr("annotations/instances.json", json.dumps({
                "images": [{"id": 1, "file_name": "image.png", "width": 1, "height": 1}],
                "categories": [{"id": 1, "name": "object"}],
                "annotations": [{"id": 1, "image_id": 1, "category_id": 1,
                                 "bbox": [0, 0, 1, 1], "area": 1}],
            }))
        elif profile_id == "coco-keypoints":
            archive.writestr("input/image.png", image_bytes)
            archive.writestr("annotations/person_keypoints.json", json.dumps({
                "images": [{"id": 1, "file_name": "image.png", "width": 1, "height": 1}],
                "categories": [{"id": 1, "name": "person", "keypoints": KEYPOINT_NAMES}],
                "annotations": [{"id": 1, "image_id": 1, "category_id": 1,
                                 "bbox": [0, 0, 1, 1], "area": 1, "num_keypoints": 0,
                                 "keypoints": [0, 0, 0] * 17}],
            }))
        else:
            raise AssertionError(f"unknown fixture profile: {profile_id}")
    return image_bytes, gt_bytes


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


class CredentialCommandTests(unittest.TestCase):
    def test_configure_uses_temporary_credential_path_and_hides_key(self):
        api_key = "cl_exp_" + "A" * 32
        with tempfile.TemporaryDirectory() as temp_dir:
            credential_file = Path(temp_dir) / "credentials.json"
            output = io.StringIO()
            with patch.dict(os.environ, {"CODERLAND_CREDENTIALS_FILE": str(credential_file)}), \
                    patch.object(coderland.getpass, "getpass", return_value=api_key) as getpass_call, \
                    redirect_stdout(output):
                coderland.command_configure(None)

            self.assertTrue(credential_file.is_file())
            self.assertEqual(json.loads(credential_file.read_text(encoding="utf-8"))["api_key"], api_key)
            self.assertNotIn(api_key, output.getvalue())
            getpass_call.assert_called_once()
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(credential_file.stat().st_mode), 0o600)

    def test_forget_removes_only_the_configured_local_credential(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credential_file = Path(temp_dir) / "credentials.json"
            credential_file.write_text('{"api_key":"synthetic"}', encoding="utf-8")
            output = io.StringIO()
            with patch.dict(os.environ, {"CODERLAND_CREDENTIALS_FILE": str(credential_file)}), \
                    redirect_stdout(output):
                coderland.command_forget(None)
                coderland.command_forget(None)

            self.assertFalse(credential_file.exists())
            self.assertIn("本机 API Key 凭据已清除", output.getvalue())
            self.assertIn("本机没有已保存的 API Key", output.getvalue())


class ProfileRoundTripTests(unittest.TestCase):
    PROFILES = (
        "paired-restoration", "segmentation", "no-reference-restoration", "coco-detection", "coco-keypoints",
    )

    def test_inspect_and_package_all_five_profiles_without_changing_image_bytes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile_limits = {"test_archive_unpacked_bytes": 4 * 1024 * 1024, "image_count": 20}
            for profile_id in self.PROFILES:
                with self.subTest(profile=profile_id):
                    source = root / f"{profile_id}.zip"
                    image_bytes, gt_bytes = _sample_archive(source, profile_id)
                    profile = {"id": profile_id, "_limits": profile_limits}
                    inspect_output = io.StringIO()
                    with patch.object(coderland, "_profile", return_value=profile), \
                            redirect_stdout(inspect_output):
                        coderland.command_inspect(SimpleNamespace(profile=profile_id, source=str(source)))
                    self.assertTrue(json.loads(inspect_output.getvalue())["valid"])

                    package_output = io.StringIO()
                    with patch.object(coderland, "_profile", return_value=profile), \
                            patch.object(Path, "cwd", return_value=root), redirect_stdout(package_output):
                        coderland.command_package(SimpleNamespace(profile=profile_id, source=str(source)))
                    package = Path(json.loads(package_output.getvalue())["output"])
                    with zipfile.ZipFile(package) as archive:
                        self.assertEqual(archive.read("input/image.png"), image_bytes)
                        if profile_id in {"paired-restoration", "segmentation"}:
                            self.assertEqual(archive.read("gt/image.png"), gt_bytes)


class ZipSafetyTests(unittest.TestCase):
    def test_rejects_traversal_absolute_and_backslash_paths(self):
        unsafe_names = ("../outside.png", "/absolute.png", "folder\\..\\outside.png")
        with tempfile.TemporaryDirectory() as temp_dir:
            for index, name in enumerate(unsafe_names):
                archive_path = Path(temp_dir) / f"unsafe-{index}.zip"
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr(name, b"synthetic")
                with self.subTest(name=name), self.assertRaises(coderland.CoderLandError):
                    coderland._collect_entries(archive_path)

    def test_rejects_duplicate_member_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / "duplicate.zip"
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr("input/image.png", b"first")
                    archive.writestr("input/image.png", b"second")
            with self.assertRaisesRegex(coderland.CoderLandError, "重复路径"):
                coderland._collect_entries(archive_path)

    def test_rejects_extreme_compression_ratio(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / "ratio.zip"
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                archive.writestr("input/repeated.png", b"x" * (4 * 1024 * 1024))
            with self.assertRaisesRegex(coderland.CoderLandError, "压缩比例"):
                coderland._collect_entries(archive_path)

class ApiPathTests(unittest.TestCase):
    def test_rejects_literal_and_encoded_dot_segments(self):
        for path in (
            "/api/v1/experiments/../users",
            "/api/v1/experiments/%2e%2e/users",
            "/api/v1/experiments/items/%2E%2E/users",
            "/api/v1/experiments/%252e%252e/admin",
            "/api/v1/experiments/%252e%252e%252fadmin",
            "/api/v1/experiments/items%252f..%252fadmin",
            "/api/v1/experiments/%252525252e%252525252e/private",
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


class RedirectSecurityTests(unittest.TestCase):
    def test_google_download_redirect_drops_api_key(self):
        request = coderland.urllib.request.Request(
            "https://code.zuicat.cn/api/v1/experiments/datasets/example/download/test",
            headers={"X-API-Key": "synthetic-secret"},
        )
        redirected = coderland._TrustedDownloadRedirectHandler().redirect_request(
            request, io.BytesIO(), 302, "Found", {},
            "https://drive.usercontent.google.com/download?id=synthetic-file-id",
        )
        self.assertIsNotNone(redirected)
        self.assertFalse(redirected.has_header("X-API-Key"))
        self.assertNotIn("synthetic-secret", repr(redirected.header_items()))
        self.assertEqual(redirected.full_url, "https://drive.usercontent.google.com/download?id=synthetic-file-id")

    def test_api_redirect_handler_still_rejects_cross_host_redirects(self):
        request = coderland.urllib.request.Request(
            "https://code.zuicat.cn/api/v1/experiments/resource",
            headers={"X-API-Key": "synthetic-secret"},
        )
        with self.assertRaises(coderland.urllib.error.URLError):
            coderland._TrustedRedirectHandler().redirect_request(
                request, io.BytesIO(), 302, "Found", {}, "https://example.com/collect",
            )

    def test_download_redirect_rejects_unapproved_hosts_and_paths(self):
        request = coderland.urllib.request.Request(
            "https://code.zuicat.cn/api/v1/experiments/datasets/example/download/test",
            headers={"X-API-Key": "synthetic-secret"},
        )
        handler = coderland._TrustedDownloadRedirectHandler()
        for url in (
            "https://example.com/collect",
            "https://drive.google.com/redirect?to=https://example.com",
            "http://drive.google.com/uc?id=synthetic-file-id",
        ):
            with self.subTest(url=url), self.assertRaises(coderland.urllib.error.URLError):
                handler.redirect_request(request, io.BytesIO(), 302, "Found", {}, url)


class RequestJsonTests(unittest.TestCase):
    def test_raw_bytes_are_forwarded_to_request(self):
        raw_body = b"synthetic-grant\nsynthetic-chunk"
        with patch.object(coderland, "_request", return_value=(b'{"received": 31}', {})) as request:
            result = coderland._request_json(
                "POST", "/api/v1/experiments/datasets/upload-chunk", raw=raw_body,
            )

        self.assertEqual(result, {"received": 31})
        request.assert_called_once_with(
            "POST", "/api/v1/experiments/datasets/upload-chunk",
            base=None, api_key=None, body=None, raw=raw_body, headers=None, timeout=120,
        )


class ApiCommandTests(unittest.TestCase):
    def test_unconfirmed_delete_never_touches_network(self):
        args = SimpleNamespace(
            method="DELETE", path="/api/v1/experiments/datasets/selected-id", json=None,
            output=None, confirmed_delete=False,
        )
        with patch.object(coderland, "load_api_key", return_value="synthetic-key"), \
                patch.object(coderland, "_request") as request, \
                patch.object(coderland, "_request_to_file") as request_to_file, \
                self.assertRaisesRegex(coderland.CoderLandError, "明确确认"):
            coderland.command_api(args)
        request.assert_not_called()
        request_to_file.assert_not_called()

    def test_double_encoded_path_is_rejected_before_network_access(self):
        args = SimpleNamespace(
            method="GET", path="/api/v1/experiments/%252e%252e/admin", json=None,
            output=None, confirmed_delete=False,
        )
        with patch.object(coderland, "load_api_key", return_value="synthetic-key"), \
                patch.object(coderland, "_request") as request, \
                patch.object(coderland, "_request_to_file") as request_to_file, \
                self.assertRaises(coderland.CoderLandError):
            coderland.command_api(args)
        request.assert_not_called()
        request_to_file.assert_not_called()

    def test_output_option_streams_to_the_requested_file(self):
        expected_size = 17
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "result.bin"
            response = StreamDownloadTests.FakeResponse(expected_size, {"Content-Length": str(expected_size)})
            with patch.object(coderland, "load_api_key", return_value="synthetic-key"), \
                    patch.object(coderland.urllib.request, "build_opener",
                                 return_value=StreamDownloadTests.FakeOpener(response)), \
                    redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                coderland.command_api(SimpleNamespace(
                    method="GET", path="/api/v1/experiments/datasets/selected-id/download/test",
                    json=None, output=str(destination), confirmed_delete=False,
                ))
            self.assertEqual(destination.read_bytes(), b"x" * expected_size)


class DatasetUploadTests(unittest.TestCase):
    def test_chunk_upload_resumes_from_server_acknowledged_offset(self):
        upload_id = "a" * 32
        grant = "G" * 40
        data = b"ABCDEFGHIJ"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "resume.zip"
            path.write_bytes(data)
            responses = [
                {"received": 3},
                {"ticket": "short-lived-ticket"},
                {"upload_grant": grant},
                {"received": 7},
                {"received": 10},
            ]
            with patch.object(coderland, "_require_buu") as require_buu, \
                    patch.object(coderland, "_request_json", side_effect=responses) as request, \
                    redirect_stdout(io.StringIO()):
                coderland._upload_dataset_file(
                    "synthetic-key", {"upload_id": upload_id}, "test", path, chunk_bytes=4,
                )

        require_buu.assert_called_once_with("synthetic-key")
        self.assertEqual(request.call_count, 5)
        self.assertEqual(request.call_args_list[0].args[:2], (
            "GET", f"{coderland.API_PREFIX}/datasets/upload-sessions/{upload_id}/status?kind=test",
        ))
        first_chunk, second_chunk = request.call_args_list[3], request.call_args_list[4]
        self.assertEqual(first_chunk.kwargs["headers"]["Content-Range"], "bytes 3-6/10")
        self.assertEqual(first_chunk.kwargs["raw"], grant.encode("ascii") + b"\nDEFG")
        self.assertEqual(second_chunk.kwargs["headers"]["Content-Range"], "bytes 7-9/10")
        self.assertEqual(second_chunk.kwargs["raw"], grant.encode("ascii") + b"\nHIJ")

    def test_upload_dataset_registers_completes_and_polls_baseline(self):
        dataset_id = "dataset-from-mock"
        upload_id = "b" * 32
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "coderland-output"
            output_dir.mkdir()
            test_zip = output_dir / "test.zip"
            _sample_archive(test_zip, "no-reference-restoration")
            profile = {
                "id": "no-reference-restoration", "categories": ["其他复原任务"],
                "_limits": {"image_count": 20, "test_archive_unpacked_bytes": 1024 * 1024,
                            "dataset_name_characters": 18, "dataset_description_characters": 20,
                            "upload_chunk_bytes": 1024},
            }
            completion_result = {"dataset_id": dataset_id, "baseline_status": "pulling"}
            poll_result = {"dataset": {"baseline_status": "ready"}}
            output = io.StringIO()
            with patch.object(Path, "cwd", return_value=root), \
                    patch.object(coderland, "load_api_key", return_value="synthetic-key"), \
                    patch.object(coderland, "_profile", return_value=profile), \
                    patch.object(coderland, "_require_buu") as require_buu, \
                    patch.object(coderland, "_state_file", return_value=("c" * 32, root / "state.json")), \
                    patch.object(coderland, "_create_dataset_session", return_value={"upload_id": upload_id}) as create, \
                    patch.object(coderland, "_upload_dataset_file") as upload_file, \
                    patch.object(coderland, "_request_json", side_effect=[completion_result, poll_result]) as request, \
                    patch.object(coderland.time, "sleep"), redirect_stdout(output):
                coderland.command_upload_dataset(SimpleNamespace(
                    profile="no-reference-restoration", category="其他复原任务", name="mock-set",
                    description="synthetic", test=str(test_zip), train=None,
                ))

            require_buu.assert_called_once_with("synthetic-key")
            create_args = create.call_args.args
            self.assertEqual(create_args[1:4], ("其他复原任务", "mock-set", "synthetic"))
            self.assertEqual(create_args[4], test_zip.resolve())
            self.assertIsNone(create_args[5])
            self.assertEqual(create_args[6], "c" * 32)
            upload_file.assert_called_once_with("synthetic-key", {"upload_id": upload_id}, "test", test_zip.resolve(), 1024)
            self.assertEqual(request.call_args_list[0].args[:2], ("POST", coderland.API_PREFIX + "/datasets/upload-complete"))
            self.assertEqual(request.call_args_list[0].kwargs["body"], {
                "upload_id": upload_id,
                "metadata": {"category": "其他复原任务", "name": "mock-set", "description": "synthetic"},
                "test_image_count": 1,
            })
            self.assertEqual(request.call_args_list[1].args[:2], ("GET", f"{coderland.API_PREFIX}/datasets/{dataset_id}"))
            self.assertIn('"baseline_status": "ready"', output.getvalue())


class UploadFileTests(unittest.TestCase):
    def test_filename_is_query_encoded_and_mapped_to_members_api_route(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "模型 #1 + weights.pt"
            path.write_bytes(b"synthetic-weights")
            with patch.object(coderland, "load_api_key", return_value="synthetic-key"), \
                    patch.object(coderland, "_upload_profiles", return_value={"limits": {"upload_chunk_bytes": 1024}}), \
                    patch.object(coderland, "_require_buu") as require_buu, \
                    patch.object(coderland, "_request_file", return_value=(b'{"ok":true}', {})) as request_file, \
                    redirect_stdout(io.StringIO()):
                coderland.command_upload_file(SimpleNamespace(
                    target=f"/api/experiments/staging/{'d' * 32}/weights?kind=weights&filename=ignored.zip",
                    file=str(path),
                ))

        require_buu.assert_called_once_with("synthetic-key")
        args, kwargs = request_file.call_args
        self.assertEqual(args[:2], ("POST", f"{coderland.API_PREFIX}/staging/{'d' * 32}/weights?kind=weights&filename=%E6%A8%A1%E5%9E%8B+%231+%2B+weights.pt"))
        self.assertEqual(args[2], path.resolve())
        self.assertEqual(kwargs["content_type"], "application/octet-stream")

    def test_rejects_external_target_before_any_upload_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "weights.pt"
            path.write_bytes(b"synthetic")
            with patch.object(coderland, "load_api_key", return_value="synthetic-key"), \
                    patch.object(coderland, "_upload_profiles", return_value={"limits": {}}), \
                    patch.object(coderland, "_require_buu") as require_buu, \
                    patch.object(coderland, "_request_file") as request_file, \
                    self.assertRaises(coderland.CoderLandError):
                coderland.command_upload_file(SimpleNamespace(target="https://example.com/upload", file=str(path)))
        require_buu.assert_not_called()
        request_file.assert_not_called()


class PaperApiTests(unittest.TestCase):
    def test_add_paper_searches_then_posts_selected_candidate_as_multipart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-1.7\nsynthetic paper\n%%EOF\n")
            api = patch.object(coderland, "_request_json", side_effect=[
                {"candidates": [
                    {"title": "First paper", "candidate_token": "signed-token-1"},
                    {"title": "Chosen paper", "authors": "A. Author", "year": "2025", "candidate_token": "signed-token-2"},
                ]},
                {"created": True, "pdf_uploaded": True},
            ])
            output = io.StringIO()
            with patch.object(coderland, "load_api_key", return_value="test-key"), api as request, \
                    patch("builtins.input", return_value="2"), redirect_stdout(output):
                coderland.command_add_paper(SimpleNamespace(
                    dataset_id="selected-dataset", query="Chosen paper", network_name="Method-X", pdf=str(pdf_path),
                ))

        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].args[:2], ("POST", coderland.API_PREFIX + "/papers/search"))
        self.assertEqual(request.call_args_list[0].kwargs["body"], {"dataset_id": "selected-dataset", "query": "Chosen paper"})
        add_call = request.call_args_list[1]
        self.assertEqual(add_call.args[:2], ("POST", coderland.API_PREFIX + "/papers"))
        body = add_call.kwargs["raw"]
        self.assertIn(b"signed-token-2", body)
        self.assertNotIn(b"signed-token-1", body)
        self.assertIn(b"name=\"pdf\"; filename=\"paper.pdf\"", body)
        self.assertIn(b"%PDF-1.7", body)
        self.assertIn(b'name="dataset_id"', body)
        self.assertIn(b"selected-dataset", body)
        self.assertIn(b'name="network_name"', body)
        self.assertIn(b"Method-X", body)
        self.assertNotIn("signed-token-", output.getvalue())

    def test_upload_paper_pdf_posts_multipart_to_selected_paper(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-1.7\nsynthetic paper\n%%EOF\n")
            output = io.StringIO()
            with patch.object(coderland, "load_api_key", return_value="test-key"), \
                    patch.object(coderland, "_request_json", return_value={"ok": True}) as request, \
                    redirect_stdout(output):
                coderland.command_upload_paper_pdf(SimpleNamespace(
                    paper_id="a" * 32, dataset_id="selected-dataset", pdf=str(pdf_path),
                ))

        request.assert_called_once()
        self.assertEqual(request.call_args.args[:2], ("POST", f"{coderland.API_PREFIX}/papers/{'a' * 32}/pdf"))
        self.assertTrue(request.call_args.kwargs["raw"].startswith(b"------CoderLandSkill"))
        self.assertIn(b"name=\"dataset_id\"", request.call_args.kwargs["raw"])

    def test_rejects_invalid_pdf_before_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "bad.pdf"
            pdf_path.write_bytes(b"not really a PDF")
            with patch.object(coderland, "load_api_key", return_value="test-key"), \
                    patch.object(coderland, "_request_json") as request, \
                    self.assertRaises(coderland.CoderLandError):
                coderland.command_upload_paper_pdf(SimpleNamespace(
                    paper_id="a" * 32, dataset_id="selected-dataset", pdf=str(pdf_path),
                ))
        request.assert_not_called()

    def test_rejects_pdf_over_50_mib_before_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "oversized.pdf"
            with pdf_path.open("wb") as stream:
                stream.truncate(coderland.MAX_PAPER_PDF_BYTES + 1)
            with patch.object(coderland, "load_api_key", return_value="test-key"), \
                    patch.object(coderland, "_request_json") as request, \
                    self.assertRaisesRegex(coderland.CoderLandError, "50 MiB"):
                coderland.command_upload_paper_pdf(SimpleNamespace(
                    paper_id="a" * 32, dataset_id="selected-dataset", pdf=str(pdf_path),
                ))
        request.assert_not_called()


class FileUploadStreamingTests(unittest.TestCase):
    class FakeStream:
        def __init__(self, chunks):
            self.chunks = list(chunks)
            self.read_sizes = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size=-1):
            self.read_sizes.append(size)
            if size <= 0:
                raise AssertionError("upload stream must never request the entire file")
            return self.chunks.pop(0) if self.chunks else b""

    class FakeResponse:
        status = 201

        def read(self):
            return b'{"uploaded":true}'

        def getheaders(self):
            return [("Content-Type", "application/json")]

    class FakeConnection:
        def __init__(self):
            self.request = None
            self.headers = {}
            self.sent_chunks = []
            self.closed = False

        def putrequest(self, method, path, **_kwargs):
            self.request = (method, path)

        def putheader(self, name, value):
            self.headers[name.casefold()] = value

        def endheaders(self):
            self.headers_finished = True

        def send(self, data):
            self.sent_chunks.append(data)

        def getresponse(self):
            return FileUploadStreamingTests.FakeResponse()

        def close(self):
            self.closed = True

    def test_request_file_sends_large_file_in_ordered_bounded_chunks(self):
        chunk_size = coderland.MAX_CHUNK_BYTES
        expected_chunks = [b"A" * chunk_size, b"B" * chunk_size, b"C" * 13]
        total = sum(map(len, expected_chunks))
        stream = self.FakeStream(expected_chunks)
        connection = self.FakeConnection()

        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "synthetic-upload.zip"
            with file_path.open("wb") as output:
                output.truncate(total)
            with patch.object(coderland.http.client, "HTTPSConnection", return_value=connection) as connect, \
                    patch.object(coderland.Path, "open", return_value=stream), \
                    redirect_stdout(io.StringIO()):
                response_body, response_headers = coderland._request_file(
                    "POST", "/api/v1/experiments/papers/selected/pdf", file_path,
                    base=coderland.API_BASE, api_key="synthetic-key", content_type="application/zip",
                )

        connect.assert_called_once_with("code.zuicat.cn", 443, timeout=3600)
        self.assertEqual(connection.request, ("POST", "/api/v1/experiments/papers/selected/pdf"))
        self.assertEqual(connection.headers["content-length"], str(total))
        self.assertEqual(connection.headers["content-type"], "application/zip")
        self.assertEqual(connection.sent_chunks, expected_chunks)
        self.assertEqual(b"".join(connection.sent_chunks), b"".join(expected_chunks))
        self.assertEqual(stream.read_sizes, [chunk_size, chunk_size, chunk_size, chunk_size])
        self.assertTrue(all(0 < size <= chunk_size for size in stream.read_sizes))
        self.assertEqual(response_body, b'{"uploaded":true}')
        self.assertEqual(response_headers, {"Content-Type": "application/json"})
        self.assertTrue(connection.closed)


class StreamDownloadTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, total, headers=None):
            self.remaining = total
            self.headers = headers or {}
            self.max_read_size = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size):
            self.max_read_size = max(self.max_read_size, size)
            amount = min(self.remaining, size)
            self.remaining -= amount
            return b"x" * amount

    class FakeOpener:
        def __init__(self, response):
            self.response = response

        def open(self, _request, timeout):
            self.timeout = timeout
            return self.response

    def test_streams_large_response_to_atomic_file_in_bounded_chunks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "weights.bin"
            total = 3 * 1024 * 1024 + 17
            response = self.FakeResponse(total, {"Content-Length": str(total), "Content-Type": "application/octet-stream"})
            output = io.StringIO()
            with patch.object(coderland.urllib.request, "build_opener", return_value=self.FakeOpener(response)), \
                    redirect_stderr(output):
                size, headers = coderland._request_to_file("GET", "/api/v1/experiments/file", destination)

            self.assertEqual(size, total)
            self.assertEqual(headers["Content-Type"], "application/octet-stream")
            self.assertEqual(destination.stat().st_size, total)
            self.assertLessEqual(response.max_read_size, 1024 * 1024)
            self.assertIn("下載中", output.getvalue())
            self.assertEqual(list(Path(temp_dir).glob("*.part")), [])

    def test_incomplete_download_keeps_previous_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "weights.bin"
            destination.write_bytes(b"previous data")
            response = self.FakeResponse(50, {"Content-Length": "100"})
            with patch.object(coderland.urllib.request, "build_opener", return_value=self.FakeOpener(response)), \
                    redirect_stderr(io.StringIO()), self.assertRaises(coderland.CoderLandError):
                coderland._request_to_file("GET", "/api/v1/experiments/file", destination)

            self.assertEqual(destination.read_bytes(), b"previous data")
            self.assertFalse(any(path.suffix == ".part" for path in Path(temp_dir).iterdir()))

    def test_unknown_content_length_streams_and_reports_byte_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "unknown-size.bin"
            response = self.FakeResponse(73)
            progress = io.StringIO()
            with patch.object(coderland.urllib.request, "build_opener", return_value=self.FakeOpener(response)), \
                    redirect_stderr(progress):
                size, _headers = coderland._request_to_file("GET", "/api/v1/experiments/file", destination)
            self.assertEqual(size, 73)
            self.assertEqual(destination.stat().st_size, 73)
            self.assertIn("服务器未提供文件总大小", progress.getvalue())

    def test_midstream_failure_preserves_destination_and_removes_partial_file(self):
        class FailingResponse(self.FakeResponse):
            def __init__(self):
                super().__init__(10, {"Content-Length": "10"})
                self.reads = 0

            def read(self, size):
                self.reads += 1
                if self.reads == 1:
                    self.max_read_size = max(self.max_read_size, size)
                    self.remaining -= 5
                    return b"first"
                raise OSError("synthetic stream interruption")

        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "existing.bin"
            destination.write_bytes(b"previous data")
            with patch.object(coderland.urllib.request, "build_opener", return_value=self.FakeOpener(FailingResponse())), \
                    redirect_stderr(io.StringIO()), self.assertRaises(coderland.CoderLandError):
                coderland._request_to_file("GET", "/api/v1/experiments/file", destination)
            self.assertEqual(destination.read_bytes(), b"previous data")
            self.assertFalse(any(path.suffix == ".part" for path in Path(temp_dir).iterdir()))


class FinalizeRecoveryTests(unittest.TestCase):
    @staticmethod
    def not_found_error():
        error = coderland.CoderLandError("404")
        error.__cause__ = coderland.urllib.error.HTTPError("https://code.zuicat.cn/resource", 404, "missing", {}, None)
        return error

    @staticmethod
    def timeout_error():
        try:
            raise coderland.CoderLandError("network timeout") from TimeoutError("synthetic timeout")
        except coderland.CoderLandError as error:
            return error

    def test_timeout_confirms_catalog_record_without_retrying_finalize(self):
        staging_id = "a" * 32
        calls = [
            {"dataset_id": "selected-dataset", "status": "uploading"},
            self.timeout_error(),
            self.not_found_error(),
            {"dataset": {"submissions": [{"id": staging_id, "workflow_status": "completed"}], "pending_submissions": []}},
        ]
        output = io.StringIO()
        with patch.object(coderland, "load_api_key", return_value="test-key"), \
                patch.object(coderland, "_request_json", side_effect=calls) as request, redirect_stdout(output):
            coderland.command_finalize(SimpleNamespace(
                staging_id=staging_id, dataset_id="selected-dataset", request_timeout=1, wait_timeout=0,
            ))

        methods = [call.args[0] for call in request.call_args_list]
        self.assertEqual(methods, ["GET", "POST", "GET", "GET"])
        self.assertEqual(methods.count("POST"), 1)
        self.assertIn('"finalized": true', output.getvalue())

    def test_missing_stage_confirms_completed_catalog_entry_without_post(self):
        staging_id = "e" * 32
        output = io.StringIO()
        with patch.object(coderland, "load_api_key", return_value="test-key"), \
                patch.object(coderland, "_request_json", side_effect=[
                    self.not_found_error(),
                    {"dataset": {"submissions": [
                        {"id": staging_id, "workflow_status": "completed", "score": 0.91},
                    ]}},
                ]) as request, redirect_stdout(output):
            coderland.command_finalize(SimpleNamespace(
                staging_id=staging_id, dataset_id="selected-dataset", request_timeout=1, wait_timeout=0,
            ))
        self.assertEqual([call.args[0] for call in request.call_args_list], ["GET", "GET"])
        self.assertIn('"confirmed_by": "dataset_catalog"', output.getvalue())




if __name__ == "__main__":
    unittest.main()
