# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


packaging_dir = Path(SPECPATH)
project_root = packaging_dir.parent
release_manifest = Path(os.environ["MLAC_MODEL_MANIFEST"]).resolve()
if not release_manifest.is_file():
    raise SystemExit(f"MLAC_MODEL_MANIFEST is not a file: {release_manifest}")

common_datas = [
    (str(project_root / "web" / "static"), "web/static"),
    (str(project_root / "LICENSE"), "."),
    (str(packaging_dir / "THIRD_PARTY_NOTICES.md"), "."),
    (str(packaging_dir / "DEPLOYMENT.md"), "."),
    (str(packaging_dir / "QWEN_RESEARCH_LICENSE_NOTICE.txt"), "."),
    (str(packaging_dir / "model-manager.py"), "."),
    (str(packaging_dir / "updater.py"), "."),
    (str(packaging_dir / "keys" / "mlac-update-public.json"), "keys"),
    (str(release_manifest), "."),
]

app_analysis = Analysis(
    [str(packaging_dir / "launcher.py")],
    pathex=[str(project_root / "web"), str(packaging_dir)],
    binaries=[],
    datas=common_datas,
    hiddenimports=[
        "server", "inference", "security", "storage", "tray_icon", "updater",
        *collect_submodules("PIL"), *collect_submodules("pystray"), *collect_submodules("keyring"),
    ],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False,
)
app_pyz = PYZ(app_analysis.pure)
app_exe = EXE(
    app_pyz, app_analysis.scripts, [], exclude_binaries=True, name="MLACStudio",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False,
    disable_windowed_traceback=False, argv_emulation=False, target_arch=None,
    codesign_identity=None, entitlements_file=None, contents_directory=".",
)
bundle = COLLECT(
    app_exe, app_analysis.binaries, app_analysis.datas, strip=False, upx=False,
    upx_exclude=[], name="MLACStudio",
)
