from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))

import updater


TEST_N = int.from_bytes(base64.b64decode("px5dlI/4IhvriMPq7m5QTTzgYtWcs2ktT4vab3R8dXpayRX2UjVVmOFgSeQwhKA6zofPlT9VTda82JSCqClbFtHXbQvQbVqTcKS97iMRR2mS2j9T7R/jSXHpdrXbR5oyEZ9BV3I1PQgzXhrO1W9bOrQQ7iO+/ZACOryJ/We+1nVztYryP46TmzGK4rmvIEjag3LhilLB8+GolbaU+oTKSdqpnysxSMTrU9ud5ZOxuUtkIFnpXBXpDFMCzpfkrJQHlu70dLyMOITzyILxw4xG5nNDFlrO9ksywsvMzoMdkC/yLGvWR45OTCreeVCh6sVKIWNYJNDv3nFJ3IQYp0bELQ=="), "big")
TEST_E = 65537
TEST_D = int.from_bytes(base64.b64decode("Tsl5Et4g/GuvSkYbTxdA0nkdzFqqysaOLw9fBuai+nuZq22oOC+e0DmIvK1Q1mX384CBs/oszEqts2mog4EjyYlah7VnKPbnxdZVGJz9u24hZrUuav96lxiWGXo5C/O9ISO0mXZldQWVugrnciZSm0VjKfI+S6qF8o0KfSZZCR+JSyheYWGkCBqKYwYBdjgGAwxrSJZIoxKQhdctY3m/mvOs1NE1YznHW7Y7wPPM/3KhyBzl4brkIgb6BCLlETqgV1MtfWW+djn3qfWvr7Qr3nV3f81RYcS9nrmpka+ZmNoROfpBQSMypJnceeCtnsmEqXTSZhFbs1YvMzBAUgs3XQ=="), "big")


def sign(payload: dict) -> dict:
    width = (TEST_N.bit_length() + 7) // 8
    digest_info = updater.SHA256_DIGEST_INFO + hashlib.sha256(updater.canonical_payload(payload)).digest()
    encoded = b"\x00\x01" + b"\xff" * (width - len(digest_info) - 3) + b"\x00" + digest_info
    signature = pow(int.from_bytes(encoded, "big"), TEST_D, TEST_N).to_bytes(width, "big")
    return {"payload": payload, "signature": {"algorithm": "rsa-sha256", "key_id": "test", "value": base64.b64encode(signature).decode()}}


class Response:
    def __init__(self, data: bytes, *, status: int = 200, etag: str | None = None) -> None:
        self.stream = io.BytesIO(data)
        self.status = status
        self.headers = {"ETag": etag} if etag else {}

    def read(self, size: int = -1) -> bytes:
        return self.stream.read(size)


class UpdateTests(unittest.TestCase):
    def test_installed_version_comes_from_build_metadata_or_development_fallback(self) -> None:
        source = (ROOT / "packaging" / "updater.py").read_text(encoding="utf-8")
        self.assertIn('Path(sys.executable).resolve().parent / "version.json"', source)
        self.assertIn('os.environ.get("MLAC_APP_VERSION", "0.0.0-dev")', source)
        self.assertNotRegex(source, r'APP_VERSION\s*=\s*"\d+\.\d+\.\d+"')

    def root(self) -> Path:
        path = ROOT / "tests" / "runtime" / uuid.uuid4().hex
        path.mkdir(parents=True, exist_ok=True)
        return path

    def key(self, root: Path) -> Path:
        path = root / "public.json"
        path.write_text(json.dumps({
            "algorithm": "rsa-sha256", "key_id": "test",
            "modulus": base64.b64encode(TEST_N.to_bytes((TEST_N.bit_length() + 7) // 8, "big")).decode(),
            "exponent": base64.b64encode(TEST_E.to_bytes(3, "big")).decode(),
        }), encoding="utf-8")
        return path

    @staticmethod
    def archive_bytes(name: str = "MLACStudio.exe", content: bytes = b"core") -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr(name, content)
        return output.getvalue()

    def payload(self, data: bytes, *, version: str = "0.3.1", channel: str = "stable", mandatory: bool = False) -> dict:
        return {
            "schema_version": 1, "version": version, "channel": channel,
            "security_mandatory": mandatory, "restart_required": True,
            "changelog": "Safer updates", "data_migration": None,
            "components": [{
                "id": "core", "kind": "core", "version": version,
                "url": f"https://github.com/vuisme/mlacstudio/releases/download/v{version}/MLAC-Studio-core-{version}.zip",
                "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            }],
        }

    def test_signature_accepts_valid_and_rejects_unsigned_or_tampered_metadata(self) -> None:
        root = self.root()
        payload = self.payload(self.archive_bytes())
        envelope = sign(payload)
        self.assertEqual(updater.verify_envelope(envelope, self.key(root))["version"], "0.3.1")
        with self.assertRaises(updater.SignatureError):
            updater.verify_envelope({"payload": payload}, self.key(root))
        envelope["payload"]["version"] = "9.9.9"
        with self.assertRaises(updater.SignatureError):
            updater.verify_envelope(envelope, self.key(root))

    def test_etag_uses_cache_on_not_modified(self) -> None:
        root = self.root()
        envelope = sign(self.payload(self.archive_bytes()))
        raw = json.dumps(envelope).encode()
        calls = []
        def first(request, timeout):
            calls.append(request)
            return Response(raw, etag='"abc"')
        self.assertTrue(updater.fetch_metadata(root, "stable", opener=first, public_key_path=self.key(root)).changed)
        def second(request, timeout):
            calls.append(request)
            raise HTTPError(request.full_url, 304, "Not Modified", {}, None)
        result = updater.fetch_metadata(root, "stable", opener=second, public_key_path=self.key(root))
        self.assertFalse(result.changed)
        self.assertEqual(calls[-1].headers["If-none-match"], '"abc"')

    def test_channels_default_stable_and_opt_in_beta_dev(self) -> None:
        root = self.root()
        self.assertEqual(updater.read_channel(root), "stable")
        for channel in ("beta", "dev"):
            updater.write_channel(root, channel)
            self.assertEqual(updater.read_channel(root), channel)
        with self.assertRaises(updater.UpdateError):
            updater.write_channel(root, "nightly")

    def test_component_selection_and_mandatory_summary(self) -> None:
        data = self.archive_bytes()
        payload = self.payload(data, mandatory=True)
        selected = updater.select_components(payload, {"driver": "552.12", "vram_mib": 12288})
        self.assertEqual([item["id"] for item in selected], ["core"])
        self.assertTrue(updater.update_summary(payload, selected)["security_mandatory"])

    def test_update_metadata_rejects_native_runtime_components(self) -> None:
        payload = self.payload(self.archive_bytes())
        payload["components"].append({
            "id": "nvidia-runtime", "kind": "nvidia-runtime", "version": "0.3.1",
            "url": "https://github.com/vuisme/mlacstudio/releases/download/v0.3.1/nvidia.zip",
            "size": 2, "sha256": "b" * 64,
        })
        with self.assertRaisesRegex(updater.UpdateError, "invalid component kind"):
            updater.validate_payload(payload)

    def test_resume_interruption_and_hash_validation(self) -> None:
        root = self.root()
        data = b"abcdefghij"
        destination = root / "asset.part"
        destination.write_bytes(data[:4])
        requests = []
        def opener(request, timeout):
            requests.append(request)
            return Response(data[4:], status=206)
        updater._download_with_resume("https://example.invalid/a", destination, len(data), hashlib.sha256(data).hexdigest(), opener=opener, retries=1)
        self.assertEqual(destination.read_bytes(), data)
        self.assertEqual(requests[0].headers["Range"], "bytes=4-")
        with self.assertRaises(updater.UpdateError):
            updater._download_with_resume("https://example.invalid/a", root / "bad.part", len(data), "0" * 64, opener=lambda request, timeout: Response(data), retries=1)

    def test_disk_check(self) -> None:
        root = self.root()
        with self.assertRaises(updater.DiskSpaceError):
            updater.check_disk_space(root, 1024, free_bytes=1024)
        updater.check_disk_space(root, 1024, free_bytes=300 * 1024 * 1024)

    def test_clean_install_atomic_recovery_rollback_and_downgrade_protection(self) -> None:
        root = self.root()
        store = updater.ComponentStore(root)
        first_data = self.archive_bytes(content=b"one")
        first = self.payload(first_data, version="0.3.1")
        state = store.install(first, first["components"], opener=lambda request, timeout: Response(first_data))
        self.assertEqual(state["app_version"], "0.3.1")
        active_one = state["components"]["core"]["active"]
        self.assertTrue((Path(active_one["path"]) / "MLACStudio.exe").is_file())

        second_data = self.archive_bytes(content=b"two")
        second = self.payload(second_data, version="0.3.2")
        state = store.install(second, second["components"], opener=lambda request, timeout: Response(second_data))
        self.assertEqual(state["components"]["core"]["previous"]["sha256"], active_one["sha256"])
        rolled = store.rollback()
        self.assertEqual(rolled["components"]["core"]["active"]["sha256"], active_one["sha256"])
        with self.assertRaises(updater.UpdateError):
            store.install(self.payload(first_data, version="0.2.9"), self.payload(first_data, version="0.2.9")["components"], opener=lambda request, timeout: Response(first_data))

        prior = store.active()
        (store.active_path).write_text("{}", encoding="utf-8")
        store.journal_path.write_text(json.dumps({"prior": prior}), encoding="utf-8")
        store.recover()
        self.assertEqual(store.active()["components"]["core"]["active"]["sha256"], active_one["sha256"])

    def test_data_migration_requires_explicit_confirmation(self) -> None:
        root = self.root()
        data = self.archive_bytes()
        payload = self.payload(data)
        payload["data_migration"] = "schema 2"
        with self.assertRaises(updater.UpdateError):
            updater.ComponentStore(root).install(payload, payload["components"], opener=lambda request, timeout: Response(data))


if __name__ == "__main__":
    unittest.main()

