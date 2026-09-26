# Release signing setup

MLAC update metadata is signed with the private RSA key stored only as the protected GitHub Actions secret `MLAC_UPDATE_SIGNING_KEY_B64`. The matching public key is committed at `packaging/keys/mlac-update-public.json` and bundled in the bootstrap/updater.

```powershell
./packaging/setup-github-signing.ps1 `
  -PrivateKeyPath "$env:USERPROFILE\.mlacstudio\release-signing.xml" `
  -UploadSecret
```

The private key path is ignored by Git and must be backed up offline. Never put the XML key in the repository, release assets, logs, or Actions artifacts. Rotate it only through a reviewed source change that updates the bundled public key.

GitHub environments:
- `release-draft`: protects signing and draft creation. Configure required reviewers.
- `production-release`: protects publishing, promotion, and rollback. Configure owner approval.

Repository variables:
- `MLAC_RUNTIME_URL`: immutable HTTPS URL for the tested native runtime ZIP.
- `MLAC_RUNTIME_SIZE`: exact runtime ZIP byte size.
- `MLAC_RUNTIME_SHA256`: lowercase SHA-256 of the runtime ZIP.

The release runner records these values in the bundled model manifest but never downloads or packages the native
runtime. End-user onboarding downloads and verifies it later on demand.
