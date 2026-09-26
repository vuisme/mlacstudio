#!/usr/bin/env python3
"""Deterministic MLAC Studio component archive and signed manifest tooling."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import stat
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

from updater import SHA256_DIGEST_INFO, canonical_payload, verify_envelope


FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)
MODEL_SUFFIXES = {".gguf", ".safetensors", ".ckpt", ".pt", ".pth"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_archive(source: Path, output: Path) -> dict[str, Any]:
    source = source.resolve()
    files = sorted(
        path for path in source.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix.lower() not in MODEL_SUFFIXES
    )
    if not files:
        raise SystemExit(f"component source is empty: {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(source).as_posix()
            if "models" in Path(relative).parts:
                raise SystemExit(f"model directory is forbidden in components: {relative}")
            info = zipfile.ZipInfo(relative, FIXED_ZIP_TIME)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    os.replace(temporary, output)
    return {"file": output.name, "size": output.stat().st_size, "sha256": sha256_file(output)}


def sign_payload(payload: dict[str, Any], private_key: Path, key_id: str) -> dict[str, Any]:
    root = ET.fromstring(private_key.read_text(encoding="utf-8"))
    values = {child.tag: int.from_bytes(base64.b64decode(child.text or ""), "big") for child in root}
    modulus, private_exponent = values["Modulus"], values["D"]
    width = (modulus.bit_length() + 7) // 8
    digest_info = SHA256_DIGEST_INFO + hashlib.sha256(canonical_payload(payload)).digest()
    padding = b"\xff" * (width - len(digest_info) - 3)
    encoded = b"\x00\x01" + padding + b"\x00" + digest_info
    signature = pow(int.from_bytes(encoded, "big"), private_exponent, modulus).to_bytes(width, "big")
    return {
        "payload": payload,
        "signature": {"algorithm": "rsa-sha256", "key_id": key_id, "value": base64.b64encode(signature).decode("ascii")},
    }


def command_archive(args: argparse.Namespace) -> None:
    metadata = build_archive(args.source, args.output)
    print(json.dumps(metadata, sort_keys=True))


def command_manifest(args: argparse.Namespace) -> None:
    components = []
    for definition in args.component:
        component_id, kind, archive_text = definition.split("=", 2)
        archive = Path(archive_text).resolve()
        components.append({
            "id": component_id,
            "kind": kind,
            "version": args.version,
            "url": f"https://github.com/vuisme/mlacstudio/releases/download/v{args.version}/{archive.name}",
            "size": archive.stat().st_size,
            "sha256": sha256_file(archive),
        })
    payload = {
        "schema_version": 1,
        "version": args.version,
        "channel": args.channel,
        "published_at": args.published_at,
        "security_mandatory": args.security_mandatory,
        "restart_required": True,
        "changelog": args.changelog,
        "data_migration": args.data_migration,
        "components": components,
    }
    envelope = sign_payload(payload, args.private_key, args.key_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(envelope, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    verify_envelope(envelope, args.public_key)
    print(args.output)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)
    archive = commands.add_parser("archive")
    archive.add_argument("--source", type=Path, required=True)
    archive.add_argument("--output", type=Path, required=True)
    archive.set_defaults(func=command_archive)
    manifest = commands.add_parser("manifest")
    manifest.add_argument("--version", required=True)
    manifest.add_argument("--channel", choices=("stable", "beta", "dev"), default="stable")
    manifest.add_argument("--published-at", required=True, help="Explicit ISO-8601 timestamp for reproducible metadata")
    manifest.add_argument("--changelog", default="")
    manifest.add_argument("--data-migration", default=None)
    manifest.add_argument("--security-mandatory", action="store_true")
    manifest.add_argument("--component", action="append", required=True, metavar="ID=KIND=ARCHIVE")
    manifest.add_argument("--private-key", type=Path, required=True)
    manifest.add_argument("--public-key", type=Path, required=True)
    manifest.add_argument("--key-id", default="mlac-release-2026-01")
    manifest.add_argument("--output", type=Path, required=True)
    manifest.set_defaults(func=command_manifest)
    return root


def main() -> int:
    args = parser().parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
