# MLAC Studio 0.3.0 release and deployment

No command in this document publishes automatically. Draft creation, promotion, and rollback require the literal
human approval phrase enforced by `github-release.ps1`.

## Release inputs

- Python 3.12 release environment with `packaging/requirements-build.txt`.
- Pinned HTTPS URL, byte size, SHA-256, and archive member paths for a tested Windows stable-diffusion.cpp runtime
  containing `sd-cli.exe`, `sd-server.exe`, required NVIDIA DLLs, and notices.
- Local model files used only to derive the separate model manifest hashes; weights are never packaged.
- Immutable model URLs in `artifact-urls.json`.
- An offline RSA private key matching `keys/mlac-update-public.json`. Keep it outside source control.
- An explicit ISO-8601 `PublishedAt` value so signed metadata is reproducible.

## Build

```powershell
& .\packaging\build.ps1 `
  -UrlMapPath .\packaging\artifact-urls.release.json `
  -LocalFilesPath .\packaging\local-files.json `
  -PrivateKeyPath X:\offline\mlac-release-signing-key.xml `
  -PublishedAt 2026-09-25T00:00:00Z `
  -Python C:\release-venv\Scripts\python.exe `
  -Version 0.3.0 -Channel stable
```

The URL map used for release builds must include `runtime-stable-diffusion-cpp-cuda12` as an object containing
`url`, `size`, and `sha256`; see `artifact-urls.example.json`. The build writes a clean `dist\mlac-release` directory,
validates runtime/model sources, creates the PyInstaller core without native inference binaries, deterministically
archives the core component, signs `MLAC-Studio-stable.json`, compiles the small bootstrap, and writes checksums.

Expected release files:

- `MLAC-Studio-Setup-0.3.0.exe`
- `MLAC-Studio-core-0.3.0.zip`
- `MLAC-Studio-stable.json`
- `MLAC-Studio-portable-0.3.0.zip` (optional source/support artifact)
- `release-manifest.json`, notices, and `SHA256SUMS.txt`

Code-sign the bootstrap and `MLACStudio.exe` with Authenticode before final checksums. The signed JSON trust chain is
separate and remains mandatory even when Authenticode is present.

## Installation and updates

The bootstrap installs itself to `%LOCALAPPDATA%\Programs\MLACStudio`, creates MLAC Studio shortcuts, checks disk
capacity, and retrieves only the changed core component from public `vuisme/mlacstudio` GitHub release assets. It
resumes partial downloads, retries transient failures, verifies size and SHA-256, extracts to staging, and atomically
updates `%LOCALAPPDATA%\MLACStudio\components\active.json`.

On first launch, the authenticated model manager detects compatible NVIDIA hardware and downloads the pinned native
runtime archive plus the selected model artifacts. It verifies the archive before safe extraction into mutable local
state; no native runtime or model weight is shipped in the GitHub application release.

The application checks the opted-in channel at startup with ETag. A valid signed manifest drives update, skip,
mandatory-security, restart, and migration messaging. Data/database migration requires an additional confirmation.
The component store retains one previous version. Only `--rollback` permits a downgrade.
Stable is the default; `MLACStudio.exe --update-channel beta` or `--update-channel dev` is the explicit opt-in, and
`--update-channel stable` returns to the production feed.

User data, admin credentials, gallery, configuration, model registry, accepted licenses, and model files survive all
component updates. Models are never selected by the bootstrap and never auto-update.

## Legacy migration

On first 0.3.0 run, the bootstrap and launcher recognize `%LOCALAPPDATA%\QwenImageStudio` and the old
`QwenImageStudio` HKCU startup value. They copy database/gallery/configuration state to the clean MLAC root, preserve
absolute external model paths, exclude weight files and transient work, write an idempotent marker, replace the startup
entry, and leave the legacy directory intact for recovery.

## Draft, promotion, rollback

```powershell
& .\packaging\github-release.ps1 -Action draft -Version 0.3.0 `
  -Approve "I APPROVE MLAC RELEASE draft"

& .\packaging\github-release.ps1 -Action promote -Version 0.3.0 -Channel stable `
  -Approve "I APPROVE MLAC RELEASE promote"

& .\packaging\github-release.ps1 -Action rollback -Version 0.3.0 -RollbackVersion 0.2.9 `
  -Approve "I APPROVE MLAC RELEASE rollback"
```

Review `workflows/mlac-release/workflow.md` before any of these actions. Promotion/rollback can affect users; a release
with `data_migration` or `security_mandatory` requires explicit human review of those fields.

## Verification

```powershell
python -m unittest discover -s tests -v
& .\packaging\build-bootstrap.ps1 -Version 0.3.0
```

Before publication, test clean bootstrap, supported NVIDIA driver classes, interrupted resume, bad hash/signature,
ETag 304, all channels, mandatory behavior, low disk, interrupted activation recovery, legacy migration, rollback,
downgrade rejection, and absence of console windows for both sd-server and sd-cli.
