"""stable-diffusion.cpp adapter and single-worker render queue."""

from __future__ import annotations

import contextlib
import base64
import json
import math
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PIL import Image, ImageFilter

from storage import Repository, image_dimensions, now_iso

RATIOS = {
    "1:1": (2048, 2048), "4:3": (2400, 1792), "3:4": (1792, 2400),
    "3:2": (2528, 1696), "2:3": (1696, 2528), "16:9": (2752, 1536), "9:16": (1536, 2752),
}
PATH_KEYS = ("sd_cli", "sd_server", "transformer", "text_encoder", "mmproj", "vae")
MODEL_STATES = {"unloaded", "loading", "ready", "rendering", "idle", "error"}
STEP_RE = re.compile(r"(?:step\s*)?(\d+)\s*/\s*(\d+)", re.IGNORECASE)


def prepare_inference_mask(mask_path: Path, target: Path, feather: int = 0) -> Path:
    """Give the model context around the painted area without changing the saved hard mask."""
    with Image.open(mask_path) as image:
        mask = image.convert("L")
        # Latent edit models need neighboring context to synthesize coherent edges.
        # Scale the transition with the image while keeping the expansion bounded.
        expansion = max(9, min(65, (min(mask.size) // 16) | 1))
        blur = max(2, min(32, feather or expansion // 4))
        mask = mask.filter(ImageFilter.MaxFilter(expansion)).filter(ImageFilter.GaussianBlur(blur))
        mask.save(target, format="PNG")
    return target


def composite_masked_output(output: Path, source: Path, mask_path: Path, feather: int = 0) -> None:
    """Blend through a narrow transition while preserving hard-zero source pixels exactly."""
    with Image.open(source) as source_image, Image.open(output) as generated_image, Image.open(mask_path) as mask_image:
        source_rgba = source_image.convert("RGBA")
        generated_rgba = generated_image.convert("RGBA")
        hard_mask = mask_image.convert("L")
        if hard_mask.size != source_rgba.size:
            raise ValueError("stored mask dimensions do not match the reference image")
        if generated_rgba.size != source_rgba.size:
            generated_rgba = generated_rgba.resize(source_rgba.size, Image.Resampling.LANCZOS)
        blend_mask = hard_mask.filter(ImageFilter.GaussianBlur(max(0, feather))) if feather else hard_mask
        composited = Image.composite(generated_rgba, source_rgba, blend_mask)
        # Feather may only affect the selected/transition pixels; exact zeros always come from source.
        zero_mask = hard_mask.point(lambda value: 255 if value == 0 else 0)
        composited.paste(source_rgba, mask=zero_mask)
        composited.save(output, format="PNG")


def as_int(value: Any, name: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not low <= number <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return number


def clean_settings(request: dict[str, Any], *, require_prompt: bool = True) -> dict[str, Any]:
    prompt = str(request.get("prompt") or "").strip()
    if require_prompt and not prompt:
        raise ValueError("enter a prompt")
    if len(prompt) > 10_000:
        raise ValueError("prompt is too long")
    ratio = request.get("ratio") or None
    if ratio not in {*RATIOS, None}:
        raise ValueError("invalid aspect ratio")
    settings: dict[str, Any] = {
        "prompt": prompt,
        "ratio": ratio,
        "steps": as_int(request.get("steps", 20), "steps", 1, 200),
        "seed": as_int(request.get("seed", 42), "seed", 0, 2**63 - 1),
    }
    for key in ("width", "height"):
        raw = request.get(key)
        if raw in (None, ""):
            settings[key] = None
        else:
            value = as_int(raw, key, 256, 4096)
            if value % 32:
                raise ValueError(f"{key} must be a multiple of 32")
            settings[key] = value
    return settings


def render_dimensions(job: dict[str, Any]) -> tuple[int, int]:
    settings = job["settings"]
    if settings["ratio"]:
        ratio_width, ratio_height = RATIOS[settings["ratio"]]
    elif job.get("input_width") and job.get("input_height"):
        aspect = job["input_width"] / job["input_height"]
        raw_width = math.sqrt(1024 * 1024 * aspect)
        ratio_width = max(256, round(raw_width / 32) * 32)
        ratio_height = max(256, round((raw_width / aspect) / 32) * 32)
    else:
        ratio_width, ratio_height = RATIOS["1:1"]
    return settings.get("width") or ratio_width, settings.get("height") or ratio_height


class StudioConfig:
    def __init__(self, defaults: dict[str, Any], repository: Repository) -> None:
        self.defaults = defaults
        self.repository = repository

    def reload(self, defaults: dict[str, Any]) -> None:
        """Replace manifest-generated defaults after an atomic model switch."""
        if not isinstance(defaults, dict):
            raise ValueError("config must be an object")
        self.defaults = defaults
        for key in PATH_KEYS:
            self.repository.set_config(f"path.{key}", str(defaults.get(key, "")))

    def get(self, key: str) -> str:
        if key not in PATH_KEYS:
            raise ValueError(f"unknown path: {key}")
        default = str(self.defaults.get(key, ""))
        if key == "sd_server" and not default:
            sd_cli = self.repository.get_config("path.sd_cli", str(self.defaults.get("sd_cli", "")))
            default = str(Path(sd_cli).with_name("sd-server.exe")) if sd_cli else ""
        return self.repository.get_config(f"path.{key}", default)

    def all(self) -> dict[str, str]:
        return {key: self.get(key) for key in PATH_KEYS}

    def info(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for key, value in self.all().items():
            path = Path(value)
            result[key] = {"path": value, "ok": bool(value) and path.is_file()}
        return result

    def update(self, changes: dict[str, Any]) -> dict[str, dict[str, Any]]:
        validated: dict[str, str] = {}
        for key, raw in changes.items():
            if key not in PATH_KEYS:
                raise ValueError(f"unknown path: {key}")
            value = str(raw or "").strip()
            if key == "sd_server" and not value:
                validated[key] = ""
                continue
            if not value or not Path(value).is_file():
                raise ValueError(f"{key}: file not found: {value or '(empty)'}")
            validated[key] = value
        for key, value in validated.items():
            self.repository.set_config(f"path.{key}", value)
        return self.info()

    @property
    def extra_args(self) -> list[str]:
        value = self.defaults.get("extra_args", [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError("config.json extra_args must be an array of strings")
        return list(value)

    @property
    def idle_timeout(self) -> int:
        raw = self.repository.get_config(
            "sd_server.idle_timeout", str(self.defaults.get("sd_server_idle_timeout", 300))
        )
        return as_int(raw, "idle timeout", 1, 86400)

    def update_idle_timeout(self, value: Any) -> int:
        timeout = as_int(value, "idle timeout", 1, 86400)
        self.repository.set_config("sd_server.idle_timeout", str(timeout))
        return timeout


def _native_process_options() -> dict[str, Any]:
    """Return platform-safe subprocess options without flashing a Windows console."""
    if os.name != "nt":
        return {"creationflags": 0, "start_new_session": True}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
        "start_new_session": False,
        "startupinfo": startupinfo,
    }


class StableDiffusionAdapter:
    def __init__(self, config: StudioConfig) -> None:
        self.config = config
        self.lock = threading.Lock()
        self.processes: dict[str, subprocess.Popen[str]] = {}

    def build_argv(self, job: dict[str, Any]) -> list[str]:
        paths = self.config.all()
        missing = [key for key in PATH_KEYS if key != "sd_server" if not paths[key] or not Path(paths[key]).is_file()]
        if missing:
            raise ValueError(f"configure existing files for: {', '.join(missing)}")
        settings = job["settings"]
        width, height = render_dimensions(job)
        argv = [
            paths["sd_cli"],
            "--diffusion-model", paths["transformer"],
            "--llm", paths["text_encoder"],
            "--llm_vision", paths["mmproj"],
            "--vae", paths["vae"],
            "--prompt", job.get("resolved_prompt") or settings["prompt"],
            "--width", str(width), "--height", str(height),
            "--steps", str(settings["steps"]), "--seed", str(settings["seed"]),
            "--output", job["output"],
            *self.config.extra_args,
        ]
        if job.get("input"):
            # Qwen-Image-2.1 editing in stable-diffusion.cpp uses a vision
            # reference image, not the Stable Diffusion img2img --init-img path.
            argv.extend(["--ref-image", job["input"]])
            if job.get("mask"):
                inference_mask = str(Path(job["output"]).with_name("inference-mask.png"))
                prepare_inference_mask(Path(job["mask"]), Path(inference_mask), int(job.get("mask_feather") or 0))
                argv.extend(["--mask", inference_mask])
        return argv

    def run(
        self,
        job: dict[str, Any],
        on_line: Callable[[str, bool], None],
        on_progress: Callable[[dict[str, Any]], None],
    ) -> None:
        argv = self.build_argv(job)
        job["argv"] = argv
        process = subprocess.Popen(
            argv,
            cwd=str(Path(argv[0]).resolve().parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **_native_process_options(),
            shell=False,
        )
        with self.lock:
            self.processes[job["id"]] = process
        try:
            on_progress({"stage": "load", "step": 0, "total": job["settings"]["steps"]})
            assert process.stdout is not None
            for raw in process.stdout:
                line = raw.rstrip("\r\n")
                if line:
                    on_line(line, raw.endswith("\r"))
                    self._parse_progress(line, job, on_progress)
            return_code = process.wait()
            if job.get("cancel_requested"):
                raise InterruptedError("render cancelled")
            if return_code != 0:
                raise RuntimeError(f"sd-cli exited with code {return_code}")
            output = Path(job["output"])
            if not output.is_file():
                raise RuntimeError("sd-cli completed without creating the output image")
            on_progress({"stage": "save", "step": job["settings"]["steps"], "total": job["settings"]["steps"]})
        finally:
            with self.lock:
                self.processes.pop(job["id"], None)

    @staticmethod
    def _parse_progress(line: str, job: dict[str, Any], callback: Callable[[dict[str, Any]], None]) -> None:
        lower = line.lower()
        match = STEP_RE.search(line)
        if match:
            step, total = int(match.group(1)), int(match.group(2))
            if 0 <= step <= total and total <= 1000:
                callback({"stage": "denoise", "step": step, "total": total})
        elif "decode" in lower or "vae" in lower and "load" not in lower:
            callback({"stage": "decode", "step": job["settings"]["steps"], "total": job["settings"]["steps"]})

    def cancel(self, job_id: str) -> None:
        with self.lock:
            process = self.processes.get(job_id)
        if not process or process.poll() is not None:
            return
        if os.name == "nt":
            with contextlib.suppress(OSError):
                process.send_signal(signal.CTRL_BREAK_EVENT)
            try:
                process.wait(timeout=2)
                return
            except subprocess.TimeoutExpired:
                pass
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                shell=False,
            )
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)


def _http_json(method: str, url: str, payload: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=True).encode("utf-8")
    request = Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read())
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            message = json.loads(detail).get("error") or detail
        except json.JSONDecodeError:
            message = detail
        raise RuntimeError(f"sd-server HTTP {exc.code}: {message}") from exc
    except (OSError, URLError) as exc:
        raise ConnectionError(f"sd-server request failed: {exc}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("sd-server returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError("sd-server returned invalid JSON")
    return result


def _data_url(path: Path) -> str:
    suffix = path.suffix.lower()
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(suffix, "image/png")
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


class SdServerSupervisor:
    """Own a loopback-only sd-server and reuse it until the render queue stays idle."""

    def __init__(
        self,
        config: StudioConfig,
        fallback: StableDiffusionAdapter | None = None,
        *,
        request_json: Callable[[str, str, dict[str, Any] | None, float], dict[str, Any]] = _http_json,
        process_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        port_factory: Callable[[], int] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        start_monitor: bool = True,
    ) -> None:
        self.config = config
        self.fallback = fallback or StableDiffusionAdapter(config)
        self.request_json = request_json
        self.process_factory = process_factory
        self.port_factory = port_factory or self._free_loopback_port
        self.clock = clock
        self.sleep = sleep
        self.lock = threading.RLock()
        self.process: subprocess.Popen[str] | None = None
        self.port: int | None = None
        self.native_jobs: dict[str, str] = {}
        self.active_job_id: str | None = None
        self.active_job: dict[str, Any] | None = None
        self.active_progress: Callable[[dict[str, Any]], None] | None = None
        self.state = "unloaded"
        self.backend = "sd-server"
        self.error: str | None = None
        self.last_activity = self.clock()
        self.idle_guard: Callable[[], bool] = lambda: True
        self.status_callback: Callable[[dict[str, Any]], None] | None = None
        self.active_log: Callable[[str, bool], None] | None = None
        self.recent_logs: deque[str] = deque(maxlen=30)
        self.stopping = threading.Event()
        self.monitor: threading.Thread | None = None
        if start_monitor:
            self.monitor = threading.Thread(target=self._monitor_idle, name="sd-server-idle", daemon=True)
            self.monitor.start()

    @staticmethod
    def _free_loopback_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    def set_idle_guard(self, callback: Callable[[], bool]) -> None:
        self.idle_guard = callback

    def set_status_callback(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self.status_callback = callback

    def status(self) -> dict[str, Any]:
        with self.lock:
            process = self.process
            if process is not None and process.poll() is not None and self.state not in {"unloaded", "error"}:
                self.state = "error"
                self.error = f"sd-server exited with code {process.poll()}"
            alive = process is not None and process.poll() is None
            idle_for = max(0.0, self.clock() - self.last_activity) if alive and self.state == "idle" else 0.0
            return {
                "status": self.state,
                "backend": self.backend,
                "pid": process.pid if alive else None,
                "idle_timeout": self.config.idle_timeout,
                "idle_remaining": max(0, round(self.config.idle_timeout - idle_for)) if idle_for else None,
                "error": self.error,
            }

    def _set_state(self, state: str, *, error: str | None = None, backend: str = "sd-server") -> None:
        if state not in MODEL_STATES:
            raise ValueError(f"invalid model state: {state}")
        with self.lock:
            self.state = state
            self.backend = backend
            self.error = error
            value = self.status()
        if self.status_callback:
            self.status_callback(value)

    def build_server_argv(self, port: int) -> list[str]:
        paths = self.config.all()
        required = ("sd_server", "transformer", "text_encoder", "mmproj", "vae")
        missing = [key for key in required if not paths[key] or not Path(paths[key]).is_file()]
        if missing:
            raise FileNotFoundError(f"configure existing files for: {', '.join(missing)}")
        return [
            paths["sd_server"], "--listen-ip", "127.0.0.1", "--listen-port", str(port),
            "--diffusion-model", paths["transformer"], "--llm", paths["text_encoder"],
            "--llm_vision", paths["mmproj"], "--vae", paths["vae"], *self.config.extra_args,
        ]

    def _ensure_server(self) -> None:
        with self.lock:
            if self.process is not None and self.process.poll() is None and self.port is not None:
                return
            self.process = None
            self.port = self.port_factory()
            self.recent_logs.clear()
            argv = self.build_server_argv(self.port)
            self._set_state("loading")
            self.process = self.process_factory(
                argv,
                cwd=str(Path(argv[0]).resolve().parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **_native_process_options(),
                shell=False,
            )
            process = self.process
            threading.Thread(target=self._read_output, args=(process,), name="sd-server-output", daemon=True).start()
        deadline = self.clock() + 300
        last_error: Exception | None = None
        while self.clock() < deadline:
            if process.poll() is not None:
                tail = "; ".join(self.recent_logs)
                raise RuntimeError(f"sd-server exited during startup{': ' + tail if tail else ''}")
            try:
                self.request_json("GET", self._url("/sdcpp/v1/capabilities"), None, 2)
                self.last_activity = self.clock()
                self._set_state("ready")
                return
            except (ConnectionError, RuntimeError) as exc:
                last_error = exc
                self.sleep(0.25)
        raise TimeoutError(f"sd-server did not become ready: {last_error}")

    def _read_output(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is None:
            return
        for raw in process.stdout:
            line = raw.rstrip("\r\n")
            if not line:
                continue
            with self.lock:
                self.recent_logs.append(line)
                callback = self.active_log
                job = self.active_job
                progress = self.active_progress
            if callback:
                callback(line, raw.endswith("\r"))
            if job and progress:
                StableDiffusionAdapter._parse_progress(line, job, progress)

    def _url(self, path: str) -> str:
        if self.port is None:
            raise RuntimeError("sd-server is not running")
        return f"http://127.0.0.1:{self.port}{path}"

    def build_request(self, job: dict[str, Any]) -> dict[str, Any]:
        width, height = render_dimensions(job)
        settings = job["settings"]
        payload: dict[str, Any] = {
            "prompt": job.get("resolved_prompt") or settings["prompt"],
            "negative_prompt": "", "width": width, "height": height, "seed": settings["seed"],
            "batch_count": 1, "output_format": "png", "output_compression": 100,
            "sample_params": {"sample_steps": settings["steps"]},
            "auto_resize_ref_image": True, "increase_ref_index": False,
            "ref_images": [], "mask_image": None,
        }
        if job.get("input"):
            payload["ref_images"] = [_data_url(Path(job["input"]))]
            if job.get("mask"):
                inference_mask = Path(job["output"]).with_name("inference-mask.png")
                prepare_inference_mask(Path(job["mask"]), inference_mask, int(job.get("mask_feather") or 0))
                payload["mask_image"] = _data_url(inference_mask)
        return payload

    def run(
        self,
        job: dict[str, Any],
        on_line: Callable[[str, bool], None],
        on_progress: Callable[[dict[str, Any]], None],
    ) -> None:
        try:
            self._run_native(job, on_line, on_progress)
        except FileNotFoundError as exc:
            on_line(f"sd-server unavailable, using sd-cli fallback: {exc}", False)
            self._run_fallback(job, on_line, on_progress)
        except (ConnectionError, TimeoutError, RuntimeError) as exc:
            self._set_state("error", error=str(exc))
            self._stop_server()
            if job.get("cancel_requested"):
                raise InterruptedError("render cancelled") from exc
            on_line(f"sd-server failed, using sd-cli fallback: {exc}", False)
            self._run_fallback(job, on_line, on_progress)

    def _run_native(
        self,
        job: dict[str, Any],
        on_line: Callable[[str, bool], None],
        on_progress: Callable[[dict[str, Any]], None],
    ) -> None:
        with self.lock:
            self.active_job_id = job["id"]
            self.active_job = job
            self.active_log = on_line
            self.active_progress = on_progress
        try:
            self._ensure_server()
            if job.get("cancel_requested"):
                raise InterruptedError("render cancelled")
            job["argv"] = self.build_server_argv(self.port or 0)
            on_progress({"stage": "load", "step": 0, "total": job["settings"]["steps"]})
            submitted = self.request_json("POST", self._url("/sdcpp/v1/img_gen"), self.build_request(job), 30)
            native_id = str(submitted.get("id") or "")
            if not native_id:
                raise RuntimeError("sd-server did not return a job id")
            with self.lock:
                self.native_jobs[job["id"]] = native_id
            self._set_state("rendering")
            on_line(f"Submitted native sd-server job {native_id}", False)
            while True:
                if job.get("cancel_requested"):
                    self.cancel(job["id"])
                    raise InterruptedError("render cancelled")
                if self.process is None or self.process.poll() is not None:
                    raise RuntimeError("sd-server exited while rendering")
                result = self.request_json("GET", self._url(f"/sdcpp/v1/jobs/{native_id}"), None, 10)
                status = str(result.get("status") or "")
                if status in {"queued", "generating"}:
                    if status == "generating":
                        progress = result.get("progress")
                        if isinstance(progress, dict) and "step" in progress:
                            on_progress({
                                "stage": "denoise", "step": int(progress.get("step") or 0),
                                "total": int(progress.get("total") or job["settings"]["steps"]),
                            })
                    self.sleep(0.1)
                    continue
                if status == "cancelled":
                    raise InterruptedError("render cancelled")
                if status == "failed":
                    error = result.get("error")
                    if isinstance(error, dict):
                        error = error.get("message")
                    raise RuntimeError(str(error or "sd-server generation failed"))
                if status != "completed":
                    raise RuntimeError(f"unexpected sd-server job status: {status or '(empty)'}")
                native_result = result.get("result") or {}
                if not isinstance(native_result, dict):
                    raise RuntimeError("sd-server returned an invalid job result")
                images = native_result.get("images") or []
                encoded = images[0].get("b64_json") if images and isinstance(images[0], dict) else None
                if not encoded:
                    raise RuntimeError("sd-server completed without an output image")
                try:
                    Path(job["output"]).write_bytes(base64.b64decode(encoded, validate=True))
                except (ValueError, TypeError) as exc:
                    raise RuntimeError("sd-server returned invalid image data") from exc
                on_progress({"stage": "save", "step": job["settings"]["steps"], "total": job["settings"]["steps"]})
                self.last_activity = self.clock()
                self._set_state("idle")
                return
        finally:
            with self.lock:
                self.native_jobs.pop(job["id"], None)
                self.active_log = None
                self.active_job = None
                self.active_progress = None
                self.active_job_id = None

    def _run_fallback(
        self,
        job: dict[str, Any],
        on_line: Callable[[str, bool], None],
        on_progress: Callable[[dict[str, Any]], None],
    ) -> None:
        self._set_state("rendering", backend="sd-cli")
        try:
            self.fallback.run(job, on_line, on_progress)
        except InterruptedError:
            self._set_state("unloaded", backend="sd-cli")
            raise
        except Exception as exc:
            self._set_state("error", error=str(exc), backend="sd-cli")
            raise
        finally:
            if self.state != "error":
                self._set_state("unloaded", backend="sd-cli")

    def cancel(self, job_id: str) -> None:
        with self.lock:
            native_id = self.native_jobs.get(job_id)
            loading = self.active_job_id == job_id and native_id is None
        if native_id:
            with contextlib.suppress(Exception):
                self.request_json("POST", self._url(f"/sdcpp/v1/jobs/{native_id}/cancel"), None, 5)
            return
        if loading:
            self._stop_server()
            return
        self.fallback.cancel(job_id)

    def _monitor_idle(self) -> None:
        while not self.stopping.wait(1):
            self._check_idle()

    def _check_idle(self) -> None:
        with self.lock:
            alive = self.process is not None and self.process.poll() is None
            idle_for = self.clock() - self.last_activity
            should_stop = alive and self.state == "idle" and idle_for >= self.config.idle_timeout
        if should_stop and self.idle_guard():
            self._stop_server()

    def _stop_server(self) -> None:
        with self.lock:
            process = self.process
            self.process = None
            self.port = None
        if process is not None and process.poll() is None:
            with contextlib.suppress(OSError):
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=2)
        self._set_state("unloaded")

    def close(self) -> None:
        self.stopping.set()
        self._stop_server()
        if self.monitor:
            self.monitor.join(timeout=2)

    def unload(self) -> None:
        self._stop_server()


class RenderQueue:
    def __init__(self, repository: Repository, adapter: Any, *, start_worker: bool = True) -> None:
        self.repository = repository
        self.adapter = adapter
        self.pending: queue.Queue[dict[str, Any]] = queue.Queue()
        self.lock = threading.RLock()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.order: list[str] = []
        self.running: dict[str, Any] | None = None
        self.subscribers: list[queue.Queue[str]] = []
        self.stopping = threading.Event()
        self.worker: threading.Thread | None = None
        if hasattr(self.adapter, "set_idle_guard"):
            self.adapter.set_idle_guard(self.is_idle)
        if hasattr(self.adapter, "set_status_callback"):
            self.adapter.set_status_callback(lambda value: self._broadcast("model", value))
        if start_worker:
            self.worker = threading.Thread(target=self._worker, name="render-queue", daemon=True)
            self.worker.start()

    def enqueue(self, request: dict[str, Any]) -> dict[str, Any]:
        settings = clean_settings(request)
        session = str(request.get("session") or self.repository.active_session)
        if session not in self.repository.sessions():
            raise ValueError("session not found")
        ident = uuid.uuid4().hex
        output = self.repository.work_output(ident)
        input_items = self.repository.inputs(session)
        input_item = input_items[0] if input_items else None
        input_path = self.repository.input_path(session)
        mask_path = self.repository.mask_path(session)
        mask_record = self.repository.mask(session) if mask_path else None
        if mask_path and settings["steps"] < 12:
            settings["steps"] = 12
        job_input: Path | None = None
        job_mask: Path | None = None
        if input_path:
            job_input = output.parent / f"source{input_path.suffix.lower()}"
            shutil.copy2(input_path, job_input)
            if mask_path:
                job_mask = output.parent / "mask.png"
                shutil.copy2(mask_path, job_mask)
        resolved_prompt = settings["prompt"]
        if input_item:
            resolved_prompt = re.sub(
                rf"@{re.escape(input_item['name'])}(?![A-Za-z0-9._-])", "<image1>", resolved_prompt
            )
        job = {
            "id": ident, "session": session, "status": "queued", "settings": settings,
            "input": str(job_input) if job_input else None, "mask": str(job_mask) if job_mask else None,
            "mask_feather": int(mask_record["feather"]) if mask_record else 0,
            "output": str(output),
            "input_width": input_item["width"] if input_item else None,
            "input_height": input_item["height"] if input_item else None,
            "resolved_prompt": resolved_prompt,
            "progress": {"stage": "load", "step": 0, "total": settings["steps"]},
            "result": {}, "created": now_iso(), "cancel_requested": False,
        }
        self.repository.create_job(job)
        with self.lock:
            self.jobs[ident] = job
            self.order.append(ident)
        self.pending.put(job)
        self._broadcast("queue", self.queue_state())
        return self.summary(job)

    def cancel(self, ident: str) -> bool:
        with self.lock:
            job = self.jobs.get(ident)
            if not job or job["status"] not in {"queued", "running", "cancelling"}:
                return False
            job["cancel_requested"] = True
            if job["status"] == "queued":
                job["status"] = "cancelled"
                self.repository.update_job(ident, status="cancelled", finished_at=now_iso())
                self._broadcast("job", self.summary(job))
            else:
                job["status"] = "cancelling"
                self.repository.update_job(ident, status="cancelling")
                self.adapter.cancel(ident)
        self._broadcast("queue", self.queue_state())
        return True

    def queue_state(self) -> list[dict[str, Any]]:
        with self.lock:
            return [self.summary(self.jobs[ident]) for ident in self.order if self.jobs[ident]["status"] in {"queued", "running", "cancelling"}]

    def is_idle(self) -> bool:
        with self.lock:
            return self.running is None and not any(
                job["status"] in {"queued", "running", "cancelling"} for job in self.jobs.values()
            )

    def model_state(self) -> dict[str, Any]:
        if hasattr(self.adapter, "status"):
            return self.adapter.status()
        return {
            "status": "unloaded", "backend": "sd-cli", "pid": None,
            "idle_timeout": None, "idle_remaining": None, "error": None,
        }

    def can_unload_model(self) -> bool:
        return self.is_idle() and hasattr(self.adapter, "unload")

    def unload_model(self) -> bool:
        with self.lock:
            if not self.is_idle() or not hasattr(self.adapter, "unload"):
                return False
            self.adapter.unload()
        self._broadcast("model", self.model_state())
        return True

    @staticmethod
    def summary(job: dict[str, Any]) -> dict[str, Any]:
        settings = job["settings"]
        argv = job.get("argv") or []
        return {
            "id": job["id"], "session": job["session"], "status": job["status"],
            "prompt": settings["prompt"], "ratio": settings["ratio"], "seed": settings["seed"],
            "masked": bool(job.get("mask")),
            "progress": job["progress"], "result": job.get("result") or {},
            "error": job.get("error"), "argv_display": " ".join(argv),
        }

    def subscribe(self) -> queue.Queue[str]:
        subscriber: queue.Queue[str] = queue.Queue(maxsize=500)
        with self.lock:
            self.subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[str]) -> None:
        with self.lock:
            with contextlib.suppress(ValueError):
                self.subscribers.remove(subscriber)

    def _broadcast(self, event_type: str, data: Any) -> None:
        payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=True)
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

    def _worker(self) -> None:
        while not self.stopping.is_set():
            try:
                job = self.pending.get(timeout=0.25)
            except queue.Empty:
                continue
            if job["status"] == "cancelled":
                self._finish_order(job["id"])
                self.pending.task_done()
                continue
            self._run(job)
            self.pending.task_done()

    def _run(self, job: dict[str, Any]) -> None:
        started = time.monotonic()
        with self.lock:
            self.running = job
            job["status"] = "running"
        self.repository.update_job(job["id"], status="running", started_at=now_iso())
        self._broadcast("queue", self.queue_state())

        def log(line: str, rewrites: bool = False) -> None:
            self.repository.append_job_log(job["id"], line)
            self._broadcast("log", {"id": job["id"], "session": job["session"], "line": line, "rewrites": rewrites})

        def progress(value: dict[str, Any]) -> None:
            job["progress"] = value
            self.repository.update_job(job["id"], progress=value)
            self._broadcast("progress", {"id": job["id"], "progress": value, "result": job.get("result") or {}})

        try:
            self.adapter.run(job, log, progress)
            if job.get("input") and job.get("mask"):
                composite_masked_output(
                    Path(job["output"]), Path(job["input"]), Path(job["mask"]), int(job.get("mask_feather") or 0)
                )
            width, height = image_dimensions(Path(job["output"]))
            elapsed = time.monotonic() - started
            params = {
                "name": f"take-{job['id'][:8]}.png", "prompt": job["settings"]["prompt"],
                "seed": job["settings"]["seed"], "steps": job["settings"]["steps"],
                "width": width, "height": height, "mode": "I2I" if job.get("input") else "T2I",
                "edit": bool(job.get("input")), "masked": bool(job.get("mask")),
                "elapsed": elapsed, "settings": job["settings"],
            }
            take_id = self.repository.add_take(job["session"], Path(job["output"]), params)
            job["result"] = {"take_id": take_id, "width": width, "height": height}
            job["status"] = "done"
            log(f"Saved take {take_id} ({width}x{height})")
            self.repository.update_job(
                job["id"], status="done", result=job["result"], finished_at=now_iso()
            )
        except InterruptedError:
            job["status"] = "cancelled"
            log("Render cancelled")
            self.repository.update_job(job["id"], status="cancelled", finished_at=now_iso())
        except Exception as exc:
            job["status"] = "cancelled" if job.get("cancel_requested") else "failed"
            job["error"] = str(exc)
            log(f"Render {job['status']}: {exc}")
            self.repository.update_job(
                job["id"], status=job["status"], error=str(exc), finished_at=now_iso()
            )
        finally:
            with self.lock:
                self.running = None
            self._broadcast("job", self.summary(job))
            self._finish_order(job["id"])
            self._broadcast("queue", self.queue_state())

    def _finish_order(self, ident: str) -> None:
        with self.lock:
            with contextlib.suppress(ValueError):
                self.order.remove(ident)
            self.jobs.pop(ident, None)

    def stop(self) -> None:
        self.stopping.set()
        if self.running:
            self.cancel(self.running["id"])
        if self.worker:
            self.worker.join(timeout=5)
        if hasattr(self.adapter, "close"):
            self.adapter.close()
