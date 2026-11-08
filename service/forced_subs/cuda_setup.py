"""Make the pip-installed CUDA libraries (cuBLAS, cuDNN) visible on Windows.

faster-whisper's CTranslate2 backend loads cuBLAS/cuDNN DLLs at runtime. When
they come from the ``nvidia-*-cu12`` pip packages they live inside
site-packages, which Windows does not search by default. Import this module
before importing faster_whisper.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def add_nvidia_dll_dirs() -> list[str]:
    if os.name != "nt":
        return []
    added: list[str] = []
    for base in map(Path, sys.path):
        nvidia = base / "nvidia"
        if not nvidia.is_dir():
            continue
        for bin_dir in nvidia.glob("*/bin"):
            if bin_dir.is_dir():
                path = str(bin_dir)
                try:
                    os.add_dll_directory(path)
                except OSError:
                    pass
                os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
                added.append(path)
    return added


add_nvidia_dll_dirs()
