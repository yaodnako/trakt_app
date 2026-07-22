from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata


root = Path(SPECPATH)
runtime_hook = os.environ.get("TRAKT_TRACKER_RELEASE_HOOK", "").strip()
version_file = os.environ.get("TRAKT_TRACKER_VERSION_FILE", "").strip()

datas = collect_data_files("trakt_tracker.web", includes=["templates/*", "static/*"])
datas += copy_metadata("trakt-tracker")
hiddenimports = collect_submodules("keyring.backends")

a = Analysis(
    [str(root / "trakt_tracker" / "__main__.py")],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[runtime_hook] if runtime_hook else [],
    excludes=["trakt_tracker.ui", "pytest"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TraktTracker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(root / "icons" / "trakt-black-white.ico"),
    version=version_file or None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="TraktTracker",
    contents_directory="_internal",
)
