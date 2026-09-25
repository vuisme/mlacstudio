# MLAC Studio 0.3.0 Windows distribution design

## Product identity and state

- Product UI, tray, executable, shortcuts, mutex, startup value, installer, and release files use **MLAC Studio**.
- Mutable state lives at `%LOCALAPPDATA%\MLACStudio`; bootstrap files live at
  `%LOCALAPPDATA%\Programs\MLACStudio`.
- Qwen wording remains only where it identifies the Qwen-Image/Qwen3 model family or its licenses.
- First launch safely imports the old `%LOCALAPPDATA%\QwenImageStudio` database, gallery, configuration, model-manager
  records, and startup opt-in. It does not copy weights or transient work. Absolute external model paths remain exact.

## Bootstrap and components

`MLAC-Studio-Setup-<version>.exe` is a small .NET Framework WinExe compiled from
`bootstrap/MLACStudioBootstrap.cs`. It contains no Python runtime, CUDA DLLs, stable-diffusion.cpp binaries, or models.

Signed metadata selects these independently versioned and content-addressed components:

| Component | Contents | Selection |
| --- | --- | --- |
| `core` | MLACStudio.exe, Python application files, static UI, notices, model manifest | Always |
| `common-runtime` | sd-cli.exe, sd-server.exe, CPU/common DLLs and notices | Always |
| `nvidia-runtime` | CUDA/NVIDIA runtime DLLs | NVIDIA driver meets signed minimum |

Installed objects use `components/<id>/<version>-<sha-prefix>/`. The atomic `components/active.json` pointer records
the active and one previous object per component. An activation journal restores the prior pointer after interruption.
Only changed SHA-256 objects download. Explicit rollback swaps active/previous; ordinary metadata cannot downgrade.

## Update trust and channels

- Metadata is canonical JSON signed with RSA-SHA256 and a bundled public key. The available Windows framework has no
  dependable built-in Ed25519 implementation; unsigned, malformed, wrong-key, or invalid signatures are rejected.
- Component URLs must be public `github.com/vuisme/mlacstudio` release assets and include byte size plus SHA-256.
- `stable` is default. `beta` and `dev` require an explicit persisted opt-in.
- Startup checks use ETag caching. Prompts show version, channel, changelog, changed components, bytes, restart, and any
  data migration. A migration always needs a separate explicit confirmation.
- An update blocks startup only when the valid signed payload sets `security_mandatory: true`.
- Models and model manifests remain a separate authenticated workflow and never auto-update with application components.

## Download and activation safety

Downloads use HTTPS, HTTP Range resume, bounded retries/timeouts, visible progress, expected size, and SHA-256. Staging
rejects traversal, model directories, and common model-weight extensions. Disk checks include a 256 MiB or 10% reserve.
Extraction completes before pointer activation; stale objects are pruned only after the active pointer is durable.

## Process behavior

The tray owns one `Local\MLACStudio.Tray.v1` mutex and the `MLACStudio` HKCU Run value. Both sd-server and sd-cli
continue to launch with argv arrays, `shell=False`, `CREATE_NO_WINDOW`, and `SW_HIDE`; sd-server stays bound to
`127.0.0.1`.
