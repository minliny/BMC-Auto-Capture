"""Console-script entry point.

This delegates to the existing release shim for now so PyInstaller, Windows
batch files, and source checkout behavior stay aligned during the package-name
transition.
"""

from __future__ import annotations


def main():
    from run import main as run_main

    return run_main()
