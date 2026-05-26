# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for bmc-auto-capture v2.0
One-directory bundle — Playwright Chromium is too large for one-file.
"""

import sys
from pathlib import Path

_root = Path(__file__).parent.parent

a = Analysis(
    [
        str(_root / "src" / "__main__.py"),
    ],
    pathex=[str(_root / "src")],
    binaries=[],
    datas=[
        (str(_root / "config" / "default_config.yaml"), "config"),
        (str(_root / "config" / "logging.yaml"), "config"),
    ],
    hiddenimports=[
        "paramiko",
        "playwright",
        "openpyxl",
        "psutil",
        "yaml",
        "PIL",
        "textual",
        "rich",
        "fastapi",
        "uvicorn",
        "pydantic",
        "aiofiles",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "pandas",
        "scipy",
        "IPython",
        "jupyter",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="bmc-auto-capture",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(_root / "assets" / "icon.ico") if (_root / "assets" / "icon.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="bmc-auto-capture",
)
