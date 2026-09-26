from __future__ import annotations

import unittest
import uuid
import sqlite3
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))
ROOT = Path(__file__).resolve().parents[1]

from storage import Repository, resolve_under, safe_name


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


if __name__ == "__main__":
    unittest.main()
