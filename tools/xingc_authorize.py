# SPDX-FileCopyrightText: 2026 雾月星辰 & MLXC <github@XingChenRS>
# SPDX-License-Identifier: AGPL-3.0-only
"""Standalone Xingc authorization CLI for a patched LK.

The device side is the renamed ``veify`` command (now ``xingc``) and the
operator's AVB public key embedded in the LK. This tool is the host side:
read the device token, sign it with the matching RSA-2048 private key, send the
payload and issue ``xingc unlock`` / ``xingc lock``.

It does not patch LK, does not provide root and does not contain any payload.
Only use it on a device you own or are explicitly authorized to service.

Examples:
    python tools\\xingc_authorize.py --check
    python tools\\xingc_authorize.py --serial SERIAL --token
    python tools\\xingc_authorize.py --serial SERIAL --sign TOKEN
    python tools\\xingc_authorize.py --serial SERIAL --unlock
    python tools\\xingc_authorize.py --serial SERIAL --lock
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nebumtk_unlock_tools import keymaterial
from nebumtk_unlock_tools.config.store import SettingsStore
from nebumtk_unlock_tools.core.env import EnvChecker
from nebumtk_unlock_tools.core.errors import VerificationError
from nebumtk_unlock_tools.core.fastboot import FastbootClient
from nebumtk_unlock_tools.lk.xingc_service import XingcUnlockService


def _require_serial(value: str, action: str) -> str:
    serial = value.strip()
    if not serial:
        raise VerificationError(f"{action} 需要 --serial")
    return serial


def _fastboot(serial: str) -> FastbootClient:
    settings = SettingsStore().load()
    tools = EnvChecker(settings).validate(need_fastboot=True)
    return FastbootClient(tools.fastboot, serial=serial, log=print)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Authorize a patched vivo/iQOO Xingc bootloader.",
    )
    parser.add_argument("--serial", default="", help="exact Fastboot serial")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="check key material and libusb only")
    action.add_argument("--token", action="store_true", help="read the device unlock token")
    action.add_argument("--sign", metavar="TOKEN", help="sign a token and print the payload")
    action.add_argument("--unlock", action="store_true", help="full token + sign + upload + xingc unlock")
    action.add_argument("--lock", action="store_true", help="send xingc lock")
    args = parser.parse_args()

    keymaterial.set_project_root(ROOT)
    service = XingcUnlockService(log=lambda message: print(f"    {message}"))

    try:
        if args.check:
            service.readiness_check()
            print("check ok: material key pair, protocol permit and libusb backend")
            return 0

        if args.sign:
            serial = args.serial.strip()
            payload = service.build_payload(args.sign, target=serial)
            print(payload.decode("ascii"))
            return 0

        if args.token:
            print(service.read_token(_require_serial(args.serial, "--token")))
            return 0

        if args.unlock:
            serial = _require_serial(args.serial, "--unlock")
            service.unlock(serial, _fastboot(serial))
            print("xingc unlock: OKAY")
            return 0

        if args.lock:
            serial = _require_serial(args.serial, "--lock")
            service.lock(serial, _fastboot(serial))
            print("xingc lock: OKAY")
            return 0

        raise VerificationError("no action selected")
    except VerificationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
# provenance: NBMTK-canary-7f3a91c4e8b2d05f
