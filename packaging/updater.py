#!/usr/bin/env python3
"""Signed component update policy and storage for MLAC Studio."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


APP_VERSION = "0.3.0"
CHANNELS = {"stable", "beta", "dev"}
DEFAULT_CHANNEL = "stable"
MANIFEST_URL = "https://github.com/vuisme/mlacstudio/releases/latest/download/MLAC-Studio-{channel}.json"
PUBLIC_KEY_PATH = Path(__file__).with_name("keys") / "mlac-update-public.json"
SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
WEIGHT_SUFFIXES = {".gguf", ".safetensors", ".ckpt", ".pt", ".pth"}


class UpdateError(RuntimeError):
    pass


class SignatureError(UpdateError):
    pass


class DiskSpaceError(UpdateError):
    pass


def canonical_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _read_public_key(path: Path = PUBLIC_KEY_PATH) -> tuple[int, int, str]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("algorithm") != "rsa-sha256":
        raise SignatureError("unsupported update signing key")
    try:
        modulus = int.from_bytes(base64.b64decode(value["modulus"]), "big")
        exponent = int.from_bytes(base64.b64decode(value["exponent"]), "big")
    except (KeyError, ValueError, TypeError) as exc:
        raise SignatureError("invalid bundled update signing key") from exc
    return modulus, exponent, str(value.get("key_id") or "")


def verify_envelope(envelope: dict[str, Any], public_key_path: Path = PUBLIC_KEY_PATH) -> dict[str, Any]:
    payload = envelope.get("payload")
    signature = envelope.get("signature")
    if not isinstance(payload, dict) or not isinstance(signature, dict):
        raise SignatureError("update metadata must be signed")
    if signature.get("algorithm") != "rsa-sha256":
        raise SignatureError("unsupported update signature algorithm")
    modulus, exponent, key_id = _read_public_key(public_key_path)
    if key_id and signature.get("key_id") != key_id:
        raise SignatureError("update signature key id does not match")
    try:
        raw_signature = base64.b64decode(signature["value"], validate=True)
    except (KeyError, ValueError, TypeError) as exc:
        raise SignatureError("invalid update signature encoding") from exc
    width = (modulus.bit_length() + 7) // 8
    if len(raw_signature) != width:
        raise SignatureError("invalid update signature length")
    decoded = pow(int.from_bytes(raw_signature, "big"), exponent, modulus).to_bytes(width, "big")
    digest_info = SHA256_DIGEST_INFO + hashlib.sha256(canonical_payload(payload)).digest()
    padding_size = width - len(digest_info) - 3
    expected = b"\x00\x01" + (b"\xff" * padding_size) + b"\x00" + digest_info
    if padding_size < 8 or decoded != expected:
        raise SignatureError("update metadata signature is invalid")
    validate_payload(payload)
    return payload


def validate_payload(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != 1:
        raise UpdateError("unsupported update metadata schema")
    if payload.get("channel") not in CHANNELS:
        raise UpdateError("invalid update channel")
    if not isinstance(payload.get("version"), str) or not payload["version"]:
        raise UpdateError("update version is required")
    components = payload.get("components")
    if not isinstance(components, list) or not components:
        raise UpdateError("update metadata has no components")
    seen: set[str] = set()
    for component in components:
        if not isinstance(component, dict):
            raise UpdateError("invalid component entry")
        component_id = str(component.get("id") or "")
        if not component_id or component_id in seen:
            raise UpdateError("component ids must be unique")
        seen.add(component_id)
        if "model" in component_id.lower():
            raise UpdateError("models are not update components")
        if component.get("kind") != "core":
            raise UpdateError(f"invalid component kind: {component_id}")
        url = str(component.get("url") or "")
        if not url.startswith("https://github.com/vuisme/mlacstudio/releases/download/"):
            raise UpdateError(f"component URL is not an MLAC Studio GitHub release asset: {component_id}")
        digest = str(component.get("sha256") or "")
        if len(digest) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in digest):
            raise UpdateError(f"invalid component SHA-256: {component_id}")
        if int(component.get("size") or 0) <= 0:
            raise UpdateError(f"invalid component size: {component_id}")


def version_key(value: str) -> tuple[int, int, int, tuple[str, ...]]:
    main, _, suffix = value.partition("-")
    parts = main.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise UpdateError(f"invalid semantic version: {value}")
    return int(parts[0]), int(parts[1]), int(parts[2]), tuple(suffix.split(".")) if suffix else ("~",)


def is_newer(candidate: str, current: str) -> bool:
    return version_key(candidate) > version_key(current)


def default_state_root() -> Path:
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return local / "MLACStudio"


def read_channel(state_root: Path) -> str:
    path = state_root / "update-settings.json"
    try:
        channel = json.loads(path.read_text(encoding="utf-8")).get("channel", DEFAULT_CHANNEL)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        channel = DEFAULT_CHANNEL
    return channel if channel in CHANNELS else DEFAULT_CHANNEL


def write_channel(state_root: Path, channel: str) -> None:
    if channel not in CHANNELS:
        raise UpdateError("invalid update channel")
    _write_json_atomic(state_root / "update-settings.json", {"channel": channel})


@dataclass
class UpdateCheck:
    payload: dict[str, Any] | None
    changed: bool
    etag: str | None


def fetch_metadata(
    state_root: Path,
    channel: str,
    *,
    opener: Callable[..., Any] = urlopen,
    public_key_path: Path = PUBLIC_KEY_PATH,
    timeout: float = 15,
) -> UpdateCheck:
    if channel not in CHANNELS:
        raise UpdateError("invalid update channel")
    cache_dir = state_root / "updates"
    cache_path = cache_dir / f"{channel}.json"
    etag_path = cache_dir / f"{channel}.etag"
    headers = {"Accept": "application/json", "User-Agent": f"MLACStudio/{APP_VERSION}"}
    try:
        etag = etag_path.read_text(encoding="ascii").strip()
        if etag:
            headers["If-None-Match"] = etag
    except OSError:
        pass
    request = Request(MANIFEST_URL.format(channel=channel), headers=headers)
    try:
        response = opener(request, timeout=timeout)
        raw = response.read()
        response_etag = response.headers.get("ETag")
        envelope = json.loads(raw.decode("utf-8"))
        payload = verify_envelope(envelope, public_key_path)
        if payload["channel"] != channel:
            raise UpdateError("signed metadata channel does not match the requested channel")
        cache_dir.mkdir(parents=True, exist_ok=True)
        _write_bytes_atomic(cache_path, raw)
        if response_etag:
            _write_bytes_atomic(etag_path, response_etag.encode("ascii"))
        return UpdateCheck(payload, True, response_etag)
    except HTTPError as exc:
        if exc.code != 304:
            raise UpdateError(f"update metadata request failed: HTTP {exc.code}") from exc
        try:
            envelope = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as cache_exc:
            raise UpdateError("server returned 304 but no valid signed metadata is cached") from cache_exc
        return UpdateCheck(verify_envelope(envelope, public_key_path), False, headers.get("If-None-Match"))
    except (OSError, URLError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError(f"could not check for updates: {exc}") from exc


def select_components(payload: dict[str, Any], gpu: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    del gpu  # Native runtimes are installed by the model manager, not the app updater.
    selected = [component for component in payload["components"] if component["kind"] == "core"]
    if not any(item["kind"] == "core" for item in selected):
        raise UpdateError("no compatible core component was selected")
    return selected


def update_summary(payload: dict[str, Any], selected: Iterable[dict[str, Any]]) -> dict[str, Any]:
    items = list(selected)
    return {
        "version": payload["version"],
        "channel": payload["channel"],
        "security_mandatory": bool(payload.get("security_mandatory", False)),
        "changelog": str(payload.get("changelog") or ""),
        "restart_required": bool(payload.get("restart_required", True)),
        "data_migration": payload.get("data_migration"),
        "components": [item["id"] for item in items],
        "size": sum(int(item["size"]) for item in items),
    }


def check_disk_space(root: Path, required_bytes: int, *, free_bytes: int | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    free = free_bytes if free_bytes is not None else shutil.disk_usage(root).free
    reserve = max(256 * 1024 * 1024, required_bytes // 10)
    if free < required_bytes + reserve:
        raise DiskSpaceError(f"update needs {required_bytes + reserve} bytes but only {free} bytes are free")


def _download_with_resume(
    url: str,
    destination: Path,
    size: int,
    digest: str,
    *,
    opener: Callable[..., Any] = urlopen,
    progress: Callable[[int, int], None] | None = None,
    retries: int = 3,
    timeout: float = 30,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(retries):
        offset = destination.stat().st_size if destination.exists() else 0
        if offset > size:
            destination.unlink()
            offset = 0
        headers = {"User-Agent": f"MLACStudio/{APP_VERSION}", "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            response = opener(Request(url, headers=headers), timeout=timeout)
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            if offset and status != 206:
                destination.unlink(missing_ok=True)
                offset = 0
            mode = "ab" if offset else "wb"
            completed = offset
            with destination.open(mode) as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    completed += len(chunk)
                    if progress:
                        progress(completed, size)
            if destination.stat().st_size != size:
                raise UpdateError("component download size mismatch")
            actual = hashlib.sha256(destination.read_bytes()).hexdigest()
            if actual.lower() != digest.lower():
                destination.unlink(missing_ok=True)
                raise UpdateError("component download SHA-256 mismatch")
            return
        except (OSError, URLError, HTTPError, UpdateError) as exc:
            if attempt + 1 >= retries:
                raise UpdateError(f"component download failed after {retries} attempts: {exc}") from exc
            time.sleep(min(2**attempt, 4))


def _safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            target = (destination / info.filename).resolve()
            if root != target and root not in target.parents:
                raise UpdateError("component archive contains path traversal")
            if Path(info.filename).suffix.lower() in WEIGHT_SUFFIXES or "models" in Path(info.filename).parts:
                raise UpdateError("component archive contains model weights")
        bundle.extractall(destination)


class ComponentStore:
    def __init__(self, state_root: Path) -> None:
        self.state_root = Path(state_root)
        self.root = self.state_root / "components"
        self.active_path = self.root / "active.json"
        self.journal_path = self.root / "activation-journal.json"

    def active(self) -> dict[str, Any]:
        try:
            value = json.loads(self.active_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {"schema_version": 1, "app_version": "0.0.0", "components": {}}

    def recover(self) -> None:
        if not self.journal_path.exists():
            return
        try:
            journal = json.loads(self.journal_path.read_text(encoding="utf-8"))
            prior = journal.get("prior")
            if isinstance(prior, dict):
                _write_json_atomic(self.active_path, prior)
        finally:
            self.journal_path.unlink(missing_ok=True)

    def changed(self, components: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        active_components = self.active().get("components", {})
        return [item for item in components if active_components.get(item["id"], {}).get("active", {}).get("sha256") != item["sha256"]]

    def install(
        self,
        payload: dict[str, Any],
        components: Iterable[dict[str, Any]],
        *,
        opener: Callable[..., Any] = urlopen,
        progress: Callable[[str, int, int], None] | None = None,
        allow_data_migration: bool = False,
        explicit_rollback: bool = False,
    ) -> dict[str, Any]:
        self.recover()
        current = self.active()
        current_version = str(current.get("app_version") or "0.0.0")
        target_version = str(payload["version"])
        if not explicit_rollback and version_key(target_version) < version_key(current_version):
            raise UpdateError("downgrade rejected; use explicit rollback")
        if payload.get("data_migration") and not allow_data_migration:
            raise UpdateError("data migration requires explicit confirmation")
        selected = list(components)
        changed = self.changed(selected)
        check_disk_space(self.root, sum(int(item["size"]) for item in changed))
        downloads = self.state_root / "updates" / "downloads"
        staged: list[tuple[dict[str, Any], Path]] = []
        for item in changed:
            archive = downloads / f"{item['id']}-{item['sha256']}.zip.part"
            _download_with_resume(
                item["url"], archive, int(item["size"]), item["sha256"], opener=opener,
                progress=(lambda done, total, ident=item["id"]: progress(ident, done, total)) if progress else None,
            )
            stage = self.root / ".staging" / f"{item['id']}-{item['sha256']}"
            if stage.exists():
                shutil.rmtree(stage)
            _safe_extract(archive, stage)
            staged.append((item, stage))

        self.root.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(self.journal_path, {"prior": current, "target_version": target_version})
        next_state = json.loads(json.dumps(current))
        next_state.update({"schema_version": 1, "app_version": target_version, "channel": payload["channel"]})
        entries = next_state.setdefault("components", {})
        for item, stage in staged:
            component_root = self.root / item["id"]
            object_name = f"{item.get('version', target_version)}-{item['sha256'][:16]}"
            destination = component_root / object_name
            component_root.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                shutil.rmtree(stage)
            else:
                os.replace(stage, destination)
            old_active = entries.get(item["id"], {}).get("active")
            entries[item["id"]] = {
                "active": {"version": item.get("version", target_version), "sha256": item["sha256"], "path": str(destination)},
                "previous": old_active,
            }
        _write_json_atomic(self.active_path, next_state)
        self.journal_path.unlink(missing_ok=True)
        self._prune(next_state)
        return next_state

    def rollback(self) -> dict[str, Any]:
        self.recover()
        state = self.active()
        rolled = False
        for entry in state.get("components", {}).values():
            previous = entry.get("previous")
            if previous:
                entry["active"], entry["previous"] = previous, entry.get("active")
                rolled = True
        if not rolled:
            raise UpdateError("no previous component version is available")
        versions = [entry["active"]["version"] for entry in state["components"].values() if entry.get("active")]
        if versions:
            state["app_version"] = min(versions, key=version_key)
        _write_json_atomic(self.active_path, state)
        return state

    def _prune(self, state: dict[str, Any]) -> None:
        for component_id, entry in state.get("components", {}).items():
            keep = {Path(value["path"]).resolve() for value in (entry.get("active"), entry.get("previous")) if value}
            component_root = self.root / component_id
            if not component_root.is_dir():
                continue
            for child in component_root.iterdir():
                if child.is_dir() and child.resolve() not in keep:
                    shutil.rmtree(child)


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    _write_bytes_atomic(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"))
