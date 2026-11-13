# SPDX-FileCopyrightText: 2026 雾月星辰 & MLXC <github@XingChenRS>
# SPDX-License-Identifier: AGPL-3.0-only
"""Prepare a Vivo Xingc-patched LK from a stock LK image, offline.

This tool is the missing local half of the LK workflow: the application can
patch an LK once ``material/xingc`` exists, but it deliberately does not invent
the material. This tool inspects one stock LK, derives the three Xingc patch
sites from the image itself, writes a reviewable patch catalog, creates (or
reuses) an RSA-2048 key pair, patches the image and validates the result.

It never connects to a device and never writes a partition.

    python tools\\prepare_xingc_lk.py --lk imags\\lk.img
    python tools\\prepare_xingc_lk.py --lk imags\\lk.img --out imags\\lk_xingc.img
    python tools\\prepare_xingc_lk.py --lk imags\\lk.img --dry-run
    python tools\\prepare_xingc_lk.py --lk imags\\lk.img --catalog path\\to\\patch-catalog.json

The auto-detection is intentionally strict: it only accepts an LK whose ``lk``
partition contains exactly one known command context and exactly one known log
marker. Anything else is refused instead of guessed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from nebumtk_unlock_tools.lk.avbkey import (
    AVB_RSA2048_PUBLIC_KEY_BLOB_SIZE,
    AvbPublicKeyBlob,
    public_key_to_avb_blob,
)
from nebumtk_unlock_tools.lk.ubl.catalog import (
    LocatorRecord,
    PatchCatalog,
    PatchRecord,
)
from nebumtk_unlock_tools.lk.ubl.lk_image import LkImage
from nebumtk_unlock_tools.lk.ubl.workflow import run_patch, verify_files
from nebumtk_unlock_tools.lk.ubl.xingc_locator import locate_patch

PROTOCOL_PERMIT = b"NEBUMTK-XINGC-PROTOCOL-PERMIT-v1\n"
PRIVATE_KEY_NAME = "verify-private.pem"
PUBLIC_BLOB_NAME = "verify-public.avbpub"
CATALOG_NAME = "patch-catalog.json"

# Stock LK stores the fastboot verify command with a typo ("veify"). Two known
# table layouts put it in different contexts; both are accepted, but only one
# may match in a given image.
COMMAND_LAYOUTS: tuple[tuple[str, bytes], ...] = (
    ("unlock2", b"\x0a\x00veify\x00"),
    ("non_unlock2", b"oem reboot-recovery\x00veify\x00set slot b failed!\x00"),
)
LOG_NEEDLE = b"[FASTBOOT]%s veify start"
HEDGEHOG_PROFILE = "hedgehog"
KEY_REPLACE_LEN = AVB_RSA2048_PUBLIC_KEY_BLOB_SIZE


class PrepareError(RuntimeError):
    pass


@dataclass(frozen=True)
class Site:
    patch_id: str
    kind: str
    offset: int
    needle: bytes
    replace_at: int
    replace_len: int
    layout: str = ""


@dataclass(frozen=True)
class Detection:
    layout: str
    command: Site
    log: Site
    key_offset: int
    key_blob: bytes


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _find_unique(haystack: bytes, needle: bytes, label: str) -> int:
    first = haystack.find(needle)
    if first < 0:
        raise PrepareError(f"{label}: needle not found in the lk partition")
    second = haystack.find(needle, first + 1)
    if second >= 0:
        raise PrepareError(
            f"{label}: needle is ambiguous (0x{first:x}, 0x{second:x}); "
            "refusing to guess which site to patch"
        )
    return first


def _detect_command(image: LkImage) -> tuple[str, Site]:
    start, end = image.partition("lk").payload_range
    payload = image.data[start:end]
    matches: list[tuple[str, int, bytes, int]] = []
    for layout, needle in COMMAND_LAYOUTS:
        count = payload.count(needle)
        if count == 0:
            continue
        if count > 1:
            raise PrepareError(
                f"command layout {layout}: needle is ambiguous "
                f"({count} matches); refusing to guess"
            )
        index = payload.find(needle)
        replace_at = needle.index(b"veify")
        if payload[index + replace_at : index + replace_at + 5] != b"veify":
            raise PrepareError(f"command layout {layout}: veify offset drifted")
        matches.append((layout, start + index + replace_at, needle, replace_at))
    if not matches:
        raise PrepareError(
            "no known Xingc command context found; this LK is not a supported "
            "Vivo 'veify' layout"
        )
    if len(matches) > 1:
        layouts = ", ".join(item[0] for item in matches)
        raise PrepareError(f"multiple command layouts matched: {layouts}")
    layout, offset, needle, replace_at = matches[0]
    return layout, Site(
        patch_id="xingc-rename-veify-command",
        kind="signature",
        offset=offset,
        needle=needle,
        replace_at=replace_at,
        replace_len=5,
        layout=layout,
    )


def _detect_log(image: LkImage, command_offset: int) -> Site:
    start, end = image.partition("lk").payload_range
    payload = image.data[start:end]
    index = _find_unique(payload, LOG_NEEDLE, "log marker")
    offset = start + index + LOG_NEEDLE.index(b"veify")
    if offset == command_offset:
        raise PrepareError("log marker resolved to the command site")
    return Site(
        patch_id="xingc-rename-veify-log-marker",
        kind="signature",
        offset=offset,
        needle=LOG_NEEDLE,
        replace_at=LOG_NEEDLE.index(b"veify"),
        replace_len=5,
        layout="log",
    )


def _probe_key(image: LkImage) -> tuple[int, bytes]:
    probe = PatchRecord(
        id="xingc-replace-hedgehog-public-key",
        human_name="probe",
        category="probe",
        risk="probe",
        summary="probe",
        principle="probe",
        ida_context="probe",
        locators=(
            LocatorRecord(
                kind="seed_auth_profile",
                partition="lk",
                profile=HEDGEHOG_PROFILE,
                length=KEY_REPLACE_LEN,
            ),
        ),
        dynamic="hedgehog_public_key_blob",
        replace_len=KEY_REPLACE_LEN,
    )
    status = locate_patch(
        image.data,
        probe,
        replacement=None,
        search_ranges=image.partition_search_ranges(),
    )
    if status.offset is None:
        raise PrepareError(
            f"seed_auth profile {HEDGEHOG_PROFILE!r} not found "
            f"({status.state}: {status.detail})"
        )
    blob = image.data[status.offset : status.offset + KEY_REPLACE_LEN]
    if len(blob) != KEY_REPLACE_LEN:
        raise PrepareError("hedgehog key blob is truncated")
    try:
        AvbPublicKeyBlob.from_bytes(blob)
    except ValueError as exc:
        raise PrepareError(f"hedgehog key blob is not an AVB RSA-2048 key: {exc}") from exc
    return status.offset, blob


def detect(image: LkImage) -> Detection:
    layout, command = _detect_command(image)
    log = _detect_log(image, command.offset)
    key_offset, key_blob = _probe_key(image)
    return Detection(
        layout=layout,
        command=command,
        log=log,
        key_offset=key_offset,
        key_blob=key_blob,
    )


def _record_from_site(site: Site) -> PatchRecord:
    return PatchRecord(
        id=site.patch_id,
        human_name=site.patch_id,
        category="xingc",
        risk="high",
        summary="Detected from the stock LK image by prepare_xingc_lk.",
        principle="Rename the stock veify site to xingc (same length).",
        ida_context=f"auto-detected; layout={site.layout}",
        locators=(
            LocatorRecord(
                kind="signature",
                needle=site.needle,
                replace_at=site.replace_at,
                replace_len=site.replace_len,
                partition="lk",
                layout=site.layout,
            ),
        ),
        patch_hex=b"xingc".hex(),
        replace_len=site.replace_len,
    )


def build_catalog(detection: Detection) -> PatchCatalog:
    key_patch = PatchRecord(
        id="xingc-replace-hedgehog-public-key",
        human_name="xingc-replace-hedgehog-public-key",
        category="xingc",
        risk="high",
        summary="Replace the hedgehog seed_auth public key with the operator key.",
        principle="The device then verifies the XinGC token with the operator key.",
        ida_context=f"auto-detected; layout={detection.layout}",
        locators=(
            LocatorRecord(
                kind="seed_auth_profile",
                partition="lk",
                profile=HEDGEHOG_PROFILE,
                length=KEY_REPLACE_LEN,
                original_sha256=_sha256(detection.key_blob),
            ),
        ),
        dynamic="hedgehog_public_key_blob",
        replace_len=KEY_REPLACE_LEN,
    )
    return PatchCatalog(
        {
            "xingc-rename-veify-command": _record_from_site(detection.command),
            "xingc-rename-veify-log-marker": _record_from_site(detection.log),
            "xingc-replace-hedgehog-public-key": key_patch,
        }
    )


def catalog_to_json(catalog: PatchCatalog) -> dict:
    records = []
    for patch in catalog.patches:
        record: dict[str, object] = {
            "id": patch.id,
            "human_name": patch.human_name,
            "category": patch.category,
            "risk": patch.risk,
            "summary": patch.summary,
            "principle": patch.principle,
            "ida_context": patch.ida_context,
            "locators": [],
        }
        for locator in patch.locators:
            item: dict[str, object] = {"kind": locator.kind}
            if locator.needle is not None:
                item["needle_hex"] = locator.needle.hex()
            if locator.replace_at:
                item["replace_at"] = locator.replace_at
            if locator.replace_len is not None:
                item["replace_len"] = locator.replace_len
            if locator.length is not None:
                item["length"] = locator.length
            if locator.partition:
                item["partition"] = locator.partition
            if locator.profile:
                item["profile"] = locator.profile
            if locator.original_sha256:
                item["original_sha256"] = locator.original_sha256
            if locator.layout:
                item["layout"] = locator.layout
            record["locators"].append(item)  # type: ignore[union-attr]
        if patch.patch_hex is not None:
            record["patch_hex"] = patch.patch_hex
        if patch.dynamic is not None:
            record["dynamic"] = patch.dynamic
        record["replace_len"] = patch.replacement_size
        records.append(record)
    return {"patches": records}


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.pending-{uuid.uuid4().hex}")
    try:
        pending.write_bytes(data)
        if _sha256(pending.read_bytes()) != _sha256(data):
            raise PrepareError(f"write verification failed: {path}")
        pending.replace(path)
    except Exception:
        pending.unlink(missing_ok=True)
        raise


def _load_or_create_key(
    directory: Path,
    *,
    regenerate: bool,
) -> tuple[object, bytes]:
    private_path = directory / PRIVATE_KEY_NAME
    public_path = directory / PUBLIC_BLOB_NAME
    if private_path.is_file() and not regenerate:
        try:
            key = serialization.load_pem_private_key(
                private_path.read_bytes(),
                password=None,
            )
        except (ValueError, TypeError) as exc:
            raise PrepareError(f"cannot parse existing {private_path}: {exc}") from exc
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size != 2048:
            raise PrepareError(f"{private_path} is not an RSA-2048 private key")
        blob = public_key_to_avb_blob(key.public_key())
        if public_path.is_file() and public_path.read_bytes() != blob:
            raise PrepareError(
                f"{public_path} does not match {private_path}; move both aside "
                "or pass --regenerate-key"
            )
        if not public_path.is_file():
            _write_atomic(public_path, blob)
        return key, blob

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    blob = public_key_to_avb_blob(key.public_key())
    _write_atomic(private_path, pem)
    try:
        private_path.chmod(0o600)
    except OSError:
        pass
    _write_atomic(public_path, blob)
    return key, blob


def _prepare_material(
    directory: Path,
    catalog: PatchCatalog,
    *,
    regenerate_key: bool,
    overwrite_catalog: bool,
) -> tuple[Path, Path, bytes]:
    directory.mkdir(parents=True, exist_ok=True)
    catalog_path = directory / CATALOG_NAME
    if catalog_path.is_file() and not overwrite_catalog:
        existing = PatchCatalog.from_path(catalog_path)
        if catalog_to_json(existing) != catalog_to_json(catalog):
            raise PrepareError(
                f"{catalog_path} already exists and differs; "
                "pass --overwrite-catalog to replace it"
            )
    else:
        payload = (
            json.dumps(
                catalog_to_json(catalog),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        _write_atomic(catalog_path, payload)

    permit_path = directory / "protocol-permit"
    if permit_path.is_file() and permit_path.read_bytes() != PROTOCOL_PERMIT:
        raise PrepareError(f"{permit_path} exists with unexpected content")
    if not permit_path.is_file():
        _write_atomic(permit_path, PROTOCOL_PERMIT)

    _key, blob = _load_or_create_key(directory, regenerate=regenerate_key)
    return catalog_path, directory / PRIVATE_KEY_NAME, blob


def _resolve_output(source: Path, requested: Path | None) -> Path:
    if requested is not None:
        return requested
    return source.with_name(f"{source.stem}_xingc{source.suffix or '.img'}")


def _receipt(
    source: Path,
    source_data: bytes,
    output: Path,
    output_data: bytes,
    detection: Detection,
    catalog_path: Path,
    key_blob: bytes,
    checks: int,
) -> dict:
    return {
        "tool": "prepare_xingc_lk",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "input": {
            "path": str(source),
            "size": len(source_data),
            "sha256": _sha256(source_data),
        },
        "output": {
            "path": str(output),
            "size": len(output_data),
            "sha256": _sha256(output_data),
        },
        "detected": {
            "layout": detection.layout,
            "command_offset": detection.command.offset,
            "log_offset": detection.log.offset,
            "key_offset": detection.key_offset,
            "stock_key_sha256": _sha256(detection.key_blob),
        },
        "operator_key": {"avb_blob_sha256": _sha256(key_blob)},
        "catalog": {
            "path": str(catalog_path),
            "sha256": _sha256(catalog_path.read_bytes()),
        },
        "validation": {"ok": True, "checks": checks},
    }


def _check(args: argparse.Namespace) -> int:
    source = Path(args.lk).expanduser().resolve()
    if not source.is_file():
        raise PrepareError(f"LK image is missing: {source}")
    image = LkImage.from_path(source)
    material_dir = Path(args.key_dir).expanduser().resolve()
    catalog_path = material_dir / CATALOG_NAME
    private_path = material_dir / PRIVATE_KEY_NAME
    public_path = material_dir / PUBLIC_BLOB_NAME
    permit_path = material_dir / "protocol-permit"

    problems: list[str] = []
    for label, path in (
        ("catalog", catalog_path),
        ("private key", private_path),
        ("public blob", public_path),
        ("protocol permit", permit_path),
    ):
        print(f"[{'OK' if path.is_file() else 'MISSING'}] {label}: {path}")
        if not path.is_file():
            problems.append(label)

    if private_path.is_file() and public_path.is_file():
        try:
            key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
            if not isinstance(key, rsa.RSAPrivateKey) or key.key_size != 2048:
                raise PrepareError("private key is not RSA-2048")
            derived = public_key_to_avb_blob(key.public_key())
            if derived != public_path.read_bytes():
                raise PrepareError("private key and public blob do not match")
            print("[OK] key pair matches")
        except Exception as exc:
            print(f"[FAIL] key pair: {exc}")
            problems.append("key pair")

    catalog = None
    if catalog_path.is_file():
        try:
            catalog = PatchCatalog.from_path(catalog_path)
            for patch_id in PatchCatalog.XINGC_IDS:
                if patch_id not in {patch.id for patch in catalog.patches}:
                    raise PrepareError(f"catalog is missing {patch_id}")
            print("[OK] catalog contains all three Xingc patches")
            if permit_path.is_file():
                if permit_path.read_bytes() != PROTOCOL_PERMIT:
                    raise PrepareError("protocol-permit content is wrong")
                print("[OK] protocol permit content")
            from nebumtk_unlock_tools.lk.ubl.workflow import preflight_check

            blob = public_path.read_bytes() if public_path.is_file() else b""
            ok, detail = preflight_check(source, blob, catalog)
            print(f"[{'OK' if ok else 'FAIL'}] stock LK preflight: {detail}")
            if not ok:
                problems.append("preflight")
        except Exception as exc:
            print(f"[FAIL] catalog: {exc}")
            problems.append("catalog")

    output = _resolve_output(source, Path(args.out) if args.out else None).resolve()
    if output.is_file() and catalog is not None and public_path.is_file():
        try:
            ok, steps = verify_files(source, output, public_path.read_bytes(), catalog)
            print(f"[{'OK' if ok else 'FAIL'}] patched output: {output}")
            if not ok:
                for step in steps:
                    if step.startswith("[FAIL]"):
                        print(f"       {step}")
                problems.append("output")
        except Exception as exc:
            print(f"[FAIL] patched output: {exc}")
            problems.append("output")
    else:
        print(f"[SKIP] patched output: {output}")

    try:
        import usb.backend.libusb1  # noqa: F401

        print("[OK] pyusb/libusb available (needed for the Xingc USB step)")
    except Exception:
        print("[WARN] pyusb/libusb missing (needed for the Xingc USB step)")

    if problems:
        print("check failed: " + ", ".join(problems))
        return 2
    print("check ok")
    return 0


def run(args: argparse.Namespace) -> int:
    source = Path(args.lk).expanduser().resolve()
    if not source.is_file() or source.stat().st_size <= 0:
        raise PrepareError(f"LK image is missing or empty: {source}")
    if args.check:
        return _check(args)
    source_data = source.read_bytes()
    image = LkImage.from_path(source)
    if not any(part.name == "lk" for part in image.partitions):
        raise PrepareError("image has no 'lk' partition")

    detection = detect(image)
    catalog = (
        PatchCatalog.from_path(Path(args.catalog))
        if args.catalog
        else build_catalog(detection)
    )

    material_dir = Path(args.key_dir).expanduser().resolve()
    output = _resolve_output(source, Path(args.out) if args.out else None).resolve()
    if output == source:
        raise PrepareError("output must differ from the input LK")
    if output.exists() and not args.overwrite and not args.dry_run:
        raise PrepareError(f"output exists: {output}; pass --overwrite to replace it")

    print(f"LK      : {source}")
    print(f"size    : {len(source_data):,} bytes  sha256={_sha256(source_data)[:16]}")
    print(f"layout  : {detection.layout}")
    print(f"command : 0x{detection.command.offset:x}")
    print(f"log     : 0x{detection.log.offset:x}")
    print(f"key     : 0x{detection.key_offset:x}  stock_sha256={_sha256(detection.key_blob)[:16]}")
    print(f"output  : {output}")

    if args.dry_run:
        print("dry-run: no material, no output written")
        if args.json:
            print(json.dumps(catalog_to_json(catalog), ensure_ascii=False, indent=2))
        return 0

    catalog_path, _, key_blob = _prepare_material(
        material_dir,
        catalog,
        regenerate_key=args.regenerate_key,
        overwrite_catalog=args.overwrite_catalog,
    )

    pending = output.with_name(f".{output.name}.pending-{uuid.uuid4().hex}")
    try:
        input_size, output_size, signed, steps = run_patch(
            source,
            pending,
            key_blob,
            catalog,
        )
        if "lk" not in signed:
            raise PrepareError("patcher did not re-sign the lk partition")
        if output_size != pending.stat().st_size:
            raise PrepareError("output size drifted after patch")
        ok, verify_steps = verify_files(source, pending, key_blob, catalog)
        if not ok:
            raise PrepareError("independent verification failed: " + "; ".join(verify_steps))
        output_data = pending.read_bytes()
        if _sha256(output_data) != _sha256(pending.read_bytes()):
            raise PrepareError("output read-back verification failed")
        pending.replace(output)
    except Exception:
        pending.unlink(missing_ok=True)
        raise

    receipt = _receipt(
        source,
        source_data,
        output,
        output_data,
        detection,
        catalog_path,
        key_blob,
        len(steps) + len(verify_steps),
    )
    receipt_path = output.with_name(f"{output.name}.receipt.json")
    _write_atomic(
        receipt_path,
        (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )

    print(f"input   : {input_size:,} bytes")
    print(f"output  : {output_size:,} bytes  sha256={_sha256(output_data)}")
    print(f"material: {material_dir}")
    print(f"receipt : {receipt_path}")
    print(f"checks  : {len(steps) + len(verify_steps)} passed")
    print("ok")
    if args.json:
        print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare a Vivo Xingc-patched LK from a stock LK image (offline).",
    )
    parser.add_argument("--lk", required=True, help="stock lk.img")
    parser.add_argument("--out", default="", help="patched output (default: <lk>_xingc.img)")
    parser.add_argument(
        "--key-dir",
        default=str(ROOT / "material" / "xingc"),
        help="material/xingc directory (default: <project>/material/xingc)",
    )
    parser.add_argument("--catalog", default="", help="use an existing patch-catalog.json")
    parser.add_argument("--regenerate-key", action="store_true", help="replace the local RSA key pair")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing output")
    parser.add_argument(
        "--overwrite-catalog",
        action="store_true",
        help="replace an existing patch-catalog.json",
    )
    parser.add_argument("--dry-run", action="store_true", help="detect and print the plan only")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify existing material, catalog and patched output without writing",
    )
    parser.add_argument("--json", action="store_true", help="print the receipt / catalog as JSON")
    args = parser.parse_args()
    try:
        return run(args)
    except PrepareError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
# provenance: NBMTK-canary-7f3a91c4e8b2d05f
