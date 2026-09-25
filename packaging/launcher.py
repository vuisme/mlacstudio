#!/usr/bin/env python3
"""Single-instance Windows tray launcher for MLAC Studio."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from types import ModuleType
from typing import Any, Callable
from urllib.parse import urlparse


MUTEX_NAME = r"Local\MLACStudio.Tray.v1"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "MLACStudio"
LEGACY_RUN_VALUE = "QwenImageStudio"
ERROR_ALREADY_EXISTS = 183


def _resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def _application_dir() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]


def _state_root() -> Path:
    local_app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return local_app_data / "MLACStudio"


def _legacy_state_root() -> Path:
    local_app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return local_app_data / "QwenImageStudio"


def _active_component_path(component_id: str) -> Path | None:
    try:
        active = json.loads((_state_root() / "components" / "active.json").read_text(encoding="utf-8"))
        path = active["components"][component_id]["active"]["path"]
        resolved = Path(path).resolve()
        return resolved if resolved.is_dir() else None
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _runtime_dir() -> Path:
    common = _active_component_path("common-runtime")
    nvidia = _active_component_path("nvidia-runtime")
    search = [str(path) for path in (nvidia, common) if path is not None]
    if search:
        os.environ["PATH"] = os.pathsep.join(search + [os.environ.get("PATH", "")])
    return common or (_application_dir() / "runtime")


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_SOURCE = PROJECT_ROOT / "web"
if str(WEB_SOURCE) not in sys.path:
    sys.path.insert(0, str(WEB_SOURCE))

import server  # noqa: E402  PyInstaller resolves this through the spec's web pathex.
from tray_icon import create_tray_image  # noqa: E402
import updater  # noqa: E402


LEGACY_STATE_FILES = {
    "config.json", "model-manager-state.json", "installed-profiles.json", "license-acceptance.json", "hf-source.json"
}
WEIGHT_SUFFIXES = {".gguf", ".safetensors", ".ckpt", ".pt", ".pth"}


def _remap_legacy_config(path: Path, old_root: Path, new_root: Path) -> None:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(config, dict):
        return
    data_dir = config.get("data_dir")
    if isinstance(data_dir, str):
        try:
            resolved = Path(data_dir).resolve()
            if resolved == (old_root / "data").resolve():
                config["data_dir"] = str((new_root / "data").resolve())
        except OSError:
            pass
    temporary = path.with_suffix(".migration.tmp")
    temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def migrate_legacy_state(old_root: Path | None = None, new_root: Path | None = None) -> bool:
    """Copy mutable legacy state once, excluding model weights and transient work."""
    old_root = Path(old_root or _legacy_state_root())
    new_root = Path(new_root or _state_root())
    marker = new_root / ".legacy-migration-v1.json"
    if marker.exists() or not old_root.is_dir() or old_root.resolve() == new_root.resolve():
        return False
    new_root.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in sorted(LEGACY_STATE_FILES):
        source = old_root / name
        target = new_root / name
        if source.is_file() and not source.is_symlink() and not target.exists():
            shutil.copy2(source, target)
            copied.append(name)
    source_data = old_root / "data"
    target_data = new_root / "data"
    if source_data.is_dir():
        for source in source_data.rglob("*"):
            relative = source.relative_to(source_data)
            if source.is_symlink() or "work" in relative.parts or source.suffix.lower() in WEIGHT_SUFFIXES:
                continue
            target = target_data / relative
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                copied.append(str(Path("data") / relative))
    config_path = new_root / "config.json"
    if config_path.exists():
        _remap_legacy_config(config_path, old_root, new_root)
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(json.dumps({"source": str(old_root), "copied": copied}, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, marker)
    return True


def _message(title: str, message: str, *, error: bool = False) -> None:
    if os.name == "nt":
        flags = 0x10 if error else 0x40
        ctypes.windll.user32.MessageBoxW(None, message, title, flags)
    else:
        print(f"{title}: {message}", file=sys.stderr if error else sys.stdout)


def _confirm(title: str, message: str, *, warning: bool = False) -> bool:
    if os.name == "nt":
        flags = 0x04 | (0x30 if warning else 0x40)
        return int(ctypes.windll.user32.MessageBoxW(None, message, title, flags)) == 6
    return False


def _update_settings() -> dict[str, Any]:
    path = _state_root() / "update-settings.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_update_settings(value: dict[str, Any]) -> None:
    path = _state_root() / "update-settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def maybe_start_update() -> str:
    """Check signed metadata at startup and hand accepted updates to the bootstrap."""
    if not getattr(sys, "frozen", False) or os.environ.get("MLAC_SKIP_UPDATE_CHECK") == "1":
        return "continue"
    settings = _update_settings()
    channel = settings.get("channel", updater.DEFAULT_CHANNEL)
    try:
        result = updater.fetch_metadata(_state_root(), channel)
        payload = result.payload
        if payload is None or not updater.is_newer(str(payload["version"]), updater.APP_VERSION):
            return "continue"
        selected = updater.select_components(payload, updater.detect_nvidia())
        summary = updater.update_summary(payload, selected)
    except updater.UpdateError:
        return "continue"
    mandatory = bool(summary["security_mandatory"])
    if not mandatory and settings.get("skipped_version") == summary["version"]:
        return "continue"
    size_mib = int(summary["size"]) / (1024 * 1024)
    details = [
        f"MLAC Studio {summary['version']} is available on the {summary['channel']} channel.",
        f"Download: {size_mib:.1f} MiB ({', '.join(summary['components'])})",
    ]
    if summary["changelog"]:
        details.extend(["", str(summary["changelog"])])
    if summary["restart_required"]:
        details.extend(["", "MLAC Studio will restart after the update."])
    if summary["data_migration"]:
        details.extend(["", f"Data migration: {summary['data_migration']}", "A separate confirmation will be required."])
    if mandatory:
        details.extend(["", "This signed manifest marks the release as a mandatory security update."])
    accepted = _confirm("MLAC Studio Update", "\n".join(details), warning=mandatory)
    if not accepted:
        if mandatory:
            return "blocked"
        settings["skipped_version"] = summary["version"]
        _write_update_settings(settings)
        return "continue"
    if summary["data_migration"] and not _confirm(
        "MLAC Studio Data Migration",
        "Back up MLAC Studio data before continuing. Approve the signed data/database migration now?",
        warning=True,
    ):
        return "blocked" if mandatory else "continue"
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    bootstrap = local / "Programs" / "MLACStudio" / "MLACStudioBootstrap.exe"
    if not bootstrap.is_file():
        bootstrap = _application_dir() / "MLACStudioBootstrap.exe"
    if not bootstrap.is_file():
        _message("MLAC Studio Update", "The updater executable is missing. Re-run the MLAC Studio bootstrap installer.", error=True)
        return "blocked" if mandatory else "continue"
    cached = _state_root() / "updates" / f"{channel}.json"
    argv = [str(bootstrap), "--manifest-file", str(cached), "--channel", channel]
    if summary["data_migration"]:
        argv.append("--approve-data-migration")
    subprocess.Popen(argv, close_fds=True, **updater_process_options())
    return "updating"


def updater_process_options() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startupinfo}


class WindowsMutex:
    """Own a named Windows mutex until the launcher exits."""

    def __init__(self, handle: Any, kernel32: Any) -> None:
        self.handle = handle
        self.kernel32 = kernel32

    @classmethod
    def acquire(cls, name: str = MUTEX_NAME, *, kernel32: Any | None = None) -> WindowsMutex | None:
        if kernel32 is None:
            if os.name != "nt":
                raise OSError("MLAC Studio tray mode requires Windows")
            kernel32 = ctypes.windll.kernel32
        kernel32.SetLastError(0)
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            raise ctypes.WinError()
        if int(kernel32.GetLastError()) == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return None
        return cls(handle, kernel32)

    def close(self) -> None:
        if self.handle:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None


class StartupRegistry:
    """Manage the per-user Windows startup entry."""

    def __init__(self, *, winreg_module: ModuleType | Any | None = None, command: str | None = None) -> None:
        if winreg_module is None:
            import winreg as winreg_module

        self.winreg = winreg_module
        self.command = command or startup_command()

    def enabled(self) -> bool:
        try:
            with self.winreg.OpenKey(
                self.winreg.HKEY_CURRENT_USER,
                RUN_KEY,
                0,
                self.winreg.KEY_QUERY_VALUE,
            ) as key:
                self.winreg.QueryValueEx(key, RUN_VALUE)
                return True
        except FileNotFoundError:
            return False

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            with self.winreg.CreateKeyEx(
                self.winreg.HKEY_CURRENT_USER,
                RUN_KEY,
                0,
                self.winreg.KEY_SET_VALUE,
            ) as key:
                self.winreg.SetValueEx(key, RUN_VALUE, 0, self.winreg.REG_SZ, self.command)
            return
        try:
            with self.winreg.OpenKey(
                self.winreg.HKEY_CURRENT_USER,
                RUN_KEY,
                0,
                self.winreg.KEY_SET_VALUE,
            ) as key:
                self.winreg.DeleteValue(key, RUN_VALUE)
        except FileNotFoundError:
            pass

    def migrate_legacy(self) -> bool:
        try:
            with self.winreg.OpenKey(
                self.winreg.HKEY_CURRENT_USER, RUN_KEY, 0, self.winreg.KEY_QUERY_VALUE
            ) as key:
                self.winreg.QueryValueEx(key, LEGACY_RUN_VALUE)
        except FileNotFoundError:
            return False
        self.set_enabled(True)
        try:
            with self.winreg.OpenKey(
                self.winreg.HKEY_CURRENT_USER, RUN_KEY, 0, self.winreg.KEY_SET_VALUE
            ) as key:
                self.winreg.DeleteValue(key, LEGACY_RUN_VALUE)
        except FileNotFoundError:
            pass
        return True

    def toggle(self) -> bool:
        enabled = not self.enabled()
        self.set_enabled(enabled)
        return enabled


def startup_command() -> str:
    if getattr(sys, "frozen", False):
        argv = [str(Path(sys.executable).resolve()), "--startup"]
    else:
        argv = [str(Path(sys.executable).resolve()), str(Path(__file__).resolve()), "--startup"]
    return subprocess.list2cmdline(argv)


def _valid_loopback_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or port is None:
        return None
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        return None
    return f"http://127.0.0.1:{port}/"


def write_instance_info(path: Path, url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"url": url}), encoding="utf-8")
    os.replace(temporary, path)


def read_instance_url(
    path: Path,
    fallback: str,
    *,
    attempts: int = 20,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    for attempt in range(attempts):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            url = _valid_loopback_url(value.get("url") if isinstance(value, dict) else None)
            if url:
                return url
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass
        if attempt + 1 < attempts:
            sleep(0.1)
    return fallback


class TrayController:
    """Coordinate tray actions and orderly application shutdown."""

    def __init__(
        self,
        app: Any,
        httpd: Any,
        server_thread: threading.Thread,
        url: str,
        mutex: WindowsMutex,
        instance_path: Path,
        *,
        registry: StartupRegistry | Any | None = None,
        browser_open: Callable[[str], Any] = webbrowser.open,
        pystray_module: ModuleType | Any | None = None,
    ) -> None:
        self.app = app
        self.httpd = httpd
        self.server_thread = server_thread
        self.url = url
        self.mutex = mutex
        self.instance_path = instance_path
        self.registry = registry or StartupRegistry()
        self.browser_open = browser_open
        self.pystray = pystray_module
        self.icon: Any | None = None
        self._shutdown_lock = threading.Lock()
        self._shutdown_complete = False

    def open_studio(self, icon: Any = None, item: Any = None) -> None:
        del icon, item
        self.browser_open(self.url)

    def model_status_text(self, item: Any = None) -> str:
        del item
        state = self.app.model_state()
        status = str(state.get("status") or "unknown").replace("_", " ").title()
        pid = state.get("pid")
        return f"Model: {status}" + (f" (PID {pid})" if pid else "")

    def can_unload(self, item: Any = None) -> bool:
        del item
        state = self.app.model_state()
        return bool(self.app.can_unload_model() and state.get("status") not in {"unloaded", "loading"})

    def unload_model(self, icon: Any = None, item: Any = None) -> None:
        del item
        self.app.unload_model()
        if icon is not None:
            icon.update_menu()

    def startup_checked(self, item: Any = None) -> bool:
        del item
        return self.registry.enabled()

    def toggle_startup(self, icon: Any = None, item: Any = None) -> None:
        del item
        self.registry.toggle()
        if icon is not None:
            icon.update_menu()

    def exit(self, icon: Any = None, item: Any = None) -> None:
        del item
        self.shutdown(icon)

    def _pystray(self) -> Any:
        if self.pystray is None:
            import pystray

            self.pystray = pystray
        return self.pystray

    def build_menu(self) -> Any:
        tray = self._pystray()
        return tray.Menu(
            tray.MenuItem("Open Studio", self.open_studio, default=True),
            tray.Menu.SEPARATOR,
            tray.MenuItem(self.model_status_text, None, enabled=False),
            tray.MenuItem("Unload model", self.unload_model, enabled=self.can_unload),
            tray.MenuItem("Run at Windows startup", self.toggle_startup, checked=self.startup_checked),
            tray.Menu.SEPARATOR,
            tray.MenuItem("Exit", self.exit),
        )

    def run(self) -> None:
        tray = self._pystray()
        self.icon = tray.Icon("MLACStudio", create_tray_image(), "MLAC Studio", self.build_menu())
        self.icon.run()

    def shutdown(self, icon: Any | None = None) -> None:
        with self._shutdown_lock:
            if self._shutdown_complete:
                return
            self._shutdown_complete = True
        try:
            try:
                self.httpd.shutdown()
            finally:
                try:
                    self.httpd.server_close()
                    self.server_thread.join(timeout=5)
                finally:
                    self.app.close()
        finally:
            with contextlib.suppress(OSError):
                self.instance_path.unlink()
            self.mutex.close()
            active_icon = icon or self.icon
            if active_icon is not None:
                active_icon.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MLAC Studio for Windows")
    parser.add_argument("--config", type=Path, default=_state_root() / "config.json")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--update-channel", choices=sorted(updater.CHANNELS), help="Persist the signed update channel")
    parser.add_argument("--startup", action="store_true", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    migrate_legacy_state()
    if args.update_channel:
        updater.write_channel(_state_root(), args.update_channel)
    if os.name == "nt" and getattr(sys, "frozen", False):
        with contextlib.suppress(OSError):
            StartupRegistry().migrate_legacy()
    defaults = server.load_config(args.config)
    port = args.port if args.port is not None else int(defaults.get("port", 8730))
    if not 1 <= port <= 65535:
        _message("MLAC Studio", f"Invalid localhost port: {port}", error=True)
        return 2

    url = f"http://127.0.0.1:{port}/"
    instance_path = _state_root() / "instance.json"
    try:
        mutex = WindowsMutex.acquire()
    except OSError as exc:
        _message("MLAC Studio", f"Could not create the single-instance mutex:\n\n{exc}", error=True)
        return 1
    if mutex is None:
        webbrowser.open(read_instance_url(instance_path, url))
        return 0

    update_action = maybe_start_update()
    if update_action == "updating":
        mutex.close()
        return 0
    if update_action == "blocked":
        mutex.close()
        _message("MLAC Studio Update", "MLAC Studio cannot start until the required update is installed.", error=True)
        return 3

    resources = _resource_root()
    server.STATIC_DIR = resources / "web" / "static"
    server.REPO_ROOT = _application_dir()
    app: Any | None = None
    httpd: Any | None = None
    controller: TrayController | None = None
    try:
        app = server.StudioApp(
            args.config,
            manifest_path=resources / "release-manifest.json",
            runtime_dir=_runtime_dir(),
            model_dir=_state_root() / "models",
            model_manager_path=resources / "model-manager.py",
        )
        httpd = server.StudioServer(("127.0.0.1", port), server.handler_for(app))
        server_thread = threading.Thread(target=httpd.serve_forever, name="mlac-studio-http", daemon=True)
        server_thread.start()
        controller = TrayController(app, httpd, server_thread, url, mutex, instance_path)
        write_instance_info(instance_path, url)
        print(f"MLAC Studio listening on {url}", flush=True)
        if not args.no_browser and not args.startup:
            browser_timer = threading.Timer(0.5, controller.open_studio)
            browser_timer.daemon = True
            browser_timer.start()
        controller.run()
    except OSError as exc:
        _message("MLAC Studio", f"Could not start the application on localhost port {port}:\n\n{exc}", error=True)
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        if controller is not None:
            controller.shutdown()
        else:
            if httpd is not None:
                httpd.server_close()
            if app is not None:
                app.close()
            mutex.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
