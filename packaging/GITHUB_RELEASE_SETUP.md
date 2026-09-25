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

Repository variable:
- `MLAC_RUNTIME_DIRECTORY`: a path available on the selected release runner containing `stable-diffusion.cpp`. Large component release builds normally require a self-hosted Windows runner because GitHub-hosted runners do not carry this runtime.
