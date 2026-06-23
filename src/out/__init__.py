"""Output helpers with lazy imports.

Keep package import cheap so standalone tools such as acceptance DOCX export do
not load the full execution configuration stack.
"""

from __future__ import annotations

from importlib import import_module

_EXPORTS = {
    "write_result_csv": ".collector",
    "write_final_result_csv": ".collector",
    "compute_summary": ".collector",
    "build_pivot_csv": ".summary",
    "print_terminal_summary": ".summary",
    "write_text_file": ".file_writer",
    "write_html_file": ".file_writer",
    "write_log_file": ".file_writer",
    "overlay_device_info": ".screenshot",
    "render_text_to_image": ".screenshot",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value
