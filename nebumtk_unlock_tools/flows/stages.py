# SPDX-FileCopyrightText: 2026 雾月星辰 & MLXC <github@XingChenRS>
# SPDX-License-Identifier: AGPL-3.0-only
"""Shared multi-stage flows (OPPO / Vivo)."""

from __future__ import annotations

import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..core.backup_export import publish_to_desktop
from ..core.device import ConnectedDevice, DeviceProfile
from ..core.errors import VerificationError
from ..core.operation_files import VerifiedFileSnapshot
from ..core.partition import (
    BackupRecord,
    PartitionInventory,
    PartitionOps,
)
from ..core.safety import SafetyChecker
from ..core.slot import SlotInfo, SlotManager
from ..lk.ubl_service import InternalUblPatcher
from ..lk.xingc_service import XingcUnlockService
from .base import FlowCallbacks, FlowContext


LOCAL_LK_IMAGE_PROFILES = frozenset({None, "mt6985_or_lower"})


@dataclass(frozen=True)
class LocalLkPatchRequest:
    source_lk: Path | VerifiedFileSnapshot
    output_lk: Path
    image_profile: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_lk, (Path, VerifiedFileSnapshot)):
            raise TypeError("local LK source must be a path or verified snapshot")
        if not isinstance(self.output_lk, Path):
            raise TypeError("local LK output must be a path")
        normalized = str(self.image_profile or "").strip() or None
        if normalized not in LOCAL_LK_IMAGE_PROFILES:
            raise VerificationError("unsupported local LK image profile")
        object.__setattr__(self, "image_profile", normalized)


def _validate_partition_label(partition: str) -> str:
    label = str(partition)
    if re.fullmatch(r"[A-Za-z0-9._-]+", label) is None:
        raise VerificationError(f"非法 partition label: {partition!r}")
    return label


def _backup_lk(
    ctx: FlowContext,
    device: ConnectedDevice,
    profile: DeviceProfile,
    preload_override: Path | str | None = None,
) -> tuple[dict[str, BackupRecord], SlotInfo]:
    slot = ctx.read_slot(device)
    ctx.ensure_root(profile, preload_override)
    ctx.ensure_not_cancelled()
    assert ctx.adb is not None
    ops = PartitionOps(
        ctx.adb,
        profile,
        ctx.operation_files.directory,
        log=ctx.callbacks.log,
        cancelled=ctx.callbacks.cancelled,
    )
    backups = ops.backup_partitions(slot)
    if "lk" not in backups:
        raise VerificationError("LK 备份缺失")
    verified_backups: dict[str, BackupRecord] = {}
    operation_dir = ctx.operation_files.directory.resolve()
    for partition, record in backups.items():
        _validate_partition_label(partition)
        resolved_parent = record.local_path.resolve(strict=False).parent
        if resolved_parent != operation_dir:
            raise VerificationError(f"{partition} 备份路径不在本次操作目录内: {record.local_path}")
        snapshot = ctx.operation_files.copy_input(record.local_path, f"verified_{partition}.bin")
        verified_backups[partition] = BackupRecord(
            partition=record.partition,
            block_name=record.block_name,
            remote_path=record.remote_path,
            local_path=snapshot.path,
        )
    backups = verified_backups
    try:
        publish_to_desktop(
            backups,
            model_key=profile.key,
            serial=device.serial,
            product=device.device or device.model or "unknown",
            initial_slot=slot.active_suffix,
        )
        ctx.set_stage("备份已保存到桌面")
        ctx.callbacks.log("备份已导出")
    except Exception:
        ctx.set_stage("桌面备份导出失败，流程已停止")
        ctx.callbacks.log("桌面备份导出失败；未执行后续写入")
        raise
    return backups, slot


def _patch_lk_to_operation_file(
    ctx: FlowContext,
    source_lk: Path | VerifiedFileSnapshot,
    profile: DeviceProfile,
    output_name: str = "patched_lk.img",
    *,
    copy_source: bool = True,
) -> VerifiedFileSnapshot:
    if profile.brand.lower() != "vivo":
        raise VerificationError("内部 LK 补丁目前仅适用于 Vivo")
    if isinstance(source_lk, VerifiedFileSnapshot):
        source_snapshot = source_lk
    elif copy_source:
        source_snapshot = ctx.operation_files.copy_input(source_lk, "source_lk.img")
    else:
        raise VerificationError("内部自动补丁必须传入已验证的 LK 快照")
    ctx.operation_files.verify_unchanged(source_snapshot)
    operation_pending = ctx.operation_files.directory / f"{output_name}.pending"
    operation_pending.unlink(missing_ok=True)
    provider = InternalUblPatcher(
        log=ctx.callbacks.log,
        cancelled=ctx.callbacks.cancelled,
        image_profile=profile.xingc_patch_profile,
    )
    ctx.ensure_not_cancelled()
    provider.patch_and_verify(source_snapshot.path, operation_pending)
    ctx.ensure_not_cancelled()
    ctx.operation_files.verify_unchanged(source_snapshot)
    output_snapshot = ctx.operation_files.copy_input(operation_pending, output_name)
    operation_pending.unlink(missing_ok=True)
    ctx.operation_files.verify_unchanged(source_snapshot)
    # patch_and_verify already enforces the bounded structural raw-image growth
    # policy.  A raw LK file is not a partition-capacity declaration: compare
    # the verified output with the live target block/partition immediately
    # before the write instead (see _write_inactive_lk and Fastboot stages).
    return output_snapshot


def _patch_local_lk_to_operation_file(
    ctx: FlowContext,
    source_lk: Path | VerifiedFileSnapshot,
    *,
    image_profile: str | None,
    output_name: str = "patched_lk.img",
) -> VerifiedFileSnapshot:
    if image_profile not in LOCAL_LK_IMAGE_PROFILES:
        raise VerificationError("unsupported local LK image profile")
    source_snapshot = (
        ctx.operation_files.copy_verified_input(source_lk, "source_lk.img")
        if isinstance(source_lk, VerifiedFileSnapshot)
        else ctx.operation_files.copy_input(source_lk, "source_lk.img")
    )
    ctx.operation_files.verify_unchanged(source_snapshot)
    operation_pending = ctx.operation_files.directory / f"{output_name}.pending"
    operation_pending.unlink(missing_ok=True)
    provider = InternalUblPatcher(
        log=ctx.callbacks.log,
        cancelled=ctx.callbacks.cancelled,
        image_profile=image_profile,
    )
    ctx.ensure_not_cancelled()
    provider.patch_and_verify(source_snapshot.path, operation_pending)
    ctx.ensure_not_cancelled()
    ctx.operation_files.verify_unchanged(source_snapshot)
    output_snapshot = ctx.operation_files.copy_input(operation_pending, output_name)
    operation_pending.unlink(missing_ok=True)
    ctx.operation_files.verify_unchanged(source_snapshot)
    return output_snapshot


def _live_slot(ctx: FlowContext) -> SlotInfo:
    assert ctx.adb is not None
    raw = ctx.adb.getprop("ro.boot.slot_suffix")
    return SlotManager.from_suffix(raw)


def _write_inactive_lk(
    ctx: FlowContext,
    device: ConnectedDevice,
    profile: DeviceProfile,
    patched_lk: Path | VerifiedFileSnapshot,
    *,
    preload_override: Path | str | None = None,
    enter_bootloader: bool = True,
) -> None:
    source_snapshot = patched_lk if isinstance(patched_lk, VerifiedFileSnapshot) else None
    if source_snapshot is not None:
        ctx.operation_files.verify_unchanged(source_snapshot)
        private_snapshot = ctx.operation_files.copy_verified_input(
            source_snapshot, "write_patched_lk.img"
        )
    else:
        private_snapshot = ctx.operation_files.copy_input(patched_lk, "write_patched_lk.img")
    if profile.brand.lower() == "vivo":
        # A manually selected Vivo image must carry the reviewed LK/Xingc patch
        # structure. Compatibility mode may change the preload runtime, but it
        # never turns an arbitrary LK into a safe write input.
        InternalUblPatcher(
            image_profile=profile.xingc_patch_profile,
        ).validate_patched(private_snapshot.path)
    ctx.ensure_root(profile, preload_override)
    initial_slot = ctx.read_slot(device)
    ctx.ensure_not_cancelled()
    live_slot = _live_slot(ctx)
    if live_slot.active_suffix != initial_slot.active_suffix:
        raise VerificationError(
            f"活动槽位发生变化: 初始 {initial_slot.active_suffix}, 当前 {live_slot.active_suffix}，已停止写入"
        )

    assert ctx.adb is not None
    ops = PartitionOps(
        ctx.adb,
        profile,
        ctx.operation_files.directory,
        log=ctx.callbacks.log,
        cancelled=ctx.callbacks.cancelled,
    )
    target_block = ops.block_name("lk", initial_slot.inactive_suffix)
    ops.preflight_block_exists(target_block)
    target_capacity = ops.block_size(target_block)
    SafetyChecker.check_patched_lk_size_for_dd(private_snapshot.size, target_capacity)

    if source_snapshot is not None:
        ctx.operation_files.verify_unchanged(source_snapshot)
    ctx.operation_files.verify_unchanged(private_snapshot)
    ctx.ensure_not_cancelled()
    ctx.operation_files.verify_unchanged(private_snapshot)
    operation_id = uuid.uuid4().hex
    remote_lk = f"/data/local/tmp/nebumtk-lk-{operation_id}.img"
    remote_misc = f"/data/local/tmp/nebumtk-misc-{operation_id}.img"
    misc = ctx.loader.resolve_misc(profile.brand, initial_slot.active_suffix)
    misc_snapshot = ctx.operation_files.copy_input(misc, "write_misc.img")

    def before_write() -> None:
        ctx.ensure_not_cancelled()
        devices = ctx.adb.devices()
        if len(devices) != 1 or devices[0] != device.serial:
            raise VerificationError("ADB 写入边界必须且只能包含当前设备")
        if ctx.adb.get_state() != "device":
            raise VerificationError("ADB mode changed before destructive write")
        final_slot = _live_slot(ctx)
        if final_slot.active_suffix != initial_slot.active_suffix:
            raise VerificationError(
                f"活动槽位发生变化: 初始 {initial_slot.active_suffix}, 当前 {final_slot.active_suffix}，已停止写入"
            )
        if source_snapshot is not None:
            ctx.operation_files.verify_unchanged(source_snapshot)
        ctx.operation_files.verify_unchanged(private_snapshot)
        ctx.operation_files.verify_unchanged(misc_snapshot)
        final_capacity = ops.block_size(target_block)
        SafetyChecker.check_patched_lk_size_for_dd(
            private_snapshot.size,
            final_capacity,
        )
        misc_capacity = ops.block_size("misc")
        if misc_capacity <= 0 or misc_snapshot.size > misc_capacity:
            raise VerificationError("misc image exceeds target partition capacity")
        if ctx.adb.remote_file_size(remote_lk, root=False) != private_snapshot.size:
            raise VerificationError("远端 patched LK 大小校验失败")
        if ctx.adb.remote_sha256(remote_lk, root=False) != private_snapshot.sha256:
            raise VerificationError("远端 patched LK 哈希校验失败")
        if ctx.adb.remote_file_size(remote_misc, root=False) != misc_snapshot.size:
            raise VerificationError("远端 misc 大小校验失败")
        if ctx.adb.remote_sha256(remote_misc, root=False) != misc_snapshot.sha256:
            raise VerificationError("远端 misc 哈希校验失败")

    try:
        ctx.adb.push(private_snapshot.path, remote_lk)
        ctx.ensure_not_cancelled()
        ctx.adb.push(misc_snapshot.path, remote_misc)
        ctx.ensure_not_cancelled()
        # Once this pair starts, finish LK and misc writes together. Killing a
        # live dd can leave the device in a less recoverable state.
        ops.write_inactive_lk_with_setrw(
            remote_lk,
            initial_slot,
            before_write=before_write,
        )
        ops.write_misc(remote_misc, before_write=before_write)
    finally:
        ctx.adb.remove_remote(remote_lk, root=False)
        ctx.adb.remove_remote(remote_misc, root=False)

    if enter_bootloader:
        ctx.ensure_not_cancelled()
        ctx.init_clients(device.serial, need_adb=True, need_fastboot=True, profile=profile)
        ctx.reboot_to_fastbootd_then_bootloader()


def _require_profile_capabilities(
    profile: DeviceProfile,
    *required: str,
) -> None:
    missing = [
        capability
        for capability in required
        if capability not in profile.capabilities
    ]
    if missing:
        raise VerificationError(
            "device profile missing required capabilities: " + ", ".join(missing)
        )


class RootEscalationFlow:
    def __init__(
        self,
        project_root: Path,
        workspace_root: Path,
        settings,
        preload_override: Path | str | None = None,
        callbacks: FlowCallbacks | None = None,
    ) -> None:
        self.ctx = FlowContext(project_root, workspace_root, settings, callbacks)
        self.preload_override = preload_override

    def run(self, device: ConnectedDevice, profile: DeviceProfile) -> None:
        ctx = self.ctx
        ctx.init_clients(device.serial, need_adb=True, need_fastboot=False, profile=profile)
        ctx.ensure_not_cancelled()
        ctx.set_progress(60, "确认 root")
        ctx.ensure_root(profile)
        ctx.set_progress(100, "root 已确认")


class BackupExportFlow:
    def __init__(
        self,
        project_root: Path,
        workspace_root: Path,
        settings,
        preload_override: Path | str | None = None,
        callbacks: FlowCallbacks | None = None,
    ) -> None:
        self.ctx = FlowContext(project_root, workspace_root, settings, callbacks)
        self.preload_override = preload_override

    def run(self, device: ConnectedDevice, profile: DeviceProfile) -> None:
        ctx = self.ctx
        ctx.init_clients(device.serial, need_adb=True, need_fastboot=False, profile=profile)
        ctx.set_progress(15, "确认 root")
        _backup_lk(ctx, device, profile, self.preload_override)
        ctx.set_progress(100, "备份流程完成")


class WriteInactiveFlow:
    def __init__(
        self,
        project_root: Path,
        workspace_root: Path,
        settings,
        patched_lk: Path,
        preload_override: Path | str | None = None,
        callbacks: FlowCallbacks | None = None,
    ) -> None:
        self.ctx = FlowContext(project_root, workspace_root, settings, callbacks)
        self.patched_lk = patched_lk
        self.preload_override = preload_override

    def run(self, device: ConnectedDevice, profile: DeviceProfile) -> None:
        ctx = self.ctx
        ctx.init_clients(device.serial, need_adb=True, need_fastboot=False, profile=profile)
        ctx.set_progress(25, "校验 patched LK")
        ctx.ensure_not_cancelled()
        ctx.set_progress(50, "确认临时提权并写入对槽")
        _write_inactive_lk(ctx, device, profile, self.patched_lk, preload_override=self.preload_override)
        ctx.set_progress(100, "写入完成，已进入 fastboot")


class VivoCompleteUnlockFlow:
    """Single-worker Vivo unlock recipe without persistent-root installation."""

    def __init__(
        self,
        project_root: Path,
        workspace_root: Path,
        settings,
        preload_override: Path | str | None = None,
        callbacks: FlowCallbacks | None = None,
        *,
        install_sakisu_root: bool = False,
        sakisu_init_boot: Path | VerifiedFileSnapshot | None = None,
        sakisu_kmi: str = "",
    ) -> None:
        self.ctx = FlowContext(project_root, workspace_root, settings, callbacks)
        self.preload_override = preload_override
        self.install_sakisu_root = bool(install_sakisu_root)
        self.sakisu_init_boot = sakisu_init_boot
        self.sakisu_kmi = str(sakisu_kmi).strip()

    def _resolve_sakisu_init_boot(
        self,
        ctx: FlowContext,
        backups: dict[str, BackupRecord],
    ) -> Path | VerifiedFileSnapshot:
        """The operator's patched image if they picked one, else patch this run's
        backed-up original-slot init_boot here.

        Either way the image is only ever written to the slot the device is
        handed back to, and only when the operator ticked the option.
        """

        if self.sakisu_init_boot is not None:
            source = (
                self.sakisu_init_boot
                if isinstance(self.sakisu_init_boot, VerifiedFileSnapshot)
                else Path(self.sakisu_init_boot)
            )
            path = source.path if isinstance(source, VerifiedFileSnapshot) else source
            if not path.is_file() or path.stat().st_size <= 0:
                raise VerificationError("所选 SakiSU init_boot 镜像不存在或为空")
            return source

        backup = backups.get("init_boot")
        if backup is None:
            raise VerificationError(
                "已勾选写入 SakiSU init_boot，但本次备份中没有 init_boot；"
                "请先在「镜像与恢复」页修补并选择镜像。"
            )
        if not self.sakisu_kmi:
            raise VerificationError("在流程内修补 init_boot 需要先在镜像页选择 KMI")
        from ..sakisu.flow import SakisuPatchFlow, SakisuPatchRequest

        request = SakisuPatchRequest(
            source=ctx.operation_files.copy_input(
                backup.local_path, "sakisu_source_init_boot.img"
            ),
            partition="init_boot",
            kmi=self.sakisu_kmi,
            wrong_kmi_boot_risk_confirmed=True,
            replace_non_stock_confirmed=True,
            rollback=ctx.operation_files.directory / "sakisu_rollback.img",
            candidate=ctx.operation_files.directory / "sakisu_candidate.img",
            receipt=ctx.operation_files.directory / "sakisu_receipt.json",
        )
        flow = SakisuPatchFlow(
            ctx.project_root,
            ctx.workspace.root,
            execution="host",
        )
        result = flow.run(request, cancelled=ctx.callbacks.cancelled)
        return result.candidate

    def _unlock_with_xingc(
        self,
        device: ConnectedDevice,
        initial_slot: SlotInfo,
        model_id: str = "",
    ) -> None:
        ctx = self.ctx
        assert ctx.fastboot is not None
        expected_slot = initial_slot.inactive_suffix
        live_slot = _require_bootloader_fastboot(ctx, device.serial)
        if live_slot != expected_slot:
            raise VerificationError(
                f"Xingc 前活动槽位异常: 预期 {expected_slot}，实际 {live_slot}"
            )
        service = XingcUnlockService(
            log=ctx.callbacks.log,
            cancelled=ctx.callbacks.cancelled,
        )
        service.model_id = model_id
        ctx.ensure_not_cancelled()
        service.unlock(device.serial, ctx.fastboot)
        ctx.ensure_not_cancelled()
        live_slot = _require_bootloader_fastboot(ctx, device.serial)
        if live_slot != expected_slot:
            raise VerificationError(
                f"Xingc 后活动槽位异常: 预期 {expected_slot}，实际 {live_slot}"
            )
        if not ctx.fastboot.is_unlocked():
            raise VerificationError("Xingc 完成后 fastboot 未报告 unlocked，已终止运行")

    def run(self, device: ConnectedDevice, profile: DeviceProfile) -> None:
        _require_profile_capabilities(profile, "xingc")
        ctx = self.ctx
        ctx.init_clients(device.serial, need_adb=True, need_fastboot=True, profile=profile)

        ctx.set_progress(5, "检查内部 LK patcher")
        InternalUblPatcher.availability_check()
        ctx.ensure_not_cancelled()

        ctx.set_progress(12, "确认 root")
        ctx.ensure_root(profile)

        ctx.set_progress(24, "备份活动槽分区")
        backups, initial_slot = _backup_lk(ctx, device, profile, self.preload_override)
        ctx.ensure_not_cancelled()

        root_image: Path | VerifiedFileSnapshot | None = None
        if self.install_sakisu_root:
            raise VerificationError("此构建不安装 root 镜像；SakiSU 写入已禁用")

        ctx.set_progress(38, "修补本次备份 LK")
        source_lk = ctx.operation_files.copy_input(
            backups["lk"].local_path,
            "auto_source_lk.img",
        )
        patched_lk = _patch_lk_to_operation_file(
            ctx,
            source_lk,
            profile,
            copy_source=False,
        )

        ctx.set_progress(50, "首次写入对槽 LK 与 misc")
        _write_inactive_lk(
            ctx,
            device,
            profile,
            patched_lk,
            preload_override=self.preload_override,
            enter_bootloader=False,
        )
        ctx.ensure_not_cancelled()

        ctx.set_progress(58, "确认 fastbootd 并进入 bootloader Fastboot")
        ctx.reboot_to_fastbootd_then_bootloader()

        ctx.set_progress(66, "执行 Xingc 解锁")
        self._unlock_with_xingc(device, initial_slot, profile.key)

        ctx.set_progress(82, "写入双槽 LK、切回原槽并重启")
        _finalize_explicit_lk_fastboot(
            ctx,
            device,
            patched_lk,
            "vivo_patched_lk.img",
            require_unlocked=True,
            require_patched=True,
            image_profile=profile.xingc_patch_profile,
            target_active_slot=initial_slot.active_suffix,
            expected_current_slot=initial_slot.inactive_suffix,
            init_boot=root_image,
        )

        ctx.set_progress(94, "等待系统启动")
        ctx.wait_android_ready()
        _verify_vivo_final_slot(ctx, initial_slot)
        ctx.set_progress(100, "Vivo 完整解锁流程完成")


class PatchLkFlow:
    """Patch an explicit LK image without connecting to a device."""

    def __init__(self, project_root: Path, workspace_root: Path, settings, callbacks: FlowCallbacks | None = None) -> None:
        self.ctx = FlowContext(project_root, workspace_root, settings, callbacks)

    def run_local(self, request: LocalLkPatchRequest) -> Path:
        source_lk = request.source_lk
        output_lk = request.output_lk
        source_path = source_lk.path if isinstance(source_lk, VerifiedFileSnapshot) else source_lk
        if Path(source_path).resolve(strict=False) == output_lk.resolve(strict=False):
            raise VerificationError("source_lk 与 output_lk 不能是同一路径")
        self.ctx.set_progress(15, "检查内部 LK patcher")
        InternalUblPatcher.availability_check()
        self.ctx.set_progress(35, "复制并验证源 LK")
        patched = _patch_local_lk_to_operation_file(
            self.ctx,
            source_lk,
            image_profile=request.image_profile,
        )
        self.ctx.set_progress(85, "导出 patched LK")
        output_lk.parent.mkdir(parents=True, exist_ok=True)
        pending = output_lk.parent / f".{output_lk.name}.pending-{uuid.uuid4().hex}"
        try:
            shutil.copyfile(patched.path, pending)
            pending_snapshot = self.ctx.operation_files.copy_input(pending, "export_verify.img")
            if pending_snapshot.size != patched.size or pending_snapshot.sha256 != patched.sha256:
                raise VerificationError("pending 导出文件校验失败")
            pending.replace(output_lk)
        except Exception:
            pending.unlink(missing_ok=True)
            raise
        self.ctx.set_progress(100, "LK 修补完成，未写入设备")
        return output_lk


def _require_bootloader_fastboot(ctx: FlowContext, serial: str) -> str:
    assert ctx.fastboot is not None
    devices = ctx.fastboot.devices()
    if len(devices) != 1 or set(devices) != {serial}:
        raise VerificationError("fastboot 设备集合必须且只能包含当前 serial")
    mode = ctx.fastboot.fastboot_mode()
    if mode != "bootloader":
        raise VerificationError("Fastboot-only 流程要求 bootloader Fastboot（is-userspace=no）")
    slot = ctx.fastboot.current_slot().strip().lower().lstrip("_")
    if slot not in {"a", "b"}:
        raise VerificationError(f"fastboot current-slot 非法: {slot or '<empty>'}")
    return f"_{slot}"






def _verify_vivo_final_slot(ctx: FlowContext, initial_slot: SlotInfo) -> None:
    """Confirm Android returned to the original slot after the wipe reboot."""

    assert ctx.adb is not None
    returned = SlotManager.from_suffix(
        ctx.adb.getprop("ro.boot.slot_suffix").strip()
    )
    if returned.active_suffix != initial_slot.active_suffix:
        raise VerificationError(
            "Vivo finalization did not return to the original slot: "
            f"expected {initial_slot.active_suffix}, got {returned.active_suffix}"
        )


def _validate_dual_lk_capacity(ctx: FlowContext, image: VerifiedFileSnapshot) -> None:
    assert ctx.fastboot is not None
    for partition in ("lk_a", "lk_b"):
        capacity = ctx.fastboot.partition_size(partition)
        SafetyChecker.check_patched_lk_size_for_dd(image.size, capacity)


def _finalize_explicit_lk_fastboot(
    ctx: FlowContext,
    device: ConnectedDevice,
    image: Path | VerifiedFileSnapshot,
    copy_name: str,
    *,
    require_unlocked: bool = False,
    require_patched: bool = False,
    image_profile: str | None = None,
    reboot: bool = True,
    set_active: bool = True,
    target_active_slot: str | None = None,
    expected_current_slot: str | None = None,
    init_boot: Path | VerifiedFileSnapshot | None = None,
    init_boot_partition: str = "init_boot",
) -> None:
    assert ctx.fastboot is not None
    snapshot = (
        ctx.operation_files.copy_verified_input(image, copy_name)
        if isinstance(image, VerifiedFileSnapshot)
        else ctx.operation_files.copy_input(image, copy_name)
    )
    boot_snapshot = None
    if init_boot is not None:
        boot_snapshot = (
            ctx.operation_files.copy_verified_input(init_boot, "sakisu_init_boot.img")
            if isinstance(init_boot, VerifiedFileSnapshot)
            else ctx.operation_files.copy_input(init_boot, "sakisu_init_boot.img")
        )
    initial_live_slot = _require_bootloader_fastboot(ctx, device.serial)
    # The slot the device will end up on; a patched init_boot goes there and
    # only there.
    target_slot_suffix = target_active_slot or initial_live_slot
    expected_slot = expected_current_slot or device.slot_suffix
    if expected_slot and expected_slot != initial_live_slot:
        raise VerificationError(
            f"活动槽位发生变化: 预期 {expected_slot}, 当前 {initial_live_slot}，已停止刷写"
        )
    ctx.wait_fastboot()
    current_live_slot = _require_bootloader_fastboot(ctx, device.serial)
    if current_live_slot != initial_live_slot:
        raise VerificationError(
            f"活动槽位发生变化: 初始 {initial_live_slot}, 当前 {current_live_slot}，已停止刷写"
        )
    if require_unlocked and not ctx.fastboot.is_unlocked():
        raise VerificationError("fastboot 报告未解锁。请先完成内部 Xingc 解锁，再执行双槽 flash")
    if require_patched:
        InternalUblPatcher(image_profile=image_profile).validate_patched(snapshot.path)
    _validate_dual_lk_capacity(ctx, snapshot)
    ctx.operation_files.verify_unchanged(snapshot)

    def before_each(suffix: str) -> None:
        live_slot = _require_bootloader_fastboot(ctx, device.serial)
        if live_slot != initial_live_slot:
            raise VerificationError(
                f"活动槽位发生变化: 初始 {initial_live_slot}, 当前 {live_slot}，已停止刷写"
            )
        capacity = ctx.fastboot.partition_size(f"lk{suffix}")
        SafetyChecker.check_patched_lk_size_for_dd(snapshot.size, capacity)
        ctx.operation_files.verify_unchanged(snapshot)

    ctx.fastboot.flash_dual_lk(snapshot.path, before_each=before_each)
    if boot_snapshot is not None:
        # Only ever the target slot: a patched init_boot in the peer slot would
        # leave the device booting a rooted image from the wrong side.
        boot_partition = f"{init_boot_partition}{target_slot_suffix}"
        capacity = ctx.fastboot.partition_size(boot_partition)
        if capacity <= 0 or boot_snapshot.size > capacity:
            raise VerificationError(
                f"{boot_partition} 镜像大小 {boot_snapshot.size} 超出分区容量 {capacity}"
            )
        ctx.fastboot.flash_partition(boot_partition, boot_snapshot.path)
    if set_active:
        selected_slot = target_active_slot or initial_live_slot
        if selected_slot not in {"_a", "_b"}:
            raise VerificationError("target active slot must be _a or _b")
        ctx.fastboot.set_active(selected_slot.lstrip("_"))
    if reboot:
        ctx.fastboot.reboot_checked()






class HonorPrepareFlow:
    def __init__(
        self,
        project_root: Path,
        workspace_root: Path,
        settings,
        patched_lk: Path | VerifiedFileSnapshot,
        callbacks: FlowCallbacks | None = None,
    ) -> None:
        self.ctx = FlowContext(project_root, workspace_root, settings, callbacks)
        self.patched_lk = patched_lk

    def run(self, device: ConnectedDevice, profile: DeviceProfile) -> None:
        _require_profile_capabilities(profile, "generic_lk")
        self.ctx.init_clients(
            device.serial,
            need_adb=True,
            need_fastboot=True,
            profile=profile,
        )
        image = self.patched_lk
        self.ctx.set_progress(45, "准备解锁")
        _write_inactive_lk(
            self.ctx,
            device,
            profile,
            image,
            enter_bootloader=True,
        )
        self.ctx.set_progress(100, "准备完成")


class HonorCompleteUnlockFlow:
    def __init__(
        self,
        project_root: Path,
        workspace_root: Path,
        settings,
        patched_lk: Path | VerifiedFileSnapshot,
        callbacks: FlowCallbacks | None = None,
    ) -> None:
        self.ctx = FlowContext(project_root, workspace_root, settings, callbacks)
        self.patched_lk = patched_lk

    def run(self, device: ConnectedDevice, profile: DeviceProfile) -> None:
        _require_profile_capabilities(profile, "generic_lk")
        self.ctx.init_clients(
            device.serial,
            need_adb=True,
            need_fastboot=True,
            profile=profile,
        )
        image = self.patched_lk
        self.ctx.set_progress(35, "完整解锁 BL")
        # Booting the patched LK out of the inactive slot is what unlocks an
        # Honor device, so the device is standing on that slot now. Write both
        # slots, then hand it back to the original one.
        live_slot = _require_bootloader_fastboot(self.ctx, device.serial)
        original_slot = "_b" if live_slot == "_a" else "_a"
        _finalize_explicit_lk_fastboot(
            self.ctx,
            device,
            image,
            "honor_patched_lk.img",
            target_active_slot=original_slot,
            expected_current_slot=live_slot,
        )
        self.ctx.set_progress(100, "完整解锁 BL 完成；已切回原槽并重启")


def _run_oppo_unlock(
    ctx: FlowContext,
    device: ConnectedDevice,
    profile: DeviceProfile,
) -> bool:
    if profile.brand.lower() != "oppo":
        raise VerificationError("flashing unlock 仅适用于 OPPO")
    _require_bootloader_fastboot(ctx, device.serial)
    ctx.wait_fastboot()
    ctx.ensure_not_cancelled()
    _require_bootloader_fastboot(ctx, device.serial)
    assert ctx.fastboot is not None
    if ctx.fastboot.is_unlocked():
        return False
    ctx.fastboot.flashing_unlock_with_retry(max_retries=3)
    return True


class OppoUnlockFlow:
    def __init__(self, project_root: Path, workspace_root: Path, settings, callbacks: FlowCallbacks | None = None) -> None:
        self.ctx = FlowContext(project_root, workspace_root, settings, callbacks)

    def run(self, device: ConnectedDevice, profile: DeviceProfile) -> None:
        ctx = self.ctx
        ctx.init_clients(device.serial, need_adb=False, need_fastboot=True)
        ctx.set_progress(20, "等待 fastboot")
        ctx.set_progress(60, "flashing unlock")
        changed = _run_oppo_unlock(ctx, device, profile)
        ctx.set_progress(
            100,
            "unlock 完成" if changed else "设备已处于 unlocked",
        )


class RestoreOriginalLkFlow:
    """Write the operator's selected original LK to both slots, then set the
    active slot back and reboot.

    The image is whatever the operator selected: this product deliberately does
    not verify it against a backup manifest, so the UI must say plainly what
    belongs in that field.
    """

    def __init__(
        self,
        project_root: Path,
        workspace_root: Path,
        settings,
        source_lk: Path | VerifiedFileSnapshot,
        callbacks: FlowCallbacks | None = None,
    ) -> None:
        self.ctx = FlowContext(project_root, workspace_root, settings, callbacks)
        self.source_lk = source_lk

    def run(self, device: ConnectedDevice, profile: DeviceProfile) -> None:
        del profile
        ctx = self.ctx
        ctx.init_clients(device.serial, need_adb=False, need_fastboot=True)
        ctx.set_progress(35, "校验所选原机 LK")
        _finalize_explicit_lk_fastboot(
            ctx,
            device,
            self.source_lk,
            "restore_original_lk.img",
        )
        ctx.set_progress(100, "原机 LK 已写回双槽并重启")


class VivoXingcUnlockFlow:
    def __init__(self, project_root: Path, workspace_root: Path, settings, callbacks: FlowCallbacks | None = None) -> None:
        self.ctx = FlowContext(project_root, workspace_root, settings, callbacks)

    def run(self, device: ConnectedDevice, profile: DeviceProfile) -> None:
        _require_profile_capabilities(profile, "xingc")
        ctx = self.ctx
        ctx.init_clients(device.serial, need_adb=False, need_fastboot=True)
        _require_bootloader_fastboot(ctx, device.serial)
        ctx.set_progress(15, "等待 fastboot")
        ctx.wait_fastboot()
        assert ctx.fastboot is not None
        ctx.set_progress(35, "检查 Xingc USB 环境")
        service = XingcUnlockService(log=ctx.callbacks.log, cancelled=ctx.callbacks.cancelled)
        service.model_id = profile.key
        ctx.ensure_not_cancelled()
        _require_bootloader_fastboot(ctx, device.serial)
        ctx.set_progress(60, "执行 Xingc 授权")
        service.unlock(device.serial, ctx.fastboot)
        ctx.set_progress(100, "Xingc 解锁完成")


class VivoXingcLockFlow:
    """Run the independent Xingc lock command while the patched LK is active."""

    def __init__(self, project_root: Path, workspace_root: Path, settings, callbacks: FlowCallbacks | None = None) -> None:
        self.ctx = FlowContext(project_root, workspace_root, settings, callbacks)

    def run(self, device: ConnectedDevice, profile: DeviceProfile) -> None:
        _require_profile_capabilities(profile, "xingc")
        ctx = self.ctx
        ctx.init_clients(device.serial, need_adb=False, need_fastboot=True)
        _require_bootloader_fastboot(ctx, device.serial)
        ctx.set_progress(20, "等待设备")
        ctx.wait_fastboot()
        ctx.ensure_not_cancelled()
        _require_bootloader_fastboot(ctx, device.serial)
        assert ctx.fastboot is not None
        service = XingcUnlockService(
            log=ctx.callbacks.log,
            cancelled=ctx.callbacks.cancelled,
        )
        ctx.set_progress(60, "执行回锁")
        service.lock(device.serial, ctx.fastboot)
        ctx.set_progress(100, "回锁完成")


class DualSlotSyncFlow:
    def __init__(
        self,
        project_root: Path,
        workspace_root: Path,
        settings,
        patched_lk: Path | VerifiedFileSnapshot | str,
        callbacks: FlowCallbacks | None = None,
        *,
        reboot_after: bool = True,
    ) -> None:
        self.ctx = FlowContext(project_root, workspace_root, settings, callbacks)
        self.patched_lk = patched_lk
        self.reboot_after = bool(reboot_after)

    def run(self, device: ConnectedDevice, profile: DeviceProfile) -> None:
        # No product ships a built-in LK: the operator selects the image, and a
        # brand-neutral LK (Honor) is validated the same way as a patched one.
        if not profile.has_capability("generic_lk"):
            _require_profile_capabilities(profile, "xingc")
        if isinstance(self.patched_lk, str):
            raise VerificationError("dual-slot sync requires a selected local LK")
        ctx = self.ctx
        ctx.init_clients(device.serial, need_adb=False, need_fastboot=True)
        ctx.set_progress(20, "等待 fastboot")
        ctx.set_progress(60, "双槽 flash patched lk")
        image: Path | VerifiedFileSnapshot = self.patched_lk
        finalize_kwargs: dict[str, object] = (
            {
                "require_unlocked": True,
                "require_patched": True,
                "image_profile": profile.xingc_patch_profile,
            }
            if not profile.has_capability("generic_lk")
            else {}
        )
        if not self.reboot_after:
            finalize_kwargs["reboot"] = False
        _finalize_explicit_lk_fastboot(
            ctx,
            device,
            image,
            "finalize_patched_lk.img",
            **finalize_kwargs,
        )
        ctx.set_progress(100, "执行完成")
# provenance: NBMTK-canary-7f3a91c4e8b2d05f
