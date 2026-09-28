#!/usr/bin/env python3
"""Serialized signed application updates for the local Web UI."""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.request import urlopen

import updater


BUSY_STATES = {"checking", "downloading", "installing", "rolling_back", "restarting"}


class UpdateManager:
    def __init__(
        self,
        state_root: Path,
        *,
        current_version: str = updater.APP_VERSION,
        opener: Callable[..., Any] = urlopen,
        public_key_path: Path = updater.PUBLIC_KEY_PATH,
        can_mutate: Callable[[], bool] | None = None,
        restart_handler: Callable[[], None] | None = None,
        start_check: bool = True,
    ) -> None:
        self.state_root = Path(state_root)
        self.current_version = current_version
        self.opener = opener
        self.public_key_path = Path(public_key_path)
        self.can_mutate = can_mutate or (lambda: True)
        self.restart_handler = restart_handler
        self.store = updater.ComponentStore(self.state_root)
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._subscribers: set[queue.Queue[str]] = set()
        self._payload: dict[str, Any] | None = None
        self._state: dict[str, Any] = {
            "status": "idle",
            "stage": "Ready",
            "current_version": current_version,
            "installed_version": self._installed_version(),
            "latest": None,
            "channel": updater.read_channel(self.state_root),
            "skipped_version": updater.read_settings(self.state_root).get("skipped_version"),
            "bytes_downloaded": 0,
            "bytes_total": 0,
            "component": None,
            "error": None,
            "checked_at": None,
            "restart_required": False,
            "restart_available": restart_handler is not None,
            "rollback_version": self._rollback_version(),
            "startup_check_pending": bool(start_check),
        }
        if start_check:
            self.start_check(startup=True)

    def set_restart_handler(self, handler: Callable[[], None] | None) -> None:
        with self._lock:
            self.restart_handler = handler
            self._state["restart_available"] = handler is not None
        self._broadcast()

    def close(self) -> None:
        worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout=0.1)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def subscribe(self) -> queue.Queue[str]:
        subscriber: queue.Queue[str] = queue.Queue(maxsize=64)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[str]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def start_check(self, *, startup: bool = False) -> dict[str, Any]:
        return self._start("check", lambda: self._check(startup=startup))

    def set_channel(self, channel: str) -> dict[str, Any]:
        if channel not in updater.CHANNELS:
            raise updater.UpdateError("invalid update channel")
        with self._lock:
            self._ensure_idle()
            updater.write_channel(self.state_root, channel)
            self._payload = None
            self._state.update({
                "channel": channel,
                "skipped_version": None,
                "latest": None,
                "status": "idle",
                "stage": "Channel changed",
                "error": None,
            })
        self._broadcast()
        return self.start_check()

    def skip(self, version: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._ensure_idle()
            latest = self._state.get("latest")
            if not latest:
                raise updater.UpdateError("no update is available to skip")
            target = str(latest["version"])
            if version and version != target:
                raise updater.UpdateError("the requested update is no longer current")
            if latest.get("security_mandatory"):
                raise updater.UpdateError("mandatory security updates cannot be skipped")
            settings = updater.read_settings(self.state_root)
            settings.update({"channel": self._state["channel"], "skipped_version": target})
            updater.write_settings(self.state_root, settings)
            self._state.update({"status": "skipped", "stage": f"Skipped {target}", "skipped_version": target, "error": None})
        self._broadcast()
        return self.status()

    def start_install(self, *, allow_data_migration: bool = False) -> dict[str, Any]:
        return self._start("install", lambda: self._install(allow_data_migration))

    def start_rollback(self) -> dict[str, Any]:
        return self._start("rollback", self._rollback)

    def restart(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_idle()
            if not self._state.get("restart_required"):
                raise updater.UpdateError("no restart is pending")
            handler = self.restart_handler
            if handler is None:
                raise updater.UpdateError("the MLAC Studio bootstrap is unavailable")
            self._state.update({"status": "restarting", "stage": "Restarting through the bootstrap", "error": None})
        self._broadcast()
        try:
            handler()
        except Exception as exc:
            self._fail(exc)
            raise updater.UpdateError(str(exc)) from exc
        return self.status()

    def _start(self, name: str, target: Callable[[], None]) -> dict[str, Any]:
        with self._lock:
            self._ensure_idle()
            self._state.update({
                "status": "checking" if name == "check" else ("rolling_back" if name == "rollback" else "downloading"),
                "stage": "Checking signed metadata" if name == "check" else ("Preparing rollback" if name == "rollback" else "Preparing download"),
                "bytes_downloaded": 0,
                "bytes_total": 0,
                "component": None,
                "error": None,
            })
            self._worker = threading.Thread(target=self._run, args=(target,), name=f"mlac-update-{name}", daemon=True)
            self._worker.start()
        self._broadcast()
        return self.status()

    def _run(self, target: Callable[[], None]) -> None:
        try:
            target()
        except Exception as exc:
            self._fail(exc)

    def _check(self, *, startup: bool) -> None:
        channel = self.status()["channel"]
        result = updater.fetch_metadata(
            self.state_root,
            channel,
            opener=self.opener,
            public_key_path=self.public_key_path,
        )
        payload = result.payload
        latest = None
        status = "up_to_date"
        stage = "MLAC Studio is up to date"
        if payload is not None and updater.is_newer(str(payload["version"]), self.current_version):
            selected = updater.select_components(payload)
            latest = updater.update_summary(payload, selected)
            skipped = updater.read_settings(self.state_root).get("skipped_version")
            if skipped == latest["version"] and not latest["security_mandatory"]:
                status = "skipped"
                stage = f"Skipped {latest['version']}"
            else:
                status = "available"
                stage = f"MLAC Studio {latest['version']} is available"
        with self._lock:
            self._payload = payload
            self._state.update({
                "status": status,
                "stage": stage,
                "latest": latest,
                "skipped_version": updater.read_settings(self.state_root).get("skipped_version"),
                "checked_at": int(time.time()),
                "startup_check_pending": False,
                "error": None,
            })
        self._broadcast()

    def _install(self, allow_data_migration: bool) -> None:
        if not self.can_mutate():
            raise updater.UpdateError("finish or cancel active renders before installing an update")
        state = self.status()
        channel = state["channel"]
        payload = updater.load_cached_metadata(self.state_root, channel, public_key_path=self.public_key_path)
        if not updater.is_newer(str(payload["version"]), self.current_version):
            raise updater.UpdateError("the cached release is not newer than this MLAC Studio version")
        selected = updater.select_components(payload)
        summary = updater.update_summary(payload, selected)
        if state.get("latest") and state["latest"]["version"] != summary["version"]:
            raise updater.UpdateError("the signed update changed; check again before installing")
        totals = {str(item["id"]): int(item["size"]) for item in selected}
        completed = {component: 0 for component in totals}

        def progress(component: str, done: int, total: int) -> None:
            completed[component] = done
            with self._lock:
                self._state.update({
                    "status": "downloading",
                    "stage": f"Downloading {component}",
                    "component": component,
                    "bytes_downloaded": sum(completed.values()),
                    "bytes_total": sum(totals.values()),
                })
            self._broadcast()

        with self._lock:
            self._state.update({"bytes_total": sum(totals.values()), "stage": "Downloading signed components"})
        self._broadcast()
        installed = self.store.install(
            payload,
            selected,
            opener=self.opener,
            progress=progress,
            allow_data_migration=allow_data_migration,
        )
        with self._lock:
            self._state.update({
                "status": "restart_pending",
                "stage": f"MLAC Studio {installed['app_version']} is ready to restart",
                "installed_version": str(installed["app_version"]),
                "bytes_downloaded": sum(totals.values()),
                "bytes_total": sum(totals.values()),
                "component": None,
                "restart_required": True,
                "rollback_version": self._rollback_version(),
                "error": None,
            })
        self._broadcast()

    def _rollback(self) -> None:
        if not self.can_mutate():
            raise updater.UpdateError("finish or cancel active renders before rolling back")
        rolled = self.store.rollback()
        with self._lock:
            self._state.update({
                "status": "restart_pending",
                "stage": f"Rollback to {rolled['app_version']} is ready to restart",
                "installed_version": str(rolled["app_version"]),
                "restart_required": True,
                "rollback_version": self._rollback_version(),
                "error": None,
            })
        self._broadcast()

    def _installed_version(self) -> str:
        active = self.store.active()
        return str(active.get("app_version") or self.current_version)

    def _rollback_version(self) -> str | None:
        active = self.store.active()
        versions = [
            str(entry["previous"]["version"])
            for entry in active.get("components", {}).values()
            if isinstance(entry, dict) and isinstance(entry.get("previous"), dict) and entry["previous"].get("version")
        ]
        return min(versions, key=updater.version_key) if versions else None

    def _ensure_idle(self) -> None:
        if self._state.get("status") in BUSY_STATES or (self._worker and self._worker.is_alive()):
            raise updater.UpdateError("another update operation is already running")

    def _fail(self, exc: Exception) -> None:
        with self._lock:
            self._state.update({
                "status": "error",
                "stage": "Update operation failed",
                "error": str(exc) or exc.__class__.__name__,
                "startup_check_pending": False,
            })
        self._broadcast()

    def _broadcast(self) -> None:
        message = json.dumps({"type": "updates", "data": self.status()}, ensure_ascii=True)
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(message)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(message)
                except queue.Empty:
                    pass
