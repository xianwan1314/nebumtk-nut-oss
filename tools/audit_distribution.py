# SPDX-FileCopyrightText: 2026 雾月星辰 & MLXC <github@XingChenRS>
# SPDX-License-Identifier: AGPL-3.0-only
"""Fail a release build if protected sources or release files are invalid."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nebumtk_unlock_tools.core.platform_tools import BUILTIN_PLATFORM_TOOL_SHA256

FORBIDDEN_NAMES = {
    "verify_private.pem",
    "verify_public.avbpub",
    "xingc_vivo.json",
    "preload.so",
    "ksu.apk",
    "sukisu.apk",
    "libksud.so",
    "ksud.exe",
    "scrcpy.exe",
    "scrcpy-server",
    "devices.yaml",
}
FORBIDDEN_MARKERS = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
)
TEXT_EXTENSIONS = {".pem", ".key", ".txt", ".json", ".py", ".yaml", ".yml", ".ini", ".cfg"}
FORBIDDEN_RELEASE_SUFFIXES = {".vmp", ".map", ".pdb"}
FORBIDDEN_ROOT_ARTIFACT_SUFFIXES = {".nbk", ".ko"}
# Persistent Root *managers* stay out of the release. The reviewed SakiSU host
# patcher is an approved product asset — it ships plainly under `sakisu/` and is
# hash-pinned at load time — so that directory name is not forbidden.
FORBIDDEN_ROOT_PATH_PARTS = {"ksu", "kernelsu", "sukisu", "magisk", "resukisu"}
FORBIDDEN_REMOVED_COMPONENT_PATH_PARTS = {"scrcpy"}
SOURCE_NAMES = ("verify_private.pem", "verify_public.avbpub", "xingc_vivo.json")
MANIFEST_NAME = "release-sha256-manifest.json"
# Compiled from nebumtk_unlock_tools.PROVENANCE_MARK; a build that strips the
# notice stops carrying it, and that is a licence violation rather than a taste
# preference (AGPL 5 requires notices to survive conveyance).
PROVENANCE_MARKER = b"NEBUMTK-PROVENANCE::AGPL-3.0-only"
MAIN_EXECUTABLE = "NEBUMTK_UNLOCK_TOOLS.exe"
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
BASE_REQUIRED_RELEASE_FILES = (
    "NEBUMTK_UNLOCK_TOOLS.exe",
    # AGPL 4/6: a conveyed binary ships with its licence and notices.
    "LICENSE",
    "COPYRIGHT",
    "NOTICE.md",
    "platform-tools/adb.exe",
    "platform-tools/fastboot.exe",
    "nebumtk_unlock_tools/lk/libusb/_platform/_windows/x64/libusb-1.0.dll",
)
REQUIRED_RELEASE_FILES = BASE_REQUIRED_RELEASE_FILES


def _manifest_relpath(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        stat_result = path.lstat()
    except OSError:
        return True
    return path.is_symlink() or bool(
        getattr(stat_result, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    )


def _reparse_violations(root: Path) -> list[str]:
    violations: list[str] = []
    if _is_reparse_or_symlink(root):
        violations.append(f"distribution root is a reparse point or symlink: {root}")
        return violations
    if root.is_dir():
        for path in root.rglob("*"):
            if _is_reparse_or_symlink(path):
                violations.append(f"distribution contains a reparse point or symlink: {path}")
    return violations


def _protected_source_bytes() -> tuple[bytes, ...]:
    project_root = Path(__file__).resolve().parent.parent
    source_dir = project_root / "xingc_sources"
    protected = [
        data
        for name in SOURCE_NAMES
        if (path := source_dir / name).is_file()
        if (data := path.read_bytes())
    ]
    preload_dir = project_root / "preloads"
    if preload_dir.is_dir():
        for path in sorted(preload_dir.rglob("*")):
            if path.is_file() and (data := path.read_bytes()):
                protected.append(data)
    device_catalog_source = project_root / "devices.yaml"
    if device_catalog_source.is_file():
        protected.append(device_catalog_source.read_bytes())
    fastboot_source_dir = project_root / "lk_sources"
    if fastboot_source_dir.is_dir():
        for path in sorted(fastboot_source_dir.rglob("*")):
            if path.is_file() and (data := path.read_bytes()):
                protected.append(data)
    return tuple(protected)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_release_manifest(root: Path) -> Path:
    if root.is_file():
        raise ValueError(f"release manifest requires a distribution directory: {root}")
    reparse = _reparse_violations(root)
    if reparse:
        raise ValueError("cannot inventory unsafe distribution:\n" + "\n".join(reparse))
    manifest_path = root / MANIFEST_NAME
    entries = []
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: _manifest_relpath(item, root)):
        if path == manifest_path:
            continue
        entries.append(
            {
                "path": _manifest_relpath(path, root),
                "size": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    manifest = {"version": 1, "files": entries}
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest_path


def _release_channel(root: Path) -> str:
    marker = root / "release-channel.txt"
    try:
        channel = marker.read_text(encoding="utf-8").strip().casefold()
    except OSError:
        return "offline"
    return channel if channel == "offline" else "offline"


def _manifest_required_paths(*, online: bool = False) -> set[str]:
    del online
    return set(BASE_REQUIRED_RELEASE_FILES)


def _audit_release_manifest(
    root: Path,
    violations: list[str],
    *,
    online: bool = False,
) -> None:
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        violations.append(f"missing release manifest: {manifest_path}")
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        violations.append(f"invalid release manifest: {manifest_path}: {exc}")
        return
    files = manifest.get("files")
    if not isinstance(files, list):
        violations.append(f"invalid release manifest files list: {manifest_path}")
        return
    by_path: dict[str, dict[str, object]] = {}
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            violations.append(f"invalid release manifest entry: {manifest_path}")
            continue
        raw_path = str(entry["path"])
        relative = Path(raw_path)
        normalized = relative.as_posix()
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or normalized != raw_path.replace("\\", "/")
        ):
            violations.append(f"unsafe release manifest path: {raw_path}")
            continue
        if normalized in by_path:
            violations.append(f"duplicate release manifest path: {normalized}")
            continue
        by_path[normalized] = entry
        path = root / relative
        if not path.is_file():
            violations.append(f"release manifest listed missing file: {normalized}")
            continue
        actual_size = path.stat().st_size
        if (
            not isinstance(entry.get("size"), int)
            or isinstance(entry.get("size"), bool)
            or entry.get("size") != actual_size
        ):
            violations.append(f"release manifest size mismatch: {normalized}")
        expected_hash = entry.get("sha256")
        if (
            not isinstance(expected_hash, str)
            or expected_hash != _sha256_file(path)
        ):
            violations.append(f"release manifest hash mismatch: {normalized}")
    actual_paths = {
        _manifest_relpath(path, root)
        for path in root.rglob("*")
        if path.is_file() and path != manifest_path
    }
    for unlisted in sorted(actual_paths - set(by_path)):
        violations.append(f"unlisted release file: {unlisted}")
    for required in sorted(_manifest_required_paths(online=online)):
        entry = by_path.get(required)
        if entry is None:
            violations.append(f"release manifest missing required file: {required}")


def _audit_platform_tools(root: Path, violations: list[str]) -> None:
    directory = root / "platform-tools"
    for name, expected in BUILTIN_PLATFORM_TOOL_SHA256.items():
        path = directory / name
        if not path.is_file():
            violations.append(f"missing pinned platform tool: {path}")
        elif _sha256_file(path) != expected:
            violations.append(f"pinned platform tool hash mismatch: {name}")


def audit_distribution(root: Path) -> None:
    if not root.exists():
        raise FileNotFoundError(f"distribution path not found: {root}")
    violations: list[str] = _reparse_violations(root)
    if violations:
        raise RuntimeError("distribution protection audit failed:\n" + "\n".join(violations))
    protected_sources = _protected_source_bytes()
    files = [root] if root.is_file() else [path for path in root.rglob("*") if path.is_file()]
    for path in files:
        lowered_name = path.name.casefold()
        if (
            path.suffix.casefold() in FORBIDDEN_RELEASE_SUFFIXES
            or lowered_name.endswith(".vmp.exe")
        ):
            violations.append(f"forbidden VMP intermediate or symbol file: {path}")
            continue
        relative_parts = {
            part.casefold()
            for part in (path.relative_to(root).parts if not root.is_file() else path.parts)
        }
        if (
            path.suffix.casefold() in FORBIDDEN_ROOT_ARTIFACT_SUFFIXES
            or relative_parts.intersection(FORBIDDEN_ROOT_PATH_PARTS)
        ):
            violations.append(f"forbidden persistent Root artifact: {path}")
            continue
        if relative_parts.intersection(FORBIDDEN_REMOVED_COMPONENT_PATH_PARTS):
            violations.append(f"forbidden removed component: {path}")
            continue
        if path.name.lower() in FORBIDDEN_NAMES:
            violations.append(f"forbidden file: {path}")
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if path.suffix.lower() in TEXT_EXTENSIONS and any(marker in data for marker in FORBIDDEN_MARKERS):
            violations.append(f"private-key marker: {path}")
        if any(source in data for source in protected_sources):
            violations.append(f"plaintext protected source: {path}")
    if not root.is_file() and (root / MAIN_EXECUTABLE).is_file():
        try:
            executable = (root / MAIN_EXECUTABLE).read_bytes()
        except OSError:
            executable = b""
        if PROVENANCE_MARKER not in executable:
            violations.append(
                "built executable lost the project provenance mark "
                f"({PROVENANCE_MARKER.decode('ascii')}); AGPL 5 forbids stripping notices"
            )
    if not root.is_file() and ((root / MANIFEST_NAME).is_file() or any(root.glob("*.exe"))):
        _audit_platform_tools(root, violations)
        _audit_release_manifest(root, violations)
    if violations:
        raise RuntimeError("distribution protection audit failed:\n" + "\n".join(violations))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--write-manifest", action="store_true", help="write release-sha256-manifest.json before auditing")
    args = parser.parse_args()
    if args.write_manifest:
        write_release_manifest(args.path)
    audit_distribution(args.path)


if __name__ == "__main__":
    main()
# provenance: NBMTK-canary-7f3a91c4e8b2d05f
