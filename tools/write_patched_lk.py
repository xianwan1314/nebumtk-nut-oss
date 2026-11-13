# SPDX-FileCopyrightText: 2026 雾月星辰 & MLXC <github@XingChenRS>
# SPDX-License-Identifier: AGPL-3.0-only
"""Write a prepared patched LK into the inactive slot, using an existing root.

This is the device-side companion to ``prepare_xingc_lk.py``. It is a thin
wrapper around the same flow the GUI's 「准备解锁」 button runs, so the safety
checks are the product's own:

* one exact ADB device, exact serial, ``device`` state;
* profile match plus the Vivo ``xingc`` capability;
* an already-working ``su`` (temporary root is fine, but it must answer more
  than once);
* the selected LK must pass the product's structural + key-blob validation;
* target ``lk`` and ``misc`` capacities are read live and compared before any
  write;
* the flow re-validates serial, mode, active slot, capacities, remote file
  sizes and SHA-256 at the final boundary, then writes the inactive ``lk`` and
  the directional ``misc`` with ``dd`` and reboots to bootloader Fastboot.

Without ``--write`` it only inspects and prints; nothing is written. With
``--write`` it requires ``--yes`` and writes exactly like the GUI.

    python tools\\write_patched_lk.py --lk imags\\lk_xingc.img --check
    python tools\\write_patched_lk.py --lk imags\\lk_xingc.img --serial SERIAL --write --yes
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nebumtk_unlock_tools import keymaterial
from nebumtk_unlock_tools.config.store import SettingsStore
from nebumtk_unlock_tools.core.adb import AdbClient
from nebumtk_unlock_tools.core.device import DeviceDetector
from nebumtk_unlock_tools.core.env import EnvChecker
from nebumtk_unlock_tools.core.errors import VerificationError
from nebumtk_unlock_tools.core.partition import PartitionOps
from nebumtk_unlock_tools.core.safety import SafetyChecker
from nebumtk_unlock_tools.core.slot import SlotManager
from nebumtk_unlock_tools.core.su import probe_custom_su
from nebumtk_unlock_tools.flows.base import FlowCallbacks
from nebumtk_unlock_tools.flows.stages import WriteInactiveFlow
from nebumtk_unlock_tools.lk.ubl_service import InternalUblPatcher
from nebumtk_unlock_tools.resources.loader import ResourceLoader


class WriteError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pick_serial(adb: AdbClient, requested: str) -> str:
    devices = adb.devices()
    if not devices:
        raise WriteError("no ADB device found; connect the device and enable ADB")
    if requested:
        if requested not in devices:
            raise WriteError(f"requested serial {requested!r} is not in {devices}")
        if len(devices) != 1:
            raise WriteError(
                "exactly one ADB device is required at the final boundary; "
                f"currently {devices}"
            )
        return requested
    if len(devices) != 1:
        raise WriteError(
            f"multiple ADB devices ({devices}); pass --serial explicitly"
        )
    return devices[0]


def _probe_root(adb: AdbClient, profile) -> str:
    candidates: list[str] = []
    for candidate in (str(getattr(profile, "su_path", "")).strip(), "su"):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        try:
            first = probe_custom_su(adb, candidate)
            second = probe_custom_su(adb, candidate)
        except Exception:
            continue
        if first.uid == 0 and second.uid == 0:
            adb.su_path = candidate
            return candidate
    raise WriteError(
        "no working root provider. Check `adb shell su -c id` (it must return "
        "uid=0 twice). A one-shot root will not survive this flow."
    )


def _print_plan(
    serial: str,
    device,
    profile,
    slot,
    target_block: str,
    lk: Path,
    lk_capacity: int,
    misc: Path,
    misc_capacity: int,
    su_path: str,
) -> None:
    print(f"serial      : {serial}")
    print(f"device      : {device.brand} / {device.model} / {device.device}")
    print(f"firmware    : {device.firmware or 'unknown'}")
    print(f"profile     : {profile.key} ({profile.display_name})")
    print(f"root        : {su_path}")
    print(f"active slot : {slot.active_suffix}  -> inactive {slot.inactive_suffix}")
    print(f"target      : {target_block}")
    print(f"lk image    : {lk}  {lk.stat().st_size:,} bytes  sha256={_sha256(lk)}")
    print(f"lk capacity : {lk_capacity:,} bytes")
    print(f"misc image  : {misc}  {misc.stat().st_size:,} bytes")
    print(f"misc cap    : {misc_capacity:,} bytes")


def _check(args: argparse.Namespace) -> int:
    project_root = ROOT
    workspace = project_root / "workspace"
    settings = SettingsStore().load()
    tools = EnvChecker(settings).validate(need_adb=True, need_fastboot=False)

    probe_adb = AdbClient(tools.adb, serial="")
    serial = _pick_serial(probe_adb, args.serial)
    adb = AdbClient(tools.adb, serial=serial)
    state = adb.get_state()
    if state != "device":
        raise WriteError(f"ADB state for {serial} is {state!r}, expected 'device'")

    detector = DeviceDetector(project_root, adb)
    device = detector.read_connected(serial)
    if args.profile:
        profile = detector.get_profile_by_key(args.profile)
        SafetyChecker.check_device_profile(device, profile)
    else:
        profile = detector.match_profile(device)
    if str(profile.brand).lower() != "vivo":
        raise WriteError(f"profile brand is {profile.brand!r}, expected vivo")
    if not profile.has_capability("xingc"):
        raise WriteError(f"profile {profile.key} has no xingc capability")

    raw_slot = (device.slot_suffix or adb.getprop("ro.boot.slot_suffix")).strip()
    slot = SlotManager.from_suffix(raw_slot)

    lk = Path(args.lk).expanduser().resolve()
    if not lk.is_file() or lk.stat().st_size <= 0:
        raise WriteError(f"patched LK is missing or empty: {lk}")

    su_path = _probe_root(adb, profile)
    keymaterial.set_project_root(project_root)
    InternalUblPatcher(image_profile=profile.xingc_patch_profile).validate_patched(lk)

    workspace.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="write-preflight-", dir=workspace) as tmp:
        ops = PartitionOps(adb, profile, Path(tmp), log=lambda _m: None)
        target_block = ops.block_name("lk", slot.inactive_suffix)
        ops.preflight_block_exists(target_block)
        lk_capacity = ops.block_size(target_block)
        SafetyChecker.check_patched_lk_size_for_dd(lk.stat().st_size, lk_capacity)
        misc = ResourceLoader(project_root).resolve_misc(
            profile.brand, slot.active_suffix
        )
        misc_size = misc.stat().st_size
        misc_capacity = ops.block_size("misc")
        if misc_size > misc_capacity:
            raise WriteError(
                f"misc image {misc_size} exceeds misc capacity {misc_capacity}"
            )

    _print_plan(
        serial,
        device,
        profile,
        slot,
        target_block,
        lk,
        lk_capacity,
        misc,
        misc_capacity,
        su_path,
    )
    print()
    print("check ok: root, profile, slot, LK structure and capacities all pass")
    if not args.write:
        print("no device write performed (pass --write --yes to run 准备解锁)")
        return 0

    if not args.yes:
        raise WriteError("--write requires --yes (this writes the inactive lk and misc)")
    if adb.devices() != [serial]:
        raise WriteError("ADB device set changed before the write")
    if adb.get_state() != "device":
        raise WriteError("ADB state changed before the write")
    if adb.getprop("ro.boot.slot_suffix").strip() != slot.active_suffix:
        raise WriteError("active slot changed before the write")

    print("starting 准备解锁: writing inactive lk + misc, then rebooting to Fastboot")
    print("do not unplug the device and do not interrupt the dd step")
    print("this flow does not back up the current lk; keep the stock lk image available")
    callbacks = FlowCallbacks(
        log=lambda message: print(f"    {message}"),
        progress=lambda value, label: print(f"[{value:3d}%] {label}"),
    )
    flow = WriteInactiveFlow(project_root, workspace, settings, lk, callbacks=callbacks)
    flow.run(device, profile)
    print()
    print("prepare unlock completed; the device should now be in bootloader Fastboot")
    print("next: run the Xingc unlock step against that exact serial")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or write a prepared patched LK into the inactive slot.",
    )
    parser.add_argument("--lk", required=True, help="patched LK image")
    parser.add_argument("--serial", default="", help="exact ADB serial")
    parser.add_argument("--profile", default="", help="force a catalog profile key")
    parser.add_argument("--check", action="store_true", help="check only (default)")
    parser.add_argument("--write", action="store_true", help="write inactive lk + misc")
    parser.add_argument("--yes", action="store_true", help="required with --write")
    args = parser.parse_args()
    if args.check:
        args.write = False
    try:
        return _check(args)
    except WriteError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except VerificationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
# provenance: NBMTK-canary-7f3a91c4e8b2d05f
