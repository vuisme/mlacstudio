from __future__ import annotations

import http.client
import io
import json
import base64
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

from PIL import Image

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web"))

from server import StudioApp, StudioServer, handler_for


class NoopAdapter:
    def run(self, job, on_line, on_progress) -> None:
        raise AssertionError("inference should not run in API security tests")

    def cancel(self, job_id: str) -> None:
        pass


class RecordingModelManager:
    def __init__(self) -> None:
        self.calls = []

    def status(self):
        return {"available": True, "catalog": {"profiles": []}, "transfer": {"status": "idle"}}

    def start_install(self, profile_id, *, accepted, activate=True):
        self.calls.append((profile_id, accepted, activate))
        return self.status()

    def resolve_source(self, body):
        self.calls.append(("resolve_source", body))
        return {"confirmation_id": "preview", "artifacts": []}

    def confirm_source(self, confirmation_id):
        self.calls.append(("confirm_source", confirmation_id))
        return self.status()

    def set_token(self, token):
        self.calls.append(("set_token", token))
        return self.status()

    def test_token(self):
        self.calls.append(("test_token",))
        return {"ok": True, "name": "tester"}

    def remove_token(self):
        self.calls.append(("remove_token",))
        return self.status()

    def close(self) -> None:
        pass


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / "tests" / "runtime" / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"data_dir": "ignored", **{key: __file__ for key in ("sd_cli", "transformer", "text_encoder", "mmproj", "vae")}}), encoding="utf-8")
        self.app = StudioApp(
            self.config,
            data_dir=self.root / "data",
            adapter=NoopAdapter(),
            start_worker=False,
            start_update_check=False,
        )
        self.server = StudioServer(("127.0.0.1", 0), handler_for(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.app.close()
        self.thread.join(timeout=2)

    def request(self, method: str, path: str, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        encoded = None if body is None else json.dumps(body).encode()
        request_headers = {"Host": f"127.0.0.1:{self.port}", **(headers or {})}
        if encoded is not None:
            request_headers.setdefault("Content-Type", "application/json")
            request_headers["Content-Length"] = str(len(encoded))
        connection.request(method, path, encoded, request_headers)
        response = connection.getresponse()
        payload = response.read()
        result = (response.status, dict(response.getheaders()), payload)
        connection.close()
        return result

    def request_raw(self, method: str, path: str, body: bytes, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        request_headers = {
            "Host": f"127.0.0.1:{self.port}", "Content-Length": str(len(body)), **(headers or {})
        }
        connection.request(method, path, body, request_headers)
        response = connection.getresponse()
        payload = response.read()
        result = (response.status, dict(response.getheaders()), payload)
        connection.close()
        return result

    @staticmethod
    def cookie(headers: dict[str, str]) -> str:
        return headers["Set-Cookie"].split(";", 1)[0]

    def setup_admin(self) -> tuple[str, str]:
        status, headers, payload = self.request("GET", "/api/auth/status")
        self.assertEqual(status, 200)
        auth = json.loads(payload)
        preauth = self.cookie(headers)
        status, headers, payload = self.request(
            "POST", "/api/setup",
            {"username": "admin", "password": "correct horse battery staple", "confirm_password": "correct horse battery staple"},
            {"Cookie": preauth, "X-CSRF-Token": auth["csrf"]},
        )
        self.assertEqual(status, 200, payload)
        return self.cookie(headers), json.loads(payload)["csrf"]

    def test_setup_login_assets_public_but_app_and_api_protected(self) -> None:
        self.assertEqual(self.request("GET", "/health")[0], 200)
        self.assertEqual(self.request("GET", "/setup")[0], 200)
        self.assertEqual(self.request("GET", "/static/auth.css")[0], 200)
        self.assertEqual(self.request("GET", "/api/config")[0], 401)
        self.assertEqual(self.request("GET", "/static/app.js")[0], 303)
        self.assertEqual(self.request("GET", "/api/models")[0], 401)

    def test_bootstrap_is_minimal_and_disappears_after_admin_setup(self) -> None:
        status, _, payload = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(set(json.loads(payload)), {"needs_admin", "model_setup_available"})
        self.setup_admin()
        self.assertEqual(self.request("GET", "/api/bootstrap")[0], 404)

    def test_setup_requires_csrf_and_protected_post_requires_session_csrf(self) -> None:
        self.assertEqual(self.request("POST", "/api/setup", {})[0], 403)
        cookie, csrf = self.setup_admin()
        status, _, payload = self.request("GET", "/api/config", headers={"Cookie": cookie})
        self.assertEqual(status, 200, payload)
        config = json.loads(payload)
        self.assertEqual(config["idle_timeout"], 300)
        self.assertEqual(config["model"]["status"], "unloaded")
        body = {"session": "session-1", "settings": {"prompt": "test", "ratio": "1:1", "steps": 20, "seed": 42}}
        self.assertEqual(self.request("POST", "/api/session/save", body, {"Cookie": cookie})[0], 403)
        self.assertEqual(self.request("POST", "/api/session/save", body, {"Cookie": cookie, "X-CSRF-Token": csrf})[0], 200)

    def test_idle_timeout_update_requires_auth_and_csrf(self) -> None:
        cookie, csrf = self.setup_admin()
        self.assertEqual(self.request("POST", "/api/runtime", {"idle_timeout": 45})[0], 401)
        self.assertEqual(self.request(
            "POST", "/api/runtime", {"idle_timeout": 45}, {"Cookie": cookie}
        )[0], 403)
        status, _, payload = self.request(
            "POST", "/api/runtime", {"idle_timeout": 45},
            {"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(json.loads(payload)["idle_timeout"], 45)

    def test_update_apis_require_admin_session_and_csrf(self) -> None:
        cookie, csrf = self.setup_admin()
        self.assertEqual(self.request("GET", "/api/updates/status")[0], 401)
        status, _, payload = self.request("GET", "/api/updates/status", headers={"Cookie": cookie})
        self.assertEqual(status, 200, payload)
        self.assertEqual(json.loads(payload)["channel"], "stable")
        self.assertEqual(self.request("POST", "/api/updates/check", {}, {"Cookie": cookie})[0], 403)

        cases = [
            ("/api/updates/check", "start_check", {}),
            ("/api/updates/channel", "set_channel", {"channel": "beta"}),
            ("/api/updates/skip", "skip", {"version": "0.3.2"}),
            ("/api/updates/install", "start_install", {"allow_data_migration": True}),
            ("/api/updates/rollback", "start_rollback", {}),
            ("/api/updates/restart", "restart", {}),
        ]
        for path, method, body in cases:
            with self.subTest(path=path), mock.patch.object(
                self.app.updates, method, return_value={"status": "ok"}
            ) as action:
                status, _, payload = self.request(
                    "POST", path, body, {"Cookie": cookie, "X-CSRF-Token": csrf}
                )
                self.assertIn(status, {200, 202}, payload)
                action.assert_called_once()

    def test_model_management_is_authenticated_csrf_protected_and_has_no_path_api(self) -> None:
        cookie, csrf = self.setup_admin()
        self.assertEqual(self.request("GET", "/api/models")[0], 401)
        self.assertEqual(self.request("GET", "/api/models", headers={"Cookie": cookie})[0], 200)
        self.assertEqual(self.request(
            "POST", "/api/models/install", {"profile_id": "anything"}, {"Cookie": cookie}
        )[0], 403)
        self.assertEqual(self.request(
            "POST", "/api/models/install", {"profile_id": "anything"},
            {"Cookie": cookie, "X-CSRF-Token": csrf},
        )[0], 503)
        fake = RecordingModelManager()
        self.app.model_manager = fake
        status, _, payload = self.request(
            "POST", "/api/models/install",
            {"profile_id": "medium", "accepted_licenses": [{"id": "qwen", "version": "2", "model": "Qwen"}]},
            {"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(fake.calls, [("medium", [{"id": "qwen", "version": "2", "model": "Qwen"}], True)])
        self.app.model_manager = None
        self.assertEqual(self.request(
            "POST", "/api/paths", {"paths": {"transformer": "C:\\arbitrary.gguf"}},
            {"Cookie": cookie, "X-CSRF-Token": csrf},
        )[0], 404)

    def test_hf_source_and_token_apis_are_admin_session_and_csrf_protected(self) -> None:
        cookie, csrf = self.setup_admin()
        fake = RecordingModelManager()
        self.app.model_manager = fake
        source = {"repo_id": "example/repo", "revision": "a" * 40, "files": {"transformer": "model.gguf"}}
        self.assertEqual(self.request("POST", "/api/models/source/resolve", source)[0], 401)
        self.assertEqual(self.request(
            "POST", "/api/models/source/resolve", source, {"Cookie": cookie}
        )[0], 403)
        status, _, payload = self.request(
            "POST", "/api/models/source/resolve", source,
            {"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(json.loads(payload)["confirmation_id"], "preview")
        self.assertEqual(self.request(
            "POST", "/api/models/hf-token/set", {"token": "hf_secret"},
            {"Cookie": cookie, "X-CSRF-Token": csrf},
        )[0], 200)
        self.assertIn(("set_token", "hf_secret"), fake.calls)

    def test_cross_origin_and_bad_host_are_rejected(self) -> None:
        self.assertEqual(self.request("GET", "/health", headers={"Host": "evil.example"})[0], 403)
        status = self.request(
            "POST", "/api/login", {},
            {"Origin": "http://evil.example", "Content-Type": "application/json"},
        )[0]
        self.assertEqual(status, 403)

    def test_upload_requires_csrf_and_media_requires_login(self) -> None:
        cookie, csrf = self.setup_admin()
        image = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        headers = {"Cookie": cookie, "X-Filename": "reference.png"}
        self.assertEqual(self.request_raw("POST", "/api/upload?session=session-1", image, headers)[0], 403)
        status, _, payload = self.request_raw(
            "POST", "/api/upload?session=session-1", image, {**headers, "X-CSRF-Token": csrf}
        )
        self.assertEqual(status, 200, payload)
        media_id = json.loads(payload)["id"]
        self.assertEqual(self.request("GET", f"/media/uploads/{media_id}")[0], 401)
        self.assertEqual(self.request("GET", f"/media/uploads/{media_id}", headers={"Cookie": cookie})[0], 200)

    def test_reference_apis_add_list_reorder_role_and_remove(self) -> None:
        cookie, csrf = self.setup_admin()
        auth = {"Cookie": cookie, "X-CSRF-Token": csrf}
        first_headers = {**auth, "X-Filename": "base.png"}
        status, _, payload = self.request_raw(
            "POST", "/api/upload?session=session-1", self.png(2, 2, (1, 2, 3), "RGB"), first_headers
        )
        self.assertEqual(status, 200, payload)
        base = json.loads(payload)
        second_headers = {**auth, "X-Filename": "style.png", "X-Reference-Role": "style"}
        status, _, payload = self.request_raw(
            "POST", "/api/references/add?session=session-1", self.png(2, 2, (4, 5, 6), "RGB"), second_headers
        )
        self.assertEqual(status, 200, payload)
        style = json.loads(payload)

        status, _, payload = self.request("GET", "/api/references?session=session-1", headers={"Cookie": cookie})
        self.assertEqual(status, 200, payload)
        self.assertEqual([item["role"] for item in json.loads(payload)], ["base", "style"])
        self.assertEqual(self.request(
            "POST", "/api/references/reorder",
            {"session": "session-1", "ordered_ids": [style["id"], base["id"]]},
            {"Cookie": cookie},
        )[0], 403)
        status, _, payload = self.request(
            "POST", "/api/references/reorder",
            {"session": "session-1", "ordered_ids": [style["id"], base["id"]]}, auth,
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual([item["id"] for item in json.loads(payload)], [style["id"], base["id"]])

        status, _, payload = self.request(
            "POST", "/api/references/role",
            {"session": "session-1", "id": style["id"], "role": "base"}, auth,
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(json.loads(payload)["role"], "base")
        status, _, payload = self.request(
            "POST", "/api/references/remove", {"session": "session-1", "id": base["id"]}, auth,
        )
        self.assertEqual(status, 200, payload)
        remaining = json.loads(payload)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["id"], style["id"])

    def test_config_reports_reference_and_rgba_capabilities(self) -> None:
        cookie, _ = self.setup_admin()
        status, _, payload = self.request("GET", "/api/config", headers={"Cookie": cookie})
        self.assertEqual(status, 200, payload)
        config = json.loads(payload)
        self.assertEqual(config["capabilities"]["max_references"], 10)
        self.assertIn("multi_reference", config["capabilities"])
        self.assertIn("rgba", config["capabilities"])
        self.assertIn("base", config["reference_roles"])

    @staticmethod
    def png(width: int, height: int, color: int | tuple[int, ...] = 255, mode: str = "L") -> bytes:
        output = io.BytesIO()
        Image.new(mode, (width, height), color).save(output, format="PNG")
        return output.getvalue()

    def test_mask_api_requires_auth_csrf_and_matching_png_geometry(self) -> None:
        cookie, csrf = self.setup_admin()
        source = self.png(2, 2, (10, 20, 30), "RGB")
        upload_headers = {"Cookie": cookie, "X-Filename": "reference.png", "X-CSRF-Token": csrf}
        self.assertEqual(
            self.request_raw("POST", "/api/upload?session=session-1", source, upload_headers)[0], 200
        )

        mask = self.png(2, 2)
        mask_headers = {"Content-Type": "image/png"}
        self.assertEqual(self.request_raw("POST", "/api/mask?session=session-1", mask, mask_headers)[0], 401)
        self.assertEqual(self.request_raw(
            "POST", "/api/mask?session=session-1", mask, {**mask_headers, "Cookie": cookie}
        )[0], 403)

        authorized = {**mask_headers, "Cookie": cookie, "X-CSRF-Token": csrf, "X-Mask-Feather": "25"}
        status, _, payload = self.request_raw(
            "POST", "/api/mask?session=session-1", self.png(3, 2), authorized
        )
        self.assertEqual(status, 400, payload)
        self.assertIn("dimensions", json.loads(payload)["error"])
        self.assertEqual(self.request_raw(
            "POST", "/api/mask?session=session-1", b"not a png", authorized
        )[0], 400)
        status, _, payload = self.request_raw(
            "POST", "/api/mask?session=session-1", self.png(2, 2, (255, 0, 0), "RGB"), authorized
        )
        self.assertEqual(status, 400, payload)
        self.assertIn("grayscale", json.loads(payload)["error"])
        oversized = {**authorized, "Content-Length": str(25 * 1024 * 1024 + 1)}
        self.assertEqual(self.request_raw(
            "POST", "/api/mask?session=session-1", b"x", oversized
        )[0], 400)

        status, _, payload = self.request_raw("POST", "/api/mask?session=session-1", mask, authorized)
        self.assertEqual(status, 200, payload)
        saved = json.loads(payload)["mask"]
        self.assertEqual(saved["feather"], 25)
        self.assertEqual(self.request("GET", saved["url"])[0], 401)
        self.assertEqual(self.request("GET", saved["url"], headers={"Cookie": cookie})[0], 200)

    def test_empty_mask_removes_persisted_selection(self) -> None:
        cookie, csrf = self.setup_admin()
        headers = {"Cookie": cookie, "X-Filename": "reference.png", "X-CSRF-Token": csrf}
        self.assertEqual(self.request_raw(
            "POST", "/api/upload?session=session-1", self.png(2, 2, (1, 2, 3), "RGB"), headers
        )[0], 200)
        mask_headers = {"Cookie": cookie, "X-CSRF-Token": csrf, "Content-Type": "image/png"}
        self.assertEqual(self.request_raw(
            "POST", "/api/mask?session=session-1", self.png(2, 2, 255), mask_headers
        )[0], 200)
        status, _, payload = self.request_raw(
            "POST", "/api/mask?session=session-1", self.png(2, 2, 0), mask_headers
        )
        self.assertEqual(status, 200, payload)
        self.assertIsNone(json.loads(payload)["mask"])


if __name__ == "__main__":
    unittest.main()
