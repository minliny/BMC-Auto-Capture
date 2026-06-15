# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for bmc-auto-capture v2.0
One-directory bundle — Playwright Chromium is too large for one-file.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

_root = Path(__file__).parent.parent

_runtime_hiddenimports = [
    "paramiko",
    "playwright",
    "openpyxl",
    "psutil",
    "yaml",
    "PIL",
    "textual",
    "rich",
    "fastapi",
    "fastapi.middleware.cors",
    "fastapi.responses",
    "fastapi.exceptions",
    "uvicorn",
    "pydantic",
    "aiofiles",
    "python_multipart",
    "multipart",
]

for _package in (
    "fastapi",
    "starlette",
    "uvicorn",
    "pydantic",
    "pydantic_core",
    "anyio",
    "multipart",
    "python_multipart",
):
    _runtime_hiddenimports += collect_submodules(_package)

a = Analysis(
    [
        str(_root / "run.py"),
    ],
    pathex=[str(_root)],
    binaries=[],
    datas=[
        (str(_root / "config" / "default_config.yaml"), "config"),
        (str(_root / "config" / "logging.yaml"), "config"),
    ],
    hiddenimports=sorted(set(_runtime_hiddenimports)),
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
        "src",
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
