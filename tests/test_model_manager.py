from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import unittest
import time
import uuid
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "packaging" / "model-manager.py"
SPEC = importlib.util.spec_from_file_location("qis_model_manager", MODULE_PATH)
assert SPEC and SPEC.loader
manager = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = manager
SPEC.loader.exec_module(manager)


PINNED_URL = "https://huggingface.co/example/release/resolve/0123456789abcdef0123456789abcdef01234567/"
WORKSPACE = Path(__file__).resolve().parents[1]
WORK_AREA = Path(__file__).resolve().parent / "packaging_fixtures"


def fixture_dir(name: str) -> Path:
    path = WORK_AREA / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def remove_files(*paths: Path) -> None:
    for path in paths:
        path.unlink(missing_ok=True)


class FakeResponse:
    def __init__(self, data: bytes, *, url: str, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.stream = io.BytesIO(data)
        self.url = url
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size: int = -1) -> bytes:
        return self.stream.read(size)

    def geturl(self) -> str:
        return self.url

    def getcode(self) -> int:
        return self.status


class MappingOpener:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.requests = []

    def __call__(self, request, *, timeout: int):
        self.requests.append(request)
        payload = self.payloads[request.full_url]
        return FakeResponse(payload, url=request.full_url, headers={"Content-Length": str(len(payload))})


class FakeCredentialStore:
    def __init__(self) -> None:
        self.values = {}

    def available(self) -> bool:
        return True

    def get(self, reference: str) -> str | None:
        return self.values.get(reference)

    def set(self, reference: str, token: str) -> None:
        self.values[reference] = token

    def delete(self, reference: str) -> None:
        self.values.pop(reference, None)


def artifact(
    artifact_id: str,
    role: str,
    content: bytes,
    *,
    delivery: str = "download",
    root: str = "models",
    path: str | None = None,
) -> dict:
    return {
        "id": artifact_id,
        "delivery": delivery,
        "root": root,
        "path": path or f"{artifact_id}.bin",
        "role": role,
        "url": f"{PINNED_URL}{artifact_id}.bin" if delivery == "download" else "",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "license": "qwen" if root == "models" else "runtime",
    }


def valid_manifest(contents: dict[str, bytes] | None = None) -> dict:
    contents = contents or {
        "sd-cli": b"cli",
        "sd-server": b"server",
        "transformer": b"transformer",
        "text": b"text",
        "mmproj": b"projector",
        "vae": b"vae",
    }
    artifacts = [
        artifact("sd-cli", "sd_cli", contents["sd-cli"], delivery="bundled", root="runtime", path="sd-cli.exe"),
        artifact("sd-server", "sd_server", contents["sd-server"], delivery="bundled", root="runtime", path="sd-server.exe"),
        artifact("transformer", "transformer", contents["transformer"]),
        artifact("text", "text_encoder", contents["text"]),
        artifact("mmproj", "mmproj", contents["mmproj"]),
        artifact("vae", "vae", contents["vae"]),
    ]
    refs = [item["id"] for item in artifacts]
    base = {"artifacts": refs, "extra_args": ["--backend", "diffusion=CUDA0,te=CPU,vae=CPU"]}
    return {
        "schema_version": 2,
        "release_version": "1.2.3",
        "licenses": {
            "qwen": {
                "name": "Qwen Test License", "version": "model-2.1", "model": "Qwen Test",
                "text": "Test-only model terms.", "acceptance_required": True,
            },
            "runtime": {
                "name": "Runtime MIT", "version": "runtime-1", "model": "Test runtime",
                "text": "Test-only runtime terms.", "acceptance_required": False,
            },
        },
        "artifacts": artifacts,
        "profiles": {
            "small": {**base, "name": "Small", "model_id": "qwen-test", "model_version": "q4", "description": "small test profile", "min_vram_mib": 8192, "max_vram_mib": 12287},
            "medium": {**base, "name": "Medium", "model_id": "qwen-test", "model_version": "q5", "description": "medium test profile", "min_vram_mib": 12288, "max_vram_mib": 16383},
            "large": {**base, "name": "Large", "model_id": "qwen-test", "model_version": "q6", "description": "large test profile", "min_vram_mib": 16384, "max_vram_mib": None},
        },
    }


class ManifestTests(unittest.TestCase):
    def test_valid_manifest(self) -> None:
        manifest = valid_manifest()
        self.assertIs(manager.validate_manifest(manifest), manifest)

    def test_blank_hash_is_rejected(self) -> None:
        manifest = valid_manifest()
        manifest["artifacts"][2]["sha256"] = ""
        with self.assertRaisesRegex(manager.ManagerError, "sha256"):
            manager.validate_manifest(manifest)

    def test_license_and_model_versions_are_required(self) -> None:
        manifest = valid_manifest()
        del manifest["licenses"]["qwen"]["version"]
        with self.assertRaisesRegex(manager.ManagerError, "license version"):
            manager.validate_manifest(manifest)
        manifest = valid_manifest()
        del manifest["profiles"]["medium"]["model_version"]
        with self.assertRaisesRegex(manager.ManagerError, "model_version"):
            manager.validate_manifest(manifest)

    def test_http_and_unpinned_huggingface_urls_are_rejected(self) -> None:
        manifest = valid_manifest()
        manifest["artifacts"][2]["url"] = "http://huggingface.co/file"
        with self.assertRaisesRegex(manager.ManagerError, "HTTPS"):
            manager.validate_manifest(manifest)
        manifest = valid_manifest()
        manifest["artifacts"][2]["url"] = "https://huggingface.co/example/release/resolve/main/file"
        with self.assertRaisesRegex(manager.ManagerError, "commit"):
            manager.validate_manifest(manifest)

    def test_disallowed_host_and_path_traversal_are_rejected(self) -> None:
        manifest = valid_manifest()
        manifest["artifacts"][2]["url"] = "https://example.com/file"
        with self.assertRaisesRegex(manager.ManagerError, "allowlisted"):
            manager.validate_manifest(manifest)
        manifest = valid_manifest()
        manifest["artifacts"][2]["path"] = "../outside.gguf"
        with self.assertRaisesRegex(manager.ManagerError, "safe relative"):
            manager.validate_manifest(manifest)

    def test_hf_source_urls_reject_mutable_queries_hosts_and_traversal(self) -> None:
        revision = "a" * 40
        self.assertEqual(
            manager.parse_hf_file_url(f"https://hf.co/example/repo/resolve/{revision}/folder/model.gguf"),
            ("example/repo", revision, "folder/model.gguf"),
        )
        for url in (
            "https://example.com/example/repo/resolve/" + revision + "/model.gguf",
            "https://huggingface.co/example/repo/resolve/main/model.gguf",
            f"https://huggingface.co/example/repo/resolve/{revision}/model.gguf?token=secret",
            f"https://huggingface.co/example/repo/resolve/{revision}/../model.gguf",
            f"https://huggingface.co/example/repo/resolve/{revision}/%2e%2e/model.gguf",
        ):
            with self.subTest(url=url), self.assertRaises(manager.ManagerError):
                manager.parse_hf_file_url(url)
        manifest = valid_manifest()
        manifest["artifacts"][2]["path"] = "C:\\outside.gguf"
        with self.assertRaisesRegex(manager.ManagerError, "safe relative"):
            manager.validate_manifest(manifest)


class HardwareTests(unittest.TestCase):
    def test_parses_nvidia_smi_and_selects_profile_boundaries(self) -> None:
        def runner(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 0, "NVIDIA RTX Test, 12288, 555.42\n", "")

        info = manager.detect_hardware(fixture_dir("hardware"), runner=runner)
        self.assertEqual(info.gpu_name, "NVIDIA RTX Test")
        self.assertEqual(info.vram_mib, 12288)
        self.assertTrue(info.cuda12_driver_compatible)
        manifest = manager.validate_manifest(valid_manifest())
        self.assertEqual(manager.select_profile(manifest, "auto", info), "medium")
        large = manager.HardwareInfo(True, "GPU", 16384, "555", True, 32768, 100000)
        self.assertEqual(manager.select_profile(manifest, "auto", large), "large")

    def test_auto_profile_requires_supported_nvidia_vram(self) -> None:
        info = manager.HardwareInfo(True, None, None, None, None, 32768, 100000)
        with self.assertRaisesRegex(manager.ManagerError, "nvidia-smi"):
            manager.select_profile(manager.validate_manifest(valid_manifest()), "auto", info)


class DownloadTests(unittest.TestCase):
    def test_resumes_part_file_and_verifies_sha256(self) -> None:
        content = b"abcdef"
        item = artifact("transformer", "transformer", content)
        target = fixture_dir("resume") / "model.gguf"
        remove_files(target, target.with_name("model.gguf.part"))
        try:
            partial = target.with_name("model.gguf.part")
            partial.write_bytes(b"abc")
            manager._write_json_atomic(manager._partial_record_path(partial), manager._source_identity(item))
            requests = []

            def opener(request, *, timeout: int):
                requests.append(request)
                return FakeResponse(
                    b"def",
                    url=request.full_url,
                    status=206,
                    headers={"Content-Range": "bytes 3-5/6"},
                )

            manager.download_artifact(item, target, opener=opener, progress=lambda _: None)
            self.assertEqual(target.read_bytes(), content)
            self.assertFalse(target.with_name("model.gguf.part").exists())
            self.assertEqual(requests[0].get_header("Range"), "bytes=3-")
        finally:
            remove_files(target, target.with_name("model.gguf.part"), manager._partial_record_path(target.with_name("model.gguf.part")), manager._source_record_path(target))

    def test_hash_failure_removes_partial_file(self) -> None:
        item = artifact("transformer", "transformer", b"expected")
        target = fixture_dir("hash_failure") / "model.gguf"
        remove_files(target, target.with_name("model.gguf.part"))
        try:
            opener = MappingOpener({item["url"]: b"tampered"})
            with self.assertRaisesRegex(manager.ManagerError, "mismatch"):
                manager.download_artifact(item, target, opener=opener, progress=lambda _: None)
            self.assertFalse(target.with_name("model.gguf.part").exists())
        finally:
            remove_files(target, target.with_name("model.gguf.part"))

    def test_redirect_to_private_host_is_rejected(self) -> None:
        item = artifact("transformer", "transformer", b"expected")
        target = fixture_dir("hash_failure") / "redirect.gguf"
        remove_files(target, target.with_name("redirect.gguf.part"))
        try:
            def opener(request, *, timeout: int):
                return FakeResponse(b"expected", url="https://127.0.0.1/redirected")

            with self.assertRaisesRegex(manager.ManagerError, "private"):
                manager.download_artifact(item, target, opener=opener, progress=lambda _: None)
        finally:
            partial = target.with_name("redirect.gguf.part")
            remove_files(target, partial, manager._partial_record_path(partial), manager._source_record_path(target))

    def test_cancel_preserves_partial_file_for_resume(self) -> None:
        content = b"a" * (manager.CHUNK_SIZE + 16)
        item = artifact("transformer", "transformer", content)
        target = fixture_dir("resume") / "cancelled.gguf"
        remove_files(target, target.with_name("cancelled.gguf.part"))
        seen = {"chunks": 0}

        def progress(stage: str, written: int, total: int) -> None:
            del total
            if stage == "downloading" and written:
                seen["chunks"] += 1

        try:
            with self.assertRaises(manager.DownloadCancelled):
                manager.download_artifact(
                    item,
                    target,
                    opener=MappingOpener({item["url"]: content}),
                    progress=lambda _: None,
                    on_progress=progress,
                    cancelled=lambda: seen["chunks"] >= 1,
                )
            partial = target.with_name("cancelled.gguf.part")
            self.assertTrue(partial.is_file())
            self.assertGreater(partial.stat().st_size, 0)
            self.assertFalse(target.exists())
        finally:
            partial = target.with_name("cancelled.gguf.part")
            remove_files(target, partial, manager._partial_record_path(partial), manager._source_record_path(target))

    def test_token_is_sent_only_as_bearer_header(self) -> None:
        item = artifact("transformer", "transformer", b"expected")
        target = fixture_dir("resume") / "authorized.gguf"
        remove_files(target, target.with_name("authorized.gguf.part"))
        opener = MappingOpener({item["url"]: b"expected"})
        try:
            manager.download_artifact(item, target, opener=opener, progress=lambda _: None, auth_token="hf_secret")
            request = opener.requests[0]
            self.assertEqual(request.get_header("Authorization"), "Bearer hf_secret")
            self.assertNotIn("hf_secret", request.full_url)
        finally:
            partial = target.with_name("authorized.gguf.part")
            remove_files(target, partial, manager._partial_record_path(partial), manager._source_record_path(target))

    def test_hf_cdn_redirect_is_allowed_and_strips_authorization(self) -> None:
        request = manager.Request(
            f"{PINNED_URL}transformer.bin",
            headers={"Authorization": "Bearer hf_secret", "Range": "bytes=10-"},
        )
        public_dns = [(manager.socket.AF_INET, manager.socket.SOCK_STREAM, 6, "", ("54.192.0.1", 443))]
        with mock.patch.object(manager.socket, "getaddrinfo", return_value=public_dns):
            redirected = manager.AllowlistRedirectHandler().redirect_request(
                request, None, 302, "Found", {}, "https://us.aws.cdn.hf.co/object/model.bin"
            )
        self.assertIsNotNone(redirected)
        self.assertIsNone(redirected.get_header("Authorization"))
        self.assertEqual(redirected.get_header("Range"), "bytes=10-")
        with mock.patch.object(manager.socket, "getaddrinfo", return_value=public_dns):
            changed_port = manager.AllowlistRedirectHandler().redirect_request(
                request, None, 302, "Found", {}, "https://huggingface.co:444/object/model.bin"
            )
        self.assertIsNone(changed_port.get_header("Authorization"))

    def test_hf_token_is_never_attached_to_custom_host(self) -> None:
        item = artifact("transformer", "transformer", b"custom")
        item.update(url="https://models.example/custom.bin", source_type="custom")
        target = fixture_dir("resume") / "custom-authorized.gguf"
        remove_files(target, target.with_name(target.name + ".part"), manager._source_record_path(target))
        opener = MappingOpener({item["url"]: b"custom"})
        try:
            manager.download_artifact(item, target, opener=opener, progress=lambda _: None, auth_token="hf_secret")
            self.assertIsNone(opener.requests[0].get_header("Authorization"))
        finally:
            remove_files(target, target.with_name(target.name + ".part"), manager._source_record_path(target))

    def test_redirect_destination_is_revalidated_for_ssrf(self) -> None:
        request = manager.Request(f"{PINNED_URL}transformer.bin", headers={"Authorization": "Bearer hf_secret"})
        handler = manager.AllowlistRedirectHandler()
        with self.assertRaisesRegex(manager.ManagerError, "private"):
            handler.redirect_request(request, None, 302, "Found", {}, "https://169.254.169.254/latest/meta-data")
        private_dns = [(manager.socket.AF_INET, manager.socket.SOCK_STREAM, 6, "", ("10.0.0.4", 443))]
        with mock.patch.object(manager.socket, "getaddrinfo", return_value=private_dns):
            with self.assertRaisesRegex(manager.ManagerError, "resolved"):
                handler.redirect_request(request, None, 302, "Found", {}, "https://download.example/model.bin")

    def test_custom_no_hash_download_completes_and_is_indeterminate_without_length(self) -> None:
        item = artifact("transformer", "transformer", b"custom")
        item.update(url="https://models.example/custom.bin", size=None, sha256="", source_type="custom", verified=False)
        target = fixture_dir("resume") / "custom-no-hash.gguf"
        remove_files(target, target.with_name(target.name + ".part"), manager._source_record_path(target))
        updates = []

        def opener(request, *, timeout):
            del timeout
            return FakeResponse(b"custom", url=request.full_url)

        try:
            manager.download_artifact(item, target, opener=opener, progress=lambda _: None, on_progress=lambda *args: updates.append(args))
            self.assertEqual(target.read_bytes(), b"custom")
            self.assertTrue(manager._source_record_matches(manager._source_record_path(target), item))
            self.assertIn(("downloading", 0, 0), updates)
            self.assertEqual(updates[-1], ("complete", 6, 6))
        finally:
            remove_files(target, target.with_name(target.name + ".part"), manager._source_record_path(target))

    def test_part_is_not_reused_when_source_url_changes(self) -> None:
        old_item = artifact("transformer", "transformer", b"abcdef")
        new_item = dict(old_item, url=f"{PINNED_URL}replacement.bin")
        target = fixture_dir("resume") / "changed-source.gguf"
        partial = target.with_name(target.name + ".part")
        remove_files(target, partial, manager._partial_record_path(partial), manager._source_record_path(target))
        partial.write_bytes(b"abc")
        manager._write_json_atomic(manager._partial_record_path(partial), manager._source_identity(old_item))
        requests = []

        def opener(request, *, timeout):
            del timeout
            requests.append(request)
            return FakeResponse(b"abcdef", url=request.full_url, headers={"Content-Length": "6"})

        try:
            manager.download_artifact(new_item, target, opener=opener, progress=lambda _: None)
            self.assertIsNone(requests[0].get_header("Range"))
            self.assertEqual(target.read_bytes(), b"abcdef")
        finally:
            remove_files(target, partial, manager._partial_record_path(partial), manager._source_record_path(target))


class HuggingFaceSourceTests(unittest.TestCase):
    def test_resolve_requires_lfs_metadata_and_returns_server_expected_values(self) -> None:
        manifest = manager.validate_manifest(valid_manifest())
        revision = "b" * 40
        api_url = f"https://huggingface.co/api/models/example/repo/revision/{revision}?blobs=true"
        response = {
            "sha": revision,
            "siblings": [{
                "rfilename": "weights/model.gguf",
                "size": 123,
                "lfs": {"size": 123, "sha256": "c" * 64},
            }],
        }
        resolved = manager.resolve_hf_source(
            manifest,
            {"repo_id": "example/repo", "revision": revision, "files": {"transformer": "weights/model.gguf"}},
            opener=MappingOpener({api_url: json.dumps(response).encode()}),
        )
        item = resolved["preview"][0]
        self.assertEqual(item["size"], 123)
        self.assertEqual(item["sha256"], "c" * 64)
        self.assertEqual(item["url"], f"https://huggingface.co/example/repo/resolve/{revision}/weights/model.gguf")

    def test_remote_validator_fails_on_metadata_mismatch(self) -> None:
        manifest = manager.validate_manifest(valid_manifest())
        item = manifest["artifacts"][2]
        repo_id, revision, file_path = manager.parse_hf_file_url(item["url"])
        api_url = f"https://huggingface.co/api/models/{repo_id}/revision/{revision}?blobs=true"
        response = {"sha": revision, "siblings": [{
            "rfilename": file_path,
            "lfs": {"size": item["size"] + 1, "sha256": item["sha256"]},
        }]}
        with self.assertRaisesRegex(manager.ManagerError, "size"):
            manager.validate_remote_artifacts(
                {**manifest, "artifacts": [item]},
                opener=MappingOpener({api_url: json.dumps(response).encode()}),
            )

    def test_remote_validator_uses_range_for_non_hf_allowlisted_urls(self) -> None:
        item = artifact("runtime-download", "transformer", b"abc")
        item["url"] = "https://github.com/example/project/releases/download/v1/model.bin"
        requests = []

        def opener(request, *, timeout):
            del timeout
            requests.append(request)
            return FakeResponse(b"x", url=request.full_url, status=206, headers={"Content-Range": "bytes 0-0/3"})

        result = manager.validate_remote_artifacts({"artifacts": [item]}, opener=opener)
        self.assertEqual(result[0]["size"], 3)
        self.assertEqual(requests[0].get_header("Range"), "bytes=0-0")

    def test_arbitrary_public_https_custom_source_requires_acknowledgement(self) -> None:
        manifest = manager.validate_manifest(valid_manifest())
        request = {"files": {"transformer": {"url": "https://models.example/releases/model.gguf"}}}
        public_dns = lambda *args, **kwargs: [
            (manager.socket.AF_INET, manager.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ]
        with self.assertRaisesRegex(manager.ManagerError, "responsibility_acknowledged"):
            manager.resolve_hf_source(manifest, request, resolver=public_dns)
        resolved = manager.resolve_hf_source(
            manifest, {**request, "responsibility_acknowledged": True}, resolver=public_dns
        )
        item = resolved["artifacts"]["transformer"]
        self.assertEqual(item["source_type"], "custom")
        self.assertIsNone(item["size"])
        self.assertEqual(item["sha256"], "")
        self.assertFalse(item["verified"])
        self.assertTrue(resolved["responsibility_acknowledged"])

    def test_custom_sources_reject_unsafe_urls_and_dns(self) -> None:
        manifest = manager.validate_manifest(valid_manifest())
        for url in (
            "http://models.example/model.gguf",
            "file:///c:/model.gguf",
            "data:application/octet-stream,model",
            "https://user:secret@models.example/model.gguf",
            "https://localhost/model.gguf",
            "https://127.0.0.1/model.gguf",
            "https://169.254.1.2/model.gguf",
            "https://192.0.2.1/model.gguf",
        ):
            with self.subTest(url=url), self.assertRaises(manager.ManagerError):
                manager.resolve_hf_source(
                    manifest,
                    {"files": {"transformer": {"url": url}}, "responsibility_acknowledged": True},
                    resolver=lambda *args, **kwargs: [],
                )
        private_dns = lambda *args, **kwargs: [
            (manager.socket.AF_INET, manager.socket.SOCK_STREAM, 6, "", ("172.16.1.5", 443))
        ]
        with self.assertRaisesRegex(manager.ManagerError, "resolved"):
            manager.resolve_hf_source(
                manifest,
                {"files": {"transformer": {"url": "https://models.example/model.gguf"}}, "responsibility_acknowledged": True},
                resolver=private_dns,
            )

    def test_official_source_override_cannot_omit_hash_or_size(self) -> None:
        manifest = manager.validate_manifest(valid_manifest())
        revision = "f" * 40
        source = {
            "schema_version": 2,
            "responsibility_acknowledged": False,
            "artifacts": {
                "transformer": {
                    "source_type": "huggingface",
                    "url": f"https://huggingface.co/example/repo/resolve/{revision}/model.gguf",
                    "repo_id": "example/repo",
                    "revision": revision,
                    "file_path": "model.gguf",
                    "size": None,
                    "sha256": "",
                }
            },
        }
        with self.assertRaisesRegex(manager.ManagerError, "size or SHA-256"):
            manager.apply_source_overrides(manifest, source)


class InstallTests(unittest.TestCase):
    def test_license_is_required_before_download(self) -> None:
        manifest = manager.validate_manifest(valid_manifest())
        root = fixture_dir("license")
        with self.assertRaisesRegex(manager.ManagerError, "--accept-license"):
            manager.install_profile(
                manifest,
                "medium",
                accept_license=False,
                model_dir=root,
                runtime_dir=root,
                config_path=root / "config.json",
                data_dir=root,
                opener=MappingOpener({}),
                progress=lambda _: None,
            )

    def test_install_verifies_runtime_downloads_models_and_writes_config(self) -> None:
        contents = {
            "sd-cli": b"cli",
            "sd-server": b"server",
            "transformer": b"transformer",
            "text": b"text",
            "mmproj": b"projector",
            "vae": b"vae",
        }
        manifest = manager.validate_manifest(valid_manifest(contents))
        payloads = {
            item["url"]: contents[item["id"]]
            for item in manifest["artifacts"]
            if item["delivery"] == "download"
        }
        root = fixture_dir("install")
        runtime = root / "runtime"
        models = root / "models"
        state = root / "state"
        generated = [
            runtime / "sd-cli.exe",
            runtime / "sd-server.exe",
            models / "transformer.bin",
            models / "text.bin",
            models / "mmproj.bin",
            models / "vae.bin",
            state / "config.json",
            state / "license-acceptance.json",
        ]
        generated.extend(manager._source_record_path(path) for path in list(generated) if path.parent == models)
        remove_files(*generated)
        try:
            (runtime / "sd-cli.exe").write_bytes(contents["sd-cli"])
            (runtime / "sd-server.exe").write_bytes(contents["sd-server"])
            config_path = state / "config.json"
            manager.install_profile(
                manifest,
                "medium",
                accept_license=True,
                model_dir=models,
                runtime_dir=runtime,
                config_path=config_path,
                data_dir=root / "data",
                opener=MappingOpener(payloads),
                progress=lambda _: None,
            )
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["port"], 8730)
            self.assertEqual(Path(config["sd_cli"]), (runtime / "sd-cli.exe").resolve())
            self.assertEqual(config["packaging"]["profile"], "medium")
            self.assertNotIn("host", config)
            acceptance = json.loads((config_path.parent / "license-acceptance.json").read_text(encoding="utf-8"))
            self.assertEqual(acceptance["acceptances"][0]["license_id"], "qwen")
            self.assertEqual(acceptance["acceptances"][0]["version"], "model-2.1")
        finally:
            remove_files(*generated)


class PersistentManagerTests(unittest.TestCase):
    @staticmethod
    def runtime_dir() -> Path:
        path = WORKSPACE / "tests" / "runtime" / uuid.uuid4().hex
        path.mkdir(parents=True)
        return path

    @staticmethod
    def hardware() -> object:
        return manager.HardwareInfo(True, "NVIDIA Test", 12288, "555.42", True, 32768, 100000)

    @staticmethod
    def wait_for(worker: object) -> None:
        deadline = time.monotonic() + 5
        while worker.worker and worker.worker.is_alive() and time.monotonic() < deadline:
            worker.worker.join(timeout=0.05)
        if worker.worker and worker.worker.is_alive():
            raise AssertionError("model manager worker did not finish")

    def create_manager(
        self, root: Path, manifest: dict, payloads: dict[str, bytes],
        *, artifact_contents: dict[str, bytes] | None = None, **kwargs,
    ):
        manifest_path = root / "release-manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        runtime = root / "runtime"
        runtime.mkdir(parents=True)
        contents = artifact_contents or {}
        for item in manifest["artifacts"]:
            if item["delivery"] == "bundled":
                (runtime / item["path"]).write_bytes(contents.get(item["id"], b"fixture"))
        return manager.PersistentModelManager(
            manifest_path,
            model_dir=root / "models",
            runtime_dir=runtime,
            config_path=root / "state" / "config.json",
            data_dir=root / "state" / "data",
            opener=MappingOpener(payloads),
            hardware=self.hardware(),
            **kwargs,
        )

    def test_source_confirmation_persists_metadata_not_token(self) -> None:
        manifest = manager.validate_manifest(valid_manifest())
        root = self.runtime_dir()
        credentials = FakeCredentialStore()
        revision = "d" * 40
        file_path = "replacement.gguf"
        api_url = f"https://huggingface.co/api/models/example/repo/revision/{revision}?blobs=true"
        response = {"sha": revision, "siblings": [{
            "rfilename": file_path,
            "lfs": {"size": 321, "sha256": "e" * 64},
        }]}
        service = self.create_manager(
            root,
            manifest,
            {api_url: json.dumps(response).encode()},
            credential_store=credentials,
        )
        service.set_token("hf_private_value")
        preview = service.resolve_source({
            "repo_id": "example/repo", "revision": revision, "files": {"transformer": file_path},
        })
        service.confirm_source(preview["confirmation_id"])
        persisted = (root / "state" / "hf-source.json").read_text(encoding="utf-8")
        self.assertNotIn("hf_private_value", persisted)
        self.assertIn("credential_ref", persisted)
        transformer = next(item for item in service.manifest["artifacts"] if item["id"] == "transformer")
        self.assertEqual(transformer["size"], 321)
        self.assertEqual(transformer["sha256"], "e" * 64)
        self.assertTrue(service.status()["catalog"]["source"]["token"]["configured"])
        service.remove_token()
        self.assertFalse(credentials.values)
        service.close()

    def test_custom_no_hash_source_labels_persist_through_active_state(self) -> None:
        contents = {
            "sd-cli": b"cli", "sd-server": b"server", "transformer": b"custom-transformer",
            "text": b"text", "mmproj": b"projector", "vae": b"vae",
        }
        manifest = manager.validate_manifest(valid_manifest(contents))
        payloads = {item["url"]: contents[item["id"]] for item in manifest["artifacts"] if item["delivery"] == "download"}
        root = self.runtime_dir()
        service = self.create_manager(root, manifest, payloads, artifact_contents=contents)
        custom_url = "https://models.example/qwen/custom-transformer.gguf"
        public_dns = [(manager.socket.AF_INET, manager.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with mock.patch.object(manager.socket, "getaddrinfo", return_value=public_dns):
            preview = service.resolve_source({
                "files": {"transformer": {"url": custom_url}},
                "responsibility_acknowledged": True,
            })
        service.confirm_source(preview["confirmation_id"])
        service.opener.payloads[custom_url] = contents["transformer"]
        license_info = next(item for item in service.catalog()["profiles"] if item["id"] == "medium")["licenses"][0]
        service.start_install(
            "medium",
            accepted=[{"id": license_info["id"], "version": license_info["version"], "model": license_info["model"]}],
        )
        self.wait_for(service)
        state = service.status()
        persisted_source = json.loads((root / "state" / "hf-source.json").read_text(encoding="utf-8"))
        registry = json.loads((root / "state" / "config.json").with_name("installed-profiles.json").read_text(encoding="utf-8"))
        config = json.loads((root / "state" / "config.json").read_text(encoding="utf-8"))
        profile = next(item for item in state["catalog"]["profiles"] if item["id"] == "medium")
        self.assertTrue(persisted_source["override"]["responsibility_acknowledged"])
        self.assertFalse(persisted_source["override"]["artifacts"]["transformer"]["verified"])
        self.assertTrue(state["transfer"]["unverified"])
        self.assertTrue(any(item["unverified"] for item in state["transfer"]["files"]))
        self.assertTrue(registry["profiles"]["medium"]["unverified"])
        self.assertTrue(config["packaging"]["unverified"])
        self.assertTrue(profile["unverified"])
        self.assertTrue(state["catalog"]["active_model"]["unverified"])
        service.close()

    def test_token_test_redacts_secret_from_errors(self) -> None:
        manifest = manager.validate_manifest(valid_manifest())
        root = self.runtime_dir()
        credentials = FakeCredentialStore()

        class FailingOpener:
            def __call__(self, request, *, timeout):
                del request, timeout
                raise OSError("failed Bearer hf_private_value")

        service = self.create_manager(root, manifest, {}, credential_store=credentials)
        service.opener = FailingOpener()
        service.set_token("hf_private_value")
        with self.assertRaises(manager.ManagerError) as caught:
            service.test_token()
        self.assertNotIn("hf_private_value", str(caught.exception))
        self.assertIn("REDACTED", str(caught.exception))
        service.close()

    def test_async_install_persists_progress_license_and_active_config(self) -> None:
        contents = {
            "sd-cli": b"cli", "sd-server": b"server", "transformer": b"transformer",
            "text": b"text", "mmproj": b"projector", "vae": b"vae",
        }
        manifest = manager.validate_manifest(valid_manifest(contents))
        payloads = {item["url"]: contents[item["id"]] for item in manifest["artifacts"] if item["delivery"] == "download"}
        root = self.runtime_dir()
        configured = []
        service = self.create_manager(
            root, manifest, payloads, artifact_contents=contents, on_configured=configured.append
        )
        license_info = service.catalog()["profiles"][1]["licenses"][0]
        service.start_install(
            "medium",
            accepted=[{"id": license_info["id"], "version": license_info["version"], "model": license_info["model"]}],
        )
        self.wait_for(service)
        state = service.status()
        self.assertEqual(state["transfer"]["status"], "completed")
        self.assertEqual(state["transfer"]["percent"], 100.0)
        self.assertTrue(all(item["stage"] == "complete" for item in state["transfer"]["files"]))
        self.assertTrue(all(item["bytes_downloaded"] == item["bytes_total"] for item in state["transfer"]["files"]))
        self.assertEqual(state["catalog"]["active_profile"], "medium")
        self.assertTrue(state["catalog"]["profiles"][1]["installed"])
        self.assertEqual(len(configured), 1)
        self.assertTrue((root / "state" / "model-manager-state.json").is_file())
        acceptance = json.loads((root / "state" / "license-acceptance.json").read_text(encoding="utf-8"))
        self.assertEqual(acceptance["acceptances"][0]["model"], "Qwen Test")
        service.close()

    def test_restart_marks_active_transfer_resumable(self) -> None:
        manifest = manager.validate_manifest(valid_manifest())
        payloads = {item["url"]: b"" for item in manifest["artifacts"] if item["delivery"] == "download"}
        root = self.runtime_dir()
        state_dir = root / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "model-manager-state.json").write_text(json.dumps({
            **manager.PersistentModelManager._idle_transfer(),
            "profile_id": "medium", "status": "downloading", "stage": "downloading", "activate": True,
        }), encoding="utf-8")
        service = self.create_manager(root, manifest, payloads)
        self.assertEqual(service.status()["transfer"]["status"], "paused")
        self.assertIn("Resume", service.status()["transfer"]["error"])
        service.close()

    def test_retry_after_restart_reuses_acceptance_and_partial_state(self) -> None:
        contents = {
            "sd-cli": b"cli", "sd-server": b"server", "transformer": b"transformer",
            "text": b"text", "mmproj": b"projector", "vae": b"vae",
        }
        manifest = manager.validate_manifest(valid_manifest(contents))
        payloads = {item["url"]: contents[item["id"]] for item in manifest["artifacts"] if item["delivery"] == "download"}
        root = self.runtime_dir()
        state_dir = root / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "model-manager-state.json").write_text(json.dumps({
            **manager.PersistentModelManager._idle_transfer(),
            "profile_id": "medium", "status": "downloading", "stage": "downloading", "activate": True,
        }), encoding="utf-8")
        (state_dir / "license-acceptance.json").write_text(json.dumps({"acceptances": [{
            "license_id": "qwen", "version": "model-2.1", "model": "Qwen Test",
            "profile": "medium", "release_version": "1.2.3", "accepted_at": "now",
        }]}), encoding="utf-8")
        service = self.create_manager(root, manifest, payloads, artifact_contents=contents)
        service.retry()
        self.wait_for(service)
        self.assertEqual(service.status()["transfer"]["status"], "completed")
        self.assertEqual(service.status()["catalog"]["active_profile"], "medium")
        service.close()

    def test_switch_and_delete_are_blocked_while_rendering(self) -> None:
        manifest = manager.validate_manifest(valid_manifest())
        payloads = {item["url"]: item["id"].encode() for item in manifest["artifacts"] if item["delivery"] == "download"}
        service = self.create_manager(self.runtime_dir(), manifest, payloads, can_mutate=lambda: False)
        service.registry["profiles"]["medium"] = {"installed_at": "now"}
        with self.assertRaisesRegex(manager.ManagerError, "render"):
            service.switch("medium")
        with self.assertRaisesRegex(manager.ManagerError, "render"):
            service.delete("medium", "medium")
        service.close()

    def test_additional_profile_is_retained_until_explicit_inactive_delete(self) -> None:
        contents = {
            "sd-cli": b"cli", "sd-server": b"server", "transformer": b"transformer",
            "text": b"text", "mmproj": b"projector", "vae": b"vae",
        }
        manifest_data = valid_manifest(contents)
        manifest_data["profiles"]["small"]["max_vram_mib"] = 16383
        manifest = manager.validate_manifest(manifest_data)
        payloads = {item["url"]: contents[item["id"]] for item in manifest["artifacts"] if item["delivery"] == "download"}
        root = self.runtime_dir()
        service = self.create_manager(root, manifest, payloads, artifact_contents=contents)
        license_info = service.catalog()["profiles"][1]["licenses"][0]
        accepted = [{"id": license_info["id"], "version": license_info["version"], "model": license_info["model"]}]
        service.start_install("medium", accepted=accepted, activate=True)
        self.wait_for(service)
        service.start_install("small", accepted=[], activate=False)
        self.wait_for(service)
        transformer = root / "models" / "transformer.bin"
        self.assertTrue(transformer.is_file())
        with self.assertRaisesRegex(manager.ManagerError, "exact profile id"):
            service.delete("small", "wrong")
        state = service.delete("small", "small")
        self.assertTrue(transformer.is_file())
        self.assertEqual(state["catalog"]["active_profile"], "medium")
        self.assertFalse(any(item["id"] == "small" and item["installed"] for item in state["catalog"]["profiles"]))
        service.close()


if __name__ == "__main__":
    unittest.main()
