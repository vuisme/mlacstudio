#!/usr/bin/env python3
"""Secure model/runtime setup for MLAC Studio Windows releases."""

from __future__ import annotations

import argparse
import contextlib
from collections import Counter
import ctypes
import hashlib
import ipaddress
import json
import os
import platform
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
import threading
import time
import uuid
import zipfile
from typing import Any, BinaryIO, Callable, Iterable
from urllib.parse import quote, unquote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

SCHEMA_VERSION = 2
CHUNK_SIZE = 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_HF_API_BYTES = 16 * 1024 * 1024
MAX_RUNTIME_ARCHIVE_BYTES = 16 * 1024 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
REVISION_RE = re.compile(r"^[0-9a-fA-F]{40}$")
REPO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
REQUIRED_ROLES = {"sd_cli", "sd_server", "transformer", "text_encoder", "mmproj", "vae"}
HF_SOURCE_HOSTS = frozenset({"huggingface.co", "hf.co"})
HF_CREDENTIAL_SERVICE = "MLACStudio/HuggingFace"
ALLOWED_DOWNLOAD_HOSTS = frozenset(
    {
        "huggingface.co",
        "hf.co",
        "cdn-lfs.huggingface.co",
        "cdn-lfs-us-1.huggingface.co",
        "cdn-lfs-eu-1.huggingface.co",
        "cas-bridge.xethub.hf.co",
        "cas-server.xethub.hf.co",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)


def _url_host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def _url_origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlparse(url)
    return parsed.scheme.lower(), _url_host(url), parsed.port


def _is_public_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def _validate_public_https_url(
    url: str,
    *,
    resolve: bool = False,
    resolver: Callable[..., Any] | None = None,
) -> None:
    parsed = urlparse(str(url or ""))
    try:
        port = parsed.port
    except ValueError as exc:
        raise ManagerError("download URL contains an invalid port") from exc
    if parsed.scheme.lower() != "https":
        raise ManagerError(f"download URL must use HTTPS: {url or '(blank)'}")
    host = _url_host(url)
    if not host:
        raise ManagerError("download URL must include a host")
    if parsed.username or parsed.password:
        raise ManagerError("download URLs must not contain credentials")
    if parsed.fragment:
        raise ManagerError("download URLs must not contain fragments")
    if host == "localhost" or host.endswith(".localhost"):
        raise ManagerError("download URL must use a public host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if not address.is_global:
            raise ManagerError("download URL must not use a private, local, or reserved address")
        return
    if not resolve:
        return
    resolver = resolver or socket.getaddrinfo
    try:
        answers = resolver(host, port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ManagerError(f"download host could not be resolved: {host}") from exc
    addresses = {str(answer[4][0]).split("%", 1)[0] for answer in answers if len(answer) >= 5 and answer[4]}
    if not addresses:
        raise ManagerError(f"download host did not resolve to an address: {host}")
    if any(not _is_public_ip(address) for address in addresses):
        raise ManagerError("download host resolved to a private, local, or reserved address")


class ManagerError(RuntimeError):
    """Expected setup error suitable for display to an end user."""


class DownloadCancelled(ManagerError):
    """A user-requested cancellation that deliberately preserves partial files."""


def redact_secret(message: Any, token: str | None = None) -> str:
    value = str(message)
    if token:
        value = value.replace(token, "[REDACTED]")
    value = re.sub(r"(?i)Bearer\s+[^\s,;]+", "Bearer [REDACTED]", value)
    return re.sub(r"(?i)\bhf_[A-Za-z0-9]+\b", "[REDACTED]", value)


class KeyringCredentialStore:
    """Windows Credential Manager adapter with no file-based fallback."""

    def _keyring(self) -> Any:
        try:
            import keyring
            from keyring.backends.Windows import WinVaultKeyring
        except ImportError as exc:
            raise ManagerError("Windows credential storage is unavailable; install the keyring build dependency") from exc
        if os.name != "nt":
            raise ManagerError("Windows Credential Manager is unavailable on this platform")
        backend = keyring.get_keyring()
        if not isinstance(backend, WinVaultKeyring):
            backend = WinVaultKeyring()
            keyring.set_keyring(backend)
        backend_name = f"{backend.__class__.__module__}.{backend.__class__.__name__}"
        if not backend.__class__.__module__.startswith("keyring.backends.Windows"):
            raise ManagerError(f"Windows Credential Manager is unavailable (active backend: {backend_name})")
        return keyring

    def available(self) -> bool:
        try:
            self._keyring()
            return True
        except ManagerError:
            return False

    def get(self, reference: str) -> str | None:
        return self._keyring().get_password(HF_CREDENTIAL_SERVICE, reference)

    def set(self, reference: str, token: str) -> None:
        self._keyring().set_password(HF_CREDENTIAL_SERVICE, reference, token)

    def delete(self, reference: str) -> None:
        keyring = self._keyring()
        try:
            keyring.delete_password(HF_CREDENTIAL_SERVICE, reference)
        except keyring.errors.PasswordDeleteError:
            pass


@dataclass(frozen=True)
class HardwareInfo:
    windows_x64: bool
    gpu_name: str | None
    vram_mib: int | None
    driver_version: str | None
    cuda12_driver_compatible: bool | None
    ram_mib: int | None
    free_disk_mib: int


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _ram_mib() -> int | None:
    if os.name != "nt":
        return None
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return int(status.ullTotalPhys // (1024 * 1024))


def _driver_is_cuda12_compatible(version: str | None) -> bool | None:
    if not version:
        return None
    match = re.match(r"\s*(\d+)", version)
    return int(match.group(1)) >= 525 if match else None


def detect_hardware(
    destination: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> HardwareInfo:
    gpu_name: str | None = None
    vram_mib: int | None = None
    driver: str | None = None
    try:
        result = runner(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        first_line = next((line for line in result.stdout.splitlines() if line.strip()), "")
        fields = [field.strip() for field in first_line.split(",")]
        if len(fields) >= 3:
            gpu_name, vram_text, driver = fields[:3]
            vram_mib = int(vram_text)
    except (FileNotFoundError, subprocess.SubprocessError, ValueError, StopIteration):
        pass
    destination.mkdir(parents=True, exist_ok=True)
    machine = platform.machine().lower()
    return HardwareInfo(
        windows_x64=os.name == "nt" and machine in {"amd64", "x86_64"},
        gpu_name=gpu_name,
        vram_mib=vram_mib,
        driver_version=driver,
        cuda12_driver_compatible=_driver_is_cuda12_compatible(driver),
        ram_mib=_ram_mib(),
        free_disk_mib=int(shutil.disk_usage(destination).free // (1024 * 1024)),
    )


def _validate_url(url: str) -> None:
    _validate_public_https_url(url)
    host = _url_host(url)
    if host not in ALLOWED_DOWNLOAD_HOSTS:
        raise ManagerError(f"download host is not allowlisted: {host or '(blank)'}")


def _validate_repo_id(repo_id: Any) -> str:
    value = str(repo_id or "").strip()
    if not REPO_ID_RE.fullmatch(value):
        raise ManagerError("Hugging Face repo id must use the namespace/repository form")
    return value


def _validate_revision(revision: Any) -> str:
    value = str(revision or "").strip().lower()
    if not REVISION_RE.fullmatch(value):
        raise ManagerError("Hugging Face revision must be an immutable 40-character hexadecimal commit")
    return value


def _relative_hf_path(value: Any) -> str:
    text = str(value or "").strip()
    if "\\" in text:
        raise ManagerError("Hugging Face file paths must use forward slashes")
    parsed = PurePosixPath(text)
    if not text or parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise ManagerError("Hugging Face file path must be a safe relative repository path")
    return parsed.as_posix()


def parse_hf_file_url(url: Any) -> tuple[str, str, str]:
    text = str(url or "").strip()
    parsed = urlparse(text)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ManagerError("source URL contains an invalid port") from exc
    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() not in HF_SOURCE_HOSTS:
        raise ManagerError("source URLs must use https://huggingface.co or https://hf.co")
    if parsed.username or parsed.password or port or parsed.query or parsed.fragment:
        raise ManagerError("source URLs must not contain credentials, ports, queries, or fragments")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 5 or parts[2] != "resolve":
        raise ManagerError("Hugging Face source URLs must use /namespace/repository/resolve/<commit>/<file>")
    if any("%" in part for part in parts[:4]):
        raise ManagerError("Hugging Face repository and revision URL segments must not be encoded")
    repo_id = _validate_repo_id("/".join(parts[:2]))
    revision = _validate_revision(parts[3])
    file_path = _relative_hf_path(unquote("/".join(parts[4:])))
    return repo_id, revision, file_path


def hf_file_url(repo_id: str, revision: str, file_path: str) -> str:
    return (
        "https://huggingface.co/"
        f"{quote(_validate_repo_id(repo_id), safe='/')}/resolve/{_validate_revision(revision)}/"
        f"{quote(_relative_hf_path(file_path), safe='/')}"
    )


def _validate_artifact_url(url: str) -> None:
    _validate_url(url)
    parsed = urlparse(url)
    if parsed.query or parsed.fragment:
        raise ManagerError("artifact URLs must not contain query strings or fragments")
    if (parsed.hostname or "").lower() in HF_SOURCE_HOSTS:
        parts = [part for part in parsed.path.split("/") if part]
        try:
            revision = parts[parts.index("resolve") + 1]
        except (ValueError, IndexError) as exc:
            raise ManagerError("Hugging Face artifact URLs must use /resolve/<commit>/ paths") from exc
        if not REVISION_RE.fullmatch(revision):
            raise ManagerError("Hugging Face artifact URLs must pin a 40-character commit revision")


class AllowlistRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: BinaryIO, code: int, msg: str, headers: Any, newurl: str) -> Request:
        _validate_public_https_url(newurl, resolve=True)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and _url_origin(req.full_url) != _url_origin(newurl):
            redirected.remove_header("Authorization")
        return redirected


def secure_urlopen(request: Request, *, timeout: int = 60) -> Any:
    _validate_public_https_url(request.full_url, resolve=True)
    response = build_opener(AllowlistRedirectHandler()).open(request, timeout=timeout)
    _validate_public_https_url(response.geturl(), resolve=True)
    return response


def _read_json_response(response: Any, *, maximum: int = MAX_HF_API_BYTES) -> dict[str, Any]:
    payload = response.read(maximum + 1)
    if len(payload) > maximum:
        raise ManagerError("Hugging Face API response is too large")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManagerError("Hugging Face API returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ManagerError("Hugging Face API returned an unexpected response")
    return value


def _authorized_headers(url: str, token: str | None = None) -> dict[str, str]:
    headers = {"User-Agent": "MLACStudio-ModelManager/1", "Accept": "application/json"}
    if token and _url_host(url) in HF_SOURCE_HOSTS:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def hf_repo_metadata(
    repo_id: str,
    revision: str,
    *,
    token: str | None = None,
    opener: Callable[..., Any] = secure_urlopen,
) -> dict[str, dict[str, Any]]:
    repo_id = _validate_repo_id(repo_id)
    revision = _validate_revision(revision)
    api_url = f"https://huggingface.co/api/models/{quote(repo_id, safe='/')}/revision/{revision}?blobs=true"
    request = Request(api_url, headers=_authorized_headers(api_url, token))
    try:
        with opener(request, timeout=60) as response:
            status = getattr(response, "status", None) or response.getcode()
            if status != 200:
                raise ManagerError(f"Hugging Face metadata request returned HTTP {status}")
            data = _read_json_response(response)
    except ManagerError as exc:
        raise ManagerError(redact_secret(exc, token)) from exc
    except OSError as exc:
        raise ManagerError(f"Hugging Face metadata request failed: {redact_secret(exc, token)}") from exc
    returned_revision = str(data.get("sha") or "").lower()
    if returned_revision != revision:
        raise ManagerError("Hugging Face API did not resolve the requested immutable revision")
    result: dict[str, dict[str, Any]] = {}
    for sibling in data.get("siblings", []):
        if not isinstance(sibling, dict):
            continue
        path = sibling.get("rfilename")
        lfs = sibling.get("lfs")
        if not isinstance(path, str) or not isinstance(lfs, dict):
            continue
        digest = str(lfs.get("sha256") or "").lower()
        size = lfs.get("size", sibling.get("size"))
        if SHA256_RE.fullmatch(digest) and isinstance(size, int) and not isinstance(size, bool) and size > 0:
            result[path] = {"size": size, "sha256": digest}
    return result


def resolve_hf_source(
    manifest: dict[str, Any],
    request_data: Any,
    *,
    token: str | None = None,
    opener: Callable[..., Any] = secure_urlopen,
    resolver: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(request_data, dict):
        raise ManagerError("source request must be an object")
    files = request_data.get("files")
    if not isinstance(files, dict) or not files:
        raise ManagerError("source request must provide at least one artifact file mapping")
    base_repo = str(request_data.get("repo_id") or "").strip()
    base_revision = str(request_data.get("revision") or "").strip()
    if bool(base_repo) != bool(base_revision):
        raise ManagerError("repo id and immutable revision must be supplied together")
    if base_repo:
        base_repo = _validate_repo_id(base_repo)
        base_revision = _validate_revision(base_revision)

    downloads = [item for item in manifest["artifacts"] if item["delivery"] == "download"]
    by_id = {str(item["id"]): item for item in downloads}
    by_role: dict[str, list[dict[str, Any]]] = {}
    for item in downloads:
        by_role.setdefault(str(item.get("role") or ""), []).append(item)
    acknowledged = request_data.get("responsibility_acknowledged") is True
    requested: dict[str, dict[str, Any]] = {}
    for supplied_key, supplied_value in files.items():
        key = str(supplied_key or "")
        artifact = by_id.get(key)
        if artifact is None:
            role_matches = by_role.get(key, [])
            if len(role_matches) != 1:
                raise ManagerError(f"source mapping key is not a unique downloadable artifact id or role: {key or '(blank)'}")
            artifact = role_matches[0]
        options = supplied_value if isinstance(supplied_value, dict) else {"url": supplied_value}
        value = str(options.get("url") or options.get("path") or "").strip()
        parsed_value = urlparse(value)
        if parsed_value.scheme:
            if _url_host(value) in HF_SOURCE_HOSTS:
                repo_id, revision, file_path = parse_hf_file_url(value)
                source = {"source_type": "huggingface", "repo_id": repo_id, "revision": revision, "file_path": file_path}
            else:
                if not acknowledged:
                    raise ManagerError("custom sources require responsibility_acknowledged=true")
                _validate_public_https_url(value, resolve=True, resolver=resolver)
                raw_size = options.get("expected_size", options.get("size"))
                if raw_size is None or raw_size == "":
                    size = None
                else:
                    try:
                        size = int(raw_size)
                    except (TypeError, ValueError) as exc:
                        raise ManagerError(f"{artifact['id']}: expected size must be a positive integer") from exc
                    if isinstance(raw_size, bool) or size <= 0:
                        raise ManagerError(f"{artifact['id']}: expected size must be a positive integer")
                digest = str(options.get("sha256") or "").strip().lower()
                if digest and not SHA256_RE.fullmatch(digest):
                    raise ManagerError(f"{artifact['id']}: SHA-256 must be exactly 64 hexadecimal characters")
                source = {
                    "source_type": "custom",
                    "url": value,
                    "size": size,
                    "sha256": digest,
                    "verified": bool(digest),
                    "responsibility_acknowledged": True,
                }
        else:
            if not base_repo:
                raise ManagerError(f"{artifact['id']}: relative file path requires a repo id and immutable revision")
            source = {
                "source_type": "huggingface",
                "repo_id": base_repo,
                "revision": base_revision,
                "file_path": _relative_hf_path(value),
            }
        artifact_id = str(artifact["id"])
        if artifact_id in requested:
            raise ManagerError(f"source mapping is duplicated for artifact: {artifact_id}")
        requested[artifact_id] = source

    metadata_cache: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    resolved: dict[str, dict[str, Any]] = {}
    preview: list[dict[str, Any]] = []
    for artifact_id, source in requested.items():
        if source["source_type"] == "custom":
            entry = dict(source)
            resolved[artifact_id] = entry
            preview.append({"id": artifact_id, "role": by_id[artifact_id].get("role"), **entry})
            continue
        repo_id = str(source["repo_id"])
        revision = str(source["revision"])
        file_path = str(source["file_path"])
        key = (repo_id, revision)
        if key not in metadata_cache:
            metadata_cache[key] = hf_repo_metadata(repo_id, revision, token=token, opener=opener)
        metadata = metadata_cache[key].get(file_path)
        if metadata is None:
            raise ManagerError(f"{artifact_id}: file was not found with LFS size and SHA-256 metadata: {file_path}")
        entry = {
            "source_type": "huggingface",
            "url": hf_file_url(repo_id, revision, file_path),
            "size": int(metadata["size"]),
            "sha256": str(metadata["sha256"]).lower(),
            "verified": True,
            "repo_id": repo_id,
            "revision": revision,
            "file_path": file_path,
        }
        resolved[artifact_id] = entry
        preview.append({"id": artifact_id, "role": by_id[artifact_id].get("role"), **entry})
    return {
        "schema_version": 2,
        "responsibility_acknowledged": acknowledged and any(item["source_type"] == "custom" for item in resolved.values()),
        "artifacts": resolved,
        "preview": preview,
    }


def apply_source_overrides(manifest: dict[str, Any], source: dict[str, Any] | None) -> dict[str, Any]:
    effective = json.loads(json.dumps(manifest))
    if source is not None and (not isinstance(source, dict) or source.get("schema_version") not in {1, 2}):
        raise ManagerError("saved source configuration is invalid")
    overrides = source.get("artifacts", {}) if isinstance(source, dict) else {}
    if not isinstance(overrides, dict):
        raise ManagerError("saved source configuration is invalid")
    downloadable_ids = {str(item["id"]) for item in effective["artifacts"] if item["delivery"] == "download"}
    if not set(overrides).issubset(downloadable_ids):
        raise ManagerError("saved source configuration references an unknown artifact")
    for artifact in effective["artifacts"]:
        override = overrides.get(str(artifact["id"]))
        if override is None:
            continue
        if artifact["delivery"] != "download" or not isinstance(override, dict):
            raise ManagerError("saved source configuration is invalid")
        source_type = str(override.get("source_type") or "huggingface")
        size = override.get("size")
        digest = str(override.get("sha256") or "").lower()
        if source_type == "huggingface":
            repo_id, revision, file_path = parse_hf_file_url(override.get("url"))
            if (
                repo_id != override.get("repo_id")
                or revision != override.get("revision")
                or file_path != override.get("file_path")
            ):
                raise ManagerError("saved Hugging Face source URL metadata is inconsistent")
            if not isinstance(size, int) or isinstance(size, bool) or size <= 0 or not SHA256_RE.fullmatch(digest):
                raise ManagerError("saved Hugging Face source lacks expected size or SHA-256")
            artifact.update(url=override["url"], size=size, sha256=digest, source_type="huggingface", verified=True)
        elif source_type == "custom":
            if source.get("responsibility_acknowledged") is not True or override.get("responsibility_acknowledged") is not True:
                raise ManagerError("saved custom source lacks responsibility acknowledgement")
            _validate_public_https_url(str(override.get("url") or ""))
            if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size <= 0):
                raise ManagerError("saved custom source has an invalid expected size")
            if digest and not SHA256_RE.fullmatch(digest):
                raise ManagerError("saved custom source has an invalid SHA-256")
            artifact.update(
                url=override["url"], size=size, sha256=digest, source_type="custom",
                verified=bool(digest), responsibility_acknowledged=True,
            )
        else:
            raise ManagerError("saved source configuration has an unknown source type")
    return effective


def validate_remote_artifacts(
    manifest: dict[str, Any],
    *,
    token: str | None = None,
    opener: Callable[..., Any] = secure_urlopen,
) -> list[dict[str, Any]]:
    """Validate every downloadable URL without transferring artifact bodies."""
    results: list[dict[str, Any]] = []
    metadata_cache: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for artifact in manifest["artifacts"]:
        if artifact["delivery"] != "download":
            continue
        url = str(artifact["url"])
        parsed = urlparse(url)
        if (parsed.hostname or "").lower() in HF_SOURCE_HOSTS:
            repo_id, revision, file_path = parse_hf_file_url(url)
            key = (repo_id, revision)
            if key not in metadata_cache:
                metadata_cache[key] = hf_repo_metadata(repo_id, revision, token=token, opener=opener)
            metadata = metadata_cache[key].get(file_path)
            if metadata is None:
                raise ManagerError(f"{artifact['id']}: remote file is missing or has no LFS metadata")
            if int(metadata["size"]) != int(artifact["size"]):
                raise ManagerError(f"{artifact['id']}: remote size does not match the manifest")
            if str(metadata["sha256"]).lower() != str(artifact["sha256"]).lower():
                raise ManagerError(f"{artifact['id']}: remote LFS SHA-256 does not match the manifest")
        else:
            headers = {"User-Agent": "MLACStudio-ModelManager/1", "Range": "bytes=0-0", "Accept-Encoding": "identity"}
            request = Request(url, headers=headers)
            try:
                with opener(request, timeout=60) as response:
                    status = getattr(response, "status", None) or response.getcode()
                    if status not in {200, 206}:
                        raise ManagerError(f"{artifact['id']}: remote URL returned HTTP {status}")
                    content_range = str(response.headers.get("Content-Range", ""))
                    match = re.fullmatch(r"bytes\s+0-0/(\d+)", content_range)
                    remote_size = int(match.group(1)) if match else int(response.headers.get("Content-Length", 0))
                    if remote_size != int(artifact["size"]):
                        raise ManagerError(f"{artifact['id']}: remote size does not match the manifest")
            except ManagerError:
                raise
            except OSError as exc:
                raise ManagerError(f"{artifact['id']}: remote validation failed: {exc}") from exc
        results.append({"id": artifact["id"], "url": url, "size": artifact["size"], "sha256": artifact["sha256"]})
    return results


def hydrate_remote_artifacts(
    manifest: dict[str, Any],
    *,
    token: str | None = None,
    opener: Callable[..., Any] = secure_urlopen,
) -> dict[str, Any]:
    """Fill missing integrity metadata only from immutable Hugging Face LFS records."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get("artifacts"), list):
        raise ManagerError("manifest artifacts must be an array")
    metadata_cache: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for artifact in manifest["artifacts"]:
        if not isinstance(artifact, dict) or artifact.get("delivery") != "download":
            continue
        if _expected_size(artifact) is not None and SHA256_RE.fullmatch(str(artifact.get("sha256") or "")):
            continue
        url = str(artifact.get("url") or "")
        if _url_host(url) not in HF_SOURCE_HOSTS:
            raise ManagerError(f"{artifact.get('id', '(blank)')}: non-Hugging Face downloads require pinned size and SHA-256")
        repo_id, revision, file_path = parse_hf_file_url(url)
        key = (repo_id, revision)
        if key not in metadata_cache:
            metadata_cache[key] = hf_repo_metadata(repo_id, revision, token=token, opener=opener)
        metadata = metadata_cache[key].get(file_path)
        if metadata is None:
            raise ManagerError(f"{artifact.get('id', '(blank)')}: remote file is missing LFS size/SHA-256 metadata")
        artifact["size"] = int(metadata["size"])
        artifact["sha256"] = str(metadata["sha256"]).lower()
    return validate_manifest(manifest)


def _relative_artifact_path(value: Any) -> Path:
    text = str(value or "")
    normalized = text.replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(text)
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    invalid_part = any(
        part in {"", ".", ".."}
        or ":" in part
        or part.endswith((" ", "."))
        or part.split(".", 1)[0].upper() in reserved
        for part in posix.parts
    )
    if not text or posix.is_absolute() or windows.drive or windows.root or invalid_part:
        raise ManagerError(f"artifact path must be a safe relative path: {text or '(blank)'}")
    return Path(*posix.parts)


def _archive_spec(artifact: dict[str, Any]) -> dict[str, Any] | None:
    value = artifact.get("archive")
    return value if isinstance(value, dict) else None


def _artifact_roles(artifact: dict[str, Any]) -> dict[str, Path]:
    role = artifact.get("role")
    if role is not None:
        return {str(role): Path()}
    archive = _archive_spec(artifact)
    if archive is None:
        return {}
    members = archive.get("members")
    if not isinstance(members, dict):
        return {}
    return {str(member_role): _relative_artifact_path(member_path) for member_role, member_path in members.items()}


def _archive_destination(root: Path, artifact: dict[str, Any]) -> Path:
    archive = _archive_spec(artifact)
    if archive is None:
        raise ManagerError(f"{artifact['id']}: archive configuration is missing")
    return _resolved_target(root, _relative_artifact_path(archive.get("extract_to")))


def _archive_member_paths(root: Path, artifact: dict[str, Any]) -> dict[str, Path]:
    destination = _archive_destination(root, artifact)
    return {
        role: _resolved_target(destination, relative)
        for role, relative in _artifact_roles(artifact).items()
    }


def _artifacts_by_id(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["id"]): item for item in manifest["artifacts"]}


def validate_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise ManagerError("manifest must contain a JSON object")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ManagerError(f"manifest schema_version must be {SCHEMA_VERSION}")
    if not isinstance(manifest.get("release_version"), str) or not manifest["release_version"].strip():
        raise ManagerError("manifest release_version is required")
    licenses = manifest.get("licenses")
    if not isinstance(licenses, dict):
        raise ManagerError("manifest licenses must be an object")
    for license_id, license_info in licenses.items():
        if not isinstance(license_id, str) or not license_id or not isinstance(license_info, dict):
            raise ManagerError("manifest licenses must have non-empty names and object definitions")
        if not isinstance(license_info.get("acceptance_required"), bool):
            raise ManagerError(f"{license_id}: acceptance_required must be true or false")
        for field in ("name", "version", "model", "text"):
            if not isinstance(license_info.get(field), str) or not license_info[field].strip():
                raise ManagerError(f"{license_id}: license {field} is required")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ManagerError("manifest artifacts must be a non-empty array")

    seen_ids: set[str] = set()
    seen_roles: set[str] = set()
    seen_destinations: set[tuple[str, Path]] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ManagerError("each artifact must be an object")
        artifact_id = str(artifact.get("id") or "")
        if not artifact_id or artifact_id in seen_ids:
            raise ManagerError(f"artifact id is blank or duplicated: {artifact_id or '(blank)'}")
        seen_ids.add(artifact_id)
        delivery = artifact.get("delivery")
        if delivery not in {"download", "bundled"}:
            raise ManagerError(f"{artifact_id}: delivery must be download or bundled")
        if artifact.get("root") not in {"models", "runtime"}:
            raise ManagerError(f"{artifact_id}: root must be models or runtime")
        relative = _relative_artifact_path(artifact.get("path"))
        destination = (str(artifact["root"]), Path(relative.as_posix().lower()))
        if destination in seen_destinations:
            raise ManagerError(f"{artifact_id}: artifact destination is duplicated")
        seen_destinations.add(destination)
        archive = artifact.get("archive")
        if archive is not None:
            if delivery != "download" or artifact.get("root") != "runtime" or not isinstance(archive, dict):
                raise ManagerError(f"{artifact_id}: archives must be downloadable runtime artifacts")
            if archive.get("format") != "zip":
                raise ManagerError(f"{artifact_id}: archive format must be zip")
            _relative_artifact_path(archive.get("extract_to"))
            members = archive.get("members")
            if not isinstance(members, dict) or not members:
                raise ManagerError(f"{artifact_id}: archive members must map runtime roles to paths")
            if set(members) != {"sd_cli", "sd_server"}:
                raise ManagerError(f"{artifact_id}: runtime archives must declare sd_cli and sd_server members")
            if artifact.get("role") is not None:
                raise ManagerError(f"{artifact_id}: archive roles must be declared through archive members")
            for member_role, member_path in members.items():
                if member_role not in REQUIRED_ROLES:
                    raise ManagerError(f"{artifact_id}: unknown archive member role {member_role}")
                _relative_artifact_path(member_path)
        digest = str(artifact.get("sha256") or "")
        if not SHA256_RE.fullmatch(digest):
            raise ManagerError(f"{artifact_id}: sha256 must be exactly 64 hexadecimal characters")
        size = artifact.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ManagerError(f"{artifact_id}: size must be a positive integer")
        if delivery == "download":
            _validate_artifact_url(str(artifact.get("url") or ""))
        elif artifact.get("url") not in {None, ""}:
            raise ManagerError(f"{artifact_id}: bundled artifacts must not have a URL")
        license_id = str(artifact.get("license") or "")
        if license_id not in licenses:
            raise ManagerError(f"{artifact_id}: unknown license {license_id or '(blank)'}")
        for role in _artifact_roles(artifact):
            if role not in REQUIRED_ROLES:
                raise ManagerError(f"{artifact_id}: unknown role {role}")
            if role in seen_roles and role in {"sd_cli", "sd_server", "text_encoder", "mmproj", "vae"}:
                raise ManagerError(f"role may only be declared once: {role}")
            seen_roles.add(role)

    profiles = manifest.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ManagerError("manifest profiles must be a non-empty object")
    for profile_id, profile in profiles.items():
        if not isinstance(profile_id, str) or not profile_id or not isinstance(profile, dict):
            raise ManagerError("profile names and definitions must be non-empty")
        refs = profile.get("artifacts")
        if not isinstance(refs, list) or not refs or not all(isinstance(ref, str) for ref in refs):
            raise ManagerError(f"{profile_id}: artifacts must be a non-empty string array")
        if len(refs) != len(set(refs)) or not set(refs).issubset(seen_ids):
            raise ManagerError(f"{profile_id}: artifact references are duplicated or unknown")
        args = profile.get("extra_args")
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ManagerError(f"{profile_id}: extra_args must be a string array")
        for field in ("name", "model_id", "model_version", "description"):
            if not isinstance(profile.get(field), str) or not profile[field].strip():
                raise ManagerError(f"{profile_id}: {field} is required")
        selected_roles = [
            role
            for item in artifacts
            if item["id"] in refs
            for role in _artifact_roles(item)
        ]
        role_counts = Counter(selected_roles)
        if set(selected_roles) != REQUIRED_ROLES or any(role_counts[role] != 1 for role in REQUIRED_ROLES):
            missing = ", ".join(sorted(REQUIRED_ROLES - set(selected_roles)))
            duplicated = ", ".join(sorted(role for role, count in role_counts.items() if count > 1))
            raise ManagerError(
                f"{profile_id}: role mapping must contain each required role once "
                f"(missing: {missing or 'none'}; duplicated: {duplicated or 'none'})"
            )
        minimum = profile.get("min_vram_mib")
        maximum = profile.get("max_vram_mib")
        if not isinstance(minimum, int) or minimum < 1:
            raise ManagerError(f"{profile_id}: min_vram_mib must be a positive integer")
        if maximum is not None and (not isinstance(maximum, int) or maximum < minimum):
            raise ManagerError(f"{profile_id}: max_vram_mib must be null or at least min_vram_mib")
    return manifest


def load_manifest(source: str | Path, *, opener: Callable[..., Any] = secure_urlopen) -> dict[str, Any]:
    source_text = str(source)
    if urlparse(source_text).scheme.lower() in {"http", "https"}:
        _validate_url(source_text)
        request = Request(source_text, headers={"User-Agent": "MLACStudio-ModelManager/1"})
        try:
            with opener(request, timeout=60) as response:
                final_url = response.geturl() if hasattr(response, "geturl") else source_text
                _validate_public_https_url(final_url)
                payload = response.read(MAX_MANIFEST_BYTES + 1)
        except ManagerError:
            raise
        except OSError as exc:
            raise ManagerError(f"could not download manifest: {exc}") from exc
        if len(payload) > MAX_MANIFEST_BYTES:
            raise ManagerError("manifest is too large")
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManagerError(f"invalid manifest JSON: {exc}") from exc
    else:
        try:
            data = json.loads(Path(source).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManagerError(f"could not read manifest {source}: {exc}") from exc
    return validate_manifest(data)


def select_profile(manifest: dict[str, Any], requested: str, hardware: HardwareInfo) -> str:
    profiles = manifest["profiles"]
    if requested != "auto":
        if requested not in profiles:
            raise ManagerError(f"unknown profile: {requested}")
        return requested
    if hardware.vram_mib is None:
        raise ManagerError("automatic profile selection requires an NVIDIA GPU reported by nvidia-smi")
    compatible = [
        (int(profile["min_vram_mib"]), name)
        for name, profile in profiles.items()
        if hardware.vram_mib >= int(profile["min_vram_mib"])
        and (profile.get("max_vram_mib") is None or hardware.vram_mib <= int(profile["max_vram_mib"]))
    ]
    if not compatible:
        raise ManagerError(f"no supported profile matches {hardware.vram_mib} MiB VRAM")
    return max(compatible)[1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _resolved_target(root: Path, relative: Path) -> Path:
    root = root.resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ManagerError(f"artifact destination escapes managed directory: {relative}") from exc
    return target


def _expected_size(artifact: dict[str, Any]) -> int | None:
    value = artifact.get("size")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _artifact_unverified(artifact: dict[str, Any]) -> bool:
    return artifact.get("source_type") == "custom" and not SHA256_RE.fullmatch(str(artifact.get("sha256") or ""))


def _source_identity(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "url": str(artifact.get("url") or ""),
        "size": _expected_size(artifact),
        "sha256": str(artifact.get("sha256") or "").lower(),
        "source_type": str(artifact.get("source_type") or "packaged"),
    }


def _source_record_path(path: Path) -> Path:
    return path.with_name(path.name + ".source.json")


def _partial_record_path(path: Path) -> Path:
    return path.with_name(path.name + ".meta.json")


def _source_record_matches(path: Path, artifact: dict[str, Any]) -> bool:
    try:
        return json.loads(path.read_text(encoding="utf-8")) == _source_identity(artifact)
    except (OSError, json.JSONDecodeError):
        return False


def verify_file(path: Path, artifact: dict[str, Any], *, require_provenance: bool = True) -> None:
    expected_size = _expected_size(artifact)
    if not path.is_file():
        raise ManagerError(f"required file is missing: {path}")
    actual_size = path.stat().st_size
    if expected_size is not None and actual_size != expected_size:
        raise ManagerError(f"size mismatch for {path.name}: expected {expected_size}, got {actual_size}")
    expected_hash = str(artifact.get("sha256") or "").lower()
    if expected_hash:
        actual_hash = sha256_file(path)
        if actual_hash.lower() != expected_hash:
            raise ManagerError(f"SHA-256 mismatch for {path.name}")
    elif _artifact_unverified(artifact) and require_provenance and not _source_record_matches(_source_record_path(path), artifact):
        raise ManagerError(f"unverified source record is missing or stale for {path.name}")


def download_artifact(
    artifact: dict[str, Any],
    target: Path,
    *,
    opener: Callable[..., Any] = secure_urlopen,
    progress: Callable[[str], None] = print,
    on_progress: Callable[[str, int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    auth_token: str | None = None,
) -> Path:
    expected_size = _expected_size(artifact)
    source_identity = _source_identity(artifact)
    source_record = _source_record_path(target)
    if target.is_file():
        try:
            verify_file(target, artifact)
            progress(f"Verified existing {target.name}")
            if on_progress:
                current_size = target.stat().st_size
                on_progress("verified", current_size, expected_size or current_size)
            return target
        except ManagerError:
            target.unlink()
            source_record.unlink(missing_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    partial_record = _partial_record_path(partial)
    if partial.exists() and not _source_record_matches(partial_record, artifact):
        partial.unlink()
        partial_record.unlink(missing_ok=True)
    offset = partial.stat().st_size if partial.exists() else 0
    if expected_size is not None and offset > expected_size:
        partial.unlink()
        partial_record.unlink(missing_ok=True)
        offset = 0
    elif expected_size is not None and offset == expected_size:
        try:
            verify_file(partial, artifact, require_provenance=False)
        except ManagerError:
            partial.unlink()
            partial_record.unlink(missing_ok=True)
            offset = 0
        else:
            os.replace(partial, target)
            _write_json_atomic(source_record, source_identity)
            partial_record.unlink(missing_ok=True)
            progress(f"Installed {target.name}")
            if on_progress:
                on_progress("complete", expected_size, expected_size)
            return target

    headers = {"User-Agent": "MLACStudio-ModelManager/1", "Accept-Encoding": "identity"}
    if auth_token and _url_host(str(artifact["url"])) in HF_SOURCE_HOSTS:
        headers["Authorization"] = f"Bearer {auth_token}"
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = Request(str(artifact["url"]), headers=headers)
    written = offset
    progress_total = 0
    try:
        with opener(request, timeout=60) as response:
            final_url = response.geturl() if hasattr(response, "geturl") else request.full_url
            _validate_public_https_url(final_url)
            status = getattr(response, "status", None) or response.getcode()
            if status not in {200, 206}:
                raise ManagerError(f"download returned HTTP {status} for {target.name}")
            content_range = str(response.headers.get("Content-Range", ""))
            range_match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range)
            append = bool(offset and status == 206 and range_match and int(range_match.group(1)) == offset)
            if offset and status == 206 and not append:
                partial.unlink(missing_ok=True)
                partial_record.unlink(missing_ok=True)
                raise ManagerError(f"server returned an invalid Range response for {target.name}; retry will restart it")
            if offset and not append:
                offset = 0
            mode = "ab" if append else "wb"
            written = offset
            content_length_text = response.headers.get("Content-Length")
            try:
                content_length = int(content_length_text) if content_length_text is not None else None
            except (TypeError, ValueError):
                content_length = None
            if content_length is not None and content_length < 0:
                content_length = None
            if range_match and range_match.group(3) != "*":
                progress_total = int(range_match.group(3))
            elif content_length is not None and content_length >= 0:
                progress_total = offset + content_length
            else:
                progress_total = 0
            _write_json_atomic(partial_record, source_identity)
            if on_progress:
                on_progress("downloading", written, progress_total)
            with partial.open(mode) as output:
                while chunk := response.read(CHUNK_SIZE):
                    if cancelled and cancelled():
                        raise DownloadCancelled(f"download cancelled for {target.name}")
                    output.write(chunk)
                    written += len(chunk)
                    if expected_size is not None and written > expected_size:
                        raise ManagerError(f"download exceeded declared size for {target.name}")
                    if on_progress:
                        on_progress("downloading", written, progress_total)
            if content_length is not None and written - offset != content_length:
                raise ManagerError(f"download ended before Content-Length bytes were received for {target.name}")
    except ManagerError:
        if expected_size is not None and partial.exists() and partial.stat().st_size > expected_size:
            partial.unlink()
            partial_record.unlink(missing_ok=True)
        raise
    except OSError as exc:
        raise ManagerError(f"download failed for {target.name}: {exc}") from exc

    if on_progress:
        on_progress("verifying", written, progress_total or written)
    try:
        verify_file(partial, artifact, require_provenance=False)
    except ManagerError:
        partial.unlink(missing_ok=True)
        partial_record.unlink(missing_ok=True)
        raise
    os.replace(partial, target)
    _write_json_atomic(source_record, source_identity)
    partial_record.unlink(missing_ok=True)
    progress(f"Installed {target.name}")
    if on_progress:
        on_progress("complete", written, progress_total or written)
    return target


def _runtime_archive_ready(root: Path, artifact: dict[str, Any]) -> bool:
    destination = _archive_destination(root, artifact)
    try:
        record = json.loads((destination / ".mlac-runtime-source.json").read_text(encoding="utf-8"))
        return record == _source_identity(artifact) and all(
            path.is_file() for path in _archive_member_paths(root, artifact).values()
        )
    except (OSError, json.JSONDecodeError, ManagerError):
        return False


def _extract_runtime_archive(archive_path: Path, root: Path, artifact: dict[str, Any]) -> None:
    if _runtime_archive_ready(root, artifact):
        return
    destination = _archive_destination(root, artifact)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.extract"
    backup = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.backup"
    stage.mkdir()
    try:
        total_size = 0
        extracted_paths: set[str] = set()
        with zipfile.ZipFile(archive_path) as archive:
            for entry in archive.infolist():
                if entry.flag_bits & 0x1:
                    raise ManagerError(f"{artifact['id']}: encrypted runtime archives are not supported")
                mode = (entry.external_attr >> 16) & 0xFFFF
                if mode & 0o170000 == 0o120000:
                    raise ManagerError(f"{artifact['id']}: runtime archive links are not allowed")
                relative = _relative_artifact_path(entry.filename)
                relative_key = relative.as_posix().lower()
                if relative_key in extracted_paths:
                    raise ManagerError(f"{artifact['id']}: runtime archive contains duplicate paths")
                extracted_paths.add(relative_key)
                if relative.suffix.lower() in {".gguf", ".safetensors", ".ckpt", ".pt", ".pth"}:
                    raise ManagerError(f"{artifact['id']}: model weights are forbidden in runtime archives")
                total_size += int(entry.file_size)
                if total_size > MAX_RUNTIME_ARCHIVE_BYTES:
                    raise ManagerError(f"{artifact['id']}: expanded runtime archive is too large")
                target = _resolved_target(stage, relative)
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, CHUNK_SIZE)
        missing = [
            role
            for role, relative in _artifact_roles(artifact).items()
            if not _resolved_target(stage, relative).is_file()
        ]
        if missing:
            raise ManagerError(f"{artifact['id']}: runtime archive is missing required members: {', '.join(sorted(missing))}")
        _write_json_atomic(stage / ".mlac-runtime-source.json", _source_identity(artifact))
        if destination.exists():
            os.replace(destination, backup)
        try:
            os.replace(stage, destination)
        except OSError:
            if backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ManagerError(f"{artifact['id']}: could not extract runtime archive: {exc}") from exc
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def _materialize_artifact(root: Path, artifact: dict[str, Any], target: Path) -> None:
    if _archive_spec(artifact) is not None:
        _extract_runtime_archive(target, root, artifact)


def _record_installed_paths(installed: dict[str, Path], root: Path, artifact: dict[str, Any], target: Path) -> None:
    installed[str(artifact["id"])] = target
    role = artifact.get("role")
    if role is not None:
        installed[f"@{role}"] = target
    if _archive_spec(artifact) is not None:
        for archive_role, member_path in _archive_member_paths(root, artifact).items():
            installed[f"@{archive_role}"] = member_path


def _selected_artifacts(manifest: dict[str, Any], profile_id: str) -> list[dict[str, Any]]:
    by_id = _artifacts_by_id(manifest)
    return [by_id[artifact_id] for artifact_id in manifest["profiles"][profile_id]["artifacts"]]


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, indent=2, ensure_ascii=True)
            output.write("\n")
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def generate_config(
    manifest: dict[str, Any],
    profile_id: str,
    installed: dict[str, Path],
    config_path: Path,
    data_dir: Path,
) -> dict[str, Any]:
    roles = {
        key[1:]: str(path.resolve())
        for key, path in installed.items()
        if key.startswith("@")
    }
    if set(roles) != REQUIRED_ROLES:
        raise ManagerError("cannot generate config without every required runtime/model role")
    profile = manifest["profiles"][profile_id]
    config = {
        "port": 8730,
        "data_dir": str(data_dir.resolve()),
        "sd_cli": roles["sd_cli"],
        "sd_server": roles["sd_server"],
        "sd_server_idle_timeout": 300,
        "transformer": roles["transformer"],
        "text_encoder": roles["text_encoder"],
        "mmproj": roles["mmproj"],
        "vae": roles["vae"],
        "extra_args": list(profile["extra_args"]),
        "packaging": {
            "release_version": manifest["release_version"],
            "profile": profile_id,
            "unverified": any(_artifact_unverified(item) for item in _selected_artifacts(manifest, profile_id)),
            "unverified_artifacts": [
                str(item["id"]) for item in _selected_artifacts(manifest, profile_id) if _artifact_unverified(item)
            ],
        },
    }
    _write_json_atomic(config_path, config)
    return config


def install_profile(
    manifest: dict[str, Any],
    profile_id: str,
    *,
    accept_license: bool,
    model_dir: Path,
    runtime_dir: Path,
    config_path: Path,
    data_dir: Path,
    opener: Callable[..., Any] = secure_urlopen,
    progress: Callable[[str], None] = print,
) -> dict[str, Any]:
    selected = _selected_artifacts(manifest, profile_id)
    required_license_ids = sorted(
        {
            str(artifact["license"])
            for artifact in selected
            if bool(manifest["licenses"][str(artifact["license"])].get("acceptance_required"))
        }
    )
    if required_license_ids and not accept_license:
        raise ManagerError(
            "license acceptance is required before download; re-run with --accept-license after reviewing the notices"
        )

    model_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    required_bytes = {"models": 0, "runtime": 0}
    for item in selected:
        if item["delivery"] != "download":
            continue
        root = model_dir if item["root"] == "models" else runtime_dir
        target = _resolved_target(root, _relative_artifact_path(item["path"]))
        try:
            verify_file(target, item)
        except ManagerError:
            required_bytes[str(item["root"])] += int(item["size"])
    for root_name, root in (("models", model_dir), ("runtime", runtime_dir)):
        needed = required_bytes[root_name]
        if shutil.disk_usage(root).free < needed:
            required_mib = needed // (1024 * 1024)
            raise ManagerError(f"insufficient free disk space under {root}; about {required_mib} MiB is required")

    installed: dict[str, Path] = {}
    for artifact in selected:
        root = model_dir if artifact["root"] == "models" else runtime_dir
        target = _resolved_target(root, _relative_artifact_path(artifact["path"]))
        if artifact["delivery"] == "download":
            target = download_artifact(artifact, target, opener=opener, progress=progress)
        else:
            verify_file(target, artifact)
            progress(f"Verified bundled {target.name}")
        _materialize_artifact(root, artifact, target)
        _record_installed_paths(installed, root, artifact, target)

    config = generate_config(manifest, profile_id, installed, config_path, data_dir)
    if required_license_ids:
        acceptance = {
            "acceptances": [
                {
                    "license_id": item["id"],
                    "version": item["version"],
                    "model": item["model"],
                    "profile": profile_id,
                    "release_version": manifest["release_version"],
                    "accepted_at": datetime.now(timezone.utc).isoformat(),
                }
                for item in required_licenses(manifest, profile_id)
            ]
        }
        _write_json_atomic(config_path.with_name("license-acceptance.json"), acceptance)
    return config


def required_licenses(manifest: dict[str, Any], profile_id: str) -> list[dict[str, str]]:
    """Return the exact model/version license records required by a profile."""
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for artifact in _selected_artifacts(manifest, profile_id):
        license_id = str(artifact["license"])
        info = manifest["licenses"][license_id]
        if not info["acceptance_required"] or license_id in seen:
            continue
        seen.add(license_id)
        result.append(
            {
                "id": license_id,
                "name": str(info["name"]),
                "version": str(info["version"]),
                "model": str(info["model"]),
                "text": str(info["text"]),
            }
        )
    return sorted(result, key=lambda item: item["id"])


class PersistentModelManager:
    """Single background model installer with restart-safe state and resumable files."""

    STATE_SCHEMA = 1
    ACTIVE_STATES = {"queued", "downloading", "verifying", "configuring", "cancelling"}

    def __init__(
        self,
        manifest_path: Path,
        *,
        model_dir: Path,
        runtime_dir: Path,
        config_path: Path,
        data_dir: Path,
        opener: Callable[..., Any] = secure_urlopen,
        hardware: HardwareInfo | None = None,
        can_mutate: Callable[[], bool] | None = None,
        on_configured: Callable[[dict[str, Any]], None] | None = None,
        credential_store: Any | None = None,
    ) -> None:
        self.manifest_path = Path(manifest_path)
        self.packaged_manifest = load_manifest(self.manifest_path)
        self.model_dir = Path(model_dir)
        self.runtime_dir = Path(runtime_dir)
        self.config_path = Path(config_path)
        self.data_dir = Path(data_dir)
        self.state_path = self.config_path.with_name("model-manager-state.json")
        self.registry_path = self.config_path.with_name("installed-profiles.json")
        self.acceptance_path = self.config_path.with_name("license-acceptance.json")
        self.source_path = self.config_path.with_name("hf-source.json")
        self.opener = opener
        self.credential_store = credential_store or KeyringCredentialStore()
        self.hardware = hardware or detect_hardware(self.model_dir)
        self.can_mutate = can_mutate or (lambda: True)
        self.on_configured = on_configured or (lambda config: None)
        self.lock = threading.RLock()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.subscribers: list[queue.Queue[str]] = []
        self.source_previews: dict[str, tuple[float, dict[str, Any]]] = {}
        self.registry = self._read_json(self.registry_path, {"active_profile": None, "profiles": {}})
        self.acceptances = self._read_json(self.acceptance_path, {"acceptances": []})
        self.source_state = self._read_json(self.source_path, {"schema_version": 1, "credential_ref": None, "override": None})
        self._validate_source_state()
        self.manifest = apply_source_overrides(self.packaged_manifest, self.source_state.get("override"))
        self.transfer = self._read_json(self.state_path, self._idle_transfer())
        self._migrate_existing_config()
        if self.transfer.get("status") in self.ACTIVE_STATES:
            self.transfer["status"] = "paused"
            self.transfer["stage"] = "paused"
            self.transfer["error"] = "The application stopped during setup. Resume to continue from partial files."
            self._persist_transfer()

    @staticmethod
    def _read_json(path: Path, fallback: Any) -> Any:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, type(fallback)) else fallback
        except (OSError, json.JSONDecodeError):
            return fallback

    @classmethod
    def _idle_transfer(cls) -> dict[str, Any]:
        return {
            "schema_version": cls.STATE_SCHEMA,
            "task_id": None,
            "profile_id": None,
            "status": "idle",
            "stage": "idle",
            "files": [],
            "current_file": None,
            "bytes_downloaded": 0,
            "bytes_total": 0,
            "percent": 0.0,
            "speed_bps": 0.0,
            "eta_seconds": None,
            "error": None,
            "activate": False,
            "indeterminate": False,
            "unverified": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _persist_transfer(self) -> None:
        self.transfer["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_json_atomic(self.state_path, self.transfer)

    def _validate_source_state(self) -> None:
        if not isinstance(self.source_state, dict) or self.source_state.get("schema_version") != 1:
            raise ManagerError("saved Hugging Face source configuration is invalid")
        reference = self.source_state.get("credential_ref")
        if reference is not None and not re.fullmatch(r"[0-9a-f]{32}", str(reference)):
            raise ManagerError("saved Hugging Face credential reference is invalid")
        override = self.source_state.get("override")
        if override is not None:
            apply_source_overrides(self.packaged_manifest, override)

    def _persist_source(self) -> None:
        _write_json_atomic(self.source_path, self.source_state)

    def _token(self) -> str | None:
        reference = self.source_state.get("credential_ref")
        if not reference:
            return None
        try:
            return self.credential_store.get(str(reference))
        except Exception as exc:
            raise ManagerError("could not read the Hugging Face token from Windows Credential Manager") from exc

    def _safe_error(self, error: BaseException) -> str:
        message = str(error)
        token = None
        with contextlib.suppress(Exception):
            token = self._token()
        return redact_secret(message, token)

    def _broadcast(self) -> None:
        payload = json.dumps({"type": "models", "data": self.status()}, ensure_ascii=True)
        with self.lock:
            subscribers = list(self.subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(payload)
            except queue.Full:
                with contextlib.suppress(queue.Empty):
                    subscriber.get_nowait()
                with contextlib.suppress(queue.Full):
                    subscriber.put_nowait(payload)

    def subscribe(self) -> queue.Queue[str]:
        subscriber: queue.Queue[str] = queue.Queue(maxsize=100)
        with self.lock:
            self.subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[str]) -> None:
        with self.lock:
            with contextlib.suppress(ValueError):
                self.subscribers.remove(subscriber)

    def _compatible(self, profile: dict[str, Any]) -> bool:
        vram = self.hardware.vram_mib
        if (
            not self.hardware.windows_x64
            or not self.hardware.gpu_name
            or vram is None
            or self.hardware.cuda12_driver_compatible is False
        ):
            return False
        maximum = profile.get("max_vram_mib")
        return vram >= int(profile["min_vram_mib"]) and (maximum is None or vram <= int(maximum))

    def _accepted_keys(self) -> set[tuple[str, str, str]]:
        entries = self.acceptances.get("acceptances", []) if isinstance(self.acceptances, dict) else []
        return {
            (str(item.get("license_id")), str(item.get("version")), str(item.get("model")))
            for item in entries
            if isinstance(item, dict)
        }

    def _profile_installed(self, profile_id: str) -> bool:
        try:
            for artifact in _selected_artifacts(self.manifest, profile_id):
                root = self.model_dir if artifact["root"] == "models" else self.runtime_dir
                target = _resolved_target(root, _relative_artifact_path(artifact["path"]))
                if not target.is_file():
                    return False
                expected_size = _expected_size(artifact)
                if expected_size is not None and target.stat().st_size != expected_size:
                    return False
                if _artifact_unverified(artifact) and not _source_record_matches(_source_record_path(target), artifact):
                    return False
                if _archive_spec(artifact) is not None and not _runtime_archive_ready(root, artifact):
                    return False
            return True
        except (ManagerError, OSError):
            return False

    def _profile_integrity(self, profile_id: str, manifest: dict[str, Any] | None = None) -> str:
        selected = _selected_artifacts(manifest or self.manifest, profile_id)
        payload = [
            {
                "id": item["id"], "url": item.get("url"), "size": item.get("size"),
                "sha256": str(item.get("sha256") or "").lower(),
                "source_type": item.get("source_type", "packaged"),
            }
            for item in selected
        ]
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _profile_unverified(self, profile_id: str) -> bool:
        return any(_artifact_unverified(item) for item in _selected_artifacts(self.manifest, profile_id))

    def _registry_matches_source(self, profile_id: str) -> bool:
        record = self.registry.get("profiles", {}).get(profile_id, {})
        stored = record.get("source_integrity") if isinstance(record, dict) else None
        if stored:
            return stored == self._profile_integrity(profile_id)
        return self._profile_integrity(profile_id) == self._profile_integrity(profile_id, self.packaged_manifest)

    def _migrate_existing_config(self) -> None:
        if self.registry.get("active_profile") or not self.config_path.is_file():
            return
        config = self._read_json(self.config_path, {})
        packaging = config.get("packaging", {}) if isinstance(config, dict) else {}
        profile_id = packaging.get("profile") if isinstance(packaging, dict) else None
        if profile_id in self.manifest["profiles"] and self._profile_installed(str(profile_id)):
            self.registry = {
                "active_profile": profile_id,
                "profiles": {
                    str(profile_id): {
                        "installed_at": datetime.now(timezone.utc).isoformat(),
                        "release_version": self.manifest["release_version"],
                        "model_id": self.manifest["profiles"][str(profile_id)]["model_id"],
                        "model_version": self.manifest["profiles"][str(profile_id)]["model_version"],
                        "source_integrity": self._profile_integrity(str(profile_id)),
                        "unverified": self._profile_unverified(str(profile_id)),
                    }
                },
            }
            _write_json_atomic(self.registry_path, self.registry)

    def source_status(self) -> dict[str, Any]:
        overrides = self.source_state.get("override", {}).get("artifacts", {}) if self.source_state.get("override") else {}
        responsibility_acknowledged = bool(
            self.source_state.get("override", {}).get("responsibility_acknowledged")
            if self.source_state.get("override") else False
        )
        artifacts = []
        for item in self.manifest["artifacts"]:
            if item["delivery"] != "download":
                continue
            source_type = str(item.get("source_type") or "packaged")
            entry = {
                "id": item["id"], "role": item.get("role"), "roles": sorted(_artifact_roles(item)), "url": item["url"],
                "size": item.get("size"), "sha256": item.get("sha256") or "",
                "source_type": source_type, "verified": not _artifact_unverified(item),
                "unverified": _artifact_unverified(item), "overridden": str(item["id"]) in overrides,
            }
            if _url_host(str(item["url"])) in HF_SOURCE_HOSTS:
                repo_id, revision, file_path = parse_hf_file_url(item["url"])
                entry.update(repo_id=repo_id, revision=revision, file_path=file_path)
            artifacts.append(entry)
        token_configured = False
        if self.source_state.get("credential_ref"):
            with contextlib.suppress(Exception):
                token_configured = bool(self._token())
        return {
            "override_enabled": bool(overrides),
            "responsibility_acknowledged": responsibility_acknowledged,
            "unverified": any(item["unverified"] for item in artifacts),
            "artifacts": artifacts,
            "token": {
                "backend_available": bool(self.credential_store.available()),
                "configured": token_configured,
            },
        }

    def resolve_source(self, request_data: Any) -> dict[str, Any]:
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ManagerError("cannot change model sources while a download is running")
        try:
            resolved = resolve_hf_source(self.packaged_manifest, request_data, token=self._token(), opener=self.opener)
        except Exception as exc:
            raise ManagerError(self._safe_error(exc)) from exc
        confirmation_id = uuid.uuid4().hex
        self.source_previews[confirmation_id] = (time.monotonic() + 600, resolved)
        return {"confirmation_id": confirmation_id, "artifacts": resolved["preview"], "expires_in": 600}

    def confirm_source(self, confirmation_id: str) -> dict[str, Any]:
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ManagerError("cannot change model sources while a download is running")
            preview = self.source_previews.pop(str(confirmation_id or ""), None)
            if preview is None or preview[0] < time.monotonic():
                raise ManagerError("source confirmation expired; resolve the source again")
            old_manifest = self.manifest
            override = {key: value for key, value in preview[1].items() if key != "preview"}
            new_manifest = apply_source_overrides(self.packaged_manifest, override)
            self._discard_changed_partials(old_manifest, new_manifest)
            self.source_state["override"] = override
            self.source_state["confirmed_at"] = datetime.now(timezone.utc).isoformat()
            self._persist_source()
            self.manifest = new_manifest
        self._broadcast()
        return self.status()

    def reset_source(self) -> dict[str, Any]:
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ManagerError("cannot change model sources while a download is running")
            old_manifest = self.manifest
            self._discard_changed_partials(old_manifest, self.packaged_manifest)
            self.source_state["override"] = None
            self.source_state.pop("confirmed_at", None)
            self._persist_source()
            self.manifest = self.packaged_manifest
            self.source_previews.clear()
        self._broadcast()
        return self.status()

    def _discard_changed_partials(self, old_manifest: dict[str, Any], new_manifest: dict[str, Any]) -> None:
        old_by_id = _artifacts_by_id(old_manifest)
        for artifact in new_manifest["artifacts"]:
            old = old_by_id.get(str(artifact["id"]))
            if old is None or (old["url"], old["size"], old["sha256"]) == (
                artifact["url"], artifact["size"], artifact["sha256"]
            ):
                continue
            root = self.model_dir if artifact["root"] == "models" else self.runtime_dir
            target = _resolved_target(root, _relative_artifact_path(artifact["path"]))
            partial = target.with_name(target.name + ".part")
            partial.unlink(missing_ok=True)
            _partial_record_path(partial).unlink(missing_ok=True)

    def set_token(self, token: Any) -> dict[str, Any]:
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ManagerError("cannot change the Hugging Face token while a download is running")
        value = str(token or "")
        if not value or len(value) > 4096 or any(character.isspace() for character in value):
            raise ManagerError("Hugging Face token must be a non-empty value without whitespace")
        if not self.credential_store.available():
            raise ManagerError("Windows Credential Manager is unavailable; the token was not stored")
        reference = str(self.source_state.get("credential_ref") or uuid.uuid4().hex)
        try:
            self.credential_store.set(reference, value)
        except Exception as exc:
            raise ManagerError("could not store the Hugging Face token in Windows Credential Manager") from exc
        self.source_state["credential_ref"] = reference
        self._persist_source()
        self._broadcast()
        return self.status()

    def test_token(self) -> dict[str, Any]:
        token = self._token()
        if not token:
            raise ManagerError("no Hugging Face token is stored")
        api_url = "https://huggingface.co/api/whoami-v2"
        request = Request(api_url, headers=_authorized_headers(api_url, token))
        try:
            with self.opener(request, timeout=30) as response:
                status = getattr(response, "status", None) or response.getcode()
                if status != 200:
                    raise ManagerError(f"Hugging Face token test returned HTTP {status}")
                data = _read_json_response(response, maximum=1024 * 1024)
        except Exception as exc:
            raise ManagerError(self._safe_error(exc)) from exc
        return {"ok": True, "name": str(data.get("name") or data.get("fullname") or "authenticated")}

    def remove_token(self) -> dict[str, Any]:
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ManagerError("cannot change the Hugging Face token while a download is running")
        reference = self.source_state.get("credential_ref")
        if reference:
            try:
                self.credential_store.delete(str(reference))
            except Exception as exc:
                raise ManagerError("could not remove the Hugging Face token from Windows Credential Manager") from exc
        self.source_state["credential_ref"] = None
        self._persist_source()
        self._broadcast()
        return self.status()

    def catalog(self) -> dict[str, Any]:
        installed_registry = self.registry.get("profiles", {}) if isinstance(self.registry, dict) else {}
        accepted = self._accepted_keys()
        profiles = []
        for profile_id, profile in self.manifest["profiles"].items():
            licenses = required_licenses(self.manifest, profile_id)
            selected_downloads = [item for item in _selected_artifacts(self.manifest, profile_id) if item["delivery"] == "download"]
            expected_sizes = [_expected_size(item) for item in selected_downloads]
            record = installed_registry.get(profile_id, {}) if isinstance(installed_registry, dict) else {}
            profiles.append(
                {
                    "id": profile_id,
                    "name": profile["name"],
                    "description": profile["description"],
                    "model_id": profile["model_id"],
                    "model_version": profile["model_version"],
                    "min_vram_mib": profile["min_vram_mib"],
                    "max_vram_mib": profile.get("max_vram_mib"),
                    "compatible": self._compatible(profile),
                    "installed": (
                        profile_id in installed_registry
                        and self._registry_matches_source(profile_id)
                        and self._profile_installed(profile_id)
                    ),
                    "active": self.registry.get("active_profile") == profile_id,
                    "unverified": self._profile_unverified(profile_id),
                    "installed_unverified": bool(record.get("unverified")) if isinstance(record, dict) else False,
                    "download_bytes": sum(size for size in expected_sizes if size is not None)
                    if all(size is not None for size in expected_sizes) else None,
                    "licenses": [
                        {
                            **item,
                            "accepted": (item["id"], item["version"], item["model"]) in accepted,
                        }
                        for item in licenses
                    ],
                }
            )
        recommended = None
        with contextlib.suppress(ManagerError):
            recommended = select_profile(self.manifest, "auto", self.hardware)
        active_profile = self.registry.get("active_profile")
        active_record = installed_registry.get(active_profile, {}) if active_profile in installed_registry else {}
        hardware_warnings: list[str] = []
        if not self.hardware.windows_x64:
            hardware_warnings.append("This build is intended for 64-bit Windows; installation is allowed but may not run.")
        if not self.hardware.gpu_name:
            hardware_warnings.append("No NVIDIA GPU was detected; installation is allowed but inference may fail or be very slow.")
        if self.hardware.cuda12_driver_compatible is False:
            hardware_warnings.append(
                f"NVIDIA driver {self.hardware.driver_version} is older than the recommended CUDA 12 driver; installation is allowed at your own risk."
            )
        if recommended is None:
            hardware_warnings.append("This machine is below all recommended model profiles; you may still choose and install any profile.")
        return {
            "release_version": self.manifest["release_version"],
            "hardware": asdict(self.hardware),
            "hardware_warnings": hardware_warnings,
            "recommended_profile": recommended,
            "active_profile": self.registry.get("active_profile"),
            "active_model": {
                "profile_id": active_profile,
                "unverified": bool(active_record.get("unverified")) if isinstance(active_record, dict) else False,
                "warning": "Active model includes custom artifacts without SHA-256 verification."
                if isinstance(active_record, dict) and active_record.get("unverified") else None,
            } if active_profile else None,
            "profiles": profiles,
            "source": self.source_status(),
        }

    def status(self) -> dict[str, Any]:
        with self.lock:
            transfer = json.loads(json.dumps(self.transfer))
        return {"available": True, "catalog": self.catalog(), "transfer": transfer}

    def _validate_acceptance(self, profile_id: str, accepted: Any) -> list[dict[str, str]]:
        supplied = {
            (str(item.get("id")), str(item.get("version")), str(item.get("model")))
            for item in (accepted if isinstance(accepted, list) else [])
            if isinstance(item, dict)
        }
        already = self._accepted_keys()
        required = required_licenses(self.manifest, profile_id)
        missing = [
            item for item in required
            if (item["id"], item["version"], item["model"]) not in supplied | already
        ]
        if missing:
            raise ManagerError("explicit acceptance is required for: " + ", ".join(item["name"] for item in missing))
        return [
            item for item in required
            if (item["id"], item["version"], item["model"]) in supplied
        ]

    def start_install(self, profile_id: str, *, accepted: Any, activate: bool = True) -> dict[str, Any]:
        if profile_id not in self.manifest["profiles"]:
            raise ManagerError("unknown profile")
        if activate and not self.can_mutate():
            raise ManagerError("cannot switch models while a render is active or queued")
        newly_accepted = self._validate_acceptance(profile_id, accepted)
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ManagerError("another model download is already running")
            selected = _selected_artifacts(self.manifest, profile_id)
            files = [
                {
                    "id": item["id"],
                    "name": Path(str(item["path"])).name,
                    "stage": "pending",
                    "bytes_downloaded": 0,
                    "bytes_total": _expected_size(item) or 0,
                    "percent": 0.0,
                    "speed_bps": 0.0,
                    "eta_seconds": None,
                    "error": None,
                    "indeterminate": _expected_size(item) is None,
                    "unverified": _artifact_unverified(item),
                }
                for item in selected
            ]
            self.transfer = {
                **self._idle_transfer(),
                "task_id": uuid.uuid4().hex,
                "profile_id": profile_id,
                "status": "queued",
                "stage": "preparing",
                "files": files,
                "bytes_total": sum(_expected_size(item) or 0 for item in selected)
                if all(_expected_size(item) is not None for item in selected) else 0,
                "activate": bool(activate),
                "indeterminate": any(_expected_size(item) is None for item in selected),
                "unverified": any(_artifact_unverified(item) for item in selected),
            }
            self.cancel_event.clear()
            self._record_acceptances(profile_id, newly_accepted)
            self._persist_transfer()
            self.worker = threading.Thread(
                target=self._install_worker,
                args=(profile_id, bool(activate)),
                name="model-download-manager",
                daemon=True,
            )
            self.worker.start()
        self._broadcast()
        return self.status()

    def _set_file_progress(self, artifact_id: str, stage: str, downloaded: int, total: int, started: float) -> None:
        with self.lock:
            current_item = None
            for item in self.transfer["files"]:
                if item["id"] == artifact_id:
                    item.update(
                        stage=stage,
                        bytes_downloaded=downloaded,
                        bytes_total=total,
                        percent=round((downloaded / total * 100) if total else 0.0, 2),
                        indeterminate=not bool(total),
                    )
                    current_item = item
                    break
            completed = sum(int(item["bytes_downloaded"]) for item in self.transfer["files"])
            elapsed = max(0.001, time.monotonic() - started)
            speed = completed / elapsed
            totals = [int(item.get("bytes_total") or 0) for item in self.transfer["files"]]
            all_known = all(total > 0 for total in totals)
            overall_total = sum(totals) if all_known else 0
            remaining = max(0, overall_total - completed) if all_known else 0
            file_remaining = max(0, total - downloaded)
            if current_item is not None:
                current_item["speed_bps"] = round(speed, 2)
                current_item["eta_seconds"] = round(file_remaining / speed, 1) if speed > 0 and total and file_remaining else None
            self.transfer.update(
                current_file=artifact_id,
                stage=stage,
                bytes_downloaded=completed,
                bytes_total=overall_total,
                percent=round((completed / overall_total * 100) if overall_total else 0.0, 2),
                speed_bps=round(speed, 2),
                eta_seconds=round(remaining / speed, 1) if speed > 0 and all_known and remaining else None,
                indeterminate=not all_known,
            )
            self._persist_transfer()
        self._broadcast()

    def _record_acceptances(self, profile_id: str, items: list[dict[str, str]]) -> None:
        if not items:
            return
        entries = self.acceptances.setdefault("acceptances", [])
        existing = self._accepted_keys()
        for item in items:
            key = (item["id"], item["version"], item["model"])
            if key in existing:
                continue
            entries.append(
                {
                    "license_id": item["id"],
                    "version": item["version"],
                    "model": item["model"],
                    "profile": profile_id,
                    "release_version": self.manifest["release_version"],
                    "accepted_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        _write_json_atomic(self.acceptance_path, self.acceptances)

    def _install_worker(self, profile_id: str, activate: bool) -> None:
        started = time.monotonic()
        installed: dict[str, Path] = {}
        try:
            self.model_dir.mkdir(parents=True, exist_ok=True)
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            required_bytes = {"models": 0, "runtime": 0}
            for artifact in _selected_artifacts(self.manifest, profile_id):
                root = self.model_dir if artifact["root"] == "models" else self.runtime_dir
                target = _resolved_target(root, _relative_artifact_path(artifact["path"]))
                try:
                    verify_file(target, artifact)
                except ManagerError:
                    partial = target.with_name(target.name + ".part")
                    partial_size = partial.stat().st_size if partial.is_file() else 0
                    expected_size = _expected_size(artifact)
                    if expected_size is not None:
                        required_bytes[str(artifact["root"])] += max(0, expected_size - partial_size)
            for root_name, root in (("models", self.model_dir), ("runtime", self.runtime_dir)):
                if shutil.disk_usage(root).free < required_bytes[root_name]:
                    raise ManagerError(f"insufficient free disk space under {root}")
            with self.lock:
                self.transfer["status"] = "downloading"
                self._persist_transfer()
            for artifact in _selected_artifacts(self.manifest, profile_id):
                if self.cancel_event.is_set():
                    raise DownloadCancelled("download cancelled")
                root = self.model_dir if artifact["root"] == "models" else self.runtime_dir
                target = _resolved_target(root, _relative_artifact_path(artifact["path"]))
                callback = lambda stage, done, total, ident=str(artifact["id"]): self._set_file_progress(
                    ident, stage, done, total, started
                )
                if artifact["delivery"] == "download":
                    target = download_artifact(
                        artifact,
                        target,
                        opener=self.opener,
                        progress=lambda message: None,
                        on_progress=callback,
                        cancelled=self.cancel_event.is_set,
                        auth_token=self._token(),
                    )
                else:
                    bundled_size = _expected_size(artifact) or 0
                    callback("verifying", bundled_size, bundled_size)
                    verify_file(target, artifact)
                    callback("complete", bundled_size, bundled_size)
                _materialize_artifact(root, artifact, target)
                _record_installed_paths(installed, root, artifact, target)
            profiles = self.registry.setdefault("profiles", {})
            profiles[profile_id] = {
                "installed_at": datetime.now(timezone.utc).isoformat(),
                "release_version": self.manifest["release_version"],
                "model_id": self.manifest["profiles"][profile_id]["model_id"],
                "model_version": self.manifest["profiles"][profile_id]["model_version"],
                "source_integrity": self._profile_integrity(profile_id),
                "unverified": self._profile_unverified(profile_id),
                "unverified_artifacts": [
                    str(item["id"]) for item in _selected_artifacts(self.manifest, profile_id) if _artifact_unverified(item)
                ],
            }
            should_activate = activate and self.can_mutate()
            if should_activate:
                with self.lock:
                    self.transfer["status"] = "configuring"
                    self.transfer["stage"] = "configuring"
                    self._persist_transfer()
                config = generate_config(self.manifest, profile_id, installed, self.config_path, self.data_dir)
                self.registry["active_profile"] = profile_id
                self.on_configured(config)
            _write_json_atomic(self.registry_path, self.registry)
            with self.lock:
                completed_bytes = sum(int(item.get("bytes_downloaded") or 0) for item in self.transfer["files"])
                self.transfer.update(
                    status="completed",
                    stage="completed",
                    current_file=None,
                    bytes_downloaded=completed_bytes,
                    bytes_total=completed_bytes,
                    percent=100.0,
                    eta_seconds=0,
                    indeterminate=False,
                    error=None if should_activate or not activate else "Installed, but activation was deferred because rendering became busy.",
                )
                self._persist_transfer()
        except DownloadCancelled as exc:
            message = self._safe_error(exc)
            with self.lock:
                self._set_current_file_error(message)
                self.transfer.update(status="cancelled", stage="cancelled", error=message, current_file=None)
                self._persist_transfer()
        except Exception as exc:
            message = self._safe_error(exc)
            with self.lock:
                self._set_current_file_error(message)
                self.transfer.update(status="error", stage="error", error=message, current_file=None)
                self._persist_transfer()
        finally:
            self._broadcast()

    def _set_current_file_error(self, message: str) -> None:
        current = self.transfer.get("current_file")
        for item in self.transfer.get("files", []):
            if item.get("id") == current:
                item["error"] = message
                item["stage"] = "error"
                break

    def cancel(self) -> dict[str, Any]:
        with self.lock:
            if not self.worker or not self.worker.is_alive():
                raise ManagerError("no model download is running")
            self.transfer["status"] = "cancelling"
            self.transfer["stage"] = "cancelling"
            self._persist_transfer()
            self.cancel_event.set()
        self._broadcast()
        return self.status()

    def retry(self) -> dict[str, Any]:
        with self.lock:
            profile_id = self.transfer.get("profile_id")
            activate = bool(self.transfer.get("activate", True))
            status = self.transfer.get("status")
        if status not in {"error", "cancelled", "paused"} or not profile_id:
            raise ManagerError("there is no interrupted model download to resume")
        return self.start_install(str(profile_id), accepted=[], activate=activate)

    def switch(self, profile_id: str) -> dict[str, Any]:
        if not self.can_mutate():
            raise ManagerError("cannot switch models while a render is active or queued")
        if (
            profile_id not in self.registry.get("profiles", {})
            or not self._registry_matches_source(profile_id)
            or not self._profile_installed(profile_id)
        ):
            raise ManagerError("profile is not installed")
        installed: dict[str, Path] = {}
        for artifact in _selected_artifacts(self.manifest, profile_id):
            root = self.model_dir if artifact["root"] == "models" else self.runtime_dir
            target = _resolved_target(root, _relative_artifact_path(artifact["path"]))
            verify_file(target, artifact)
            _materialize_artifact(root, artifact, target)
            _record_installed_paths(installed, root, artifact, target)
        config = generate_config(self.manifest, profile_id, installed, self.config_path, self.data_dir)
        self.registry["active_profile"] = profile_id
        _write_json_atomic(self.registry_path, self.registry)
        self.on_configured(config)
        self._broadcast()
        return self.status()

    def delete(self, profile_id: str, confirmation: str) -> dict[str, Any]:
        if confirmation != profile_id:
            raise ManagerError("type the exact profile id to confirm deletion")
        if not self.can_mutate():
            raise ManagerError("cannot delete models while a render is active or queued")
        if profile_id == self.registry.get("active_profile"):
            raise ManagerError("switch to another profile before deleting the active profile")
        profiles = self.registry.get("profiles", {})
        if profile_id not in profiles:
            raise ManagerError("profile is not installed")
        retained_ids: set[str] = set()
        for other_id in profiles:
            if other_id != profile_id:
                retained_ids.update(self.manifest["profiles"][other_id]["artifacts"])
        for artifact in _selected_artifacts(self.manifest, profile_id):
            if artifact["delivery"] != "download" or artifact["id"] in retained_ids:
                continue
            root = self.model_dir if artifact["root"] == "models" else self.runtime_dir
            target = _resolved_target(root, _relative_artifact_path(artifact["path"]))
            target.unlink(missing_ok=True)
            _source_record_path(target).unlink(missing_ok=True)
            partial = target.with_name(target.name + ".part")
            partial.unlink(missing_ok=True)
            _partial_record_path(partial).unlink(missing_ok=True)
            if _archive_spec(artifact) is not None:
                extraction = _archive_destination(root, artifact)
                if extraction.is_dir():
                    shutil.rmtree(extraction)
        del profiles[profile_id]
        _write_json_atomic(self.registry_path, self.registry)
        self._broadcast()
        return self.status()

    def close(self) -> None:
        self.cancel_event.set()
        worker = self.worker
        if worker and worker.is_alive():
            worker.join(timeout=5)


def default_paths() -> tuple[Path, Path, Path, Path]:
    local_app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    state_root = local_app_data / "MLACStudio"
    return state_root / "models", state_root / "runtime", state_root / "config.json", state_root / "data"


def _default_manifest() -> Path:
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    return base / "release-manifest.json"


def build_parser() -> argparse.ArgumentParser:
    model_dir, runtime_dir, config_path, data_dir = default_paths()
    parser = argparse.ArgumentParser(description="MLAC Studio model and runtime manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    detect = subparsers.add_parser("detect", help="report Windows, NVIDIA, memory, and disk information")
    detect.add_argument("--destination", type=Path, default=model_dir)
    detect.add_argument("--json", action="store_true")

    validate = subparsers.add_parser("validate", help="validate a release manifest")
    validate.add_argument("--manifest", default=str(_default_manifest()))

    profiles = subparsers.add_parser("profiles", help="list profiles from a validated manifest")
    profiles.add_argument("--manifest", default=str(_default_manifest()))

    install = subparsers.add_parser("install", help="verify runtime, download weights, and write config.json")
    install.add_argument("--manifest", default=str(_default_manifest()))
    install.add_argument("--profile", default="auto")
    install.add_argument("--accept-license", action="store_true")
    install.add_argument("--model-dir", type=Path, default=model_dir)
    install.add_argument("--runtime-dir", type=Path, default=runtime_dir)
    install.add_argument("--config", type=Path, default=config_path)
    install.add_argument("--data-dir", type=Path, default=data_dir)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "detect":
            info = detect_hardware(args.destination)
            if args.json:
                print(json.dumps(asdict(info), indent=2))
            else:
                for key, value in asdict(info).items():
                    print(f"{key}: {value}")
            return 0
        manifest = load_manifest(args.manifest)
        if args.command == "validate":
            print(f"Manifest {manifest['release_version']} is valid")
            return 0
        if args.command == "profiles":
            for name, profile in manifest["profiles"].items():
                maximum = profile.get("max_vram_mib") or "unbounded"
                print(f"{name}: {profile['min_vram_mib']}..{maximum} MiB VRAM - {profile.get('description', '')}")
            return 0
        hardware = detect_hardware(args.model_dir)
        profile_id = select_profile(manifest, args.profile, hardware)
        install_profile(
            manifest,
            profile_id,
            accept_license=args.accept_license,
            model_dir=args.model_dir,
            runtime_dir=args.runtime_dir,
            config_path=args.config,
            data_dir=args.data_dir,
        )
        print(f"Configuration written to {args.config}")
        return 0
    except ManagerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
