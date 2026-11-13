# SPDX-FileCopyrightText: 2026 雾月星辰 & MLXC <github@XingChenRS>
# SPDX-License-Identifier: AGPL-3.0-only
"""Entry point.

`--license` and `--version` exist so the provenance marks are part of the
product's observable behaviour, not only of its source: a repackaged build that
strips them stops being able to answer.

Provenance token: NBMTK-canary-7f3a91c4e8b2d05f

"""

from __future__ import annotations

import sys


def _print_license() -> int:
    import nebumtk_unlock_tools as nut

    print(nut.license_notice())
    print()
    print(f"version: {nut.__version__}")
    print(f"upstream: {nut.__upstream__}")
    print(f"license: {nut.__license__}")
    return 0


def _self_check() -> int:
    """Verify the shipped signatures/material without starting the GUI."""

    from pathlib import Path

    import nebumtk_unlock_tools as nut
    from nebumtk_unlock_tools import keymaterial
    from nebumtk_unlock_tools.config.store import Settings
    from nebumtk_unlock_tools.core.env import EnvChecker
    from nebumtk_unlock_tools.lk.keys import verify_key_pair
    from nebumtk_unlock_tools.lk.ubl_service import InternalUblPatcher
    from nebumtk_unlock_tools.lk.xingc_service import XingcUnlockService
    from nebumtk_unlock_tools.resources.catalog import load_embedded_device_catalog

    if getattr(sys, "frozen", False):
        root = Path(sys.executable).resolve().parent
    else:
        root = Path(__file__).resolve().parent.parent
    keymaterial.set_project_root(root)

    checks: list[tuple[bool, str, str]] = []

    def run(name: str, check) -> None:
        try:
            detail = check()
        except Exception as exc:  # noqa: BLE001 - diagnostic boundary
            checks.append((False, name, f"{type(exc).__name__}: {exc}"))
        else:
            checks.append((True, name, str(detail or "")))

    def catalog() -> str:
        data = load_embedded_device_catalog()
        return f"{len(data.get('devices', {}))} devices, revision {data.get('revision')}"

    def keys() -> str:
        verify_key_pair()
        return "RSA private/public pair matches"

    def patched_lk() -> str:
        candidates = (
            root / "images" / "lk_xingc.img",
            root / "imags" / "lk_xingc.img",
        )
        path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if path is None:
            raise FileNotFoundError("lk_xingc.img not found")
        InternalUblPatcher().validate_patched(path)
        return str(path)

    def xingc() -> str:
        XingcUnlockService.readiness_check()
        return "protocol permit and libusb backend"

    def platform_tools() -> str:
        resolved = EnvChecker(
            Settings(platform_tools_path=str(root / "platform-tools"))
        ).validate(need_fastboot=True)
        return f"{Path(resolved.adb).name}, {Path(resolved.fastboot).name}"

    def misc() -> str:
        expected = (
            "misc_vivo_common/a_to_b_bootloader.img",
            "misc_vivo_common/b_to_a_bootloader.img",
            "misc_oppo_common/a_to_b_bootloader.img",
            "misc_oppo_common/b_to_a_bootloader.img",
            "misc_honor_common/a_to_b_bootloader.img",
            "misc_honor_common/b_to_a_bootloader.img",
        )
        missing = [name for name in expected if not (root / "misc" / name).is_file()]
        if missing:
            raise FileNotFoundError("missing misc images: " + ", ".join(missing))
        return f"{len(expected)} images"

    run("catalog signature", catalog)
    run("Xingc key pair", keys)
    run("patched LK", patched_lk)
    run("Xingc readiness", xingc)
    run("platform-tools", platform_tools)
    run("misc images", misc)

    ok = all(passed for passed, _, _ in checks)
    lines = [
        "NEBUMTK self-check",
        f"version: {nut.__version__}",
        f"root: {root}",
    ]
    for passed, name, detail in checks:
        lines.append(f"[{'ok' if passed else 'FAIL'}] {name}: {detail}")
    lines.append(f"result: {'OK' if ok else 'FAIL'}")

    text = "\n".join(lines) + "\n"
    try:
        (root / "self-check.txt").write_text(text, encoding="utf-8")
    except OSError:
        pass
    print(text, end="")
    return 0 if ok else 2


def main() -> int:
    args = [argument.strip().lower() for argument in sys.argv[1:]]
    if args and args[0] in {"--license", "-l", "--licence"}:
        return _print_license()
    if args and args[0] in {"--version", "-v"}:
        import nebumtk_unlock_tools as nut

        print(f"{nut.__version__} ({nut.__license__})")
        return 0
    if args and args[0] in {"--self-check", "--selfcheck"}:
        return _self_check()
    from nebumtk_unlock_tools.app import main as run_app

    return run_app()


if __name__ == "__main__":
    raise SystemExit(main())
# provenance: NBMTK-canary-7f3a91c4e8b2d05f
