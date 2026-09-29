#!/usr/bin/env python3
"""Local-only Windows web server for MLAC Studio."""

from __future__ import annotations

import argparse
import contextlib
import hmac
import importlib.util
import json
import mimetypes
import os
import queue
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import time
from typing import Any, BinaryIO, Callable
from urllib.parse import parse_qs, unquote, urlparse

from inference import RenderQueue, SdServerSupervisor, StudioConfig, clean_settings
from security import (
    AuthSession,
    AuthStore,
    PreAuthTokens,
    clear_session_cookie,
    cookie_value,
    host_allowed,
    peer_allowed,
    preauth_cookie,
    same_origin_allowed,
    session_cookie,
)
from storage import MAX_MASK_BYTES, MAX_UPLOAD_BYTES, REFERENCE_ROLES, Repository, resolve_under

WEB_DIR = Path(__file__).resolve().parent
REPO_ROOT = WEB_DIR.parent
PACKAGING_DIR = REPO_ROOT / "packaging"
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

from updates import UpdateManager  # noqa: E402

STATIC_DIR = WEB_DIR / "static"
PUBLIC_FILES = {"/static/auth.css", "/static/auth.js"}


def load_config(path: Path, *, required: bool = False) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        if required:
            raise SystemExit(f"config file not found: {path}") from exc
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit("config.json must contain a JSON object")
    return data


def load_model_manager(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("qis_embedded_model_manager", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load model manager: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class StudioApp:
    def __init__(
        self,
        config_path: Path,
        *,
        data_dir: Path | None = None,
        adapter: Any | None = None,
        start_worker: bool = True,
        manifest_path: Path | None = None,
        runtime_dir: Path | None = None,
        model_dir: Path | None = None,
        model_manager_path: Path | None = None,
        manager_opener: Callable[..., Any] | None = None,
        hardware: Any | None = None,
        update_state_root: Path | None = None,
        update_opener: Callable[..., Any] | None = None,
        update_public_key_path: Path | None = None,
        start_update_check: bool = True,
    ) -> None:
        defaults = load_config(config_path)
        configured_data = Path(str(defaults.get("data_dir", config_path.parent / "data")))
        if not configured_data.is_absolute():
            configured_data = REPO_ROOT / configured_data
        self.repository = Repository(data_dir or configured_data)
        self.auth = AuthStore(self.repository.database)
        self.preauth = PreAuthTokens()
        self.config = StudioConfig(defaults, self.repository)
        inference_adapter = adapter if adapter is not None else SdServerSupervisor(self.config)
        self.runner = RenderQueue(
            self.repository,
            inference_adapter,
            start_worker=start_worker,
        )
        self.config_path = config_path
        self.model_manager: Any | None = None
        self.model_manager_error: str | None = None
        frozen_root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else REPO_ROOT
        manager_source = model_manager_path or (frozen_root / "model-manager.py" if getattr(sys, "frozen", False) else REPO_ROOT / "packaging" / "model-manager.py")
        manifest_source = manifest_path or (frozen_root / "release-manifest.json" if getattr(sys, "frozen", False) else REPO_ROOT / "packaging" / "release-manifest.json")
        if manager_source.is_file() and manifest_source.is_file():
            try:
                manager_module = load_model_manager(manager_source)
                manager_options: dict[str, Any] = {}
                if manager_opener is not None:
                    manager_options["opener"] = manager_opener
                if hardware is not None:
                    manager_options["hardware"] = hardware
                self.model_manager = manager_module.PersistentModelManager(
                    manifest_source,
                    model_dir=model_dir or config_path.parent / "models",
                    runtime_dir=runtime_dir or config_path.parent / "runtime",
                    config_path=config_path,
                    data_dir=data_dir or configured_data,
                    can_mutate=self.runner.is_idle,
                    on_configured=self.reload_config,
                    **manager_options,
                )
            except Exception as exc:
                self.model_manager_error = str(exc)
        update_options: dict[str, Any] = {
            "can_mutate": self.runner.is_idle,
            "start_check": start_update_check,
        }
        if update_opener is not None:
            update_options["opener"] = update_opener
        if update_public_key_path is not None:
            update_options["public_key_path"] = update_public_key_path
        self.updates = UpdateManager(update_state_root or config_path.parent, **update_options)

    def close(self) -> None:
        self.updates.close()
        if self.model_manager is not None:
            self.model_manager.close()
        self.runner.stop()

    def set_restart_handler(self, handler: Callable[[], None] | None) -> None:
        self.updates.set_restart_handler(handler)

    def reload_config(self, config: dict[str, Any]) -> None:
        self.runner.unload_model()
        self.config.reload(config)

    def models_state(self) -> dict[str, Any]:
        if self.model_manager is None:
            return {"available": False, "error": self.model_manager_error or "release manifest is unavailable"}
        return {"available": True, **self.model_manager.status()}

    def model_state(self) -> dict[str, Any]:
        return self.runner.model_state()

    def can_unload_model(self) -> bool:
        return self.runner.can_unload_model()

    def unload_model(self) -> bool:
        return self.runner.unload_model()


class Handler(BaseHTTPRequestHandler):
    server_version = "MLACStudio/0.3"
    app: StudioApp

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}", flush=True)

    def do_HEAD(self) -> None:
        self._dispatch(head=True)

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def _dispatch(self, *, head: bool = False) -> None:
        path = urlparse(self.path).path
        if not peer_allowed(self.client_address[0]):
            return self._json(403, {"error": "non-loopback client refused"}, head=head)
        if not host_allowed(self.headers.get("Host", "")):
            return self._json(403, {"error": "unrecognized Host header"}, head=head)
        if not same_origin_allowed(self.command, self.headers):
            return self._json(403, {"error": "cross-origin request refused"}, head=head)

        if self.command in {"GET", "HEAD"} and path == "/health":
            return self._json(200, {"status": "ok", "configured": self.app.auth.is_configured()}, head=head)
        if self.command in {"GET", "HEAD"} and path == "/api/auth/status":
            return self._auth_status(head=head)
        if self.command in {"GET", "HEAD"} and path == "/api/bootstrap":
            return self._bootstrap_status(head=head)
        if self.command in {"GET", "HEAD"} and path in {"/setup", "/login"}:
            return self._auth_page(path, head=head)
        if self.command in {"GET", "HEAD"} and path in PUBLIC_FILES:
            return self._static(path.removeprefix("/static/"), head=head)
        if self.command == "POST" and path in {"/api/setup", "/api/login"}:
            return self._public_post(path)

        session = self._session()
        if session is None:
            if path.startswith("/api/") or path.startswith("/media/"):
                return self._json(401, {"error": "authentication required"}, head=head)
            return self._redirect("/setup" if not self.app.auth.is_configured() else "/login")
        if self.command not in {"GET", "HEAD"} and not self._csrf_ok(session):
            return self._json(403, {"error": "invalid CSRF token"})

        try:
            if self.command in {"GET", "HEAD"}:
                return self._authenticated_get(path, head=head)
            if self.command == "POST":
                return self._authenticated_post(path)
            self._json(405, {"error": "method not allowed"})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except FileNotFoundError:
            self._json(404, {"error": "not found"})
        except Exception as exc:
            print(f"request failed: {exc!r}", file=sys.stderr, flush=True)
            self._json(500, {"error": "internal server error"})

    def _session(self) -> AuthSession | None:
        return self.app.auth.get_session(cookie_value(self.headers.get("Cookie"), "qis_session"))

    def _csrf_ok(self, session: AuthSession) -> bool:
        supplied = self.headers.get("X-CSRF-Token", "")
        return bool(supplied and hmac.compare_digest(supplied, session.csrf))

    def _auth_status(self, *, head: bool = False) -> None:
        configured = self.app.auth.is_configured()
        session = self._session()
        if session:
            return self._json(
                200,
                {"configured": configured, "authenticated": True, "username": session.username, "csrf": session.csrf},
                head=head,
            )
        raw, csrf = self.app.preauth.issue()
        return self._json(
            200,
            {"configured": configured, "authenticated": False, "csrf": csrf},
            headers={"Set-Cookie": preauth_cookie(raw)},
            head=head,
        )

    def _bootstrap_status(self, *, head: bool = False) -> None:
        """Expose only the minimum first-run routing state before an admin exists."""
        if self.app.auth.is_configured():
            return self._json(404, {"error": "not found"}, head=head)
        return self._json(
            200,
            {"needs_admin": True, "model_setup_available": self.app.model_manager is not None},
            head=head,
        )

    def _auth_page(self, path: str, *, head: bool = False) -> None:
        configured = self.app.auth.is_configured()
        if path == "/setup" and configured:
            return self._redirect("/" if self._session() else "/login")
        if path == "/login" and not configured:
            return self._redirect("/setup")
        return self._file(STATIC_DIR / ("setup.html" if path == "/setup" else "login.html"), head=head)

    def _public_post(self, path: str) -> None:
        preauth = cookie_value(self.headers.get("Cookie"), "qis_preauth")
        if not self.app.preauth.verify(preauth, self.headers.get("X-CSRF-Token")):
            return self._json(403, {"error": "invalid CSRF token"})
        try:
            body = self._json_body()
            if path == "/api/setup":
                if self.app.auth.is_configured():
                    return self._json(409, {"error": "admin setup is already complete"})
                password = str(body.get("password") or "")
                if password != str(body.get("confirm_password") or ""):
                    raise ValueError("passwords do not match")
                requested_username = str(body.get("username") or "")
                self.app.auth.create_admin(requested_username, password)
                username = self.app.auth.authenticate(self.client_address[0], requested_username, password)
            else:
                blocked, retry = self.app.auth.throttled(self.client_address[0])
                if blocked:
                    return self._json(429, {"error": "too many failed logins", "retry_after": retry}, headers={"Retry-After": str(retry)})
                username = self.app.auth.authenticate(
                    self.client_address[0], str(body.get("username") or ""), str(body.get("password") or "")
                )
                if not username:
                    blocked, retry = self.app.auth.throttled(self.client_address[0])
                    status = 429 if blocked else 401
                    headers = {"Retry-After": str(retry)} if blocked else None
                    return self._json(status, {"error": "invalid username or password"}, headers=headers)
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        assert username is not None
        session, csrf = self.app.auth.create_session(username)
        self.app.preauth.consume(preauth)
        self._json(200, {"ok": True, "csrf": csrf}, headers={"Set-Cookie": session_cookie(session)})

    def _authenticated_get(self, path: str, *, head: bool = False) -> None:
        query = parse_qs(urlparse(self.path).query)
        session = (query.get("session") or [self.app.repository.active_session])[0]
        if path == "/":
            return self._file(STATIC_DIR / "index.html", head=head)
        if path.startswith("/static/"):
            return self._static(path.removeprefix("/static/"), head=head)
        if path.startswith("/media/uploads/"):
            return self._file(self.app.repository.media_path("uploads", path.rsplit("/", 1)[-1]), head=head)
        if path.startswith("/media/gallery/"):
            return self._file(self.app.repository.media_path("gallery", path.rsplit("/", 1)[-1]), head=head)
        if path.startswith("/media/masks/"):
            return self._file(self.app.repository.media_path("masks", path.rsplit("/", 1)[-1]), head=head)
        if path == "/api/config":
            auth = self._session()
            return self._json(200, {
                "paths": self.app.config.info(), "sessions": self.app.repository.sessions(),
                "active": self.app.repository.active_session,
                "settings": self.app.repository.session_settings(self.app.repository.active_session),
                "model": self.app.runner.model_state(),
                "models": self.app.models_state(),
                "updates": self.app.updates.status(),
                "idle_timeout": self.app.config.idle_timeout,
                "capabilities": self.app.runner.capabilities(),
                "reference_roles": list(REFERENCE_ROLES),
                "csrf": auth.csrf if auth else "",
            }, head=head)
        if path in {"/api/inputs", "/api/references"}:
            return self._json(200, self.app.repository.inputs(session), head=head)
        if path == "/api/capabilities":
            return self._json(200, self.app.runner.capabilities(), head=head)
        if path == "/api/mask":
            return self._json(200, {"mask": self.app.repository.mask(session)}, head=head)
        if path == "/api/takes":
            return self._json(200, self.app.repository.takes(session), head=head)
        if path == "/api/queue":
            return self._json(200, self.app.runner.queue_state(), head=head)
        if path == "/api/models":
            return self._json(200, self.app.models_state(), head=head)
        if path == "/api/updates/status":
            return self._json(200, self.app.updates.status(), head=head)
        if path == "/api/events":
            return self._events(head=head)
        self._json(404, {"error": "not found"}, head=head)

    def _authenticated_post(self, path: str) -> None:
        if path in {"/api/upload", "/api/references/add"}:
            return self._upload(replace_base=path == "/api/upload")
        if path == "/api/mask":
            return self._mask_upload()
        body = self._json_body()
        session_name = str(body.get("session") or self.app.repository.active_session)
        if path == "/api/render":
            return self._json(200, self.app.runner.enqueue(body))
        if path == "/api/cancel":
            return self._json(200, {"cancelled": self.app.runner.cancel(str(body.get("id") or ""))})
        if path == "/api/runtime":
            timeout = self.app.config.update_idle_timeout(body.get("idle_timeout"))
            self.app.runner._broadcast("model", self.app.runner.model_state())
            return self._json(200, {"idle_timeout": timeout, "model": self.app.runner.model_state()})
        if path.startswith("/api/models/"):
            return self._models_post(path, body)
        if path.startswith("/api/updates/"):
            return self._updates_post(path, body)
        if path == "/api/session/activate":
            return self._json(200, self.app.repository.activate_session(session_name))
        if path == "/api/session/save":
            self.app.repository.save_settings(
                session_name, clean_settings(body.get("settings") or {}, require_prompt=False)
            )
            return self._json(200, {"ok": True})
        if path == "/api/session/duplicate":
            return self._json(200, self.app.repository.duplicate_session(session_name, str(body.get("new_name") or "")))
        if path == "/api/session/delete":
            return self._json(200, self.app.repository.delete_session(session_name))
        if path == "/api/inputs/delete":
            self.app.repository.delete_input(session_name, str(body.get("name") or ""))
            self.app.runner._broadcast("inputs", {"session": session_name})
            return self._json(200, {"ok": True})
        if path == "/api/references/remove":
            self.app.repository.delete_input(session_name, ident=str(body.get("id") or ""))
            self.app.runner._broadcast("inputs", {"session": session_name})
            return self._json(200, self.app.repository.inputs(session_name))
        if path == "/api/references/reorder":
            result = self.app.repository.reorder_inputs(session_name, body.get("ordered_ids"))
            self.app.runner._broadcast("inputs", {"session": session_name})
            return self._json(200, result)
        if path == "/api/references/role":
            result = self.app.repository.set_input_role(
                session_name, str(body.get("id") or ""), str(body.get("role") or "")
            )
            self.app.runner._broadcast("inputs", {"session": session_name})
            return self._json(200, result)
        if path == "/api/takes/delete":
            self.app.repository.delete_take(str(body.get("id") or ""))
            return self._json(200, {"ok": True})
        if path == "/api/takes/use":
            return self._json(200, self.app.repository.take_as_input(session_name, str(body.get("id") or "")))
        if path == "/api/logout":
            raw = cookie_value(self.headers.get("Cookie"), "qis_session")
            self.app.auth.delete_session(raw)
            return self._json(200, {"ok": True}, headers={"Set-Cookie": clear_session_cookie()})
        self._json(404, {"error": "not found"})

    def _models_post(self, path: str, body: dict[str, Any]) -> None:
        manager = self.app.model_manager
        if manager is None:
            return self._json(503, {"error": self.app.model_manager_error or "model setup is unavailable"})
        try:
            if path == "/api/models/install":
                result = manager.start_install(
                    str(body.get("profile_id") or ""),
                    accepted=body.get("accepted_licenses") or [],
                    activate=bool(body.get("activate", True)),
                )
            elif path == "/api/models/cancel":
                result = manager.cancel()
            elif path == "/api/models/retry":
                result = manager.retry()
            elif path == "/api/models/switch":
                result = manager.switch(str(body.get("profile_id") or ""))
            elif path == "/api/models/delete":
                result = manager.delete(
                    str(body.get("profile_id") or ""), str(body.get("confirmation") or "")
                )
            elif path == "/api/models/source/resolve":
                return self._json(200, manager.resolve_source(body))
            elif path == "/api/models/source/confirm":
                result = manager.confirm_source(str(body.get("confirmation_id") or ""))
            elif path == "/api/models/source/reset":
                result = manager.reset_source()
            elif path == "/api/models/hf-token/set":
                result = manager.set_token(body.get("token"))
            elif path == "/api/models/hf-token/test":
                return self._json(200, manager.test_token())
            elif path == "/api/models/hf-token/remove":
                result = manager.remove_token()
            else:
                return self._json(404, {"error": "not found"})
        except Exception as exc:
            if exc.__class__.__name__ == "ManagerError":
                return self._json(409, {"error": str(exc)})
            raise
        return self._json(200, result)

    def _updates_post(self, path: str, body: dict[str, Any]) -> None:
        manager = self.app.updates
        try:
            if path == "/api/updates/check":
                result = manager.start_check()
            elif path == "/api/updates/channel":
                result = manager.set_channel(str(body.get("channel") or ""))
            elif path == "/api/updates/skip":
                result = manager.skip(str(body.get("version") or "") or None)
            elif path == "/api/updates/install":
                result = manager.start_install(allow_data_migration=bool(body.get("allow_data_migration", False)))
            elif path == "/api/updates/rollback":
                result = manager.start_rollback()
            elif path == "/api/updates/restart":
                result = manager.restart()
            else:
                return self._json(404, {"error": "not found"})
        except Exception as exc:
            if exc.__class__.__name__ in {"UpdateError", "DiskSpaceError", "SignatureError"}:
                return self._json(409, {"error": str(exc)})
            raise
        return self._json(202 if path in {"/api/updates/check", "/api/updates/channel", "/api/updates/install", "/api/updates/rollback"} else 200, result)

    def _upload(self, *, replace_base: bool = False) -> None:
        length = self._content_length(MAX_UPLOAD_BYTES)
        original = unquote(self.headers.get("X-Filename", ""))
        if not original:
            raise ValueError("X-Filename header is required")
        query = parse_qs(urlparse(self.path).query)
        session = (query.get("session") or [self.app.repository.active_session])[0]
        fd, temp_name = tempfile.mkstemp(prefix="qis-upload-", suffix=Path(original).suffix, dir=self.app.repository.work_dir)
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as output:
                self._copy_body(output, length)
            result = self.app.repository.add_upload(
                session,
                original,
                temp,
                role=self.headers.get("X-Reference-Role"),
                replace_base=replace_base,
            )
            self.app.runner._broadcast("inputs", {"session": session})
            self._json(200, result)
        finally:
            temp.unlink(missing_ok=True)

    def _mask_upload(self) -> None:
        length = self._content_length(MAX_MASK_BYTES)
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "image/png":
            raise ValueError("mask Content-Type must be image/png")
        try:
            feather = int(self.headers.get("X-Mask-Feather", "0"))
        except ValueError as exc:
            raise ValueError("X-Mask-Feather must be an integer") from exc
        query = parse_qs(urlparse(self.path).query)
        session = (query.get("session") or [self.app.repository.active_session])[0]
        fd, temp_name = tempfile.mkstemp(prefix="qis-mask-", suffix=".png", dir=self.app.repository.work_dir)
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as output:
                self._copy_body(output, length)
            result = self.app.repository.add_mask(session, temp, feather)
            self.app.runner._broadcast("mask", {"session": session})
            self._json(200, {"mask": result})
        finally:
            temp.unlink(missing_ok=True)

    def _events(self, *, head: bool = False) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if head:
            return
        subscriber = self.app.runner.subscribe()
        model_subscriber = self.app.model_manager.subscribe() if self.app.model_manager is not None else None
        update_subscriber = self.app.updates.subscribe()
        try:
            initial = json.dumps({"type": "queue", "data": self.app.runner.queue_state()})
            self.wfile.write(f"data: {initial}\n\n".encode("utf-8"))
            model = json.dumps({"type": "model", "data": self.app.runner.model_state()})
            self.wfile.write(f"data: {model}\n\n".encode("utf-8"))
            models = json.dumps({"type": "models", "data": self.app.models_state()})
            self.wfile.write(f"data: {models}\n\n".encode("utf-8"))
            updates = json.dumps({"type": "updates", "data": self.app.updates.status()})
            self.wfile.write(f"data: {updates}\n\n".encode("utf-8"))
            self.wfile.flush()
            last_ping = time.monotonic()
            while True:
                message = None
                try:
                    message = subscriber.get(timeout=0.25)
                except queue.Empty:
                    pass
                if message is None and model_subscriber is not None:
                    with contextlib.suppress(queue.Empty):
                        message = model_subscriber.get_nowait()
                if message is None:
                    with contextlib.suppress(queue.Empty):
                        message = update_subscriber.get_nowait()
                if message is not None:
                    self.wfile.write(f"data: {message}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    last_ping = time.monotonic()
                elif time.monotonic() - last_ping >= 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.runner.unsubscribe(subscriber)
            if model_subscriber is not None:
                self.app.model_manager.unsubscribe(model_subscriber)
            self.app.updates.unsubscribe(update_subscriber)

    def _json_body(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json")
        length = self._content_length(1 << 20)
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON body") from exc
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def _content_length(self, maximum: int) -> int:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError as exc:
            raise ValueError("valid Content-Length is required") from exc
        if length < 0 or length > maximum:
            raise ValueError(f"request body must be at most {maximum} bytes")
        return length

    def _copy_body(self, output: BinaryIO, remaining: int) -> None:
        while remaining:
            chunk = self.rfile.read(min(1 << 20, remaining))
            if not chunk:
                raise ValueError("request body ended early")
            output.write(chunk)
            remaining -= len(chunk)

    def _static(self, relative: str, *, head: bool = False) -> None:
        try:
            path = resolve_under(STATIC_DIR, relative)
        except ValueError:
            return self._json(404, {"error": "not found"}, head=head)
        self._file(path, head=head)

    def _file(self, path: Path, *, head: bool = False) -> None:
        if not path.is_file():
            return self._json(404, {"error": "not found"}, head=head)
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store" if path.suffix.lower() in {".html", ".json"} else "private, max-age=3600")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' blob:; connect-src 'self'; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        if not head:
            with path.open("rb") as source:
                while chunk := source.read(1 << 20):
                    self.wfile.write(chunk)

    def _json(self, status: int, value: Any, *, headers: dict[str, str] | None = None, head: bool = False) -> None:
        payload = json.dumps(value, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        for key, header_value in (headers or {}).items():
            self.send_header(key, header_value)
        self.end_headers()
        if not head:
            self.wfile.write(payload)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()


class StudioServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            super().handle_error(request, client_address)


def handler_for(app: StudioApp) -> type[Handler]:
    class BoundHandler(Handler):
        pass

    BoundHandler.app = app
    return BoundHandler


def main() -> None:
    parser = argparse.ArgumentParser(description="MLAC Studio for Windows")
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "config.json")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    defaults = load_config(args.config)
    port = args.port if args.port is not None else int(defaults.get("port", 8730))
    app = StudioApp(args.config)
    server = StudioServer(("127.0.0.1", port), handler_for(app))
    print(f"MLAC Studio listening on http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()


if __name__ == "__main__":
    main()
