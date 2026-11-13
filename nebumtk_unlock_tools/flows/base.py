# SPDX-FileCopyrightText: 2026 雾月星辰 & MLXC <github@XingChenRS>
# SPDX-License-Identifier: AGPL-3.0-only
"""Shared flow helpers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config.store import Settings
from ..core.activity import ActivityEvent, ActivityKind, WaitTarget
from ..core.adb import AdbClient
from ..core.device import ConnectedDevice, DeviceProfile
from ..core.env import EnvChecker
from ..core.errors import FlowCancelledError, VerificationError
from ..core.fastboot import FastbootClient
from ..core.partition import PartitionOps
from ..core.root import RootEscalator
from ..core.slot import SlotInfo, SlotManager
from ..core.su import SuProvider, probe_custom_su, resolve_su_provider
from ..core.verifier import StepVerifier
from ..core.workspace import WorkspaceManager
from ..resources.loader import ProtectedPreload, ResourceLoader


@dataclass
class FlowCallbacks:
    log: Callable[[str], None] = field(default=lambda _message: None)
    progress: Callable[[int, str], None] = field(default=lambda _value, _label: None)
    activity: Callable[[ActivityEvent], None] = field(default=lambda _event: None)
    cancelled: Callable[[], bool] = field(default=lambda: False)
    confirmation: Callable[[str], bool] = field(default=lambda _message: True)
    skip_profile_identity: bool = False


class FlowContext:
    def __init__(
        self,
        project_root: Path,
        workspace_root: Path,
        settings: Settings,
        callbacks: FlowCallbacks | None = None,
    ) -> None:
        self.project_root = project_root
        self.workspace = WorkspaceManager(workspace_root)
        self.settings = settings
        self.callbacks = callbacks or FlowCallbacks()
        if self.callbacks.skip_profile_identity and not self.settings.test_mode_enabled:
            raise VerificationError(
                "测试兼容模式仅允许在设置中显式开启后使用"
            )
        self.loader = ResourceLoader(project_root)
        self.operation_files = self.workspace.operation_files("flow")
        self.adb: AdbClient | None = None
        self.fastboot: FastbootClient | None = None
        self.su_provider: SuProvider | None = None
        self._progress_value = 0

    def ensure_not_cancelled(self) -> None:
        if self.callbacks.cancelled():
            raise FlowCancelledError("用户已取消")

    def set_progress(self, percent: int, label: str) -> None:
        self._progress_value = percent
        self.callbacks.progress(percent, label)

    def set_stage(self, label: str) -> None:
        self.callbacks.progress(self._progress_value, label)

    def cancellable_sleep(self, seconds: float) -> None:
        remaining = max(0.0, seconds)
        step = 0.5
        while remaining > 0:
            self.ensure_not_cancelled()
            slice_ = min(step, remaining)
            time.sleep(slice_)
            remaining -= slice_

    def init_clients(
        self,
        serial: str,
        *,
        need_adb: bool = True,
        need_fastboot: bool = False,
        profile: DeviceProfile | None = None,
        su_provider: str = "protected",
    ) -> None:
        tools = EnvChecker(self.settings).validate(need_adb=need_adb, need_fastboot=need_fastboot)
        if need_adb:
            protected_executable = profile.su_path if profile is not None else "su"
            self.su_provider = resolve_su_provider(
                su_provider,
                protected_executable=protected_executable,
                settings=self.settings,
            )
            su_path = self.su_provider.executable
            self.adb = AdbClient(
                tools.adb,
                serial=serial,
                log=self.callbacks.log,
                su_path=su_path,
                cancelled=self.callbacks.cancelled,
            )
        if need_fastboot:
            self.fastboot = FastbootClient(
                tools.fastboot,
                serial=serial,
                log=self.callbacks.log,
                cancelled=self.callbacks.cancelled,
            )

    def select_su_provider(
        self,
        profile: DeviceProfile,
        kind: str,
    ) -> SuProvider:
        provider = resolve_su_provider(
            kind,
            protected_executable=profile.su_path,
            settings=self.settings,
        )
        self.su_provider = provider
        if self.adb is not None:
            self.adb.su_path = provider.executable
        return provider

    def read_slot(self, device: ConnectedDevice) -> SlotInfo:
        assert self.adb is not None
        self.set_stage("读取当前槽位…")
        slot_raw = (device.slot_suffix or "").strip()
        if not slot_raw:
            slot_raw = self.adb.getprop("ro.boot.slot_suffix")
        StepVerifier.check_non_empty(slot_raw, step="读取活动槽位")
        slot = SlotManager.from_suffix(slot_raw)
        self.set_stage(f"当前槽位: {slot.active_suffix}（对槽: {slot.inactive_suffix}）")
        return slot

    def _make_escalator(
        self,
        profile: DeviceProfile | None = None,
        protected_preload: ProtectedPreload | None = None,
    ) -> RootEscalator:
        assert self.adb is not None
        return RootEscalator(
            self.adb,
            max_retries=4,
            post_boot_settle=30.0,
            remote_wrapper=profile.preload_remote if profile is not None else RootEscalator.REMOTE_WRAPPER,
            grant_responder=(
                protected_preload.grant_responder
                if protected_preload is not None
                else None
            ),
            execution_mode=(
                profile.preload_mode if profile is not None else "preload"
            ),
            trigger_command=profile.preload_trigger if profile is not None else "/system/bin/true",
            payload_args=profile.preload_args if profile is not None else (),
            trigger_env=profile.preload_env if profile is not None else None,
            clean_environment=profile.preload_clean_env if profile is not None else False,
            log=self.callbacks.log,
            stage=self.set_stage,
            cancelled=self.callbacks.cancelled,
        )

    def escalate_root(self, profile: DeviceProfile | None = None, preload_override: Path | str | None = None) -> None:
        del profile, preload_override
        raise VerificationError(
            "此构建不执行提权；请先在设备上取得 root，并确认 "
            "`adb shell su -c id` 返回 uid=0。"
        )

    def partition_ops(self, profile: DeviceProfile, serial: str) -> PartitionOps:
        assert self.adb is not None
        exports = self.workspace.exports_dir(serial)
        return PartitionOps(
            self.adb,
            profile,
            exports,
            log=self.callbacks.log,
            cancelled=self.callbacks.cancelled,
        )

    def push_preload(self, preload: ProtectedPreload, remote_path: str) -> None:
        assert self.adb is not None
        if not preload.wrapper.is_file():
            raise VerificationError(f"受保护 preload wrapper 不存在: {preload.wrapper}")
        self.adb.remove_remote(remote_path, root=False)
        try:
            self.adb.push(preload.wrapper, remote_path)
        except Exception:
            self.adb.remove_remote(remote_path, root=False)
            raise

    def resolve_preload(
        self,
        profile: DeviceProfile,
        override: Path | str | None,
    ) -> ProtectedPreload:
        if isinstance(override, Path):
            raise VerificationError("禁止使用明文 preload.so，请选择内置受保护资源")
        preload_ref = (
            override
            if isinstance(override, str)
            else profile.privilege_runtime.preload_ref
        )
        preload = self.loader.resolve_preload(preload_ref)
        if preload is not None and preload.wrapper.is_file():
            return preload
        raise VerificationError("内置受保护 preload 不可用")

    def ensure_root(self, profile: DeviceProfile, preload_override: Path | str | None = None) -> None:
        """Require an already-rooted device; this build never escalates.

        Temporary root is fine, but ``su`` must answer repeatedly. No payload
        is pushed and no root manager is installed.
        """

        del preload_override
        assert self.adb is not None
        self.set_stage("确认 root 权限…")
        if self.su_provider is not None and self.su_provider.kind == "custom":
            if not self.settings.test_mode_enabled:
                raise VerificationError("custom su 仅在应急兼容模式下可用")
            result = probe_custom_su(self.adb, self.su_provider.executable)
            if result.uid != 0:
                raise VerificationError("custom su did not provide uid 0")
            self.set_stage("root 已获取")
            return

        candidates: list[str] = []
        for candidate in (
            str(self.adb.su_path or "").strip(),
            str(getattr(profile, "su_path", "") or "").strip(),
            "su",
        ):
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        for candidate in candidates:
            try:
                first = probe_custom_su(self.adb, candidate)
                second = probe_custom_su(self.adb, candidate)
            except Exception as exc:
                self.callbacks.log(f"su 探测失败 {candidate}: {exc}")
                continue
            if first.uid == 0 and second.uid == 0:
                self.adb.su_path = candidate
                self.su_provider = SuProvider("protected", candidate)
                self.set_stage("root 已获取")
                self.callbacks.log(f"已有 root: {candidate}")
                return

        if self.settings.test_mode_enabled:
            custom = str(self.settings.custom_su or "").strip()
            if custom and custom not in candidates:
                try:
                    if probe_custom_su(self.adb, custom).uid == 0:
                        self.adb.su_path = custom
                        self.su_provider = SuProvider("custom", custom)
                        self.set_stage("root 已获取")
                        return
                except Exception as exc:
                    self.callbacks.log(f"custom su 探测失败: {exc}")

        raise VerificationError(
            "设备未 root；此构建不执行提权。请先自行取得 root，"
            "并确认 `adb shell su -c id` 返回 uid=0。"
        )

    def cleanup_materialized_preloads(self) -> None:
        loader_error: Exception | None = None
        try:
            self.loader.cleanup_materialized_preloads()
        except Exception as exc:
            loader_error = exc

        try:
            self.operation_files.cleanup()
        except Exception as exc:
            if loader_error is not None:
                self.callbacks.log(f"operation 文件清理失败: {exc}")
                raise loader_error from exc
            raise

        if loader_error is not None:
            raise loader_error

    def push_misc(self, misc: Path) -> str:
        assert self.adb is not None
        remote = f"{PartitionOps.REMOTE_TMP}/misc.img"
        self.adb.remove_remote(remote)
        self.adb.push(misc, remote)
        return remote

    def _begin_wait(
        self,
        *,
        target: WaitTarget,
        title: str,
        detail: str,
        timeout: float,
        serial: str,
        expected_mode: str = "",
    ) -> float:
        deadline = time.monotonic() + timeout
        self.callbacks.activity(ActivityEvent(
            ActivityKind.WAITING, title, detail, deadline=deadline,
            target=target, serial=serial, expected_mode=expected_mode,
        ))
        return deadline

    def _report_wait_tick(
        self,
        *,
        target: WaitTarget,
        title: str,
        detail: str,
        deadline: float,
        serial: str,
        expected_mode: str = "",
    ) -> None:
        self.callbacks.activity(ActivityEvent(
            ActivityKind.WAITING, title, detail, deadline=deadline,
            target=target, serial=serial, expected_mode=expected_mode,
        ))

    def _finish_wait(
        self,
        *,
        target: WaitTarget,
        title: str,
        detail: str,
        serial: str,
        expected_mode: str = "",
    ) -> None:
        self.callbacks.activity(ActivityEvent(
            ActivityKind.SUCCESS, title, detail, target=target,
            serial=serial, expected_mode=expected_mode,
        ))

    def _timeout_wait(
        self,
        *,
        target: WaitTarget,
        title: str,
        detail: str,
        serial: str,
        expected_mode: str = "",
    ) -> None:
        self.callbacks.activity(ActivityEvent(
            ActivityKind.TIMEOUT, title, detail, target=target,
            serial=serial, expected_mode=expected_mode,
        ))

    def _wait_exact_fastboot_mode(
        self,
        expected_mode: str,
        *,
        timeout: float,
        phase: str,
    ) -> None:
        assert self.adb is not None
        assert self.fastboot is not None
        target = self.fastboot.serial
        wait_target = WaitTarget.FASTBOOTD if expected_mode == "fastbootd" else WaitTarget.BOOTLOADER
        deadline = self._begin_wait(
            target=wait_target, title=phase, detail="等待目标设备进入指定 Fastboot 模式",
            timeout=timeout, serial=target, expected_mode=expected_mode,
        )
        last_tick = time.monotonic()
        while time.monotonic() < deadline:
            self.ensure_not_cancelled()
            fastboot_devices = self.fastboot.devices()
            if fastboot_devices:
                if len(fastboot_devices) != 1 or fastboot_devices[0] != target:
                    raise VerificationError(f"{phase}: fastboot 设备集合必须且只能包含当前 serial")
                mode = self.fastboot.fastboot_mode()
                if mode == expected_mode:
                    self._finish_wait(
                        target=wait_target, title=phase, detail="已确认目标 Fastboot 模式",
                        serial=target, expected_mode=expected_mode,
                    )
                    return
                raise VerificationError(
                    f"{phase}: Fastboot 模式错误，要求 {expected_mode}，实际 {mode}"
                )
            if target in self.adb.devices():
                raise VerificationError(f"{phase}: 设备返回 ADB，已终止运行")
            if time.monotonic() - last_tick >= 1.0:
                self._report_wait_tick(
                    target=wait_target, title=phase, detail="仍未发现目标设备，继续等待",
                    deadline=deadline, serial=target, expected_mode=expected_mode,
                )
                last_tick = time.monotonic()
            self.cancellable_sleep(2.0)
        self._timeout_wait(
            target=wait_target, title=phase, detail="等待目标 Fastboot 模式超时",
            serial=target, expected_mode=expected_mode,
        )
        raise VerificationError(f"{phase}: 等待 {expected_mode} 超时")

    def reboot_to_fastbootd_then_bootloader(self, timeout: float = 180.0) -> None:
        """Require fastbootd after the slot switch, with one bootloader fallback."""
        assert self.adb is not None
        assert self.fastboot is not None
        self.set_stage("重启并等待 fastbootd…")
        try:
            self.adb.reboot("bootloader")
        except Exception as exc:
            raise VerificationError(
                "对槽写入已完成，但无法请求设备重启至 fastbootd；"
                "已停止，未执行后续 fastboot 操作。"
            ) from exc

        target = self.fastboot.serial
        deadline = self._begin_wait(
            target=WaitTarget.FASTBOOTD, title="等待 fastbootd", detail="等待设备进入 fastbootd",
            timeout=timeout, serial=target, expected_mode="fastbootd",
        )
        last_tick = time.monotonic()
        fallback_attempted = False
        while time.monotonic() < deadline:
            self.ensure_not_cancelled()
            fastboot_devices = self.fastboot.devices()
            if fastboot_devices:
                if len(fastboot_devices) != 1 or fastboot_devices[0] != target:
                    raise VerificationError(
                        "对槽写入后 fastboot 设备集合必须且只能包含当前 serial"
                    )
                mode = self.fastboot.fastboot_mode()
                if mode == "fastbootd":
                    self._finish_wait(
                        target=WaitTarget.FASTBOOTD, title="等待 fastbootd", detail="已确认 fastbootd",
                        serial=target, expected_mode="fastbootd",
                    )
                    break
                if mode == "bootloader":
                    if not fallback_attempted:
                        fallback_attempted = True
                        self.set_stage(
                            "检测到 bootloader Fastboot，尝试 fastboot reboot fastboot…"
                        )
                        self.fastboot.reboot_fastboot()
                        self.cancellable_sleep(2.0)
                        continue
                    raise VerificationError(
                        "执行 fastboot reboot fastboot 后仍处于 bootloader Fastboot；"
                        "已终止运行。"
                    )
                raise VerificationError(
                    "检测到 Fastboot，但无法确认 is-userspace=yes 的 fastbootd；"
                    "已终止运行。"
                )
            if target in self.adb.devices():
                raise VerificationError(
                    "对槽写入后设备进入了 Android/ADB，而非 fastbootd；"
                    "已停止，未执行后续 fastboot 操作。请检查 misc 和槽位状态后手动恢复。"
                )
            if time.monotonic() - last_tick >= 1.0:
                self._report_wait_tick(
                    target=WaitTarget.FASTBOOTD, title="等待 fastbootd", detail="仍未发现目标设备，继续等待",
                    deadline=deadline, serial=target, expected_mode="fastbootd",
                )
                last_tick = time.monotonic()
            self.cancellable_sleep(2.0)
        else:
            self._timeout_wait(
                target=WaitTarget.FASTBOOTD, title="等待 fastbootd", detail="等待 fastbootd 超时",
                serial=target, expected_mode="fastbootd",
            )
            raise VerificationError(
                "等待 fastbootd 超时；已停止，未执行后续 fastboot 操作。"
                "请勿继续刷写，检查设备状态后手动进入恢复流程。"
            )

        self.set_stage("已确认 fastbootd，切换至 bootloader Fastboot…")
        self.fastboot.reboot_bootloader()
        self.cancellable_sleep(2.0)
        self._wait_exact_fastboot_mode(
            "bootloader",
            timeout=timeout,
            phase="fastbootd 切换 bootloader",
        )



    def wait_android_ready(self, timeout: float = 900.0) -> None:
        assert self.adb is not None
        self.set_stage("等待 Android 启动完成…")
        target = self.adb.serial
        deadline = self._begin_wait(
            target=WaitTarget.ANDROID_READY, title="等待 Android 就绪", detail="等待 Android 启动完成",
            timeout=timeout, serial=target,
        )
        last_tick = time.monotonic()
        while time.monotonic() < deadline:
            self.ensure_not_cancelled()
            if target in self.adb.devices():
                boot_completed = self.adb.getprop("sys.boot_completed").strip()
                package_manager = self.adb.shell("cmd package path android", timeout=15, check=False)
                storage = self.adb.shell(
                    "test -d /sdcard && ls /sdcard >/dev/null 2>&1",
                    timeout=15,
                    check=False,
                )
                if (
                    boot_completed == "1"
                    and package_manager.returncode == 0
                    and "package:" in package_manager.stdout
                    and storage.returncode == 0
                ):
                    self.set_stage("Android 已就绪，等待系统稳定…")
                    self.cancellable_sleep(5.0)
                    self._finish_wait(
                        target=WaitTarget.ANDROID_READY, title="等待 Android 就绪", detail="Android 已就绪",
                        serial=target,
                    )
                    return
            if time.monotonic() - last_tick >= 1.0:
                self._report_wait_tick(
                    target=WaitTarget.ANDROID_READY, title="等待 Android 就绪", detail="仍未发现目标设备，继续等待",
                    deadline=deadline, serial=target,
                )
                last_tick = time.monotonic()
            self.cancellable_sleep(2.0)
        self._timeout_wait(
            target=WaitTarget.ANDROID_READY, title="等待 Android 就绪", detail="等待 Android 就绪超时",
            serial=target,
        )
        raise VerificationError(f"等待 Android 就绪超时: {target or '任意设备'}")

    def reboot_to_fastboot(self) -> None:
        assert self.adb is not None
        # 优先使用 reboot bootloader；某些平台该参数无效则回退 reboot
        try:
            self.adb.reboot("bootloader")
        except Exception:
            try:
                self.adb.reboot()
            except Exception:
                pass
        self.cancellable_sleep(3)
        assert self.fastboot is not None
        try:
            self.fastboot.wait_for_device(timeout=180)
        except VerificationError as exc:
            raise VerificationError(
                "写入已完成，但未检测到目标设备进入 fastboot。"
                "请手动进入 bootloader 后继续下一阶段。"
            ) from exc

    def wait_fastboot(self, timeout: float = 120.0) -> None:
        assert self.fastboot is not None
        target = self.fastboot.serial
        deadline = self._begin_wait(
            target=WaitTarget.FASTBOOT, title="等待 fastboot", detail="等待目标 Fastboot 设备",
            timeout=timeout, serial=target,
        )
        last_tick = time.monotonic()
        while time.monotonic() < deadline:
            self.ensure_not_cancelled()
            slice_timeout = min(10.0, deadline - time.monotonic())
            try:
                self.fastboot.wait_for_device(timeout=max(1.0, slice_timeout))
                self._finish_wait(
                    target=WaitTarget.FASTBOOT, title="等待 fastboot", detail="已确认 Fastboot 设备",
                    serial=target,
                )
                return
            except VerificationError:
                if time.monotonic() - last_tick >= 1.0:
                    self._report_wait_tick(
                        target=WaitTarget.FASTBOOT, title="等待 fastboot", detail="仍未发现目标设备，继续等待",
                        deadline=deadline, serial=target,
                    )
                    last_tick = time.monotonic()
                continue
        self._timeout_wait(
            target=WaitTarget.FASTBOOT, title="等待 fastboot", detail="等待 fastboot 超时",
            serial=target,
        )
        raise VerificationError(f"等待 fastboot 超时: {target or '任意设备'}")

    def finalize_dual_lk_fastboot(self, image: Path, active_slot: str) -> None:
        assert self.fastboot is not None
        self.wait_fastboot()
        self.fastboot.flash_dual_lk(image)
        letter = active_slot.lstrip("_")
        try:
            self.fastboot.set_active(letter)
        except Exception as exc:
            # set_active 失败不影响 flash 结果，用户可手动 set，但仍需可见告警
            self.callbacks.log(f"set_active {letter} 失败: {exc}")
        self.fastboot.reboot()
# provenance: NBMTK-canary-7f3a91c4e8b2d05f
