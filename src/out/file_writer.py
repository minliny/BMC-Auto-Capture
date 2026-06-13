"""
File writer — writes TXT, HTML, and log files to output directories.
"""

import os
from datetime import datetime

from ..utils.path_safety import safe_join_under_root, is_safe_path_component


def write_text_file(output_dir: str, filename: str, content: str) -> str:
    if not is_safe_path_component(filename):
        raise ValueError(f"Unsafe filename for text file: {filename!r}")
    path = safe_join_under_root(output_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def write_html_file(output_dir: str, filename: str, html: str) -> str:
    if not is_safe_path_component(filename):
        raise ValueError(f"Unsafe filename for HTML file: {filename!r}")
    path = safe_join_under_root(output_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


def write_log_file(output_dir: str, filename: str, content: str) -> str:
    if not is_safe_path_component(filename):
        raise ValueError(f"Unsafe filename for log file: {filename!r}")
    path = safe_join_under_root(output_dir, filename)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"[{timestamp}]\n{content}\n")
    return path
