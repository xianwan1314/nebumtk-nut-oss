# SPDX-FileCopyrightText: 2026 雾月星辰 & MLXC <github@XingChenRS>
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import ctypes
import os
import platform
import sys
from pathlib import Path
from typing import Any


def _bundle_root() -> Path:
    return Path(__file__).resolve().parent


def _windows_libusb_arch() -> str:
    machine = platform.machine().upper()
    if machine in {"AMD64", "X86_64"}:
        return "x64"
    if machine in {"ARM64", "AARCH64"}:
        return "arm64"
    return "x86"


def _candidate_dll_paths() -> list[Path]:
    """Ordered list of places we ship / expect libusb-1.0.dll."""
    arch = _windows_libusb_arch()
    root = _bundle_root()
    paths: list[Path] = [
        # Explicit Nuitka data dir (nebula/native/libusb)
        root / "libusb" / "libusb-1.0.dll",
        root / "libusb" / arch / "libusb-1.0.dll",
        root / "native" / "libusb" / "libusb-1.0.dll",
        root / "native" / "libusb" / arch / "libusb-1.0.dll",
        # pip libusb package layout (dev + if Nuitka expands package data)
        root / "libusb" / "_platform" / "_windows" / arch / "libusb-1.0.dll",
        root / "libusb" / "_platform" / "windows" / arch / "libusb-1.0.dll",
    ]
    try:
        import libusb

        pkg = Path(libusb.__file__).resolve().parent
        # PyPI ``libusb`` ships the DLL under ``_platform/_windows/<arch>``;
        # older/local layouts may use ``_platform/windows/<arch>``.
        paths.append(pkg / "_platform" / "_windows" / arch / "libusb-1.0.dll")
        paths.append(pkg / "_platform" / "windows" / arch / "libusb-1.0.dll")
        paths.append(pkg / "_platform" / "_windows" / "libusb-1.0.dll")
        paths.append(pkg / "libusb-1.0.dll")
    except ImportError:
        pass
    # Beside the exe (manual drop-in)
    if getattr(sys, "frozen", False) or os.environ.get("NUITKA_ONEFILE_PARENT"):
        paths.append(Path(sys.executable).resolve().parent / "libusb-1.0.dll")
    # Meipass / onefile extract dir via __file__ of this module
    here = Path(__file__).resolve().parent
    paths.extend(
        [
            here.parent / "native" / "libusb" / "libusb-1.0.dll",
            here.parent / "native" / "libusb" / arch / "libusb-1.0.dll",
            here.parent / "libusb" / "libusb-1.0.dll",
        ]
    )

    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def list_found_dlls() -> list[Path]:
    return [p for p in _candidate_dll_paths() if p.is_file()]


def get_libusb_backend() -> Any:
    import usb.backend.libusb1

    tried: list[str] = []
    for dll in _candidate_dll_paths():
        tried.append(str(dll))
        if not dll.is_file():
            continue
        # Preload so PyUSB's find_library does not depend on PATH.
        try:
            ctypes.CDLL(str(dll))
        except OSError:
            continue
        backend = usb.backend.libusb1.get_backend(find_library=lambda _name, p=str(dll): p)
        if backend is not None:
            return backend

    backend = usb.backend.libusb1.get_backend()
    if backend is not None:
        return backend

    found = list_found_dlls()
    detail = (
        f"已找到 DLL 但无法加载: {', '.join(str(p) for p in found)}"
        if found
        else "打包内未找到 libusb-1.0.dll"
    )
    raise RuntimeError(
        "PyUSB 无可用 libusb 后端（No backend available）。"
        f"{detail}。"
        "请确认使用最新 NEBUMTK 构建，"
        "设备驱动为 WinUSB/libusbK（可用 Zadig），"
        "或将 libusb-1.0.dll 放在 exe 同目录。"
    )
# provenance: NBMTK-canary-7f3a91c4e8b2d05f
