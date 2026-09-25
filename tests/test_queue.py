from __future__ import annotations

import threading
import time
import unittest
import base64
import uuid
from pathlib import Path

import sys

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))
ROOT = Path(__file__).resolve().parents[1]

from inference import RenderQueue
from storage import Repository


class FakeAdapter:
    def __init__(self, gate: threading.Event | None = None) -> None:
        self.gate = gate
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.cancelled: set[str] = set()
        self.seen: list[dict] = []

    def run(self, job, on_line, on_progress) -> None:
        self.seen.append(job)
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            on_line("loading model", False)
            on_progress({"stage": "denoise", "step": 1, "total": 2})
            if self.gate:
                self.gate.wait(3)
            if job["id"] in self.cancelled:
                raise InterruptedError()
            Path(job["output"]).write_bytes(base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            ))
        finally:
            with self.lock:
                self.active -= 1

    def cancel(self, job_id: str) -> None:
        self.cancelled.add(job_id)
        if self.gate:
            self.gate.set()


class UnloadableAdapter(FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.unloads = 0

    def status(self) -> dict:
        return {
            "status": "ready" if not self.unloads else "unloaded",
            "backend": "sd-server", "pid": 1234 if not self.unloads else None,
            "idle_timeout": 300, "idle_remaining": None, "error": None,
        }

    def unload(self) -> None:
        self.unloads += 1


class QueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = ROOT / "tests" / "runtime" / uuid.uuid4().hex
        self.repo = Repository(self.test_dir)

    @staticmethod
    def request(prompt: str) -> dict:
        return {"session": "session-1", "prompt": prompt, "ratio": "1:1", "steps": 2, "seed": 1}

    def test_jobs_run_one_at_a_time_and_create_gallery_items(self) -> None:
        adapter = FakeAdapter()
        renders = RenderQueue(self.repo, adapter)
        try:
            renders.enqueue(self.request("first"))
            renders.enqueue(self.request("second"))
            self.assertTrue(self._wait(lambda: len(self.repo.takes("session-1")) == 2))
            self.assertEqual(adapter.max_active, 1)
        finally:
            renders.stop()

    def test_queued_job_can_be_cancelled(self) -> None:
        gate = threading.Event()
        adapter = FakeAdapter(gate)
        renders = RenderQueue(self.repo, adapter)
        try:
            first = renders.enqueue(self.request("first"))
            second = renders.enqueue(self.request("second"))
            self.assertTrue(self._wait(lambda: renders.running is not None))
            self.assertTrue(renders.cancel(second["id"]))
            gate.set()
            self.assertTrue(self._wait(lambda: not renders.queue_state()))
            self.assertEqual(len(self.repo.takes("session-1")), 1)
            self.assertEqual(first["status"], "queued")
        finally:
            renders.stop()

    def test_mask_is_snapshotted_and_associated_with_job(self) -> None:
        source_temp = self.test_dir / "source-upload.png"
        mask_temp = self.test_dir / "mask-upload.png"
        Image.new("RGB", (2, 2), (20, 30, 40)).save(source_temp)
        Image.new("L", (2, 2), 255).save(mask_temp)
        self.repo.add_upload("session-1", "source.png", source_temp)
        self.repo.add_mask("session-1", mask_temp, 12)
        adapter = FakeAdapter()
        renders = RenderQueue(self.repo, adapter)
        try:
            summary = renders.enqueue(self.request("masked edit"))
            self.assertTrue(summary["masked"])
            self.assertTrue(self._wait(lambda: len(self.repo.takes("session-1")) == 1))
            job = adapter.seen[0]
            self.assertNotEqual(Path(job["input"]), self.repo.input_path("session-1"))
            self.assertNotEqual(Path(job["mask"]), self.repo.mask_path("session-1"))
            self.assertTrue(Path(job["input"]).is_file())
            self.assertTrue(Path(job["mask"]).is_file())
            with self.repo.connect() as conn:
                row = conn.execute("SELECT mask_path FROM jobs WHERE id = ?", (summary["id"],)).fetchone()
            self.assertEqual(row["mask_path"], job["mask"])
        finally:
            renders.stop()

    def test_model_can_only_be_unloaded_while_queue_is_idle(self) -> None:
        adapter = UnloadableAdapter()
        renders = RenderQueue(self.repo, adapter, start_worker=False)
        try:
            self.assertTrue(renders.can_unload_model())
            self.assertTrue(renders.unload_model())
            self.assertEqual(adapter.unloads, 1)
            renders.running = {"id": "active"}
            self.assertFalse(renders.can_unload_model())
            self.assertFalse(renders.unload_model())
            self.assertEqual(adapter.unloads, 1)
        finally:
            renders.running = None
            renders.stop()

    @staticmethod
    def _wait(predicate, timeout: float = 5) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False


if __name__ == "__main__":
    unittest.main()
