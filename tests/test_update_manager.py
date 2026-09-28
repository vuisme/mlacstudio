from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
import time
import unittest
import uuid
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))
sys.path.insert(0, str(ROOT / "web"))

import updater
from updates import BUSY_STATES, UpdateManager


TEST_N = int.from_bytes(base64.b64decode("px5dlI/4IhvriMPq7m5QTTzgYtWcs2ktT4vab3R8dXpayRX2UjVVmOFgSeQwhKA6zofPlT9VTda82JSCqClbFtHXbQvQbVqTcKS97iMRR2mS2j9T7R/jSXHpdrXbR5oyEZ9BV3I1PQgzXhrO1W9bOrQQ7iO+/ZACOryJ/We+1nVztYryP46TmzGK4rmvIEjag3LhilLB8+GolbaU+oTKSdqpnysxSMTrU9ud5ZOxuUtkIFnpXBXpDFMCzpfkrJQHlu70dLyMOITzyILxw4xG5nNDFlrO9ksywsvMzoMdkC/yLGvWR45OTCreeVCh6sVKIWNYJNDv3nFJ3IQYp0bELQ=="), "big")
TEST_E = 65537
TEST_D = int.from_bytes(base64.b64decode("Tsl5Et4g/GuvSkYbTxdA0nkdzFqqysaOLw9fBuai+nuZq22oOC+e0DmIvK1Q1mX384CBs/oszEqts2mog4EjyYlah7VnKPbnxdZVGJz9u24hZrUuav96lxiWGXo5C/O9ISO0mXZldQWVugrnciZSm0VjKfI+S6qF8o0KfSZZCR+JSyheYWGkCBqKYwYBdjgGAwxrSJZIoxKQhdctY3m/mvOs1NE1YznHW7Y7wPPM/3KhyBzl4brkIgb6BCLlETqgV1MtfWW+djn3qfWvr7Qr3nV3f81RYcS9nrmpka+ZmNoROfpBQSMypJnceeCtnsmEqXTSZhFbs1YvMzBAUgs3XQ=="), "big")


def signed(payload: dict) -> bytes:
    width = (TEST_N.bit_length() + 7) // 8
    digest_info = updater.SHA256_DIGEST_INFO + hashlib.sha256(updater.canonical_payload(payload)).digest()
    encoded = b"\x00\x01" + b"\xff" * (width - len(digest_info) - 3) + b"\x00" + digest_info
    signature = pow(int.from_bytes(encoded, "big"), TEST_D, TEST_N).to_bytes(width, "big")
    envelope = {
        "payload": payload,
        "signature": {"algorithm": "rsa-sha256", "key_id": "test", "value": base64.b64encode(signature).decode()},
    }
    return json.dumps(envelope).encode()


class Response:
    def __init__(self, data: bytes, *, etag: str | None = None) -> None:
        self.stream = io.BytesIO(data)
        self.status = 200
        self.headers = {"ETag": etag} if etag else {}

    def read(self, size: int = -1) -> bytes:
        return self.stream.read(size)


class UpdateManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / "tests" / "runtime" / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.key = self.root / "public.json"
        self.key.write_text(json.dumps({
            "algorithm": "rsa-sha256",
            "key_id": "test",
            "modulus": base64.b64encode(TEST_N.to_bytes((TEST_N.bit_length() + 7) // 8, "big")).decode(),
            "exponent": base64.b64encode(TEST_E.to_bytes(3, "big")).decode(),
        }), encoding="utf-8")

    @staticmethod
    def archive(content: bytes) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as bundle:
            bundle.writestr("MLACStudio.exe", content)
        return output.getvalue()

    @staticmethod
    def payload(data: bytes, version: str, channel: str = "stable") -> dict:
        return {
            "schema_version": 1,
            "version": version,
            "channel": channel,
            "security_mandatory": False,
            "restart_required": True,
            "changelog": f"Signed fixture {version}",
            "data_migration": None,
            "components": [{
                "id": "core",
                "kind": "core",
                "version": version,
                "url": f"https://github.com/vuisme/mlacstudio/releases/download/v{version}/MLAC-Studio-core-{version}.zip",
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }],
        }

    def wait(self, manager: UpdateManager) -> dict:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = manager.status()
            if state["status"] not in BUSY_STATES:
                return state
            time.sleep(0.01)
        self.fail(f"update operation did not finish: {manager.status()}")

    def test_signed_031_to_032_install_progress_and_rollback(self) -> None:
        baseline_data = self.archive(b"0.3.1")
        target_data = self.archive(b"0.3.2")
        baseline = self.payload(baseline_data, "0.3.1")
        target = self.payload(target_data, "0.3.2")
        store = updater.ComponentStore(self.root)
        store.install(baseline, baseline["components"], opener=lambda request, timeout: Response(baseline_data))

        envelope = signed(target)

        def opener(request, timeout):
            if str(request.full_url).endswith("MLAC-Studio-stable.json"):
                return Response(envelope, etag='"fixture-032"')
            return Response(target_data)

        manager = UpdateManager(
            self.root,
            current_version="0.3.1",
            opener=opener,
            public_key_path=self.key,
            start_check=False,
        )
        manager.start_check()
        checked = self.wait(manager)
        self.assertEqual(checked["status"], "available")
        self.assertEqual(checked["latest"]["version"], "0.3.2")

        manager.start_install()
        installed = self.wait(manager)
        self.assertEqual(installed["status"], "restart_pending")
        self.assertEqual(installed["installed_version"], "0.3.2")
        self.assertEqual(installed["bytes_downloaded"], len(target_data))
        self.assertEqual(store.active()["components"]["core"]["previous"]["version"], "0.3.1")

        manager.start_rollback()
        rolled = self.wait(manager)
        self.assertEqual(rolled["installed_version"], "0.3.1")
        self.assertEqual(store.active()["components"]["core"]["active"]["version"], "0.3.1")

    def test_skip_channel_serialization_and_error_reporting(self) -> None:
        data = self.archive(b"0.3.2")
        payload = self.payload(data, "0.3.2")
        manager = UpdateManager(
            self.root,
            current_version="0.3.1",
            opener=lambda request, timeout: Response(signed(payload)),
            public_key_path=self.key,
            start_check=False,
        )
        manager.start_check()
        self.assertEqual(self.wait(manager)["status"], "available")
        self.assertEqual(manager.skip("0.3.2")["status"], "skipped")
        self.assertEqual(updater.read_settings(self.root)["skipped_version"], "0.3.2")

        manager.set_channel("beta")
        with self.assertRaisesRegex(updater.UpdateError, "already running"):
            manager.start_install()
        failed = self.wait(manager)
        self.assertEqual(failed["status"], "error")
        self.assertIn("channel", failed["error"].lower())


if __name__ == "__main__":
    unittest.main()
