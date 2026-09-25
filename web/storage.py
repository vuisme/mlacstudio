"""SQLite metadata and filesystem-backed images for the standalone studio."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import struct
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, UnidentifiedImageError

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_MASK_BYTES = 25 * 1024 * 1024
MAX_MASK_PIXELS = 16 * 1024 * 1024


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def safe_name(value: str, default: str = "image") -> str:
    cleaned = SAFE_NAME.sub("-", Path(value).name.strip()).strip(".-")
    return cleaned[:100] or default


def resolve_under(root: Path, relative: str) -> Path:
    """Resolve an untrusted relative path without allowing traversal or absolute paths."""
    if not relative or Path(relative).is_absolute():
        raise ValueError("invalid media path")
    base = root.resolve()
    target = (base / relative).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ValueError("invalid media path") from exc
    return target


class Repository:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.database = self.data_dir / "studio.db"
        self.upload_dir = self.data_dir / "uploads"
        self.mask_dir = self.data_dir / "masks"
        self.gallery_dir = self.data_dir / "gallery"
        self.work_dir = self.data_dir / "work"
        for directory in (self.data_dir, self.upload_dir, self.mask_dir, self.gallery_dir, self.work_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._init_database()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database, timeout=15, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_database(self) -> None:
        schema = """
        PRAGMA journal_mode = WAL;
        CREATE TABLE IF NOT EXISTS admin (
            id INTEGER PRIMARY KEY CHECK (id = 1), username TEXT NOT NULL,
            salt BLOB NOT NULL, password_hash BLOB NOT NULL,
            iterations INTEGER NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auth_sessions (
            token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, csrf TEXT NOT NULL,
            created_at INTEGER NOT NULL, last_seen INTEGER NOT NULL, expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT NOT NULL,
            attempted_at INTEGER NOT NULL, success INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS login_attempts_lookup ON login_attempts(address, attempted_at);
        CREATE TABLE IF NOT EXISTS app_config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (
            name TEXT PRIMARY KEY, settings TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS inputs (
            id TEXT PRIMARY KEY, session_name TEXT NOT NULL UNIQUE REFERENCES sessions(name) ON DELETE CASCADE,
            stored_name TEXT NOT NULL, original_name TEXT NOT NULL, width INTEGER NOT NULL,
            height INTEGER NOT NULL, relative_path TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS masks (
            id TEXT PRIMARY KEY, session_name TEXT NOT NULL UNIQUE REFERENCES sessions(name) ON DELETE CASCADE,
            input_id TEXT NOT NULL REFERENCES inputs(id) ON DELETE CASCADE,
            relative_path TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
            feather INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS takes (
            id TEXT PRIMARY KEY, session_name TEXT NOT NULL REFERENCES sessions(name) ON DELETE CASCADE,
            relative_path TEXT NOT NULL, params TEXT NOT NULL, starred INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS takes_session ON takes(session_name, created_at DESC);
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, session_name TEXT NOT NULL, status TEXT NOT NULL,
            settings TEXT NOT NULL, input_path TEXT, mask_path TEXT, output_path TEXT NOT NULL,
            progress TEXT NOT NULL, result TEXT, error TEXT, log TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
        );
        """
        with self.connect() as conn:
            conn.executescript(schema)
            columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(jobs)")}
            if "mask_path" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN mask_path TEXT")
            conn.execute(
                "INSERT OR IGNORE INTO sessions(name, settings, created_at) VALUES ('session-1', '{}', ?)",
                (now_iso(),),
            )
            conn.execute("INSERT OR IGNORE INTO app_config(key, value) VALUES ('active_session', 'session-1')")
            conn.execute(
                "UPDATE jobs SET status = 'failed', error = 'Studio stopped before this job completed', "
                "finished_at = ? WHERE status IN ('queued', 'running', 'cancelling')",
                (now_iso(),),
            )

    def get_config(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM app_config WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_config(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO app_config(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def sessions(self) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT name FROM sessions ORDER BY created_at, name").fetchall()
        return [str(row["name"]) for row in rows]

    @property
    def active_session(self) -> str:
        return self.get_config("active_session", "session-1")

    def activate_session(self, name: str) -> dict[str, Any]:
        name = self._session_name(name)
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sessions(name, settings, created_at) VALUES (?, '{}', ?)",
                (name, now_iso()),
            )
            row = conn.execute("SELECT settings FROM sessions WHERE name = ?", (name,)).fetchone()
        self.set_config("active_session", name)
        return {"name": name, "settings": json.loads(row["settings"])}

    def save_settings(self, name: str, settings: dict[str, Any]) -> None:
        name = self._session_name(name)
        with self.connect() as conn:
            if conn.execute("SELECT 1 FROM sessions WHERE name = ?", (name,)).fetchone() is None:
                raise ValueError("session not found")
            conn.execute("UPDATE sessions SET settings = ? WHERE name = ?", (json.dumps(settings), name))

    def session_settings(self, name: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT settings FROM sessions WHERE name = ?", (name,)).fetchone()
        return json.loads(row["settings"]) if row else {}

    def duplicate_session(self, source: str, new_name: str) -> dict[str, Any]:
        source = self._session_name(source)
        new_name = self._session_name(new_name)
        with self.lock, self.connect() as conn:
            row = conn.execute("SELECT settings FROM sessions WHERE name = ?", (source,)).fetchone()
            if not row:
                raise ValueError("session not found")
            try:
                conn.execute(
                    "INSERT INTO sessions(name, settings, created_at) VALUES (?, ?, ?)",
                    (new_name, row["settings"], now_iso()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("a session with that name already exists") from exc
            input_row = conn.execute("SELECT * FROM inputs WHERE session_name = ?", (source,)).fetchone()
            if input_row:
                source_path = resolve_under(self.upload_dir, str(input_row["relative_path"]))
                input_id = uuid.uuid4().hex
                target_name = f"{input_id}{source_path.suffix.lower()}"
                shutil.copy2(source_path, self.upload_dir / target_name)
                conn.execute(
                    "INSERT INTO inputs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (input_id, new_name, input_row["stored_name"], input_row["original_name"],
                     input_row["width"], input_row["height"], target_name, now_iso()),
                )
                mask_row = conn.execute("SELECT * FROM masks WHERE session_name = ?", (source,)).fetchone()
                if mask_row:
                    mask_source = resolve_under(self.mask_dir, str(mask_row["relative_path"]))
                    mask_id = uuid.uuid4().hex
                    mask_name = f"{mask_id}.png"
                    shutil.copy2(mask_source, self.mask_dir / mask_name)
                    conn.execute(
                        "INSERT INTO masks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (mask_id, new_name, input_id, mask_name, mask_row["width"], mask_row["height"],
                         mask_row["feather"], now_iso()),
                    )
        return self.activate_session(new_name)

    def delete_session(self, name: str) -> dict[str, Any]:
        name = self._session_name(name)
        with self.lock, self.connect() as conn:
            inputs = conn.execute("SELECT relative_path FROM inputs WHERE session_name = ?", (name,)).fetchall()
            masks = conn.execute("SELECT relative_path FROM masks WHERE session_name = ?", (name,)).fetchall()
            takes = conn.execute("SELECT relative_path FROM takes WHERE session_name = ?", (name,)).fetchall()
            conn.execute("DELETE FROM sessions WHERE name = ?", (name,))
        for row in inputs:
            self._unlink(self.upload_dir, str(row["relative_path"]))
        for row in masks:
            self._unlink(self.mask_dir, str(row["relative_path"]))
        for row in takes:
            self._unlink(self.gallery_dir, str(row["relative_path"]))
        names = self.sessions()
        return self.activate_session(names[0] if names else "session-1")

    @staticmethod
    def _session_name(name: str) -> str:
        value = str(name or "").strip()
        if not value or len(value) > 80 or any(c in value for c in "\r\n\0"):
            raise ValueError("invalid session name")
        return value

    def inputs(self, session: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM inputs WHERE session_name = ?", (session,)).fetchall()
        return [self._input_json(row) for row in rows]

    @staticmethod
    def _input_json(row: sqlite3.Row) -> dict[str, Any]:
        ident = str(row["id"])
        return {
            "id": ident, "name": str(row["stored_name"]), "original_name": str(row["original_name"]),
            "width": int(row["width"]), "height": int(row["height"]),
            "url": f"/media/uploads/{ident}", "thumb": f"/media/uploads/{ident}",
        }

    def add_upload(self, session: str, original_name: str, source: Path) -> dict[str, Any]:
        suffix = Path(original_name).suffix.lower()
        if suffix not in IMAGE_EXTENSIONS:
            raise ValueError("reference must be PNG, JPEG, or WebP")
        if source.stat().st_size > MAX_UPLOAD_BYTES:
            raise ValueError("reference image is larger than 25 MB")
        try:
            width, height = image_dimensions(source)
        except (OSError, ValueError) as exc:
            raise ValueError("uploaded file is not a valid image") from exc
        ident = uuid.uuid4().hex
        target_name = f"{ident}{suffix}"
        target = self.upload_dir / target_name
        source.replace(target)
        stored_name = safe_name(original_name)
        with self.lock, self.connect() as conn:
            old = conn.execute("SELECT relative_path FROM inputs WHERE session_name = ?", (session,)).fetchone()
            old_mask = conn.execute("SELECT relative_path FROM masks WHERE session_name = ?", (session,)).fetchone()
            conn.execute("DELETE FROM inputs WHERE session_name = ?", (session,))
            conn.execute(
                "INSERT INTO inputs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ident, session, stored_name, Path(original_name).name, width, height, target_name, now_iso()),
            )
        if old:
            self._unlink(self.upload_dir, str(old["relative_path"]))
        if old_mask:
            self._unlink(self.mask_dir, str(old_mask["relative_path"]))
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM inputs WHERE id = ?", (ident,)).fetchone()
        return self._input_json(row)

    def delete_input(self, session: str, name: str) -> None:
        with self.lock, self.connect() as conn:
            row = conn.execute(
                "SELECT relative_path FROM inputs WHERE session_name = ? AND stored_name = ?", (session, name)
            ).fetchone()
            mask_row = conn.execute("SELECT relative_path FROM masks WHERE session_name = ?", (session,)).fetchone()
            conn.execute("DELETE FROM inputs WHERE session_name = ? AND stored_name = ?", (session, name))
        if row:
            self._unlink(self.upload_dir, str(row["relative_path"]))
        if row and mask_row:
            self._unlink(self.mask_dir, str(mask_row["relative_path"]))

    def input_path(self, session: str) -> Path | None:
        with self.connect() as conn:
            row = conn.execute("SELECT relative_path FROM inputs WHERE session_name = ?", (session,)).fetchone()
        return resolve_under(self.upload_dir, str(row["relative_path"])) if row else None

    def mask(self, session: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM masks WHERE session_name = ?", (session,)).fetchone()
        if not row:
            return None
        ident = str(row["id"])
        return {
            "id": ident, "input_id": str(row["input_id"]), "width": int(row["width"]),
            "height": int(row["height"]), "feather": int(row["feather"]),
            "url": f"/media/masks/{ident}",
        }

    def add_mask(self, session: str, source: Path, feather: int = 0) -> dict[str, Any] | None:
        if source.stat().st_size > MAX_MASK_BYTES:
            raise ValueError("mask is larger than 25 MB")
        if not 0 <= feather <= 100:
            raise ValueError("mask feather must be between 0 and 100")
        with self.connect() as conn:
            input_row = conn.execute("SELECT * FROM inputs WHERE session_name = ?", (session,)).fetchone()
        if not input_row:
            raise ValueError("add a reference image before painting a mask")
        try:
            with Image.open(source) as uploaded:
                if uploaded.format != "PNG":
                    raise ValueError("mask must be a PNG image")
                uploaded.load()
                if uploaded.width * uploaded.height > MAX_MASK_PIXELS:
                    raise ValueError("mask is too large")
                if uploaded.size != (int(input_row["width"]), int(input_row["height"])):
                    raise ValueError("mask dimensions must match the reference image")
                rgba = uploaded.convert("RGBA")
                red, green, blue, alpha = rgba.split()
                if ImageChops.difference(red, green).getbbox() or ImageChops.difference(red, blue).getbbox():
                    raise ValueError("mask PNG must be grayscale")
                mask = ImageChops.multiply(red, alpha)
                rgba.close()
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            raise ValueError("uploaded mask is not a valid PNG image") from exc

        if mask.getextrema()[1] == 0:
            mask.close()
            self.delete_mask(session)
            return None

        ident = uuid.uuid4().hex
        target_name = f"{ident}.png"
        target = self.mask_dir / target_name
        mask.save(target, format="PNG", optimize=True)
        mask.close()
        with self.lock, self.connect() as conn:
            old = conn.execute("SELECT relative_path FROM masks WHERE session_name = ?", (session,)).fetchone()
            conn.execute("DELETE FROM masks WHERE session_name = ?", (session,))
            conn.execute(
                "INSERT INTO masks(id, session_name, input_id, relative_path, width, height, feather, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ident, session, input_row["id"], target_name, input_row["width"], input_row["height"],
                 feather, now_iso()),
            )
        if old:
            self._unlink(self.mask_dir, str(old["relative_path"]))
        return self.mask(session)

    def delete_mask(self, session: str) -> None:
        with self.lock, self.connect() as conn:
            row = conn.execute("SELECT relative_path FROM masks WHERE session_name = ?", (session,)).fetchone()
            conn.execute("DELETE FROM masks WHERE session_name = ?", (session,))
        if row:
            self._unlink(self.mask_dir, str(row["relative_path"]))

    def mask_path(self, session: str) -> Path | None:
        with self.connect() as conn:
            row = conn.execute("SELECT relative_path FROM masks WHERE session_name = ?", (session,)).fetchone()
        return resolve_under(self.mask_dir, str(row["relative_path"])) if row else None

    def media_path(self, kind: str, ident: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", ident):
            raise ValueError("invalid media id")
        media = {
            "uploads": ("inputs", self.upload_dir),
            "masks": ("masks", self.mask_dir),
            "gallery": ("takes", self.gallery_dir),
        }
        if kind not in media:
            raise ValueError("invalid media kind")
        table, root = media[kind]
        with self.connect() as conn:
            row = conn.execute(f"SELECT relative_path FROM {table} WHERE id = ?", (ident,)).fetchone()
        if not row:
            raise FileNotFoundError(ident)
        path = resolve_under(root, str(row["relative_path"]))
        if not path.is_file():
            raise FileNotFoundError(ident)
        return path

    def takes(self, session: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM takes WHERE session_name = ? ORDER BY created_at DESC", (session,)
            ).fetchall()
        return [self._take_json(row) for row in rows]

    @staticmethod
    def _take_json(row: sqlite3.Row) -> dict[str, Any]:
        ident = str(row["id"])
        return {
            "id": ident, "url": f"/media/gallery/{ident}", "thumb": f"/media/gallery/{ident}",
            "starred": bool(row["starred"]), "params": json.loads(row["params"]),
        }

    def add_take(self, session: str, source: Path, params: dict[str, Any]) -> str:
        ident = uuid.uuid4().hex
        target_name = f"{ident}.png"
        target = self.gallery_dir / target_name
        source.replace(target)
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO takes(id, session_name, relative_path, params, created_at) VALUES (?, ?, ?, ?, ?)",
                (ident, session, target_name, json.dumps(params), now_iso()),
            )
        return ident

    def delete_take(self, ident: str) -> None:
        with self.lock, self.connect() as conn:
            row = conn.execute("SELECT relative_path FROM takes WHERE id = ?", (ident,)).fetchone()
            conn.execute("DELETE FROM takes WHERE id = ?", (ident,))
        if row:
            self._unlink(self.gallery_dir, str(row["relative_path"]))

    def take_as_input(self, session: str, ident: str) -> dict[str, Any]:
        path = self.media_path("gallery", ident)
        temporary = self.work_dir / f"upload-{uuid.uuid4().hex}{path.suffix}"
        shutil.copy2(path, temporary)
        return self.add_upload(session, path.name, temporary)

    def work_output(self, job_id: str) -> Path:
        directory = self.work_dir / job_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory / "output.png"

    def create_job(self, job: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO jobs(id, session_name, status, settings, input_path, mask_path, output_path, progress, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job["id"], job["session"], job["status"], json.dumps(job["settings"]), job.get("input"),
                 job.get("mask"), job["output"], json.dumps(job["progress"]), job["created"]),
            )

    def update_job(self, ident: str, **values: Any) -> None:
        allowed = {"status", "progress", "result", "error", "log", "started_at", "finished_at"}
        fields: list[str] = []
        args: list[Any] = []
        for key, value in values.items():
            if key not in allowed:
                continue
            fields.append(f"{key} = ?")
            args.append(json.dumps(value) if key in {"progress", "result"} and value is not None else value)
        if fields:
            args.append(ident)
            with self.connect() as conn:
                conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", args)

    def append_job_log(self, ident: str, line: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET log = substr(log || ?, -1048576) WHERE id = ?", (line + "\n", ident))

    def _unlink(self, root: Path, relative: str) -> None:
        try:
            resolve_under(root, relative).unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


def image_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as source:
        header = source.read(32)
        if header.startswith(b"\x89PNG\r\n\x1a\n") and header[12:16] == b"IHDR":
            width, height = struct.unpack(">II", header[16:24])
            return _valid_dimensions(width, height)
        if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            return _webp_dimensions(header + source.read(32))
        if header.startswith(b"\xff\xd8"):
            source.seek(2)
            return _jpeg_dimensions(source)
    raise ValueError("unsupported image format")


def _valid_dimensions(width: int, height: int) -> tuple[int, int]:
    if not (0 < width <= 100_000 and 0 < height <= 100_000):
        raise ValueError("invalid image dimensions")
    return width, height


def _jpeg_dimensions(source: Any) -> tuple[int, int]:
    sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while True:
        byte = source.read(1)
        if not byte:
            break
        if byte != b"\xff":
            continue
        while byte == b"\xff":
            byte = source.read(1)
        marker = byte[0]
        if marker in {0xD8, 0xD9}:
            continue
        raw_length = source.read(2)
        if len(raw_length) != 2:
            break
        length = struct.unpack(">H", raw_length)[0]
        if length < 2:
            break
        if marker in sof_markers:
            data = source.read(5)
            if len(data) != 5:
                break
            height, width = struct.unpack(">HH", data[1:5])
            return _valid_dimensions(width, height)
        source.seek(length - 2, os.SEEK_CUR)
    raise ValueError("invalid JPEG")


def _webp_dimensions(data: bytes) -> tuple[int, int]:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return _valid_dimensions(width, height)
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return _valid_dimensions((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        width, height = struct.unpack("<HH", data[26:30])
        return _valid_dimensions(width & 0x3FFF, height & 0x3FFF)
    raise ValueError("invalid WebP")
