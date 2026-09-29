from __future__ import annotations

import base64
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import sys
import subprocess
from unittest import mock

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web"))

from inference import (
    capability_report,
    RenderQueue,
    SdServerSupervisor,
    StableDiffusionAdapter,
    StudioConfig,
    composite_masked_output,
    compose_preset_prompt,
    prepare_inference_mask,
    _native_process_options,
)
from storage import Repository


class FakeProcess:
    def __init__(self) -> None:
        self.stdout = iter(["sampling 1/2\n", "decoding image\n"])
        self.pid = 123

    def wait(self, timeout=None) -> int:
        return 0

    def poll(self):
        return None


class FakeServerProcess:
    next_pid = 7000

    def __init__(self) -> None:
        self.stdout = iter(())
        self.returncode = None
        self.pid = FakeServerProcess.next_pid
        FakeServerProcess.next_pid += 1
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None) -> int:
        return 0 if self.returncode is None else self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9


class NativeApi:
    PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="

    def __init__(self) -> None:
        self.calls = []
        self.next_job = 0
        self.capabilities = {"supported_modes": ["img_gen"]}

    def __call__(self, method, url, payload, timeout):
        self.calls.append((method, url, payload, timeout))
        if url.endswith("/sdcpp/v1/capabilities"):
            return self.capabilities
        if url.endswith("/sdcpp/v1/img_gen"):
            self.next_job += 1
            return {"id": f"native-{self.next_job}", "status": "queued"}
        if "/sdcpp/v1/jobs/" in url and not url.endswith("/cancel"):
            return {
                "id": url.rsplit("/", 1)[-1], "status": "completed",
                "result": {"images": [{"b64_json": self.PNG}], "output_format": "png"},
            }
        if url.endswith("/cancel"):
            return {"status": "cancelled"}
        raise AssertionError(url)


class FallbackMustNotRun:
    def run(self, job, on_line, on_progress):
        raise AssertionError("sd-cli fallback should not run")

    def cancel(self, job_id):
        pass


class RecordingFallback:
    def __init__(self) -> None:
        self.jobs = []

    def run(self, job, on_line, on_progress):
        self.jobs.append(job["id"])
        Path(job["output"]).write_bytes(base64.b64decode(NativeApi.PNG))

    def cancel(self, job_id):
        pass


class PresetPromptTests(unittest.TestCase):
    def test_transparent_wraps_raw_prompt_once(self) -> None:
        raw = "cute girl chibi sticker"
        effective = compose_preset_prompt(raw, "transparent")
        self.assertEqual(raw, "cute girl chibi sticker")
        self.assertTrue(effective.startswith("This is an RGBA image with transparency."))
        self.assertIn(raw, effective)
        self.assertTrue(effective.endswith("The image has alpha channel and the background is transparent."))
        self.assertEqual(compose_preset_prompt(effective, "transparent"), effective)

    def test_subject_extraction_treats_raw_prompt_as_target_and_preserves_details(self) -> None:
        effective = compose_preset_prompt("the woman in the red dress", "subject-extraction")
        self.assertIn("Extract only the woman in the red dress from the base image.", effective)
        self.assertIn("exact identity", effective)
        self.assertIn("logos, text", effective)
        self.assertIn("Do not redraw, restyle", effective)
        self.assertEqual(effective.count("This is an RGBA image with transparency."), 1)

    def test_none_keeps_raw_prompt(self) -> None:
        self.assertEqual(compose_preset_prompt("  plain prompt  ", "none"), "plain prompt")


class AdapterTests(unittest.TestCase):
    def test_windows_native_processes_are_hidden_without_a_console(self) -> None:
        fake_startup = mock.Mock()
        fake_startup.dwFlags = 0
        with (
            mock.patch("inference.os.name", "nt"),
            mock.patch.object(subprocess, "STARTUPINFO", return_value=fake_startup, create=True),
            mock.patch.object(subprocess, "STARTF_USESHOWWINDOW", 1, create=True),
            mock.patch.object(subprocess, "SW_HIDE", 0, create=True),
            mock.patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True),
            mock.patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True),
        ):
            options = _native_process_options()
        self.assertEqual(options["creationflags"], 0x08000000 | 512)
        self.assertEqual(options["startupinfo"].wShowWindow, 0)
        self.assertFalse(options["start_new_session"])

    def server_fixture(self, *, idle_timeout=300):
        root = ROOT / "tests" / "runtime" / uuid.uuid4().hex
        repo = Repository(root)
        paths = {}
        for key in ("sd_cli", "sd_server", "transformer", "text_encoder", "mmproj", "vae"):
            path = root / f"{key}.bin"
            path.write_bytes(b"placeholder")
            paths[key] = str(path)
        config = StudioConfig({**paths, "sd_server_idle_timeout": idle_timeout}, repo)
        api = NativeApi()
        processes = []
        popen_calls = []

        def process_factory(argv, **kwargs):
            process = FakeServerProcess()
            processes.append(process)
            popen_calls.append((argv, kwargs))
            return process

        supervisor = SdServerSupervisor(
            config, FallbackMustNotRun(), request_json=api, process_factory=process_factory,
            port_factory=lambda: 19234, sleep=lambda delay: None, start_monitor=False,
        )
        return root, supervisor, api, processes, popen_calls

    @staticmethod
    def native_job(root: Path, ident: str = "job") -> dict:
        return {
            "id": ident,
            "settings": {"prompt": "test", "ratio": "1:1", "width": 512, "height": 512, "steps": 2, "seed": 7},
            "output": str(root / "work" / f"{ident}.png"), "input": None, "mask": None,
            "cancel_requested": False,
        }

    def test_sd_server_starts_lazily_on_loopback_and_uses_native_api(self) -> None:
        root, supervisor, api, processes, popen_calls = self.server_fixture()
        job = self.native_job(root)
        self.assertEqual(processes, [])

        supervisor.run(job, lambda line, rewrites: None, lambda progress: None)

        self.assertEqual(len(processes), 1)
        argv, kwargs = popen_calls[0]
        self.assertEqual(argv[argv.index("--listen-ip") + 1], "127.0.0.1")
        self.assertEqual(argv[argv.index("--listen-port") + 1], "19234")
        self.assertFalse(kwargs["shell"])
        submit = next(call for call in api.calls if call[1].endswith("/sdcpp/v1/img_gen"))
        self.assertEqual(submit[2]["sample_params"]["sample_steps"], 2)
        self.assertTrue(Path(job["output"]).is_file())
        self.assertEqual(supervisor.status()["status"], "idle")
        supervisor.close()

    def test_sd_server_is_reused_across_jobs(self) -> None:
        root, supervisor, _, processes, _ = self.server_fixture()
        supervisor.run(self.native_job(root, "one"), lambda line, rewrites: None, lambda progress: None)
        first_pid = supervisor.status()["pid"]
        supervisor.run(self.native_job(root, "two"), lambda line, rewrites: None, lambda progress: None)
        self.assertEqual(len(processes), 1)
        self.assertEqual(supervisor.status()["pid"], first_pid)
        supervisor.close()

    def test_sd_server_unloads_only_after_idle_timeout_and_empty_queue(self) -> None:
        now = [0.0]
        root, supervisor, _, processes, _ = self.server_fixture(idle_timeout=5)
        supervisor.clock = lambda: now[0]
        supervisor.run(self.native_job(root), lambda line, rewrites: None, lambda progress: None)
        queue_empty = [False]
        supervisor.set_idle_guard(lambda: queue_empty[0])
        now[0] = 6.0
        supervisor._check_idle()
        self.assertFalse(processes[0].terminated)
        queue_empty[0] = True
        supervisor._check_idle()
        self.assertTrue(processes[0].terminated)
        self.assertEqual(supervisor.status()["status"], "unloaded")
        supervisor.close()

    def test_sd_server_recovers_by_starting_a_new_process_after_exit(self) -> None:
        root, supervisor, _, processes, _ = self.server_fixture()
        supervisor.run(self.native_job(root, "one"), lambda line, rewrites: None, lambda progress: None)
        processes[0].returncode = 1
        supervisor.run(self.native_job(root, "two"), lambda line, rewrites: None, lambda progress: None)
        self.assertEqual(len(processes), 2)
        self.assertEqual(supervisor.status()["pid"], processes[1].pid)
        supervisor.close()

    def test_native_edit_sends_reference_and_prepared_mask_data_urls(self) -> None:
        root, supervisor, api, _, _ = self.server_fixture()
        source = root / "source.png"
        mask = root / "mask.png"
        Image.new("RGB", (32, 32), (10, 20, 30)).save(source)
        Image.new("L", (32, 32), 255).save(mask)
        job = self.native_job(root, "edit")
        job.update({"input": str(source), "mask": str(mask), "mask_feather": 4})
        supervisor.run(job, lambda line, rewrites: None, lambda progress: None)
        submit = next(call for call in api.calls if call[1].endswith("/sdcpp/v1/img_gen"))
        self.assertTrue(submit[2]["ref_images"][0].startswith("data:image/png;base64,"))
        self.assertTrue(submit[2]["mask_image"].startswith("data:image/png;base64,"))
        self.assertTrue((Path(job["output"]).parent / "inference-mask.png").is_file())
        supervisor.close()

    def test_native_request_preserves_reference_order_and_increases_indices(self) -> None:
        root, supervisor, api, _, _ = self.server_fixture()
        first = root / "first.png"
        second = root / "second.png"
        Image.new("RGB", (2, 2), (1, 2, 3)).save(first)
        Image.new("RGBA", (2, 2), (4, 5, 6, 7)).save(second)
        job = self.native_job(root, "multi")
        job.update({
            "input": str(second),
            "references": [{"path": str(first)}, {"path": str(second)}],
        })
        supervisor.run(job, lambda line, rewrites: None, lambda progress: None)
        submit = next(call for call in api.calls if call[1].endswith("/sdcpp/v1/img_gen"))
        payload = submit[2]
        self.assertTrue(payload["increase_ref_index"])
        decoded = [base64.b64decode(value.split(",", 1)[1]) for value in payload["ref_images"]]
        self.assertEqual(decoded, [first.read_bytes(), second.read_bytes()])
        supervisor.close()

    def test_native_server_rechecks_reported_reference_limit_before_submit(self) -> None:
        root, supervisor, api, _, _ = self.server_fixture()
        api.capabilities = {"supports_multi_reference": False, "max_reference_images": 1}
        first = root / "first.png"
        second = root / "second.png"
        Image.new("RGB", (2, 2), (1, 2, 3)).save(first)
        Image.new("RGB", (2, 2), (4, 5, 6)).save(second)
        job = self.native_job(root, "limited")
        job.update({
            "input": str(first),
            "references": [{"path": str(first)}, {"path": str(second)}],
        })
        with self.assertRaisesRegex(ValueError, "at most 1"):
            supervisor.run(job, lambda line, rewrites: None, lambda progress: None)
        self.assertFalse(any(call[1].endswith("/sdcpp/v1/img_gen") for call in api.calls))
        supervisor.close()

    def test_missing_sd_server_uses_sd_cli_fallback(self) -> None:
        root, supervisor, _, processes, _ = self.server_fixture()
        fallback = RecordingFallback()
        supervisor.fallback = fallback
        supervisor.config.repository.set_config("path.sd_server", "")
        job = self.native_job(root)
        lines = []
        supervisor.run(job, lambda line, rewrites: lines.append(line), lambda progress: None)
        self.assertEqual(processes, [])
        self.assertEqual(fallback.jobs, ["job"])
        self.assertTrue(any("sd-cli fallback" in line for line in lines))
        self.assertTrue(Path(job["output"]).is_file())
        supervisor.close()

    def test_adapter_uses_argv_and_never_a_shell(self) -> None:
        root = ROOT / "tests" / "runtime" / uuid.uuid4().hex
        repo = Repository(root)
        paths = {}
        for key in ("sd_cli", "transformer", "text_encoder", "mmproj", "vae"):
            path = root / f"{key}.bin"
            path.write_bytes(b"placeholder")
            paths[key] = str(path)
        adapter = StableDiffusionAdapter(StudioConfig({**paths, "extra_args": ["--cfg-scale", "1.0"]}, repo))
        output = root / "work" / "output.png"
        output.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        ))
        job = {
            "id": "job", "settings": {"prompt": "test", "ratio": "1:1", "width": None, "height": None, "steps": 2, "seed": 7},
            "output": str(output), "input": None, "cancel_requested": False,
        }
        with patch("inference.subprocess.Popen", return_value=FakeProcess()) as popen:
            adapter.run(job, lambda line, rewrites: None, lambda progress: None)
        argv = popen.call_args.args[0]
        self.assertIsInstance(argv, list)
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertEqual(argv[1:9:2], ["--diffusion-model", "--llm", "--llm_vision", "--vae"])
        self.assertIn("--output", argv)

    def test_qwen_edit_uses_reference_image_not_init_img(self) -> None:
        root = ROOT / "tests" / "runtime" / uuid.uuid4().hex
        repo = Repository(root)
        paths = {}
        for key in ("sd_cli", "transformer", "text_encoder", "mmproj", "vae"):
            path = root / f"{key}.bin"
            path.write_bytes(b"placeholder")
            paths[key] = str(path)
        adapter = StableDiffusionAdapter(StudioConfig(paths, repo))
        source = root / "source.png"
        source.write_bytes(b"image")
        mask = root / "mask.png"
        Image.new("L", (512, 512), 0).save(mask)
        job = {
            "id": "edit", "settings": {"prompt": "pink hair", "ratio": None, "width": 512, "height": 512, "steps": 2, "seed": 7},
            "output": str(root / "out.png"), "input": str(source), "input_width": 512, "input_height": 512,
            "resolved_prompt": "pink hair", "mask": str(mask), "cancel_requested": False,
        }
        argv = adapter.build_argv(job)
        self.assertIn("--ref-image", argv)
        inference_mask = Path(argv[argv.index("--mask") + 1])
        self.assertEqual(inference_mask.name, "inference-mask.png")
        self.assertTrue(inference_mask.is_file())
        self.assertNotIn("--init-img", argv)

    def test_cli_repeats_reference_argument_in_order(self) -> None:
        root = ROOT / "tests" / "runtime" / uuid.uuid4().hex
        repo = Repository(root)
        paths = {}
        for key in ("sd_cli", "transformer", "text_encoder", "mmproj", "vae"):
            path = root / f"{key}.bin"
            path.write_bytes(b"placeholder")
            paths[key] = str(path)
        adapter = StableDiffusionAdapter(StudioConfig(paths, repo))
        first = root / "first.png"
        second = root / "second.png"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        job = {
            "id": "multi", "settings": {"prompt": "test", "ratio": "1:1", "width": 512, "height": 512, "steps": 2, "seed": 7},
            "output": str(root / "out.png"), "input": str(first), "mask": None,
            "references": [{"path": str(second)}, {"path": str(first)}], "cancel_requested": False,
        }
        argv = adapter.build_argv(job)
        indices = [index for index, value in enumerate(argv) if value == "--ref-image"]
        self.assertEqual([argv[index + 1] for index in indices], [str(second), str(first)])

    def test_capability_report_gates_explicit_backend_limits_and_rgba(self) -> None:
        report = capability_report({
            "supports_multi_reference": False,
            "max_reference_images": 4,
            "supports_rgba": False,
        })
        self.assertFalse(report["multi_reference"])
        self.assertEqual(report["max_references"], 1)
        self.assertFalse(report["rgba"])
        self.assertTrue(report["rgba_reported"])

    def test_render_queue_rejects_explicitly_unsupported_multi_reference_and_rgba(self) -> None:
        class UnsupportedAdapter:
            def status(self):
                return {
                    "status": "unloaded",
                    "capabilities": capability_report({
                        "supports_multi_reference": False,
                        "supports_rgba": False,
                    }),
                }

            def cancel(self, job_id):
                pass

        root = ROOT / "tests" / "runtime" / uuid.uuid4().hex
        repository = Repository(root)
        for index in range(2):
            source = repository.work_dir / f"ref-{index}.png"
            Image.new("RGB", (2, 2), (index, index, index)).save(source)
            repository.add_upload("session-1", source.name, source)
        queue = RenderQueue(repository, UnsupportedAdapter(), start_worker=False)
        with self.assertRaisesRegex(ValueError, "at most 1"):
            queue.enqueue({"session": "session-1", "prompt": "test"})

        repository.delete_input("session-1", ident=repository.inputs("session-1")[1]["id"])
        with self.assertRaisesRegex(ValueError, "RGBA output is unsupported"):
            queue.enqueue({"session": "session-1", "prompt": "transparent", "preset": "transparent"})

    def test_mask_composite_resizes_and_preserves_hard_zero_pixels(self) -> None:
        root = ROOT / "tests" / "runtime" / uuid.uuid4().hex
        root.mkdir(parents=True, exist_ok=True)
        source = root / "source.png"
        output = root / "output.png"
        mask = root / "mask.png"
        source_pixels = [
            (1, 2, 3, 0), (10, 20, 30, 255), (40, 50, 60, 255),
            (70, 80, 90, 255), (100, 110, 120, 255), (130, 140, 150, 255),
        ]
        source_image = Image.new("RGBA", (3, 2))
        source_image.putdata(source_pixels)
        source_image.save(source)
        Image.new("RGB", (1, 1), (240, 10, 20)).save(output)
        mask_image = Image.new("L", (3, 2))
        mask_image.putdata([0, 255, 128, 0, 255, 64])
        mask_image.save(mask)

        composite_masked_output(output, source, mask)

        with Image.open(output) as result:
            rgba = result.convert("RGBA")
            pixels = [rgba.getpixel((x, y)) for y in range(rgba.height) for x in range(rgba.width)]
        self.assertEqual(pixels[0], source_pixels[0])
        self.assertEqual(pixels[3], source_pixels[3])
        self.assertEqual(pixels[1], (240, 10, 20, 255))
        self.assertEqual(pixels[4], (240, 10, 20, 255))
        self.assertNotEqual(pixels[2], source_pixels[2])
        self.assertNotEqual(pixels[2], (240, 10, 20, 255))


if __name__ == "__main__":
    unittest.main()
