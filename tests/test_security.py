from __future__ import annotations

import unittest
import uuid
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))
ROOT = Path(__file__).resolve().parents[1]

from security import AuthStore, PBKDF2_ITERATIONS, host_allowed, password_hash, peer_allowed, session_cookie
from storage import Repository


class SecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = ROOT / "tests" / "runtime" / uuid.uuid4().hex
        self.repo = Repository(self.test_dir)
        self.now = [1_000_000.0]
        self.auth = AuthStore(self.repo.database, clock=lambda: self.now[0])

    def test_admin_password_uses_salted_pbkdf2_sha256(self) -> None:
        self.auth.create_admin("admin", "correct horse battery staple")
        with self.repo.connect() as conn:
            row = conn.execute("SELECT * FROM admin").fetchone()
        self.assertEqual(row["iterations"], PBKDF2_ITERATIONS)
        self.assertEqual(len(row["salt"]), 32)
        self.assertEqual(
            bytes(row["password_hash"]),
            password_hash("correct horse battery staple", bytes(row["salt"]), row["iterations"]),
        )
        self.assertEqual(self.auth.authenticate("127.0.0.1", "admin", "correct horse battery staple"), "admin")
        self.assertIsNone(self.auth.authenticate("127.0.0.2", "admin", "wrong password"))

    def test_cookie_is_httponly_and_strict(self) -> None:
        value = session_cookie("token")
        self.assertIn("HttpOnly", value)
        self.assertIn("SameSite=Strict", value)
        self.assertIn("Path=/", value)

    def test_host_and_peer_checks_allow_loopback_only(self) -> None:
        self.assertTrue(host_allowed("127.0.0.1:8730"))
        self.assertTrue(host_allowed("localhost:8730"))
        self.assertFalse(host_allowed("example.com"))
        self.assertTrue(peer_allowed("::1"))
        self.assertFalse(peer_allowed("192.168.1.10"))

    def test_login_throttle_expires(self) -> None:
        with self.repo.connect() as conn:
            for _ in range(5):
                conn.execute(
                    "INSERT INTO login_attempts(address, attempted_at, success) VALUES (?, ?, 0)",
                    ("127.0.0.1", int(self.now[0])),
                )
        blocked, retry = self.auth.throttled("127.0.0.1")
        self.assertTrue(blocked)
        self.assertGreater(retry, 0)
        self.now[0] += 901
        self.assertEqual(self.auth.throttled("127.0.0.1"), (False, 0))


if __name__ == "__main__":
    unittest.main()
