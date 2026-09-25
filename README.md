# MLAC Studio 0.3.0 for Windows

A local, standalone Windows image studio using Qwen-Image-2.1 through
[`stable-diffusion.cpp`](https://github.com/leejet/stable-diffusion.cpp). The upstream static studio UI is retained and
adapted for a local SQLite database, filesystem gallery, and a single render queue backed by stable-diffusion.cpp.
Helm Runtime, Diffusers, Torch, and remote services are not used.

The Python web service stays running while model inference is managed separately. It starts `sd-server.exe` only when
the first queued job is ready to run, reuses that server for later jobs, and unloads it after the configured idle
timeout once no job is queued or running. If the native server cannot start or a native job fails, the existing
per-job `sd-cli.exe` path remains available as an automatic fallback.

The server always binds to `127.0.0.1`. It does not configure or modify 9router, Tailscale, cloudflared, or any other
network service.

## Requirements

- Windows 10 or 11
- 64-bit Python 3.12 (the pinned version in `.python-version`)
- [`uv`](https://docs.astral.sh/uv/) for the local environment
- A working CUDA build of `stable-diffusion.cpp`
- For source development, local Qwen-Image transformer, text encoder, mmproj, and VAE files; packaged builds install
  these later through the authenticated model UI

This checkout's [`config.json`](config.json) already points at:

| Component | Default |
| --- | --- |
| Runtime | `D:\AI\runtimes\stable-diffusion.cpp\sd-cli.exe` |
| Native server | `D:\AI\runtimes\stable-diffusion.cpp\sd-server.exe` |
| Transformer | `D:\AI\models\qwen-image-2.1-uncensored-gguf\qwen-image-2.1-UC-Q4_K_M.gguf` |
| Text encoder | `...\text_encoders\Qwen3VL-8B-Instruct-Q4_K_M.gguf` |
| MMProj | `...\text_encoders\mmproj-Qwen3VL-8B-Instruct-F16.gguf` |
| VAE | `...\vae\qwen_image_2.1_vae_bf16.safetensors` |

The adapters map these to `--diffusion-model`, `--llm`, `--llm_vision`, and `--vae`. Extra stable-diffusion.cpp
arguments are configured as an argv array under `extra_args`; they are never evaluated by a shell. Both executables
are launched with argv lists and `shell=False`.

`sd_server` may be left empty to force the per-job `sd-cli` fallback. When omitted from `config.json`, the studio also
looks for `sd-server.exe` beside the configured `sd-cli.exe`.

## Start

From PowerShell:

```powershell
uv sync
.\run-windows.ps1
```

`uv sync` creates the Python 3.12 `.venv`, installs the bundled Windows Pillow wheel, and resolves the pinned pystray
package. Pillow is used for mask validation, exact post-render compositing, and the programmatic tray icon. Pystray
provides the Windows notification-area controller. Packaged builds also bundle keyring's WinVault backend for optional
HF tokens. The launcher opens
`http://127.0.0.1:8730/`, starts the local Python server, and remains available in the system tray.
To suppress the browser or choose another local port:

```powershell
.\run-windows.ps1 -NoBrowser
.\run-windows.ps1 -Port 8740
```

On first launch, create the single local admin account. The password must contain at least 12 characters. The Studio
then opens its model setup panel even when no `config.json` exists. Select the recommended compatible hardware
profile, review and explicitly accept the model/version-specific license, and start the verified download. Later
visits open the login screen and the same **Models** panel remains available for installs and profile changes.

Only one launcher instance runs per Windows session. Starting the application again opens the existing Studio URL
instead of trying to bind the port a second time.

## System tray

The notification-area menu provides:

- **Open Studio** to open the authenticated localhost UI in the default browser.
- A disabled **Model: ...** status line showing the current native model state and PID when loaded.
- **Unload model** when the render queue is idle, which terminates `sd-server.exe` without stopping the Studio.
- **Run at Windows startup**, an unchecked per-user opt-in stored in `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.
- **Exit**, which shuts down the localhost server, closes the application queue, and terminates any supervised
  `sd-server.exe` process.

Startup launches directly to the tray without opening a browser. The installer offers the same unchecked opt-in;
uninstall always removes the startup entry, including entries enabled later from the tray.

## Usage

- Enter a prompt and choose an aspect ratio, dimensions, steps, and seed for text-to-image generation.
- Add one PNG, JPEG, or WebP reference to switch to image-to-image mode. A new upload replaces the current reference.
- With a reference selected, paint the red mask with mouse, pen, or touch. Brush/erase, size, feather, undo/redo,
  clear, and invert controls are available. The grayscale PNG mask is saved automatically.
- Masked edits use at least 12 sampling steps. The saved hard mask is expanded and softened into a separate inference
  mask so Qwen receives enough neighboring context to synthesize coherent edges; the original mask remains unchanged.
- Native generation uses `/sdcpp/v1/img_gen` and polls `/sdcpp/v1/jobs/{id}`. Text-to-image jobs send the prompt and
  sampling settings; edits send the reference through `ref_images` and the prepared mask through `mask_image`.
- The CLI fallback passes the same edit inputs with `--ref-image` and `--mask`. For both backends, the generated image
  is resized to the source geometry when needed and blended through the selected feather transition. Pixels where the
  original hard mask is exactly zero remain pixel-identical to the decoded source.
- Mask quality still depends on the selected area and prompt. Paint the complete object plus a little surrounding
  context, and describe the replacement, camera angle, lighting, shadows, and background. Q4 GGUF can still follow
  complex structural edits less reliably than the much slower BF16 fallback; review each result rather than assuming
  a technically successful job is semantically correct.
- Generated images appear in the session's local gallery and can be reused as a reference.
- Renders execute one at a time. Additional requests remain queued and can be cancelled.
- Progress and process output stream to the page through server-sent events.
- The runtime card reports `unloaded`, `loading`, `ready`, `rendering`, `idle`, or `error`, plus the inference PID when
  a process is alive.
- Use **Models** to see installed and active profiles, install another compatible profile, switch profiles, resume or
  retry interrupted downloads, and remove an inactive profile after typing an explicit confirmation. Existing models
  are retained when switching. Switching or deletion is blocked while a render is queued or active.
- The model panel also controls the idle unload timeout. Runtime and model paths come only from the validated bundled
  release manifest. Official `huggingface.co`/`hf.co` overrides must pin a 40-hex commit; the app resolves LFS size and
  SHA-256 through the HF API. An admin may instead configure an arbitrary public HTTPS URL only after explicitly
  acknowledging responsibility. Size and SHA-256 are optional only for those custom sources, and no-hash files remain
  labeled **UNVERIFIED** in source state, downloads, the installed registry, active-model state, and the Web UI.
- Optional private-repository tokens are stored only through Windows Credential Manager. The persisted state contains
  a credential reference, never the token. Bearer authorization is attached only to `huggingface.co`/`hf.co` origin
  and API requests and is removed on every cross-host redirect, including redirects to Hugging Face CDN hosts.

## Local data

Source-checkout runtime state is stored under `data/` by default. Packaged builds use
`%LOCALAPPDATA%\MLACStudio`:

- `data/studio.db`: admin hash, sessions, settings, jobs, gallery metadata, and runtime path overrides
- `data/uploads/`: the current reference file for each session
- `data/masks/`: the current grayscale PNG brush mask for each session/reference
- `data/gallery/`: completed PNG takes
- `data/work/`: temporary render outputs
- `models/`: model artifacts, source-provenance sidecars, and resumable `.part` files (packaged builds)
- `model-manager-state.json`: persistent download stage, byte counts, errors, and resume state (packaged builds)
- `installed-profiles.json`: installed and active profile registry (packaged builds)
- `license-acceptance.json`: model-, license-, and version-specific acceptance records (packaged builds)
- `hf-source.json`: confirmed HF/custom source metadata, custom responsibility acknowledgement, verification labels,
  and an optional Windows credential reference; never a token

`data/` is ignored by Git. Back it up if the gallery matters. Deleting it resets the application, including the admin
account.

## Security

- The listener is hard-coded to `127.0.0.1`; there is no public bind option.
- The supervised inference server is also explicitly launched with `--listen-ip 127.0.0.1` on an ephemeral local port.
- All app, model-management, API, SSE, upload, and media routes require login. Before the first admin exists, only
  health, setup/login assets, the narrowly scoped bootstrap status, and CSRF-protected admin creation are public;
  every request is still restricted to loopback Host/Origin rules.
- Passwords use a random 32-byte salt and PBKDF2-HMAC-SHA256 with 600,000 iterations via Python's standard library.
- Authentication uses a random server-side session with an `HttpOnly`, `SameSite=Strict` cookie.
- State-changing JSON and upload requests require a per-session CSRF token.
- Mask uploads require authentication and CSRF, accept PNG only, enforce upload/pixel limits, and must exactly match
  the current reference dimensions. Stored paths remain constrained to the mask directory.
- Setup and login requests also use short-lived pre-auth CSRF tokens.
- Failed logins are throttled after five attempts in a 15-minute window.
- Host and Origin checks reject DNS-rebinding and cross-site requests.
- Media access uses opaque IDs and resolved-path containment checks.
- The inference process is launched with an argv list and `shell=False`. Windows cancellation stops the process group and
  falls back to `taskkill /T /F` if needed.
- Tray actions call the in-process application controller directly; no unauthenticated unload or shutdown web route is
  exposed. Existing web APIs retain login and CSRF enforcement.
- Source and token APIs require the authenticated admin session and CSRF. All downloads reject HTTP, embedded
  credentials, fragments, localhost, and private/link-local/reserved IP destinations. Custom host DNS and every redirect
  destination are revalidated for public addresses. Official HF sources additionally reject mutable revisions,
  traversal, and query strings. Browser-selected filesystem destinations are never accepted, and token values are
  redacted from API errors, SSE state, and logs.

This is local application protection, not a substitute for Windows account security or disk encryption.

## Configuration

For source development, edit [`config.json`](config.json) to change the default port, data directory, idle timeout, or
fixed stable-diffusion.cpp arguments. Packaged builds atomically generate this file after a verified profile install
or switch. `sd_server_idle_timeout` defaults to `300` seconds. The timeout starts only after a render completes and
unload is deferred while any job is queued or running. Do not put passwords, tokens, or other secrets in this file.

## Tests

The suite uses Pillow plus a mocked inference adapter; it does not load models or run the GPU:

```powershell
python -m unittest discover -s tests -v
```

Coverage includes authentication, bootstrap scoping, CSRF and route protection, manifest/license validation, public
HTTPS/redirect SSRF checks, immutable HF metadata and CDN redirects, authorization stripping, acknowledged custom
sources, no-hash completion and labels, mocked credential storage, token redaction, URL-bound `.part` resume,
cancellation, SHA-256 failure handling, persistent async download state, atomic config
activation, render-busy switch/delete guards, installer packaging contracts, launcher-without-config behavior, native
inference lifecycle, masks, the render queue, and local gallery persistence. All model downloads use mocks or local
fixtures; the test suite performs no model network access.

## Direct server invocation

For development:

```powershell
python .\web\server.py --config .\config.json --port 8730
```

The server still binds only to `127.0.0.1`.

## Windows packaging

The tiny online bootstrap, deterministic component tooling, signed update metadata, and deployment instructions are
under [`packaging/`](packaging/DEPLOYMENT.md). The 0.3.0 bootstrap is a compiled Windows GUI executable and embeds no
Python, CUDA, stable-diffusion.cpp binaries, or model weights. It selects the core, common runtime, and compatible
NVIDIA runtime components, downloads only changed GitHub release assets, verifies SHA-256 plus the signed manifest,
and atomically advances `active.json` while retaining one rollback version. Models remain managed separately in the
authenticated web UI and are never automatically updated.

## License and upstream

The project remains under the [MIT license](LICENSE). Upstream provenance is recorded in [UPSTREAM.md](UPSTREAM.md).
