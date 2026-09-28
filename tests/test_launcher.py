from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))

import launcher


class FakeKernel32:
    def __init__(self, *, last_error: int = 0) -> None:
        self.last_error = last_error
        self.closed: list[int] = []

    def SetLastError(self, value: int) -> None:
        del value

    def CreateMutexW(self, security, initial_owner: bool, name: str) -> int:
        del security, initial_owner
        self.name = name
        return 42

    def GetLastError(self) -> int:
        return self.last_error

    def CloseHandle(self, handle: int) -> None:
        self.closed.append(handle)


class FakeKey:
    def __init__(self, registry: FakeWinreg) -> None:
        self.registry = registry

    def __enter__(self) -> FakeKey:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        del exc_type, exc_value, traceback


class FakeWinreg:
    HKEY_CURRENT_USER = object()
    KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def OpenKey(self, root, path: str, reserved: int, access: int) -> FakeKey:
        del root, path, reserved, access
        return FakeKey(self)

    def CreateKeyEx(self, root, path: str, reserved: int, access: int) -> FakeKey:
        del root, path, reserved, access
        return FakeKey(self)

    def QueryValueEx(self, key: FakeKey, name: str) -> tuple[str, int]:
        del key
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key: FakeKey, name: str, reserved: int, value_type: int, value: str) -> None:
        del key, reserved, value_type
        self.values[name] = value

    def DeleteValue(self, key: FakeKey, name: str) -> None:
        del key
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


class FakeMenu(tuple):
    SEPARATOR = object()

    def __new__(cls, *items):
        return super().__new__(cls, items)


class FakeMenuItem:
    def __init__(self, text, action, **kwargs) -> None:
        self.text = text
        self.action = action
        self.options = kwargs


class FakeIcon:
    def __init__(self, name, image, title, menu) -> None:
        self.name = name
        self.image = image
        self.title = title
        self.menu = menu
        self.ran = False
        self.stopped = False
        self.updated = False

    def run(self) -> None:
        self.ran = True

    def stop(self) -> None:
        self.stopped = True

    def update_menu(self) -> None:
        self.updated = True


class FakePystray:
    Menu = FakeMenu
    MenuItem = FakeMenuItem
    Icon = FakeIcon


class LauncherTests(unittest.TestCase):
    @staticmethod
    def runtime_dir() -> Path:
        path = ROOT / "tests" / "runtime" / uuid.uuid4().hex
        path.mkdir(parents=True, exist_ok=True)
        return path

    def controller(self, instance_path: Path, **kwargs):
        app = kwargs.pop("app", mock.Mock())
        app.model_state.return_value = {"status": "ready", "pid": 1234}
        app.can_unload_model.return_value = True
        app.updates.status.return_value = {"status": "up_to_date", "stage": "MLAC Studio is up to date", "latest": None}
        return launcher.TrayController(
            app,
            kwargs.pop("httpd", mock.Mock()),
            kwargs.pop("server_thread", mock.Mock()),
            "http://127.0.0.1:8730/",
            kwargs.pop("mutex", mock.Mock()),
            instance_path,
            registry=kwargs.pop("registry", mock.Mock()),
            browser_open=kwargs.pop("browser_open", mock.Mock()),
            pystray_module=kwargs.pop("pystray_module", FakePystray),
        )

    def test_runtime_download_directory_is_mutable_state_not_application_bundle(self) -> None:
        root = self.runtime_dir()
        legacy_common = root / "components" / "common-runtime" / "old"
        with (
            mock.patch.object(launcher, "_state_root", return_value=root),
            mock.patch.object(
                launcher,
                "_active_component_path",
                side_effect=lambda ident: legacy_common if ident == "common-runtime" else None,
            ),
            mock.patch.dict(launcher.os.environ, {"PATH": "base"}, clear=False),
        ):
            self.assertEqual(launcher._runtime_dir(), root / "runtime")
            self.assertIn(str(legacy_common), launcher.os.environ["PATH"])

    def test_named_mutex_rejects_second_instance_and_closes_its_handle(self) -> None:
        kernel32 = FakeKernel32(last_error=launcher.ERROR_ALREADY_EXISTS)
        self.assertIsNone(launcher.WindowsMutex.acquire(kernel32=kernel32))
        self.assertEqual(kernel32.name, launcher.MUTEX_NAME)
        self.assertEqual(kernel32.closed, [42])

        kernel32 = FakeKernel32()
        mutex = launcher.WindowsMutex.acquire(kernel32=kernel32)
        self.assertIsNotNone(mutex)
        mutex.close()
        mutex.close()
        self.assertEqual(kernel32.closed, [42])

    def test_startup_registry_is_opt_in_and_toggles_hkcu_run_value(self) -> None:
        winreg = FakeWinreg()
        registry = launcher.StartupRegistry(winreg_module=winreg, command='"C:\\Studio\\MLACStudio.exe" --startup')
        self.assertFalse(registry.enabled())
        self.assertTrue(registry.toggle())
        self.assertTrue(registry.enabled())
        self.assertEqual(winreg.values[launcher.RUN_VALUE], registry.command)
        self.assertFalse(registry.toggle())
        self.assertFalse(registry.enabled())

    def test_legacy_startup_registry_is_replaced(self) -> None:
        winreg = FakeWinreg()
        winreg.values[launcher.LEGACY_RUN_VALUE] = '"C:\\Old\\QwenImageStudio.exe" --startup'
        registry = launcher.StartupRegistry(winreg_module=winreg, command='"C:\\MLAC\\MLACStudio.exe" --startup')
        self.assertTrue(registry.migrate_legacy())
        self.assertNotIn(launcher.LEGACY_RUN_VALUE, winreg.values)
        self.assertEqual(winreg.values[launcher.RUN_VALUE], registry.command)

    def test_legacy_data_migration_preserves_user_state_but_not_weights(self) -> None:
        root = self.runtime_dir()
        legacy = root / "QwenImageStudio"
        current = root / "MLACStudio"
        (legacy / "data" / "gallery").mkdir(parents=True)
        (legacy / "data" / "work").mkdir(parents=True)
        (legacy / "models").mkdir(parents=True)
        external = root / "external-model.gguf"
        external.write_bytes(b"external")
        (legacy / "config.json").write_text(json.dumps({
            "data_dir": str(legacy / "data"), "transformer": str(external)
        }), encoding="utf-8")
        (legacy / "data" / "studio.db").write_bytes(b"db")
        (legacy / "data" / "gallery" / "take.png").write_bytes(b"png")
        (legacy / "data" / "work" / "temp.png").write_bytes(b"temp")
        (legacy / "models" / "weight.gguf").write_bytes(b"weight")

        self.assertTrue(launcher.migrate_legacy_state(legacy, current))
        migrated = json.loads((current / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(migrated["data_dir"], str((current / "data").resolve()))
        self.assertEqual(migrated["transformer"], str(external))
        self.assertTrue((current / "data" / "studio.db").is_file())
        self.assertTrue((current / "data" / "gallery" / "take.png").is_file())
        self.assertFalse((current / "data" / "work" / "temp.png").exists())
        self.assertFalse((current / "models").exists())
        self.assertFalse(launcher.migrate_legacy_state(legacy, current))

    def test_tray_menu_exposes_status_unload_startup_and_exit(self) -> None:
        controller = self.controller(self.runtime_dir() / "instance.json")
        menu = controller.build_menu()
        items = [item for item in menu if isinstance(item, FakeMenuItem)]
        self.assertEqual([item.text for item in items if isinstance(item.text, str)], [
            "Open Studio", "Unload model", "Check for updates", "Open Updates", "Run at Windows startup", "Exit"
        ])
        statuses = [item for item in items if callable(item.text)]
        self.assertEqual(statuses[0].text(), "Model: Ready (PID 1234)")
        self.assertEqual(statuses[1].text(), "Updates: MLAC Studio is up to date")
        self.assertTrue(all(not item.options["enabled"] for item in statuses))

        controller.run()
        self.assertTrue(controller.icon.ran)
        self.assertEqual(controller.icon.title, "MLAC Studio")

    def test_tray_actions_open_browser_unload_and_update_checked_state(self) -> None:
        browser_open = mock.Mock()
        registry = mock.Mock()
        controller = self.controller(
            self.runtime_dir() / "instance.json", browser_open=browser_open, registry=registry
        )
        icon = FakeIcon("name", None, "title", None)
        controller.open_studio()
        controller.unload_model(icon)
        controller.toggle_startup(icon)
        browser_open.assert_called_once_with("http://127.0.0.1:8730/")
        controller.app.unload_model.assert_called_once_with()
        registry.toggle.assert_called_once_with()
        self.assertTrue(icon.updated)

    def test_exit_shuts_down_server_closes_app_and_releases_mutex(self) -> None:
        instance_path = self.runtime_dir() / "instance.json"
        instance_path.write_text("{}", encoding="utf-8")
        httpd = mock.Mock()
        app = mock.Mock()
        app.model_state.return_value = {"status": "unloaded", "pid": None}
        app.can_unload_model.return_value = False
        server_thread = mock.Mock()
        mutex = mock.Mock()
        icon = FakeIcon("name", None, "title", None)
        controller = self.controller(
            instance_path, app=app, httpd=httpd, server_thread=server_thread, mutex=mutex
        )

        controller.exit(icon)
        controller.shutdown(icon)

        httpd.shutdown.assert_called_once_with()
        httpd.server_close.assert_called_once_with()
        server_thread.join.assert_called_once_with(timeout=5)
        app.close.assert_called_once_with()
        mutex.close.assert_called_once_with()
        self.assertTrue(icon.stopped)
        self.assertFalse(instance_path.exists())

    def test_second_launch_opens_recorded_loopback_url_without_starting_server(self) -> None:
        root = self.runtime_dir()
        config = root / "config.json"
        config.write_text(json.dumps({"port": 8730}), encoding="utf-8")
        instance = root / "instance.json"
        instance.write_text(json.dumps({"url": "http://127.0.0.1:9012/"}), encoding="utf-8")
        with (
            mock.patch.object(sys, "argv", ["launcher.py", "--config", str(config)]),
            mock.patch.object(launcher, "_state_root", return_value=root),
            mock.patch.object(launcher.WindowsMutex, "acquire", return_value=None),
            mock.patch.object(launcher.webbrowser, "open") as open_browser,
            mock.patch.object(launcher.server, "StudioApp") as studio_app,
        ):
            self.assertEqual(launcher.main(), 0)
        open_browser.assert_called_once_with("http://127.0.0.1:9012/")
        studio_app.assert_not_called()

    def test_missing_config_starts_setup_capable_web_app(self) -> None:
        root = self.runtime_dir()
        config = root / "missing-config.json"
        app = mock.Mock()
        httpd = mock.Mock()
        controller = mock.Mock()
        with (
            mock.patch.object(sys, "argv", ["launcher.py", "--config", str(config), "--no-browser"]),
            mock.patch.object(launcher, "_state_root", return_value=root),
            mock.patch.object(launcher, "_resource_root", return_value=root / "resources"),
            mock.patch.object(launcher, "_application_dir", return_value=root / "app"),
            mock.patch.object(launcher.WindowsMutex, "acquire", return_value=mock.Mock()),
            mock.patch.object(launcher.server, "StudioApp", return_value=app) as studio_app,
            mock.patch.object(launcher.server, "StudioServer", return_value=httpd),
            mock.patch.object(launcher.server, "STATIC_DIR", launcher.server.STATIC_DIR),
            mock.patch.object(launcher.server, "REPO_ROOT", launcher.server.REPO_ROOT),
            mock.patch.object(launcher, "TrayController", return_value=controller),
            mock.patch.object(launcher, "_message") as message,
        ):
            self.assertEqual(launcher.main(), 0)
        studio_app.assert_called_once()
        self.assertEqual(studio_app.call_args.args[0], config)
        controller.run.assert_called_once_with()
        controller.shutdown.assert_called_once_with()
        message.assert_not_called()

    def test_instance_record_never_redirects_outside_loopback(self) -> None:
        path = self.runtime_dir() / "instance.json"
        path.write_text(json.dumps({"url": "https://example.com/"}), encoding="utf-8")
        self.assertEqual(
            launcher.read_instance_url(path, "http://127.0.0.1:8730/", attempts=1),
            "http://127.0.0.1:8730/",
        )

    def test_signed_mandatory_update_blocks_startup_when_declined(self) -> None:
        payload = {
            "version": "0.3.3", "channel": "stable", "security_mandatory": True,
            "restart_required": True, "changelog": "security fix", "data_migration": None,
            "components": [{"id": "core", "kind": "core", "size": 10}],
        }
        with (
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.object(launcher.updater, "fetch_metadata", return_value=launcher.updater.UpdateCheck(payload, True, '"e"')),
            mock.patch.object(launcher, "_confirm", return_value=False),
            mock.patch.object(launcher, "_state_root", return_value=self.runtime_dir()),
        ):
            self.assertEqual(launcher.maybe_start_update(), "blocked")


if __name__ == "__main__":
    unittest.main()

