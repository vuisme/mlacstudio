from __future__ import annotations

import unittest
import uuid
import sqlite3
from pathlib import Path
from unittest import mock

from PIL import Image

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))
ROOT = Path(__file__).resolve().parents[1]

from storage import MAX_REFERENCES, Repository, image_metadata, resolve_under, safe_name


class PathSafetyTests(unittest.TestCase):
    def test_resolve_under_rejects_traversal_and_absolute_paths(self) -> None:
        root = ROOT / "tests" / "runtime" / uuid.uuid4().hex
        root.mkdir(parents=True)
        self.assertEqual(resolve_under(root, "folder/image.png"), (root / "folder/image.png").resolve())
        with self.assertRaises(ValueError):
            resolve_under(root, "../secret.txt")
        with self.assertRaises(ValueError):
            resolve_under(root, str((root / "absolute.png").resolve()))

    def test_safe_name_drops_directories(self) -> None:
        self.assertEqual(safe_name("../../bad name.png"), "bad-name.png")

    def test_image_metadata_requires_nontrivial_alpha(self) -> None:
        root = ROOT / "tests" / "runtime" / uuid.uuid4().hex
        root.mkdir(parents=True)
        opaque = root / "opaque.png"
        transparent = root / "transparent.png"
        Image.new("RGBA", (2, 2), (255, 255, 255, 252)).save(opaque)
        Image.new("RGBA", (2, 2), (255, 255, 255, 255)).save(transparent)
        with Image.open(transparent) as image:
            alpha = image.getchannel("A")
            alpha.putpixel((0, 0), 0)
            image.putalpha(alpha)
            image.save(transparent)
        self.assertEqual(image_metadata(opaque), (2, 2, False))
        self.assertEqual(image_metadata(transparent), (2, 2, True))

    def test_existing_jobs_table_gets_mask_path_migration(self) -> None:
        root = ROOT / "tests" / "runtime" / uuid.uuid4().hex
        root.mkdir(parents=True)
        database = root / "studio.db"
        with sqlite3.connect(database) as conn:
            conn.execute(
                "CREATE TABLE jobs (id TEXT PRIMARY KEY, session_name TEXT NOT NULL, status TEXT NOT NULL, "
                "settings TEXT NOT NULL, input_path TEXT, output_path TEXT NOT NULL, progress TEXT NOT NULL, "
                "result TEXT, error TEXT, log TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, "
                "started_at TEXT, finished_at TEXT)"
            )
        repository = Repository(root)
        with repository.connect() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        self.assertIn("mask_path", columns)

    def test_legacy_single_input_is_migrated_as_base_without_changing_file(self) -> None:
        root = ROOT / "tests" / "runtime" / uuid.uuid4().hex
        uploads = root / "uploads"
        uploads.mkdir(parents=True)
        image_path = uploads / "legacy.png"
        Image.new("RGBA", (3, 2), (10, 20, 30, 40)).save(image_path)
        database = root / "studio.db"
        with sqlite3.connect(database) as conn:
            conn.executescript(
                """
                CREATE TABLE sessions (name TEXT PRIMARY KEY, settings TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
                CREATE TABLE inputs (
                    id TEXT PRIMARY KEY, session_name TEXT NOT NULL UNIQUE REFERENCES sessions(name) ON DELETE CASCADE,
                    stored_name TEXT NOT NULL, original_name TEXT NOT NULL, width INTEGER NOT NULL,
                    height INTEGER NOT NULL, relative_path TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE masks (
                    id TEXT PRIMARY KEY, session_name TEXT NOT NULL UNIQUE REFERENCES sessions(name) ON DELETE CASCADE,
                    input_id TEXT NOT NULL REFERENCES inputs(id) ON DELETE CASCADE,
                    relative_path TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
                    feather INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
                );
                INSERT INTO sessions VALUES ('session-1', '{}', 'old');
                INSERT INTO inputs VALUES ('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'session-1', 'legacy.png', 'legacy.png', 3, 2, 'legacy.png', 'old');
                """
            )

        repository = Repository(root)

        references = repository.inputs("session-1")
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0]["role"], "base")
        self.assertEqual(references[0]["position"], 0)
        self.assertTrue(references[0]["has_alpha"])
        self.assertEqual(repository.input_path("session-1"), image_path.resolve())
        with repository.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_references_keep_order_roles_alpha_and_promote_new_base_on_remove(self) -> None:
        root = ROOT / "tests" / "runtime" / uuid.uuid4().hex
        repository = Repository(root)

        def upload(name: str, mode: str, color) -> dict:
            source = repository.work_dir / name
            Image.new(mode, (2, 2), color).save(source)
            return repository.add_upload("session-1", name, source)

        base = upload("base.png", "RGBA", (1, 2, 3, 40))
        style = upload("style.png", "RGB", (4, 5, 6))
        subject = upload("subject.png", "RGB", (7, 8, 9))
        repository.set_input_role("session-1", style["id"], "style")
        repository.set_input_role("session-1", subject["id"], "subject")
        with self.assertRaisesRegex(ValueError, "base reference must remain first"):
            repository.reorder_inputs("session-1", [subject["id"], base["id"], style["id"]])
        reordered = repository.reorder_inputs("session-1", [base["id"], subject["id"], style["id"]])
        self.assertEqual([item["id"] for item in reordered], [base["id"], subject["id"], style["id"]])
        self.assertTrue(next(item for item in reordered if item["id"] == base["id"])["has_alpha"])

        repository.delete_input("session-1", ident=base["id"])
        remaining = repository.inputs("session-1")
        self.assertEqual(remaining[0]["role"], "base")
        self.assertEqual([item["position"] for item in remaining], [0, 1])

    def test_reference_count_limit_is_enforced(self) -> None:
        root = ROOT / "tests" / "runtime" / uuid.uuid4().hex
        repository = Repository(root)
        for index in range(MAX_REFERENCES):
            source = repository.work_dir / f"ref-{index}.png"
            Image.new("RGB", (1, 1), (index, index, index)).save(source)
            repository.add_upload("session-1", source.name, source)
        extra = repository.work_dir / "extra.png"
        Image.new("RGB", (1, 1), 0).save(extra)
        with self.assertRaisesRegex(ValueError, "at most"):
            repository.add_upload("session-1", extra.name, extra)

    def test_session_cumulative_upload_limit_is_enforced(self) -> None:
        root = ROOT / "tests" / "runtime" / uuid.uuid4().hex
        repository = Repository(root)
        first = repository.work_dir / "first.png"
        second = repository.work_dir / "second.png"
        Image.new("RGB", (8, 8), (1, 2, 3)).save(first)
        Image.new("RGB", (8, 8), (4, 5, 6)).save(second)
        limit = first.stat().st_size
        with mock.patch("storage.MAX_SESSION_UPLOAD_BYTES", limit):
            repository.add_upload("session-1", first.name, first)
            with self.assertRaisesRegex(ValueError, "100 MB in total"):
                repository.add_upload("session-1", second.name, second)


if __name__ == "__main__":
    unittest.main()
