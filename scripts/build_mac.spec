# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for bmc-auto-capture — one-directory bundle."""
import os
from pathlib import Path

_ROOT = Path(os.path.abspath(os.path.dirname(SPECPATH))).parent

a = Analysis(
    [str(_ROOT / "src" / "__main__.py")],
    pathex=[str(_ROOT / "src")],
    binaries=[],
    datas=[
        (str(_ROOT / "config" / "default_config.yaml"), "config"),
        (str(_ROOT / "config" / "logging.yaml"), "config"),
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
        "python_multipart",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "scipy", "IPython", "jupyter"],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)
exe = EXE(pyz, a.scripts, a.binaries, a.zipfiles, a.datas,
    name="bmc-auto-capture", console=True, debug=False, strip=False, upx=True,
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=True, name="bmc-auto-capture",
)
