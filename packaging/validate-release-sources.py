#!/usr/bin/env python3
"""Validate release artifact URLs against immutable remote metadata."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys


def load_manager() -> object:
    path = Path(__file__).with_name("model-manager.py")
    spec = importlib.util.spec_from_file_location("qis_release_source_validator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load model manager: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--token-env", default="HF_TOKEN")
    parser.add_argument("--hydrate", action="store_true", help="fill missing immutable Hugging Face LFS metadata")
    args = parser.parse_args()
    manager = load_manager()
    try:
        if args.hydrate:
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            manifest = manager.hydrate_remote_artifacts(
                manifest, token=os.environ.get(args.token_env) or None
            )
            temporary = args.manifest.with_suffix(args.manifest.suffix + ".tmp")
            temporary.write_text(json.dumps(manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
            os.replace(temporary, args.manifest)
            print(f"hydrated pinned metadata in {args.manifest}")
            return 0
        else:
            manifest = manager.load_manifest(args.manifest)
        results = manager.validate_remote_artifacts(manifest, token=os.environ.get(args.token_env) or None)
    except manager.ManagerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for item in results:
        print(f"verified {item['id']}: {item['size']} bytes {item['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
