# SPDX-FileCopyrightText: 2026 雾月星辰 & MLXC <github@XingChenRS>
# SPDX-License-Identifier: AGPL-3.0-only
"""Main application window."""

from __future__ import annotations

import json
import uuid
import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QProcess, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPalette, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config.store import SettingsStore
from ..core.activity import ActivityEvent, ActivityKind
from ..core.adb import AdbClient
from ..core.device import ConnectedDevice, DeviceDetector, DeviceProfile
from ..core.env import EnvChecker
from ..core.errors import EnvError, VerificationError
from ..core.logging import (
    SessionLog,
    compact_log_message,
    format_device_summary,
)
from ..core.slot import SlotManager
from ..core.workspace import WorkspaceManager
from ..core.operation_files import VerifiedFileSnapshot
from ..flows.generic_partition import (
    BackupSelectedRequest,
    ChecklistRequest,
    GenericCallbacks,
    GenericPartitionFacade,
    InventoryRequest,
)
from ..flows.vivo_dyn import DynCaptureRequest, DynRestoreRequest, VivoDynFacade
from ..sakisu import SakisuPatchFlow, SakisuPatchRequest
from .device_scan_worker import DeviceScanResult, DeviceScanWorker
from .generic_partition_worker import GenericPartitionWorker
from .file_snapshot_worker import FileSnapshotWorker
from .activity_panel import ActivityPanel
from .action_request import DestructiveFlowRequest, DeviceActionContext
from .destructive_dialog import DestructiveDialog
from .pages import (
    AdvancedPage,
    AuthorizedFeaturesPage,
    DevicePage,
    ImagesBrandsPage,
    PartitionToolsPage,
    DestructiveSummary,
)
from .presentation import DeviceSnapshot, ProgressSnapshot, online_stylesheet
from .windows_backdrop import NativeWindowAppearance, schedule_native_window_chrome
from .reboot_worker import RebootWorker
from .sakisu_worker import SakisuPatchWorker
from .settings_dialog import SettingsDialog
from .shell import OnlineShell
from .window_frame import IntegratedWindowSurface, native_windows_resize_hit
from .vivo_dyn_worker import VivoDynWorker
from .worker import CustomSuProbeWorker, FlowWorker, TerminalWorker, UtilityWorker

LOG_VIEW_MAX_BLOCKS = 5000

FLOW_DISPLAY_NAMES = {
    "root_escalation": "临时提权",
    "backup_export": "备份",
    "write_inactive": "写入对槽",
    "patch_lk": "LK 修补",
    "oppo_unlock": "完整解锁 BL",
    "vivo_xingc_unlock": "Xingc 解锁",
    "vivo_xingc_lock": "Xingc 回锁",
    "dual_slot_sync": "双槽同步",
    "dual_slot_apply": "完整解锁 BL",
    "restore_original_lk": "还原双槽原 LK",
    "honor_prepare": "准备解锁",
    "honor_complete": "完整解锁 BL",
    "vivo_complete_unlock": "Vivo 完整解锁",
}

DESTRUCTIVE_ACTION_TEXT = {
    "write_inactive": "写入非活动槽位",
    "dual_slot_sync": "同步双槽 LK",
    "dual_slot_apply": "执行完整解锁步骤",
    "restore_original_lk": "还原双槽原 LK",
    "honor_prepare": "准备解锁",
    "honor_complete": "完整解锁 BL",
    "oppo_unlock": "执行 OPPO 解锁",
    "vivo_complete_unlock": "擦除数据并继续 Vivo 解锁",
    "vivo_xingc_lock": "执行 Xingc 回锁",
}


def _safe_diagnostic_error_message(value: object) -> str:
    return compact_log_message(value)


class MainWindow(QMainWindow):
    diagnostic_received = Signal(str)

    def __init__(
        self,
        project_root: Path,
        workspace_root: Path,
        *,
        settings_store: SettingsStore | None = None,
    ) -> None:
        super().__init__()
        self.project_root = project_root
        self.workspace_root = workspace_root
        self.settings_store = settings_store or SettingsStore()
        self.settings = self.settings_store.load()
        self.workspace = WorkspaceManager(workspace_root)
        self.worker: FlowWorker | None = None
        self.utility_worker: UtilityWorker | None = None
        self.terminal_worker: TerminalWorker | None = None
        self.reboot_worker: RebootWorker | None = None
        self.generic_worker: GenericPartitionWorker | None = None
        self.dyn_worker: VivoDynWorker | None = None
        self.sakisu_worker: SakisuPatchWorker | None = None
        self.su_probe_worker: CustomSuProbeWorker | None = None
        self.file_snapshot_worker: FileSnapshotWorker | None = None
        self._snapshot_result: VerifiedFileSnapshot | None = None
        self._snapshot_continuation: Callable[
            [VerifiedFileSnapshot, DeviceActionContext | None], None
        ] | None = None
        self._snapshot_device_context: DeviceActionContext | None = None
        # The partition checklist reuses the list the operator already read over
        # ADB: partition sizes do not change with the transport, so one read per
        # serial serves both modes.
        self._partition_inventory: tuple[str, object] | None = None
        self.scan_worker: DeviceScanWorker | None = None
        self._validated_custom_su = ""
        self.device_scan_timer = QTimer(self)
        self.device_scan_timer.setInterval(2000)
        self._flow_active = False
        self._flow_cancelled = False
        self._scan_active = False
        self._closing = False
        self._refresh_pending = False
        self._scan_generation = 0
        self._current_serial = ""
        self._current_mode = ""
        self._context_serial = ""
        self._active_flow_label = ""
        self._flow_session_log: SessionLog | None = None
        self._profiles_cache: dict[str, DeviceProfile] = {}
        self._runtime_representative_by_model: dict[str, str] = {}
        # Test mode lets the operator pick the template by hand, because
        # automatic detection is known to be wrong on some models.  Once they
        # have, a scan result must not quietly put it back: that changed the
        # capability set underneath them and greyed out actions they had just
        # made ready.
        self._model_chosen_by_operator = False
        # Whether the patched-LK output path is still the name suggested for the
        # current source, and so may be re-suggested when the source changes.
        self._output_lk_is_derived = True
        self._last_scan_signature: tuple[object, ...] | None = None
        self._last_activity_key: tuple[object, ...] | None = None
        self._last_progress_value: int | None = None
        self._confirmed_test_mode_fingerprint: tuple[str, str, str, str] | None = None
        self._last_scan_result: DeviceScanResult | None = None

        self.setWindowTitle("NEBUMTK · MTK TOOLBOX")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setAutoFillBackground(True)
        window_palette = self.palette()
        window_palette.setColor(QPalette.ColorRole.Window, QColor("#F7F2E9"))
        window_palette.setColor(QPalette.ColorRole.Base, QColor("#F7F2E9"))
        self.setPalette(window_palette)
        self.resize(1360, 820)
        self.setMinimumSize(1024, 680)
        schedule_native_window_chrome(self, NativeWindowAppearance.frameless())

        self._xingc_token = ""
        self._xingc_payload = b""
        self._xingc_serial = ""
        self._utility_device_bound = False
        self._utility_serial = ""
        self._utility_clear_xingc_on_finish = False

        self._install_online_pages()
        self.diagnostic_received.connect(self._append_log)

        # signals
        self.refresh_button.clicked.connect(lambda: self.refresh_devices(auto=False))
        self.device_scan_timer.timeout.connect(lambda: self.refresh_devices(auto=True))
        self.activity_panel.cancel_requested.connect(self.cancel_flow)
        self.settings_button.clicked.connect(self.open_settings)
        self.open_backup_button.clicked.connect(self.open_backup_dir)
        self.open_logs_button.clicked.connect(self.open_logs_dir)
        self.advanced_page.open_workspace_button.clicked.connect(self.open_workspace_dir)
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        # `activated` fires only for operator interaction, never for the
        # programmatic `setCurrentIndex` that automatic matching uses, so it is
        # the one honest signal that the template was chosen by hand.
        self.model_combo.activated.connect(self._mark_model_chosen_by_operator)
        self.runtime_combo.currentIndexChanged.connect(self._on_runtime_changed)
        self.patched_lk_browse.clicked.connect(self.browse_patched_lk)
        self.original_lk_browse.clicked.connect(self.browse_original_lk)
        self.source_lk_browse.clicked.connect(self.browse_source_lk)
        self.output_lk_browse.clicked.connect(self.browse_output_lk)
        self.inspect_lk_button.clicked.connect(self.inspect_external_lk)
        self.root_button.clicked.connect(lambda: self._start_common("root_escalation"))
        self.backup_button.clicked.connect(lambda: self._start_common("backup_export"))
        self.patch_lk_button.clicked.connect(self._start_patch_lk)
        self.write_button.clicked.connect(lambda: self._start_write())
        self.auto_flow_button.clicked.connect(self._start_auto_flow)
        self.oppo_unlock_button.clicked.connect(lambda: self._start_brand_flow("oppo", "oppo_unlock"))
        self.authorized_page.honor_auto_button.clicked.connect(self._start_honor_complete)
        self.vivo_xingc_button.clicked.connect(self._start_vivo_xingc_unlock)
        self.dual_slot_sync_button.clicked.connect(self._start_dual_slot_sync)
        self.original_lk_sync_button.clicked.connect(self._start_restore_original_lk)
        self.authorized_page.honor_unlock_button.clicked.connect(self._start_honor_complete)
        self.authorized_page.reboot_action_button.clicked.connect(
            lambda: self._start_reboot("system")
        )
        self.xingc_token_button.clicked.connect(self._xingc_read_token)
        self.xingc_sign_button.clicked.connect(self._xingc_sign_token)
        self.xingc_auth_button.clicked.connect(self._xingc_authorize)
        self.xingc_lock_button.clicked.connect(self._xingc_lock)
        self.cmd_run_button.clicked.connect(self.open_platform_tools_terminal)
        self.cmd_edit.returnPressed.connect(self.run_cmd)
        self.reboot_system_button.clicked.connect(lambda: self._start_reboot("system"))
        self.reboot_fastbootd_button.clicked.connect(lambda: self._start_reboot("fastbootd"))
        self.reboot_bootloader_button.clicked.connect(lambda: self._start_reboot("bootloader"))
        self.reboot_recovery_button.clicked.connect(lambda: self._start_reboot("recovery"))
        self.device_page.diagnostics_button.clicked.connect(self._show_safe_device_diagnostics)
        self.partition_page.inventory_button.clicked.connect(self._start_partition_inventory)
        self.partition_page.backup_selected_button.clicked.connect(self._start_partition_backup)
        self.partition_page.run_checklist_button.clicked.connect(self._start_partition_checklist)
        self.partition_page.import_manifest_button.clicked.connect(self._import_partition_manifest)
        self.images_page.dyn_backup_browse.clicked.connect(self._browse_dyn_backup)
        self.images_page.dyn_capture_button.clicked.connect(self._start_dyn_capture)
        self.images_page.dyn_restore_button.clicked.connect(self._start_dyn_restore)
        self.images_page.sakisu_source_browse.clicked.connect(self._browse_sakisu_source)
        self.images_page.sakisu_patch_button.clicked.connect(self._start_sakisu_patch)
        self.advanced_page.custom_su_validate_button.clicked.connect(self._start_custom_su_probe)

        self._load_models()
        self._apply_test_mode_state(announce=False)
        self._update_default_preload()
        self.refresh_devices(auto=True)
        self.device_scan_timer.start()

    def _build_top_bar(self) -> QFrame:
        """Persistent bar above the pages: current device, multi-device reserve,
        and (in test mode) the manual model/runtime template selectors."""

        bar = QFrame()
        bar.setObjectName("globalTopBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(13, 6, 13, 6)
        row.setSpacing(10)
        self.top_current_device_label = QLabel("当前设备：未连接")
        self.top_current_device_label.setObjectName("topCurrentDevice")
        row.addWidget(self.top_current_device_label)
        self.top_multidevice_label = QLabel("多设备 / 工作区：后续支持")
        self.top_multidevice_label.setObjectName("topMultiDevicePlaceholder")
        self.top_multidevice_label.setEnabled(False)
        row.addWidget(self.top_multidevice_label)
        row.addStretch(1)
        self.top_test_selectors = QWidget()
        selectors = QHBoxLayout(self.top_test_selectors)
        selectors.setContentsMargins(0, 0, 0, 0)
        selectors.setSpacing(8)
        # Test mode relaxes brand matching on every page, so the marker has to sit
        # in the always-visible bar. The only other trace was an empty label on
        # the settings page, which left the operator reading relaxed gating as a
        # bug in the page they were looking at.
        self.top_test_mode_label = QLabel("测试模式")
        self.top_test_mode_label.setToolTip(
            "已开启测试模式：机型匹配与品牌限制已放宽，界面会显示当前机型的常规操作。"
            "关闭路径：工具与设置 → 测试模式。"
        )
        selectors.addWidget(self.top_test_mode_label)
        selectors.addWidget(QLabel("机型模板"))
        self._top_selectors_layout = selectors
        row.addWidget(self.top_test_selectors)
        self.top_test_selectors.setVisible(False)
        return bar

    def _install_online_pages(self) -> None:
        """Install page-owned controls and retain only orchestration aliases."""

        self.device_page = DevicePage()
        self.partition_page = PartitionToolsPage()
        self.authorized_page = AuthorizedFeaturesPage()
        self.images_page = ImagesBrandsPage()
        self.advanced_page = AdvancedPage()
        self.activity_panel = ActivityPanel()
        self.shell = OnlineShell(
            {
                "device": self.device_page,
                "partitions": self.partition_page,
                "authorized": self.authorized_page,
                "images": self.images_page,
                "advanced": self.advanced_page,
            },
            self.activity_panel,
        )
        self.setStyleSheet(online_stylesheet())
        self.shell.setMinimumHeight(0)
        self.top_bar = self._build_top_bar()
        content_container = QWidget()
        content_layout = QVBoxLayout(content_container)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.addWidget(self.top_bar)
        content_layout.addWidget(self.shell, 1)
        self.window_surface = IntegratedWindowSurface(content_container, self)
        self.title_bar = self.window_surface.title_bar
        self.setCentralWidget(self.window_surface)

        self.pages = self.shell.pages
        self.page_widgets = self.shell.page_widgets
        self.page_scroll_areas = self.shell.page_scroll_areas
        self.nav_group = self.shell.nav_group
        self.nav_buttons = self.shell.nav_buttons
        self.activity_toggle_button = self.activity_panel.toggle_button
        self.activity_collapse_button = self.activity_panel.toggle_button

        device = self.device_page
        self.status_label = device.connection_status
        self.model_combo = device.model_combo
        self.refresh_button = device.refresh_button
        self.backup_path_label = device.backup_path_label
        self.current_operation_panel = device.current_operation_panel
        self.detected_connection_label = device.detected_connection_label
        self.detected_identity_label = device.detected_identity_label
        self.detected_firmware_label = device.detected_firmware_label
        self.detected_slot_label = device.detected_slot_label
        self.detected_profile_label = device.detected_profile_label
        self.reboot_system_button = device.reboot_buttons["system"]
        self.reboot_fastbootd_button = device.reboot_buttons["fastbootd"]
        self.reboot_bootloader_button = device.reboot_buttons["bootloader"]
        self.reboot_recovery_button = device.reboot_buttons["recovery"]

        authorized = self.authorized_page
        self.root_button = authorized.root_button
        self.backup_button = authorized.backup_button
        self.preload_edit = authorized.preload_edit
        self.preload_browse = authorized.preload_browse
        self.vivo_xingc_button = authorized.vivo_xingc_button
        self.xingc_token_button = authorized.xingc_token_button
        self.xingc_sign_button = authorized.xingc_sign_button
        self.xingc_auth_button = authorized.xingc_auth_button
        self.xingc_lock_button = authorized.xingc_lock_button

        self.write_button = authorized.write_button
        self.auto_flow_button = authorized.auto_flow_button
        self.honor_auto_button = authorized.honor_auto_button
        self.oppo_unlock_button = authorized.oppo_unlock_button
        self.honor_unlock_button = authorized.honor_unlock_button
        self.unlock_reboot_button = authorized.reboot_action_button
        self.dual_slot_sync_button = authorized.dual_slot_sync_button
        self.patched_lk_edit = authorized.patched_lk_edit
        self.patched_lk_browse = authorized.patched_lk_browse
        self.fastboot_patched_lk_edit = authorized.fastboot_patched_lk_edit
        self.fastboot_patched_lk_browse = authorized.fastboot_patched_lk_browse
        self.original_lk_edit = authorized.original_lk_edit
        self.original_lk_browse = authorized.original_lk_browse
        self.original_lk_sync_button = authorized.original_lk_sync_button
        self.sakisu_root_checkbox = authorized.sakisu_root_checkbox
        self.vivo_preflight_label = authorized.vivo_preflight_label

        images = self.images_page
        self.patch_lk_button = images.patch_lk_button
        self.inspect_lk_button = images.inspect_lk_button
        self.source_lk_edit = images.source_lk_edit
        self.source_lk_browse = images.source_lk_browse
        self.output_lk_edit = images.output_lk_edit
        self.output_lk_browse = images.output_lk_browse
        self.local_lk_profile_combo = images.local_lk_profile_combo
        self.image_status_label = images.image_status_label

        advanced = self.advanced_page
        self.runtime_combo = advanced.runtime_combo
        self.test_mode_banner = advanced.test_mode_banner
        self.test_runtime_panel = advanced.test_runtime_panel
        self._top_selectors_layout.addWidget(self.model_combo)
        self._top_selectors_layout.addWidget(QLabel("运行模板"))
        self._top_selectors_layout.addWidget(self.runtime_combo)
        self.model_combo.setVisible(True)
        self.runtime_combo.setVisible(True)
        self.settings_button = advanced.settings_button
        self.open_backup_button = advanced.open_backup_button
        self.open_logs_button = advanced.open_logs_button
        self.workspace_path_label = advanced.workspace_label
        self.cmd_edit = advanced.terminal_edit
        self.cmd_run_button = advanced.terminal_run_button
        self.advanced_page.custom_su_edit.setText(self.settings.custom_su)
        self.advanced_page.custom_su_edit.textChanged.connect(
            self._invalidate_custom_su_validation
        )

        activity = self.activity_panel
        self.log_file_label = activity.log_file_label
        self.log_view = activity.log_view
        self.activity_stage_label = activity.stage_label
        self.activity_progress = activity.progress
        self.cancel_button = activity.cancel_button
        self.progress_bar = self.partition_page.progress_bar
        self.step_label = self.partition_page.progress_label

    def _invalidate_custom_su_validation(self, _value: str = "") -> None:
        self._validated_custom_su = ""
        self._set_inline_status(
            self.advanced_page.custom_su_status,
            "Not validated for the current exact value.",
            "warning",
        )

    def _set_inline_status(self, label: QLabel, text: str, state: str) -> None:
        label.setText(text)
        label.setProperty("state", state)
        label.style().unpolish(label)
        label.style().polish(label)

    def _show_safe_device_diagnostics(self) -> None:
        """Report bounded, already-scanned basic facts plus a profile-match verdict.

        No additional device probing is performed. Fields that cannot be
        confirmed from the last scan are reported as unknown rather than
        fabricated as healthy.
        """

        details = (
            self.detected_connection_label.text(),
            self.detected_identity_label.text(),
            self.detected_firmware_label.text(),
            self.detected_slot_label.text(),
            self.detected_profile_label.text(),
        )
        self.device_page.diagnostics_output.setPlainText("\n".join(details)[:1024])
        result = self._last_scan_result
        mode = self._current_mode
        if not self._current_serial or result is None:
            values = ("未检测", "未知", "未知", "未知")
            state = "连接设备后自动读取"
        else:
            if mode == "fastboot":
                connection = "已连接 · Fastboot"
            elif mode == "adb":
                connection = "已连接 · ADB"
            else:
                connection = "已连接"
            if mode == "adb":
                authorized = "未授权" if bool(getattr(result, "scan_error", False)) else "已授权"
            else:
                authorized = "不适用"
            profile_key = str(getattr(result, "profile_key", "") or "")
            matched = self._get_profile(profile_key) if profile_key else None
            if matched is not None:
                match = f"匹配 · {matched.display_name}"
            else:
                match = "未匹配（可在测试模式手动选型）"
            values = (connection, authorized, match, mode.upper() if mode else "未知")
            state = "已读取基本信息"
        for label, value in zip(self.device_page.diagnostic_values, values, strict=True):
            label.setText(value)
        self.device_page.diagnostics_state.setText(state)

    def _current_profile_device(self, *, mode: str) -> tuple[DeviceProfile, ConnectedDevice] | None:
        if self._operation_active():
            return None
        if self._current_mode != mode or not self._current_serial:
            self._set_inline_status(
                self.partition_page.validation_status,
                f"Current operation requires an exact {mode.upper()} device.",
                "error",
            )
            return None
        profile = self._get_profile(str(self.model_combo.currentData() or ""))
        if profile is None:
            self._set_inline_status(
                self.partition_page.validation_status,
                "Select an explicit supported model profile.",
                "error",
            )
            return None
        result = self._last_scan_result
        if mode == "adb" and (
            result is None
            or result.serial != self._current_serial
            or result.mode != "adb"
        ):
            self._set_inline_status(
                self.partition_page.validation_status,
                "Rescan the exact ADB device before starting this operation.",
                "error",
            )
            return None
        device = ConnectedDevice(
            serial=self._current_serial,
            brand=(result.brand if result is not None else profile.brand),
            model=(result.model if result is not None else ""),
            market_name=(result.market_name if result is not None else ""),
            device=(result.device if result is not None else ""),
            firmware=(result.firmware if result is not None else ""),
            android_version=(result.android_version if result is not None else ""),
            build_incremental=(result.build_incremental if result is not None else ""),
            security_patch=(result.security_patch if result is not None else ""),
            platform=(result.platform if result is not None else ""),
            manufacturer=(result.manufacturer if result is not None else ""),
            device_fingerprint=(result.device_fingerprint if result is not None else ""),
            slot_suffix=(result.slot_suffix if result is not None else ""),
            bootloader_state=(result.bootloader_state if result is not None else "unknown"),
            profile=profile,
        )
        return profile, device

    def _selected_local_su(self, profile: DeviceProfile) -> str | None:
        provider = str(self.advanced_page.su_provider_combo.currentData() or "existing")
        if provider == "existing":
            return profile.su_path
        value = self.advanced_page.custom_su_edit.text().strip()
        if value != self._validated_custom_su:
            self._set_inline_status(
                self.advanced_page.custom_su_status,
                "Validate this exact custom su value before use.",
                "error",
            )
            return None
        return value

    def _start_generic_worker(
        self, request: object, label: str, *, inventory: object | None = None
    ) -> None:
        facade = GenericPartitionFacade(
            self.project_root,
            self.workspace_root,
            self.settings,
            GenericCallbacks(log=lambda message: self.diagnostic_received.emit(message)),
        )
        worker = GenericPartitionWorker(
            facade, request, inventory=inventory, parent=self
        )
        self.generic_worker = worker
        self._flow_active = True
        self._pause_device_scanning()
        self._set_busy(True)
        self.activity_panel.apply_progress(
            ProgressSnapshot.waiting(label, cancellable=True)
        )
        worker.result_ready.connect(self._on_generic_result)
        worker.finished_error.connect(
            lambda error: self._on_typed_worker_error("Partition operation", error)
        )
        worker.cancelled_signal.connect(self._on_cancelled)
        worker.finished.connect(
            lambda worker=worker: self._finish_aux_worker("generic_worker", worker)
        )
        worker.start()

    def _start_partition_inventory(self) -> None:
        current = self._current_profile_device(mode="adb")
        if current is None:
            return
        profile, device = current
        executable = self._selected_local_su(profile)
        if executable is None:
            return
        self._start_generic_worker(
            InventoryRequest(device, profile, executable),
            "Reading typed partition inventory…",
        )

    def _selected_inventory_names(self) -> tuple[str, ...]:
        return self.partition_page.selected_inventory_names()

    def _start_partition_backup(self) -> None:
        current = self._current_profile_device(mode="adb")
        if current is None:
            return
        profile, device = current
        executable = self._selected_local_su(profile)
        selected = self._selected_inventory_names()
        if executable is None or not selected:
            self._set_inline_status(
                self.partition_page.validation_status,
                "Select at least one inventory row and a validated su provider.",
                "error",
            )
            return
        sizes = {
            self.partition_page.inventory_table.item(row, 1).text(): int(
                self.partition_page.inventory_table.item(row, 5).data(Qt.UserRole) or 0
            )
            for row in range(self.partition_page.inventory_table.rowCount())
        }
        total = sum(sizes.get(name, 0) for name in selected)
        if total >= self.partition_page.LARGE_PARTITION_BYTES:
            reply = QMessageBox.question(
                self,
                "Confirm large partition backup",
                f"Device serial: {device.serial}\nCurrent mode: ADB\n"
                f"Exact partitions: {', '.join(selected)}\nTotal device-read size: {total} bytes\n"
                "This operation is cancellable between partition reads.",
            )
            if not self._is_yes_reply(reply):
                return
        self._start_generic_worker(
            BackupSelectedRequest(device, profile, executable, selected),
            "Backing up selected partitions…",
        )

    def _start_partition_checklist(self) -> None:
        mode = str(self._current_mode or "").strip()
        if mode not in {"adb", "fastboot"}:
            self._set_inline_status(
                self.partition_page.validation_status,
                "分区清单需要已确认的 ADB 或 Fastboot 设备。",
                "error",
            )
            return
        current = self._current_profile_device(mode=mode)
        if current is None:
            return
        profile, device = current
        try:
            items = self.partition_page.checklist_items()
        except Exception as exc:
            self._set_inline_status(
                self.partition_page.validation_status, str(exc), "error"
            )
            return
        executable = ""
        if mode == "adb":
            executable = self._selected_local_su(profile) or ""
            if not executable:
                return
        inventory = None
        if self._partition_inventory is not None:
            cached_serial, cached = self._partition_inventory
            if cached_serial == str(device.serial):
                inventory = cached
        if inventory is None:
            self._set_inline_status(
                self.partition_page.validation_status,
                "请先用 ADB 读一次分区清单；同一序列号下 Fastboot 通用。",
                "error",
            )
            return
        reply = QMessageBox.question(
            self,
            "确认分区清单",
            f"设备: {device.serial}\n模式: {mode.upper()}\n\n"
            + "\n".join(f"· {item.label}" for item in items)
            + "\n\n每步开始前都可中断；已写入的分区不会回滚。确认开始？",
        )
        if not self._is_yes_reply(reply):
            return
        self._start_generic_worker(
            ChecklistRequest(device, profile, mode, tuple(items), executable),
            "Running partition checklist…",
            inventory=inventory,
        )

    def _import_partition_manifest(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择备份 manifest",
            str(Path.home() / "Desktop"),
            "backup-manifest.json (backup-manifest.json)",
        )
        if not path:
            return
        manifest_path = Path(path)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            partitions = manifest["partitions"]
            if not isinstance(partitions, dict):
                raise ValueError("manifest 的 partitions 不是表")
        except Exception as exc:
            self._set_inline_status(
                self.partition_page.validation_status,
                f"manifest 无效: {exc}",
                "error",
            )
            return
        imported = 0
        missing: list[str] = []
        for name, record in partitions.items():
            filename = record.get("file") if isinstance(record, dict) else None
            if not filename:
                continue
            image = manifest_path.parent / str(filename)
            if image.is_file():
                self.partition_page.mark_write_row(str(name), image)
                imported += 1
            else:
                missing.append(str(name))
        detail = f"已导入 {imported} 个分区的镜像并勾选写入；可取消勾选或更换文件。"
        if missing:
            detail += f" 缺文件的：{', '.join(sorted(missing))}。"
        self._set_inline_status(
            self.partition_page.validation_status,
            detail,
            "success" if imported else "warning",
        )

    def _confirm_destructive(
        self,
        *,
        device: DeviceActionContext,
        source: VerifiedFileSnapshot | None,
        targets: tuple[str, ...],
        slot_effect: str,
        boundary: str,
        action_text: str,
    ) -> bool:
        if source is not None:
            size, sha256 = source.size, source.sha256
            source_text = str(source.path)
        else:
            size, sha256 = 0, "N/A (no local image)"
            source_text = "N/A (live verified state)"
        return DestructiveDialog.present(
            self,
            DestructiveSummary(
                serial=device.serial,
                mode=device.mode,
                source=source_text,
                source_size=size,
                source_sha256=sha256,
                targets=targets,
                slot_effect=slot_effect,
                cancellation_boundary=boundary,
            ),
            action_text=action_text,
        )

    def _prepare_file_snapshot(
        self,
        source: Path,
        continuation: Callable[
            [VerifiedFileSnapshot, DeviceActionContext | None], None
        ],
        *,
        device_bound: bool = True,
    ) -> None:
        if self._operation_active():
            return
        context = self._capture_device_action_context() if device_bound else None
        if device_bound and context is None:
            return
        worker = FileSnapshotWorker(source, parent=self)
        self.file_snapshot_worker = worker
        self._snapshot_result = None
        self._snapshot_continuation = continuation
        self._snapshot_device_context = context
        self._flow_active = True
        self._pause_device_scanning()
        self._set_busy(True)
        self.activity_panel.apply_progress(
            ProgressSnapshot.waiting("Verifying selected local file…", cancellable=True)
        )
        worker.result_ready.connect(self._on_file_snapshot_ready)
        worker.finished_error.connect(
            lambda error: self._on_typed_worker_error("Local file verification", error)
        )
        worker.cancelled_signal.connect(self._on_cancelled)
        worker.finished.connect(self._finish_file_snapshot_worker)
        worker.start()

    def _on_file_snapshot_ready(self, snapshot: object) -> None:
        if self.sender() is self.file_snapshot_worker and isinstance(
            snapshot, VerifiedFileSnapshot
        ):
            self._snapshot_result = snapshot

    def _finish_file_snapshot_worker(self) -> None:
        worker = self.sender()
        if worker is not self.file_snapshot_worker:
            return
        snapshot = self._snapshot_result
        continuation = self._snapshot_continuation
        context = self._snapshot_device_context
        worker.deleteLater()
        self.file_snapshot_worker = None
        self._snapshot_result = None
        self._snapshot_continuation = None
        self._snapshot_device_context = None
        self._flow_active = False
        self._reset_progress()
        self._set_busy(False)
        if self._closing:
            self._resume_pending_close()
            return
        if snapshot is not None and continuation is not None and context is None:
            continuation(snapshot, None)
        elif (
            snapshot is not None
            and continuation is not None
            and self._device_action_context_matches(context)
        ):
            continuation(snapshot, context)
        elif snapshot is not None:
            self._set_inline_status(
                self.partition_page.validation_status,
                "Device context changed while verifying the selected file.",
                "error",
            )
        if not self._operation_active() and not self._scan_active:
            self._resume_device_scanning()

    def _capture_device_action_context(self) -> DeviceActionContext | None:
        result = self._last_scan_result
        if (
            result is None
            or result.serial != self._current_serial
            or result.mode != self._current_mode
            or self._scan_active
            or result.scan_generation != self._scan_generation
        ):
            self._set_inline_status(
                self.partition_page.validation_status,
                "A fresh exact-device scan is required before this operation.",
                "error",
            )
            return None
        try:
            return DeviceActionContext(
                serial=result.serial,
                mode=result.mode,
                scan_generation=result.scan_generation,
                active_slot=result.slot_suffix,
            )
        except ValueError as exc:
            self._set_inline_status(
                self.partition_page.validation_status,
                str(exc),
                "error",
            )
            return None

    def _device_action_context_matches(self, context: DeviceActionContext) -> bool:
        result = self._last_scan_result
        return bool(
            result is not None
            and self._current_serial == context.serial == result.serial
            and self._current_mode == context.mode == result.mode
            and result.scan_generation == context.scan_generation
            and result.slot_suffix == context.active_slot
        )

    def _confirm_and_start_flow(self, request: DestructiveFlowRequest) -> None:
        self._pause_device_scanning()
        if not self._device_action_context_matches(request.device):
            self._set_inline_status(
                self.partition_page.validation_status,
                "Device context changed before destructive confirmation.",
                "error",
            )
            self._resume_device_scanning()
            return
        if not self._confirm_destructive(
            device=request.device,
            source=request.source,
            targets=request.targets,
            slot_effect=request.slot_effect,
            boundary=request.cancellation_boundary,
            action_text=DESTRUCTIVE_ACTION_TEXT.get(request.flow_kind, "执行危险操作"),
        ):
            self._resume_device_scanning()
            return
        if not self._device_action_context_matches(request.device):
            self._set_inline_status(
                self.partition_page.validation_status,
                "Device context changed during destructive confirmation.",
                "error",
            )
            self._resume_device_scanning()
            return
        kwargs: dict[str, object] = {
            "confirm": False,
            "preload_override": request.preload_override,
            "confirmed_active_slot": request.device.active_slot,
        }
        if request.flow_kind in {
            "write_inactive",
            "dual_slot_sync",
            "dual_slot_apply",
            "honor_prepare",
            "honor_complete",
        }:
            kwargs["patched_lk"] = request.artifact_ref or request.source
        elif request.flow_kind == "restore_original_lk":
            kwargs["source_lk"] = request.source
        if request.flow_kind == "vivo_complete_unlock":
            kwargs["install_sakisu_root"] = request.install_sakisu_root
            kwargs["sakisu_init_boot"] = request.sakisu_init_boot
            kwargs["sakisu_kmi"] = request.sakisu_kmi
        self._start_flow(request.flow_kind, **kwargs)



    def _start_confirmed_inactive_write(
        self,
        device,
        profile,
        executable: str,
        base: str,
        active: str,
        inactive: str,
        snapshot: VerifiedFileSnapshot,
        context: DeviceActionContext,
    ) -> None:
        if self._confirm_destructive(
            device=context,
            source=snapshot,
            targets=(f"{base}{inactive}",),
            slot_effect=f"active {active}; write inactive {inactive}",
            boundary="cancellable until the final verified dd boundary",
            action_text=f"写入 {base}{inactive}",
        ):
            self._start_generic_worker(
                WriteInactiveRequest(
                    device,
                    profile,
                    executable,
                    base,
                    snapshot,
                    context.active_slot,
                ),
                "Writing the verified inactive partner…",
            )


    def _start_confirmed_dual_flash(
        self,
        device,
        base: str,
        snapshot: VerifiedFileSnapshot,
        context: DeviceActionContext,
    ) -> None:
        if self._confirm_destructive(
            device=context,
            source=snapshot,
            targets=(f"{base}_a", f"{base}_b"),
            slot_effect="both slots written; active slot unchanged",
            boundary="cancellable until each individual flash command",
            action_text=f"写入 {base}_a 与 {base}_b",
        ):
            self._start_generic_worker(
                DualFlashRequest(device, base, snapshot, context.active_slot),
                "Writing both verified slot partners…",
            )


    def _start_confirmed_manifest_restore(
        self,
        device,
        base: str,
        snapshot: VerifiedFileSnapshot,
        context: DeviceActionContext,
    ) -> None:
        if self._confirm_destructive(
            device=context,
            source=snapshot,
            targets=(f"{base}_a", f"{base}_b"),
            slot_effect="manifest initial slot retained for explicit restore step",
            boundary="cancellable until each individual flash command",
            action_text=f"恢复 {base}_a 与 {base}_b",
        ):
            self._start_generic_worker(
                RestoreManifestRequest(
                    device,
                    base,
                    snapshot,
                    uuid.uuid4().hex,
                    context.active_slot,
                    context.scan_generation,
                ),
                "Restoring the verified manifest pair…",
            )




    def _on_generic_result(self, result: object) -> None:
        worker = self.sender()
        if worker is not self.generic_worker:
            return
        if isinstance(worker.request, InventoryRequest) and not isinstance(
            worker.request,
            BackupSelectedRequest,
        ):
            self.partition_page.set_inventory(result.entries)
            # Partition sizes do not change with the transport, so one read per
            # serial serves the fastboot checklist too.
            self._partition_inventory = (str(worker.request.device.serial), result)
            self.partition_page.set_checklist_partitions(
                [entry.name for entry in result.entries]
            )
            self._set_inline_status(
                self.partition_page.validation_status,
                f"Loaded {len(result.entries)} typed inventory entries.",
                "success",
            )
        else:
            self._set_inline_status(
                self.partition_page.validation_status,
                "Verified partition operation completed.",
                "success",
            )

    def _on_typed_worker_error(self, title: str, error: object) -> None:
        message = _safe_diagnostic_error_message(error)
        self._append_activity(f"{title} failed", key=(title, message), error=True)
        QMessageBox.critical(self, title, message)

    def _browse_dyn_backup(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Vivo dyn backup directory")
        if path:
            self.images_page.dyn_backup_edit.setText(path)

    def _start_dyn_capture(self) -> None:
        self._start_dyn_operation(restore=False)

    def _start_dyn_restore(self) -> None:
        self._start_dyn_operation(restore=True)

    def _start_dyn_operation(self, *, restore: bool) -> None:
        current = self._current_profile_device(mode="adb")
        if current is None:
            return
        profile, device = current
        if profile.brand.casefold() != "vivo":
            self._set_inline_status(
                self.images_page.image_status_label,
                "Vivo dyn operations require an explicit Vivo profile.",
                "error",
            )
            return
        if restore:
            directory = Path(self.images_page.dyn_backup_edit.text().strip())
            manifest = directory / "backup-manifest.json"
            if not manifest.is_file():
                self._set_inline_status(
                    self.images_page.image_status_label,
                    "Select a dyn backup directory containing backup-manifest.json.",
                    "error",
                )
                return
            self._prepare_file_snapshot(
                manifest,
                lambda snapshot, context: self._start_confirmed_dyn_restore(
                    device, profile, snapshot, context
                ),
            )
            return
        else:
            request = DynCaptureRequest(device, profile)
            label = "Capturing Vivo dyn backup…"
        self._launch_dyn_worker(request, label)

    def _start_confirmed_dyn_restore(
        self,
        device,
        profile,
        snapshot: VerifiedFileSnapshot,
        context: DeviceActionContext,
    ) -> None:
        if not self._confirm_destructive(
            device=context,
            source=snapshot,
            targets=("/data/vendor/secradio/dyn",),
            slot_effect="no slot change",
            boundary="cancellable before each verified remote restore boundary",
            action_text="恢复 Dyn",
        ):
            return
        self._launch_dyn_worker(
            DynRestoreRequest(device, profile, snapshot),
            "Restoring selected Vivo dyn backup…",
        )

    def _launch_dyn_worker(self, request: object, label: str) -> None:
        worker = VivoDynWorker(
            VivoDynFacade(
                self.project_root,
                self.workspace_root,
                self.settings,
            ),
            request,
            parent=self,
        )
        self.dyn_worker = worker
        self._flow_active = True
        self._pause_device_scanning()
        self._set_busy(True)
        self.activity_panel.apply_progress(ProgressSnapshot.waiting(label, cancellable=True))
        worker.result_ready.connect(
            lambda _result: self._set_inline_status(
                self.images_page.image_status_label,
                "Dyn 操作已完成。",
                "success",
            )
        )
        worker.log_message.connect(self._on_worker_log)
        worker.finished_error.connect(
            lambda message: self._on_typed_worker_error("Vivo dyn", message)
        )
        worker.cancelled_signal.connect(self._on_cancelled)
        worker.finished.connect(
            lambda worker=worker: self._finish_aux_worker("dyn_worker", worker)
        )
        worker.start()


    def _browse_sakisu_source(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select local boot or init_boot image")
        if not path:
            return
        source = Path(path)
        self.images_page.sakisu_source_edit.setText(path)
        suffix = source.suffix or ".img"
        self.images_page.sakisu_rollback_edit.setText(
            str(source.with_name(f"{source.stem}.rollback{suffix}"))
        )
        self.images_page.sakisu_candidate_edit.setText(
            str(source.with_name(f"{source.stem}.sakisu{suffix}"))
        )
        self.images_page.sakisu_receipt_edit.setText(
            str(source.with_name(f"{source.stem}.sakisu.json"))
        )

    def _start_sakisu_patch(self) -> None:
        if self._operation_active():
            return
        page = self.images_page
        source = Path(page.sakisu_source_edit.text().strip())
        rollback = Path(page.sakisu_rollback_edit.text().strip())
        candidate = Path(page.sakisu_candidate_edit.text().strip())
        receipt = Path(page.sakisu_receipt_edit.text().strip())
        if (
            not source.is_file()
            or source.stat().st_size <= 0
            or not all(str(path).strip() for path in (rollback, candidate, receipt))
        ):
            self._set_inline_status(
                page.image_status_label,
                "Select a non-empty source and explicit rollback, candidate and receipt outputs.",
                "error",
            )
            return
        if not page.sakisu_wrong_kmi_confirm.isChecked():
            self._set_inline_status(
                page.image_status_label,
                "Confirm the explicit wrong-KMI boot risk before patching.",
                "error",
            )
            return
        self._prepare_file_snapshot(
            source,
            lambda snapshot, context: self._launch_sakisu_patch(
                snapshot, rollback, candidate, receipt, context
            ),
            device_bound=False,
        )

    def _launch_sakisu_patch(
        self,
        source: VerifiedFileSnapshot,
        rollback: Path,
        candidate: Path,
        receipt: Path,
        context: DeviceActionContext | None,
    ) -> None:
        page = self.images_page
        request = SakisuPatchRequest(
            source=source,
            partition=page.sakisu_partition_combo.currentText(),
            kmi=page.sakisu_kmi_combo.currentText(),
            wrong_kmi_boot_risk_confirmed=True,
            replace_non_stock_confirmed=page.sakisu_replace_non_stock_confirm.isChecked(),
            rollback=rollback,
            candidate=candidate,
            receipt=receipt,
        )
        worker = SakisuPatchWorker(
            SakisuPatchFlow(
                self.project_root,
                self.workspace_root,
                execution="host",
            ),
            request,
            parent=self,
        )
        self.sakisu_worker = worker
        self._flow_active = True
        self._pause_device_scanning()
        self._set_busy(True)
        self.activity_panel.apply_progress(
            ProgressSnapshot.waiting("Creating verified SakiSU candidate…", cancellable=True)
        )
        worker.result_ready.connect(
            lambda result: self._set_inline_status(
                page.image_status_label,
                "SakiSU 镜像已生成。",
                "success",
            )
        )
        worker.log_message.connect(self._on_worker_log)
        worker.finished_error.connect(
            lambda message: self._on_typed_worker_error("SakiSU patch-only", message)
        )
        worker.cancelled_signal.connect(self._on_cancelled)
        worker.finished.connect(
            lambda worker=worker: self._finish_aux_worker("sakisu_worker", worker)
        )
        worker.start()

    def _start_custom_su_probe(self) -> None:
        if self._operation_active():
            return
        value = self.advanced_page.custom_su_edit.text().strip()
        try:
            from ..core.su import validate_custom_su

            value = validate_custom_su(value)
        except ValueError as exc:
            self._validated_custom_su = ""
            self._set_inline_status(self.advanced_page.custom_su_status, str(exc), "error")
            return
        if self._current_mode != "adb" or not self._current_serial:
            self._set_inline_status(
                self.advanced_page.custom_su_status,
                "Connect and rescan one exact ADB device before validation.",
                "error",
            )
            return
        from dataclasses import replace

        candidate_settings = replace(self.settings, custom_su=value)
        worker = CustomSuProbeWorker(candidate_settings, self._current_serial, parent=self)
        self.su_probe_worker = worker
        self._flow_active = True
        self._pause_device_scanning()
        self._set_busy(True)

        def accepted(result: object) -> None:
            self._validated_custom_su = value
            self.settings = candidate_settings
            self.settings_store.save(candidate_settings)
            self._set_inline_status(
                self.advanced_page.custom_su_status,
                f"Validated fixed id probe: uid={result.uid}",
                "success",
            )

        worker.result_ready.connect(accepted)
        worker.finished_error.connect(
            lambda message: self._on_typed_worker_error("Custom su validation", message)
        )
        worker.cancelled_signal.connect(self._on_cancelled)
        worker.finished.connect(
            lambda worker=worker: self._finish_aux_worker("su_probe_worker", worker)
        )
        worker.start()

    def _finish_aux_worker(self, attribute: str, finished_worker=None) -> None:
        worker = getattr(self, attribute, None)
        origin = finished_worker if finished_worker is not None else self.sender()
        if worker is None or origin is not worker:
            return
        worker.deleteLater()
        setattr(self, attribute, None)
        self._flow_active = False
        self._reset_progress()
        self._set_busy(False)
        if self._closing:
            self._resume_pending_close()
        else:
            self._resume_device_scanning()

    def _on_typed_worker_error(self, title: str, error: object) -> None:
        message = _safe_diagnostic_error_message(error)
        self._append_activity(f"{title} failed", key=(title, message), error=True)
        QMessageBox.critical(self, title, message)

    # ---------- helpers ----------
    def _all_flow_buttons(self) -> tuple[QPushButton, ...]:
        return (
            self.root_button,
            self.backup_button,
            self.patch_lk_button,
            self.inspect_lk_button,
            self.write_button,
            self.auto_flow_button,
            self.honor_auto_button,
            self.oppo_unlock_button,
            self.honor_unlock_button,
            self.unlock_reboot_button,
            self.vivo_xingc_button,
            self.xingc_token_button,
            self.xingc_sign_button,
            self.xingc_auth_button,
            self.xingc_lock_button,
            self.dual_slot_sync_button,
            self.original_lk_sync_button,
            self.refresh_button,
            self.settings_button,
            self.cmd_run_button,
            self.patched_lk_browse,
            self.original_lk_browse,
            self.source_lk_browse,
            self.output_lk_browse,
            self.fastboot_patched_lk_browse,
            self.reboot_system_button,
            self.reboot_fastbootd_button,
            self.reboot_bootloader_button,
            self.reboot_recovery_button,
            self.partition_page.inventory_button,
            self.partition_page.backup_selected_button,
            self.images_page.dyn_capture_button,
            self.images_page.dyn_restore_button,
            self.images_page.dyn_backup_browse,
            self.images_page.sakisu_source_browse,
            self.images_page.sakisu_patch_button,
            self.advanced_page.custom_su_validate_button,
        )

    def _operation_active(self) -> bool:
        """Return whether any foreground operation owns the UI.

        A worker object is considered active from construction until its
        ``finished`` handler clears it.  This closes the small pre-start and
        post-finish windows where ``isRunning()`` alone can allow re-entry.
        """
        return self._flow_active or any(
            worker is not None
            for worker in (
                self.worker,
                self.utility_worker,
                self.terminal_worker,
                self.reboot_worker,
                self.generic_worker,
                self.dyn_worker,
                self.sakisu_worker,
                self.su_probe_worker,
                self.file_snapshot_worker,
            )
        )

    def _pause_device_scanning(self) -> None:
        self.device_scan_timer.stop()
        self._scan_generation += 1
        self._refresh_pending = False

    def _resume_device_scanning(self) -> None:
        if self._closing or self._operation_active():
            return
        self.refresh_devices(auto=True)
        self.device_scan_timer.start()

    @staticmethod
    def _is_yes_reply(reply) -> bool:
        return reply == QMessageBox.Yes or getattr(reply, "name", "") == "Yes"

    def _get_profile(self, key: str) -> DeviceProfile | None:
        if not key:
            return None
        if key in self._profiles_cache:
            return self._profiles_cache[key]
        try:
            detector = DeviceDetector(self.project_root, AdbClient("adb"))
            profile = detector.get_profile_by_key(key)
        except Exception:
            return None
        self._profiles_cache[key] = profile
        return profile

    @staticmethod
    def _fastboot_display_name(fastboot_mode: str) -> str:
        return {
            "fastbootd": "Fastbootd",
            "bootloader": "Fastboot",
        }.get(fastboot_mode, "Fastboot（未确认子模式）")

    def _load_models(self) -> None:
        self.model_combo.blockSignals(True)
        self.runtime_combo.blockSignals(True)
        try:
            self.model_combo.clear()
            self.model_combo.addItem("自动匹配 / 请选择", "")
            self.runtime_combo.clear()
            self.runtime_combo.addItem("请选择运行配置", "")
            self._profiles_cache.clear()
            self._runtime_representative_by_model.clear()
            try:
                detector = DeviceDetector(self.project_root, AdbClient("adb"))
                runtime_representatives = {}
                for profile in detector.list_profiles():
                    self.model_combo.addItem(profile.display_name, profile.key)
                    runtime = profile.privilege_runtime
                    representative_key = runtime_representatives.get(runtime)
                    if representative_key is None:
                        representative_key = profile.key
                        runtime_representatives[runtime] = representative_key
                        payload_id = runtime.preload_ref.removeprefix("builtin:")
                        self.runtime_combo.addItem(
                            f"{profile.display_name} · {payload_id or '无 preload'}",
                            representative_key,
                        )
                    self._runtime_representative_by_model[
                        profile.key
                    ] = representative_key
                    self._profiles_cache[profile.key] = profile
            except Exception as exc:
                self._set_device_presentation("environment")
                self._append_log(f"加载机型失败（请检查设置中的 platform-tools）: {exc}", error=True)
                return
        finally:
            self.model_combo.blockSignals(False)
            self.runtime_combo.blockSignals(False)

    def _set_device_presentation(
        self,
        state: str,
        *,
        serial: str = "",
        mode: str = "",
        fastboot_mode: str = "",
        detail: str = "",
    ) -> None:
        labels = {
            "missing": "等待设备",
            "scanning": "正在扫描…",
            "environment": "环境未就绪",
            "adb": "已连接 · ADB",
            "fastboot": f"已连接 · {self._fastboot_display_name(fastboot_mode)}",
            "busy": "流程执行中",
        }
        text = labels.get(state, "未检测到")
        detected_mode = self._fastboot_display_name(fastboot_mode) if mode == "fastboot" else mode.upper()
        if serial and state in {"adb", "fastboot", "busy"}:
            text += f"\n{format_device_summary(serial=serial, mode=detected_mode)}"
        self.status_label.setText(text)
        self.status_label.setProperty("deviceState", state)
        if hasattr(self, "top_current_device_label"):
            if serial and state in {"adb", "fastboot", "busy"}:
                model = ""
                result = self._last_scan_result
                if result is not None and getattr(result, "model", ""):
                    model = f" · {result.model}"
                self.top_current_device_label.setText(
                    f"当前设备：{serial} · {detected_mode}{model}"
                )
            else:
                self.top_current_device_label.setText("当前设备：未连接")
        self.status_label.setToolTip(detail or detected_mode or "设备扫描")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self._refresh_action_guidance()

    def _refresh_action_guidance(self) -> None:
        busy = self._operation_active()
        if busy:
            for button in self._all_flow_buttons():
                button.setEnabled(False)
            return
        mode = self._current_mode
        profile = self._get_profile(self.model_combo.currentData() or "")
        capabilities = profile.capabilities if profile is not None else frozenset()
        has_preload = "preload" in capabilities
        has_xingc = "xingc" in capabilities
        has_generic_lk = "generic_lk" in capabilities
        # Named for what it actually means rather than "has_lk": this is a
        # statement about how the brand *unlocks*, not about whether the product
        # may write an LK to the device. OPPO carries neither capability, yet it
        # writes patched LK to the inactive slot and backs up lk and init_boot.
        unlock_uses_patched_lk = has_xingc or has_generic_lk
        has_flashing_unlock = "flashing_unlock" in capabilities
        test_mode = MainWindow._test_mode_enabled(self)
        self.authorized_page.apply_capabilities(
            capabilities,
            test_mode=test_mode,
            mode=mode,
            brand=profile.brand if profile is not None else "",
        )
        self.images_page.apply_capabilities(
            capabilities,
            test_mode=test_mode,
            mode=mode,
        )
        has_model = bool(self.model_combo.currentData())
        # Test mode intentionally relaxes the auto-matched profile requirement:
        # an operator may run against a serial/mode without a bound profile. All
        # real device edges (serial, mode, slot, capacity, hash, cancellation,
        # final revalidation) remain enforced downstream.
        effective_model = has_model or test_mode
        adb_ready = bool(self._current_serial and mode == "adb" and effective_model and not busy)
        fastboot_ready = bool(self._current_serial and mode == "fastboot" and effective_model and not busy)
        local_ready = not busy

        # Test mode used to forbid the complete workflow outright.  That rule
        # came from the online product, where the model match was what authorised
        # the run.  Here the operator hand-picks the template precisely because
        # detection is sometimes wrong, and the workflow is the only path that
        # performs the finishing steps — forbidding it left test mode unable to
        # do the thing it exists for.  The test-mode confirmation still runs, and
        # every write boundary below still revalidates.
        one_click_ready = bool(adb_ready and has_xingc)
        one_click_blocked = (
            [] if one_click_ready else ["需要已识别的 Vivo ADB 设备和目标机型"]
        )
        self.auto_flow_button.setEnabled(one_click_ready)
        self.sakisu_root_checkbox.setEnabled(False)
        self.sakisu_root_checkbox.setChecked(False)
        self.auto_flow_button.setToolTip(
            "已就绪" if one_click_ready else "；".join(one_click_blocked)
        )
        if hasattr(self, "vivo_preflight_label"):
            ready_text = "已就绪"
            blocked = one_click_blocked
            self.vivo_preflight_label.setText(
                ready_text if one_click_ready else "；".join(blocked)
            )
            self.vivo_preflight_label.setProperty(
                "statusState",
                "ready" if one_click_ready else "warning",
            )
            self.vivo_preflight_label.style().unpolish(self.vivo_preflight_label)
            self.vivo_preflight_label.style().polish(self.vivo_preflight_label)
        self.root_button.setEnabled(False)
        # 保存恢复点 backs up whatever the profile's workflow lists, and the
        # flow itself refuses to continue when that list has no `lk` in it.
        # Asking the capability set instead disabled this row on OPPO, whose
        # workflow does back up lk and init_boot.
        backs_up_lk = profile is not None and "lk" in profile.backup_partitions
        self.backup_button.setEnabled(adb_ready and backs_up_lk)
        has_patched_lk = bool(self.patched_lk_edit.text().strip())
        has_original_lk = bool(self.original_lk_edit.text().strip())
        # 「准备解锁」 writes the selected patched LK into the inactive slot for
        # every brand that has an LK write path, OPPO included: OPPO unlocks
        # with a plain `flashing unlock` and then puts the stock LK back over
        # both slots, which is exactly what 「还原双槽原 LK」 is for.
        # `unlock_uses_patched_lk` must not be reused here — OPPO is by
        # definition the brand whose unlock does not ride a patched LK.
        has_writable_lk = has_xingc or has_generic_lk or has_flashing_unlock
        self.write_button.setEnabled(
            adb_ready and has_writable_lk and has_patched_lk
        )
        self.patch_lk_button.setEnabled(local_ready)
        self.inspect_lk_button.setEnabled(local_ready)
        self.patch_lk_button.setToolTip(
            "已就绪"
            if local_ready
            else "当前有任务正在运行"
        )
        self.oppo_unlock_button.setEnabled(fastboot_ready and (has_flashing_unlock or test_mode))
        self.vivo_xingc_button.setEnabled(fastboot_ready and (has_xingc or test_mode))
        # Dual-slot LK sync has exactly two scenarios. A brand whose unlock
        # cannot put the original LK back (Vivo via `xingc`, Honor via
        # `generic_lk`) may only sync a *patched* LK. A brand whose unlock
        # expects the original LK restored afterwards (OPPO via `flashing_unlock`)
        # may only sync that *original* LK. Never offer the other one.
        self.dual_slot_sync_button.setEnabled(
            fastboot_ready and unlock_uses_patched_lk and has_patched_lk
        )
        self.original_lk_sync_button.setEnabled(
            fastboot_ready and has_flashing_unlock and has_original_lk
        )
        self.honor_unlock_button.setEnabled(
            fastboot_ready and has_generic_lk and has_patched_lk
        )
        self.honor_auto_button.setEnabled(
            fastboot_ready and has_generic_lk and has_patched_lk
        )
        self.xingc_token_button.setEnabled(fastboot_ready and (has_xingc or test_mode))
        self.xingc_sign_button.setEnabled(fastboot_ready and (has_xingc or test_mode) and bool(self._xingc_token) and self._xingc_serial == self._current_serial)
        self.xingc_auth_button.setEnabled(fastboot_ready and (has_xingc or test_mode) and bool(self._xingc_payload) and self._xingc_serial == self._current_serial)
        reboot_ready = bool(self._current_serial and mode in {"adb", "fastboot"} and not busy and self.reboot_worker is None)
        self.reboot_system_button.setEnabled(reboot_ready)
        self.unlock_reboot_button.setEnabled(reboot_ready)
        self.reboot_bootloader_button.setEnabled(reboot_ready)
        self.reboot_fastbootd_button.setEnabled(reboot_ready)
        self.reboot_recovery_button.setEnabled(reboot_ready and mode == "adb")
        self.xingc_lock_button.setEnabled(fastboot_ready and (has_xingc or test_mode))
        if not busy and hasattr(self, "refresh_button"):
            self.refresh_button.setEnabled(True)

    # ---------- device scan delegate ----------
    def refresh_devices(self, *, auto: bool = False) -> None:
        """Request one background device scan without overlapping probe processes."""
        if self._closing or self._operation_active():
            return
        if self._scan_active:
            self._refresh_pending = True
            return

        self._scan_active = True
        if not auto:
            self._set_device_presentation("scanning")
            self._append_activity("正在扫描 ADB / Fastboot…", key=("scan-request",))
        self._scan_generation += 1
        generation = self._scan_generation
        worker = DeviceScanWorker(
            self.project_root,
            self.settings,
            generation=generation,
            automatic=auto,
        )
        self.scan_worker = worker
        worker.scan_completed.connect(self._apply_scan_result)
        worker.finished.connect(self._on_scan_finished)
        worker.start()

    def _set_detected_device_details(self, result: DeviceScanResult | None, *, state: str = "missing") -> None:
        if state == "environment":
            self.detected_connection_label.setText("连接: platform-tools 环境未就绪")
            self.detected_identity_label.setText("设备: —")
            self.detected_firmware_label.setText("系统: —")
            self.detected_slot_label.setText("槽位: —")
            self.detected_profile_label.setText("机型匹配: —")
            return
        if result is None or not result.serial:
            self.detected_connection_label.setText("连接: 未检测到 ADB 或 Fastboot 设备")
            self.detected_identity_label.setText("设备: —")
            self.detected_firmware_label.setText("系统: —")
            self.detected_slot_label.setText("槽位: —")
            self.detected_profile_label.setText("机型匹配: —")
            return
        if result.mode == "fastboot":
            fastboot_name = self._fastboot_display_name(result.fastboot_mode)
            self.detected_connection_label.setText(f"连接: {fastboot_name} · {result.serial}")
            self.detected_identity_label.setText("设备: Fastboot 模式下未读取 Android 属性")
            self.detected_firmware_label.setText("系统: 将在操作前重新读取并校验")
            self.detected_slot_label.setText("槽位: 将在操作前重新读取并校验")
            self.detected_profile_label.setText("机型匹配: Fastboot 扫描不自动匹配")
            return

        stable_identity = " · ".join(part for part in (result.brand, result.model, result.device) if part) or "未读取到"
        identity = f"{self._display_model_name(result)} · {stable_identity}"
        self.detected_connection_label.setText(f"连接: ADB · {result.serial}")
        self.detected_identity_label.setText(f"设备: {identity}")
        system_parts = []
        if result.android_version:
            system_parts.append(f"Android {result.android_version}")
        if result.firmware:
            system_parts.append(result.firmware)
        if result.platform:
            system_parts.append(f"平台 {result.platform}")
        system_text = " · ".join(system_parts) or "未读取到"
        if result.kernel_release:
            system_text += f" · 内核 {result.kernel_release}"
        self.detected_firmware_label.setText(f"系统: {system_text}")
        try:
            slot = SlotManager.from_suffix((result.slot_suffix or "").strip())
            slot_text = f"活动槽 {slot.active_suffix}；对槽 {slot.inactive_suffix}"
        except Exception:
            slot_text = result.slot_suffix or "未读取到"
        self.detected_slot_label.setText(f"槽位: {slot_text}")
        if result.profile_key:
            profile = self._get_profile(result.profile_key)
            profile_name = profile.display_name if profile is not None else result.profile_key
            self.detected_profile_label.setText(f"机型匹配: {profile_name}")
        else:
            self.detected_profile_label.setText("机型匹配: 未匹配，请手动选择目标机型")

    @staticmethod
    def _display_model_name(result: DeviceScanResult) -> str:
        return (
            (result.market_name or "").strip()
            or (result.model or "").strip()
            or "未读取到机型名"
        )

    @staticmethod
    def _bootloader_display(state: str) -> str:
        return {
            "locked": "已锁定",
            "unlocked": "已解锁",
        }.get(str(state).strip().casefold(), "未知")

    def _scan_signature(self, result: DeviceScanResult) -> tuple[object, ...]:
        return (
            result.environment_ready,
            result.connection_state_known,
            result.serial,
            result.mode,
            result.fastboot_mode,
            result.brand,
            result.model,
            result.market_name,
            result.device,
            result.firmware,
            result.android_version,
            result.build_incremental,
            result.security_patch,
            result.platform,
            result.manufacturer,
            result.kernel_release,
            result.bootloader_state,
            result.device_fingerprint,
            result.slot_suffix,
            result.profile_key,
            bool(result.profile_error),
            bool(result.scan_error),
        )

    def _clear_current_device_state(self) -> None:
        """Mark transport unavailable without discarding the operator context."""
        self._current_serial = ""
        self._current_mode = ""
        # LK selections may survive a transient mode switch, but an in-memory
        # Xingc token/signature is tied to one live Fastboot interaction and
        # must not survive loss of transport confirmation.
        self._clear_xingc_context()
        self.backup_path_label.setText("等待设备连接")
        if hasattr(self, "current_operation_panel"):
            self.current_operation_panel.setVisible(False)

    def _clear_device_bound_selections(self) -> None:
        """Drop inputs that must not cross an exact-device boundary."""
        self._clear_xingc_context()
        self.patched_lk_edit.clear()
        self.fastboot_patched_lk_edit.clear()
        self.original_lk_edit.clear()

    def _append_scan_activity(self, result: DeviceScanResult, previous: tuple[object, ...] | None) -> None:
        current = self._scan_signature(result)
        if current == previous:
            return
        if not result.environment_ready:
            self._append_log("平台工具不可用", error=True)
            return
        if not result.serial:
            self._append_log("设备已断开，等待设备连接…" if previous and previous[1] else "等待设备连接…")
            return
        if result.mode == "fastboot":
            fastboot_name = self._fastboot_display_name(result.fastboot_mode)
            if previous and previous[2] == result.serial and previous[3] == "adb":
                self._append_log(f"设备已从 ADB 切换至 {fastboot_name}")
            else:
                self._append_log(f"已识别 {fastboot_name}")
            return

        self._append_log("已识别 Android 系统（ADB）")
        identity = " · ".join(part for part in (result.brand, result.model, result.device) if part)
        if identity:
            self._append_log(f"设备信息：{identity}")
        if result.firmware:
            self._append_log(f"系统：{result.firmware}")
        try:
            slot = SlotManager.from_suffix((result.slot_suffix or "").strip())
            self._append_log(f"槽位：活动 {slot.active_suffix}，对槽 {slot.inactive_suffix}")
        except Exception:
            pass
        if result.profile_key:
            profile = self._get_profile(result.profile_key)
            name = profile.display_name if profile is not None else result.profile_key
            self._append_log(f"机型已匹配：{name}")
        elif result.profile_error:
            self._append_log("设备已连接，机型待手动确认")

    def _apply_scan_result(self, result: DeviceScanResult) -> None:
        worker = self.sender()
        if not isinstance(worker, DeviceScanWorker):
            return
        if (
            self._closing
            or self._operation_active()
            or worker.generation != self._scan_generation
            or result.scan_generation != worker.generation
        ):
            return
        self._last_scan_result = result
        if result.profile_key:
            matched_profile = self._get_profile(result.profile_key)
            profile_text = (
                f"已匹配 · {matched_profile.display_name}"
                if matched_profile is not None
                else f"已匹配 · {result.profile_key}"
            )
        elif result.mode == "adb":
            profile_text = "未匹配"
        else:
            profile_text = "ADB 模式下读取"
        snapshot = DeviceSnapshot(
            generation=result.scan_generation,
            mode=(
                self._fastboot_display_name(result.fastboot_mode)
                if result.mode == "fastboot"
                else (result.mode.upper() if result.mode else "—")
            ),
            serial=result.serial or "—",
            model=self._display_model_name(result) if result.serial else "—",
            manufacturer=result.manufacturer or result.brand or "—",
            codename=result.device or "—",
            android=(f"Android {result.android_version}" if result.android_version else "—"),
            build_incremental=result.build_incremental or "—",
            security_patch=result.security_patch or "—",
            firmware=result.firmware or "—",
            platform=result.platform or "—",
            kernel=result.kernel_release or "—",
            slot=result.slot_suffix or "—",
            bootloader=self._bootloader_display(result.bootloader_state),
            profile=profile_text,
        )
        self.activity_panel.apply_device_snapshot(snapshot)
        self.device_page.apply_device_snapshot(snapshot)
        auto = worker.automatic
        previous_signature = self._last_scan_signature
        self._last_scan_signature = self._scan_signature(result)
        if not result.environment_ready:
            self._clear_current_device_state()
            self._set_detected_device_details(None, state="environment")
            self._set_device_presentation("environment")
            self._append_scan_activity(result, previous_signature)
            self._refresh_action_guidance()
            return
        if not result.serial:
            self._clear_current_device_state()
            self._set_detected_device_details(None)
            self._set_device_presentation("missing", detail="ADB / Fastboot 自动扫描")
            if result.scan_error and not auto:
                self._append_activity("扫描设备失败", key=("scan-error", result.scan_error), error=True)
            self._append_scan_activity(result, previous_signature)
            return

        previous_context_serial = self._context_serial
        if previous_context_serial and previous_context_serial != result.serial:
            self._clear_device_bound_selections()
        self._context_serial = result.serial
        self._current_serial = result.serial
        self._current_mode = result.mode
        self.partition_page.apply_mode(result.mode)

        detail = "机型待确认" if result.mode == "adb" and not result.profile_key else ""
        self._set_detected_device_details(result)
        self._set_device_presentation(
            result.mode,
            serial=result.serial,
            mode=result.mode,
            fastboot_mode=result.fastboot_mode,
            detail=detail,
        )
        self._show_safe_device_diagnostics()
        self._update_backup_label(result.serial)
        if result.profile_key and self._auto_match_may_replace_model():
            current_key = self.model_combo.currentData() or ""
            if current_key != result.profile_key:
                index = self.model_combo.findData(result.profile_key)
                if index >= 0:
                    self.model_combo.setCurrentIndex(index)
        elif result.profile_error and not auto:
            self._append_activity("设备已连接，机型待手动确认", key=("manual-profile", result.serial), error=True)
        elif result.scan_error and not auto:
            self._append_activity("读取设备信息失败", key=("manual-scan", result.serial), error=True)
        self._append_scan_activity(result, previous_signature)
        self._refresh_action_guidance()


    def _on_scan_finished(self) -> None:
        worker = self.sender()
        if not isinstance(worker, DeviceScanWorker):
            return
        if worker is self.scan_worker:
            self._scan_active = False
            self.scan_worker = None
        if worker.generation != self._scan_generation:
            worker.deleteLater()
            if self._closing:
                self._resume_pending_close()
                return
            if self._refresh_pending and not self._closing and not self._operation_active():
                self._refresh_pending = False
                self.refresh_devices(auto=True)
            return
        worker.deleteLater()
        if self._closing:
            self._refresh_pending = False
            self._resume_pending_close()
            return
        if self._operation_active():
            self._refresh_pending = False
            return
        if self._refresh_pending:
            self._refresh_pending = False
            self.refresh_devices(auto=True)

    def _update_backup_label(self, serial: str) -> None:
        self.workspace.exports_dir(serial)
        self.backup_path_label.setText("备份目录已就绪")
        if hasattr(self, "current_operation_panel"):
            self.current_operation_panel.setVisible(True)

    # ---------- settings / paths ----------
    def open_settings(self) -> None:
        previous_test_mode = MainWindow._test_mode_enabled(self)
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec():
            new_settings = replace(
                dialog.result_settings(),
                custom_su=self.settings.custom_su,
            )
            try:
                self.settings_store.save(new_settings)
            except OSError as exc:
                QMessageBox.critical(self, "NEBUMTK", f"保存设置失败: {exc}")
                return
            self.settings = new_settings
            self._apply_test_mode_state(
                announce=previous_test_mode != MainWindow._test_mode_enabled(self)
            )
            path = self.settings.platform_tools_path or "(系统 PATH)"
            self._append_log(f"已保存 platform-tools: {path}")
            self._load_models()
            self.refresh_devices(auto=False)

    def open_backup_dir(self) -> None:
        if self._current_serial:
            target = self.workspace.exports_dir(self._current_serial)
        else:
            target = self.workspace_root
        target.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def open_logs_dir(self) -> None:
        target = self.workspace.logs_dir()
        target.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def open_workspace_dir(self) -> None:
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.workspace_root)))

    def open_platform_tools_terminal(self) -> None:
        """Open an interactive terminal rooted at the configured tools directory."""

        try:
            tools = EnvChecker(self.settings).validate(need_adb=True, need_fastboot=True)
        except EnvError as exc:
            QMessageBox.critical(self, "NEBUMTK", str(exc))
            return
        directory = Path(tools.adb).resolve().parent
        if os.name == "nt":
            started = QProcess.startDetached("cmd.exe", ["/K"], str(directory))[0]
            if not started:
                QMessageBox.critical(self, "NEBUMTK", "无法打开 Platform Tools 终端")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    # ---------- preload / patched lk pickers ----------
    def _update_default_preload(self) -> None:
        key = (
            self.runtime_combo.currentData()
            if MainWindow._test_mode_enabled(self)
            else self.model_combo.currentData()
        )
        if not key:
            self.preload_edit.clear()
            return
        profile = self._get_profile(key)
        if profile is None:
            self.preload_edit.clear()
            return
        preload_ref = profile.privilege_runtime.preload_ref
        if preload_ref:
            self.preload_edit.setText(preload_ref)
        else:
            self.preload_edit.clear()

    def _auto_match_may_replace_model(self) -> bool:
        """Whether a scan result may overwrite the model combo.

        Detection is known to be wrong on some models, and test mode exists
        precisely so the operator can override it.  Forcing the detected
        template back changed the capability set under them — a Vivo template
        that had just made 「准备解锁」 ready got swapped for whatever the scan
        guessed, and the action greyed out again a few seconds later.
        """

        return not (
            MainWindow._test_mode_enabled(self) and self._model_chosen_by_operator
        )

    def _mark_model_chosen_by_operator(self, _index: int = 0) -> None:
        self._model_chosen_by_operator = True

    def _on_model_changed(self, _index: int) -> None:
        self._clear_xingc_context()
        if not MainWindow._test_mode_enabled(self) or not self.runtime_combo.currentData():
            model_key = str(self.model_combo.currentData() or "")
            runtime_index = self.runtime_combo.findData(
                self._runtime_representative_by_model.get(model_key, "")
            )
            self.runtime_combo.setCurrentIndex(max(0, runtime_index))
        self._update_default_preload()
        self._refresh_action_guidance()

    def _on_runtime_changed(self, _index: int) -> None:
        self._update_default_preload()
        self._refresh_action_guidance()

    def _test_mode_enabled(self) -> bool:
        settings = getattr(self, "settings", None)
        return bool(getattr(settings, "test_mode_enabled", False))

    def _apply_test_mode_state(self, *, announce: bool) -> None:
        enabled = MainWindow._test_mode_enabled(self)
        self._confirmed_test_mode_fingerprint = None
        self.test_mode_banner.setVisible(enabled)
        if hasattr(self, "top_test_selectors"):
            self.top_test_selectors.setVisible(enabled)
        self.advanced_page.custom_su_section.setVisible(enabled)
        if hasattr(self, "test_runtime_panel"):
            self.test_runtime_panel.setVisible(enabled)
        self.runtime_combo.setEnabled(enabled and not self._operation_active())
        if enabled:
            if not self.runtime_combo.currentData():
                model_key = str(self.model_combo.currentData() or "")
                self.runtime_combo.setCurrentIndex(
                    max(
                        0,
                        self.runtime_combo.findData(
                            self._runtime_representative_by_model.get(model_key, "")
                        ),
                    )
                )
            if announce:
                self._append_activity(
                    "测试模式已开启",
                    key=("test-mode", "enabled"),
                    error=True,
                )
        else:
            model_key = str(self.model_combo.currentData() or "")
            self.runtime_combo.setCurrentIndex(
                max(
                    0,
                    self.runtime_combo.findData(
                        self._runtime_representative_by_model.get(model_key, "")
                    ),
                )
            )
            if announce:
                self._append_activity(
                    "测试模式已关闭",
                    key=("test-mode", "disabled"),
                )
        self._update_default_preload()
        self._refresh_action_guidance()

    def _confirm_test_mode_operation(self, flow_kind: str) -> bool:
        fingerprint = (
            self._current_serial,
            str(self.model_combo.currentData() or ""),
            str(self.runtime_combo.currentData() or ""),
            flow_kind,
        )
        if (
            not MainWindow._test_mode_enabled(self)
            or self._confirmed_test_mode_fingerprint == fingerprint
        ):
            return True
        reply = QMessageBox.warning(
            self,
            "确认测试模式操作",
            "测试模式将忽略当前设备与所选机型/运行配置的兼容匹配。\n\n"
            f"流程: {flow_kind}\n机型: {self.model_combo.currentText()}\n"
            f"运行配置: {self.runtime_combo.currentText()}\n\n"
            "序列号、模式、槽位、分区容量、镜像哈希和写入边界仍会强制校验。继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return False
        self._confirmed_test_mode_fingerprint = fingerprint
        return True

    def browse_patched_lk(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 patched lk")
        if path:
            self._set_selected_patched_lk(Path(path))

    def browse_original_lk(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择原机 LK")
        if path:
            self.original_lk_edit.setText(path)
            self._refresh_action_guidance()

    def _set_selected_patched_lk(self, path: Path) -> None:
        value = str(path)
        self.patched_lk_edit.setText(value)
        self.fastboot_patched_lk_edit.setText(value)
        self._refresh_action_guidance()

    def browse_source_lk(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择原始 LK")
        if not path:
            return
        source = Path(path)
        self.source_lk_edit.setText(str(source))
        # The suggested name belongs to whichever source produced it.  Keeping a
        # stale one meant changing the input could leave the output pointing at
        # a directory that no longer exists — "输出目录不存在" on a path the
        # operator never chose.
        if not self.output_lk_edit.text().strip() or self._output_lk_is_derived:
            self.output_lk_edit.setText(
                str(source.with_name(f"{source.stem}_xingc{source.suffix or '.img'}"))
            )
            self._output_lk_is_derived = True

    def browse_output_lk(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "选择 patched LK 输出路径", self.output_lk_edit.text().strip())
        if path:
            self.output_lk_edit.setText(path)
            self._output_lk_is_derived = False

    def _external_lk_paths(self, *, require_output: bool) -> tuple[Path, Path | None] | None:
        source = Path(self.source_lk_edit.text().strip())
        if not source.is_file() or source.stat().st_size <= 0:
            QMessageBox.warning(self, "NEBUMTK", "请选择非空的原始 LK 文件")
            return None
        if not require_output:
            return source, None
        output_text = self.output_lk_edit.text().strip()
        if not output_text:
            QMessageBox.warning(self, "NEBUMTK", "请选择 patched LK 输出路径")
            return None
        output = Path(output_text)
        if source.resolve() == output.resolve():
            QMessageBox.warning(self, "NEBUMTK", "原始 LK 与输出路径不能相同")
            return None
        if not output.parent.is_dir():
            QMessageBox.warning(
                self,
                "NEBUMTK",
                f"输出目录不存在: {output.parent}",
            )
            return None
        return source, output

    def _start_reboot(self, target: str) -> None:
        if self._operation_active() or not self._current_serial or self._current_mode not in {"adb", "fastboot"}:
            return
        labels = {"system": "系统", "fastbootd": "Fastbootd", "bootloader": "Bootloader", "recovery": "Recovery"}
        warning = "Recovery 目标无法自动确认。" if target == "recovery" else "工具会在请求后验证目标状态。"
        reply = QMessageBox.question(
            self,
            "确认重启",
            f"设备: {self._current_serial}\n当前模式: {self._current_mode}\n\n重启到 {labels[target]}？\n{warning}\n不会修改分区数据。",
        )
        if reply != QMessageBox.Yes:
            return
        try:
            tools = EnvChecker(self.settings).validate(
                need_adb=self._current_mode == "adb" or target == "system",
                need_fastboot=self._current_mode == "fastboot",
            )
        except EnvError as exc:
            QMessageBox.critical(
                self,
                "NEBUMTK",
                _safe_diagnostic_error_message(exc),
            )
            return
        self.reboot_worker = RebootWorker(
            adb_binary=tools.adb if tools.adb else None,
            fastboot_binary=tools.fastboot if tools.fastboot else None,
            serial=self._current_serial,
            source_mode=self._current_mode,
            target=target,
            parent=self,
        )
        self._flow_active = True
        self._pause_device_scanning()
        self._set_busy(True)
        self._append_activity(f"请求重启到 {labels[target]}…", key=("reboot-start", target, self._current_serial))
        self.reboot_worker.progress_changed.connect(self._set_indeterminate_progress)
        self.reboot_worker.activity_event.connect(self._on_activity_event)
        self.reboot_worker.result_ready.connect(self._on_reboot_result)
        self.reboot_worker.finished_error.connect(self._on_reboot_error)
        self.reboot_worker.cancelled_signal.connect(lambda: self._append_activity("已停止等待重启目标确认", key=("reboot-cancelled",)))
        self.reboot_worker.finished.connect(self._on_reboot_finished)
        self.reboot_worker.start()

    def _on_reboot_result(self, result: object) -> None:
        state, detail = result
        if state == "verified":
            self._append_activity(f"已确认重启到 {detail}", key=("reboot-verified", detail))
        else:
            self._append_activity(str(detail), key=("reboot-requested", detail))

    def _on_reboot_error(self, message: str) -> None:
        self._append_activity("重启目标未确认", key=("reboot-error", message), error=True)
        QMessageBox.warning(
            self,
            "NEBUMTK",
            _safe_diagnostic_error_message(message),
        )

    def _on_reboot_finished(self) -> None:
        worker = self.sender()
        if worker is not self.reboot_worker:
            return
        worker.deleteLater()
        self.reboot_worker = None
        self._flow_active = False
        self._reset_progress()
        self._set_busy(False)
        if self._closing:
            self._resume_pending_close()
        else:
            self._resume_device_scanning()

    def _start_utility(self, operation: str, payload: dict[str, object]) -> None:
        if self._operation_active():
            return
        device_bound = operation.startswith("xingc_")
        launch_serial = ""
        if device_bound:
            launch_serial = str(payload.get("serial", "")).strip()
            if not launch_serial or launch_serial != self._current_serial:
                self._clear_xingc_context()
                QMessageBox.warning(self, "NEBUMTK", "设备状态已变化，请重新读取 Xingc Token")
                return
            payload = dict(payload)
            payload["serial"] = launch_serial
        session_log = SessionLog(self.workspace.logs_dir(), lambda _message: None)
        if session_log.path is not None and hasattr(self, "log_file_label"):
            self.log_file_label.setText("日志: 已启用")
        worker = UtilityWorker(operation, payload, self)
        self.utility_worker = worker
        self._utility_device_bound = device_bound
        self._utility_serial = launch_serial
        self._utility_clear_xingc_on_finish = False
        self._pause_device_scanning()
        self._set_busy(True)
        worker.progress_changed.connect(self._set_indeterminate_progress)
        if hasattr(worker, "log_message"):
            worker.log_message.connect(
                lambda message, sink=session_log: self._on_worker_log(
                    message, session_log=sink
                )
            )
        worker.result_ready.connect(self._on_utility_result)
        worker.finished_error.connect(self._on_utility_error)
        worker.cancelled_signal.connect(self._on_utility_cancelled)
        worker.finished.connect(self._on_utility_finished)
        worker.start()

    def _set_image_status(self, text: str, state: str) -> None:
        if not hasattr(self, "image_status_label"):
            return
        self.image_status_label.setText(text)
        self.image_status_label.setProperty("statusState", state)
        self.image_status_label.style().unpolish(self.image_status_label)
        self.image_status_label.style().polish(self.image_status_label)

    def _on_utility_result(self, result: object) -> None:
        if not isinstance(result, tuple) or len(result) not in {2, 3}:
            raise VerificationError("工具返回结果格式非法")
        if len(result) == 3:
            operation, result_serial, value = result
            result_serial = str(result_serial).strip()
        else:
            operation, value = result
            result_serial = ""
        if str(operation).startswith("xingc_") and (
            not self._utility_device_bound
            or not result_serial
            or result_serial != self._utility_serial
            or result_serial != self._current_serial
        ):
            self._utility_clear_xingc_on_finish = True
            self._append_activity(
                "Xingc 设备上下文已变化，结果已丢弃",
                key=("xingc-device-race", str(operation)),
                error=True,
            )
            QMessageBox.warning(self, "NEBUMTK", "设备状态已变化，请重新读取 Xingc Token")
            return
        if operation == "inspect_lk":
            ok, detail = value
            self.activity_stage_label.setText("外部 LK 检测通过" if ok else "外部 LK 检测失败")
            self._append_activity("外部 LK 检测通过" if ok else "外部 LK 不可修补", key=(operation, ok), error=not ok)
            self._set_image_status(
                "外部 LK 检测通过" if ok else f"外部 LK 不可修补：{detail}",
                "ready" if ok else "warning",
            )
            if not ok:
                QMessageBox.warning(self, "NEBUMTK", f"外部 LK 不可修补: {detail}")
        elif operation == "xingc_token":
            token = value
            self._xingc_token, self._xingc_payload, self._xingc_serial = token, b"", result_serial
            self._append_activity("Xingc Token 已读取", key=(operation,))
        elif operation == "xingc_sign":
            self._xingc_payload = value
            self._xingc_token = ""
            self._append_activity("Xingc Token 已签名", key=(operation,))
        elif operation == "xingc_auth":
            self._append_activity(f"Xingc Auth 已发送：{value.payload_size} 字节", key=(operation, value.payload_size))
            self._utility_clear_xingc_on_finish = True
        self._refresh_action_guidance()

    def _on_utility_error(self, message: str) -> None:
        if self.utility_worker is not None and self.utility_worker.operation.startswith("xingc_"):
            self._utility_clear_xingc_on_finish = True
        self.activity_stage_label.setText("工具操作失败")
        self._append_activity("工具操作失败", key=("utility-error", message), error=True)
        safe_message = _safe_diagnostic_error_message(message)
        if self.utility_worker is not None and self.utility_worker.operation == "inspect_lk":
            self._set_image_status(f"LK 工具操作失败：{safe_message}", "error")
        QMessageBox.critical(self, "NEBUMTK", safe_message)

    def _on_utility_cancelled(self) -> None:
        if self.utility_worker is not None and self.utility_worker.operation.startswith("xingc_"):
            self._utility_clear_xingc_on_finish = True
        self.activity_stage_label.setText("工具操作已取消")
        self._append_activity("工具操作已取消", key=("utility-cancelled",))

    def _on_utility_finished(self) -> None:
        worker = self.sender()
        if worker is not self.utility_worker:
            return
        wait = getattr(worker, "wait", None)
        if callable(wait):
            wait()
        clear_xingc = self._utility_clear_xingc_on_finish
        worker.deleteLater()
        self.utility_worker = None
        self._utility_device_bound = False
        self._utility_serial = ""
        self._utility_clear_xingc_on_finish = False
        if clear_xingc:
            self._clear_xingc_context()
        self._reset_progress()
        self._set_busy(False)
        if self._closing:
            self._resume_pending_close()
        else:
            self._resume_device_scanning()

    def inspect_external_lk(self) -> None:
        if self._operation_active():
            return
        paths = self._external_lk_paths(require_output=False)
        if paths is None:
            return
        source, _ = paths
        profile = self._get_profile(str(self.model_combo.currentData() or ""))
        if profile is None or not profile.has_capability("xingc"):
            QMessageBox.warning(
                self,
                "NEBUMTK",
                "The selected profile does not support Xingc LK inspection.",
            )
            return
        self._start_utility(
            "inspect_lk",
            {
                "source": str(source),
                "image_profile": profile.xingc_patch_profile,
            },
        )

    # ---------- CMD ----------
    def run_cmd(self) -> None:
        if self._operation_active():
            QMessageBox.warning(self, "NEBUMTK", "当前操作尚未结束，诊断功能暂不可用")
            return
        command = self.cmd_edit.text().strip()
        if not command:
            return

        from ..core.terminal_policy import parse_diagnostic_command

        try:
            parsed = parse_diagnostic_command(command)
        except VerificationError as exc:
            QMessageBox.warning(self, "NEBUMTK", str(exc))
            return
        use_fastboot = parsed.tool == "fastboot"

        try:
            tools = EnvChecker(self.settings).validate(
                need_adb=not use_fastboot,
                need_fastboot=use_fastboot,
            )
        except EnvError as exc:
            QMessageBox.critical(self, "NEBUMTK", str(exc))
            return

        binary = tools.fastboot if use_fastboot else tools.adb
        args = list(parsed.args)
        full = [binary]
        if self._current_serial and args != ["devices"]:
            full.extend(["-s", self._current_serial])
        full.extend(args)

        self._append_activity("执行诊断命令…", key=("terminal-start", tuple(args)))
        session_log = SessionLog(self.workspace.logs_dir(), lambda _message: None)
        if session_log.path is not None and hasattr(self, "log_file_label"):
            self.log_file_label.setText("日志: 已启用")
        self.terminal_worker = TerminalWorker(full, self)
        self._pause_device_scanning()
        self._set_busy(True)
        log_serial = self._current_serial
        self.terminal_worker.result_ready.connect(
            lambda result: self._on_terminal_result(session_log, full, result, log_serial)
        )
        self.terminal_worker.finished_error.connect(
            lambda message: self._on_terminal_error(session_log, message, log_serial)
        )
        self.terminal_worker.cancelled_signal.connect(self._on_terminal_cancelled)
        self.terminal_worker.finished.connect(self._on_terminal_finished)
        self.terminal_worker.start()

    @staticmethod
    def _redact_persistent(value: object, serial: str = "") -> str:
        text = str(value)
        normalized = serial.strip()
        return text.replace(normalized, "<device>") if normalized else text

    def _on_terminal_result(
        self,
        session_log: SessionLog,
        command: list[str],
        result,
        serial: str = "",
    ) -> None:
        session_log("$ " + self._redact_persistent(" ".join(command), serial))
        for line in (result.stdout or "").splitlines():
            if line.strip():
                session_log(self._redact_persistent(line.strip(), serial))
        for line in (result.stderr or "").splitlines():
            if line.strip():
                session_log(self._redact_persistent(line.strip(), serial))
        if result.returncode:
            session_log(f"exit {result.returncode}")
            self._append_activity("诊断命令执行失败", key=("terminal-exit", result.returncode), error=True)
        else:
            session_log("exit 0")
            self._append_activity("诊断命令已执行", key=("terminal-ok",))

    def _on_terminal_error(
        self,
        session_log: SessionLog,
        message: str,
        serial: str = "",
    ) -> None:
        session_log(f"诊断命令失败: {self._redact_persistent(message, serial)}")
        self._append_activity("诊断命令执行失败", key=("terminal-error", message), error=True)

    def _on_terminal_cancelled(self) -> None:
        self._append_activity("诊断命令已取消", key=("terminal-cancelled",))

    def _on_terminal_finished(self) -> None:
        worker = self.sender()
        if worker is not self.terminal_worker:
            return
        worker.deleteLater()
        self.terminal_worker = None
        self._reset_progress()
        self._set_busy(False)
        if self._closing:
            self._resume_pending_close()
        else:
            self._resume_device_scanning()

    # ---------- start flow helpers ----------
    def _resolve_preload_override(self, *, required: bool = False) -> Path | str | None:
        text = self.preload_edit.text().strip()
        if not text:
            if required:
                QMessageBox.warning(self, "NEBUMTK", "当前机型未配置内置受保护 preload")
            return None
        if not text.startswith("builtin:"):
            QMessageBox.warning(self, "NEBUMTK", "禁止使用明文 preload.so")
            return None
        return text

    def _resolve_patched_lk(self) -> Path | str | None:
        text = self.patched_lk_edit.text().strip()
        if not text:
            QMessageBox.warning(self, "NEBUMTK", "请选择 patched lk")
            return None
        path = Path(text)
        if not path.is_file():
            QMessageBox.warning(self, "NEBUMTK", f"patched lk 不存在: {path}")
            return None
        return path

    def _selected_brand(self) -> str:
        profile = self._get_profile(self.model_combo.currentData() or "")
        return (profile.brand.lower() if profile else "")

    def _require_mode(self, expected: str) -> bool:
        if self._current_mode != expected:
            QMessageBox.warning(
                self,
                "NEBUMTK",
                f"当前连接模式是 {self._current_mode or '未检测到'}，此步骤需要 {expected}。"
                "请先刷新并确认设备状态。",
            )
            return False
        return True

    def _start_common(self, flow_kind: str) -> None:
        if self._operation_active():
            return
        if not self._require_mode("adb"):
            return
        self._start_flow(flow_kind, preload_override=None)

    def _start_write(self) -> None:
        if self._operation_active():
            return
        if not self._require_mode("adb"):
            return
        profile = self._get_profile(str(self.model_combo.currentData() or ""))
        if profile is None:
            QMessageBox.warning(self, "NEBUMTK", "请选择当前机型")
            return
        preload = None
        if profile.brand.lower() == "honor":
            context = self._capture_device_action_context()
            if context is None:
                return
            self._confirm_and_start_flow(
                DestructiveFlowRequest(
                    "honor_prepare",
                    None,
                    ("对槽", "misc"),
                    "准备解锁并进入对槽",
                    "设备写入开始前可取消",
                    context,
                    preload_override=preload,
                )
            )
            return
        patched_lk = self._resolve_patched_lk()
        if patched_lk is None:
            return
        if isinstance(patched_lk, str):
            QMessageBox.warning(self, "NEBUMTK", "请选择本地 patched LK")
            return
        active = self._last_scan_result.slot_suffix if self._last_scan_result else ""
        if active not in {"_a", "_b"}:
            self._set_inline_status(
                self.images_page.image_status_label,
                "An exact active slot (_a or _b) is required before writing LK.",
                "error",
            )
            return
        inactive = "_b" if active == "_a" else "_a"
        self._prepare_file_snapshot(
            patched_lk,
            lambda snapshot, context: self._confirm_and_start_flow(
                DestructiveFlowRequest(
                    "write_inactive",
                    snapshot,
                    (f"lk{inactive}", "misc"),
                    f"write inactive {inactive}, then enter bootloader Fastboot",
                    "cancellable before the verified LK/misc dd boundary",
                    context,
                    preload_override=preload,
                )
            ),
        )

    def _start_patch_lk(self) -> None:
        if self._operation_active():
            return
        paths = self._external_lk_paths(require_output=True)
        if paths is None:
            return
        source, output = paths
        assert output is not None
        image_profile = str(self.local_lk_profile_combo.currentData() or "").strip()
        reply = QMessageBox.question(
            self,
            "确认本地修补 LK",
            "本操作仅使用显式选择的本地文件，不会连接、写入或重启设备。\n\n"
            f"镜像类型: {self.local_lk_profile_combo.currentText()}\n"
            f"输入: {source}\n输出: {output}\n\n"
            "将执行内部 UBL 预检、修补和独立验证。确认开始？",
        )
        if reply != QMessageBox.Yes:
            return
        self._start_flow(
            "patch_lk",
            source_lk=source,
            patched_lk=output,
            local_lk_profile=image_profile,
            confirm=False,
        )

    def _start_auto_flow(self) -> None:
        if self._operation_active():
            return
        if not self._require_mode("adb"):
            return
        if any(
            field.text().strip()
            for field in (
                self.source_lk_edit,
                self.output_lk_edit,
                self.patched_lk_edit,
            )
        ):
            QMessageBox.warning(
                self,
                "NEBUMTK",
                "检测到 custom LK。一键流程只使用本次设备备份并自动修补；"
                "如需 custom LK，请清空这些字段并改用分步流程。",
            )
            return
        if self._selected_brand() != "vivo":
            QMessageBox.warning(self, "NEBUMTK", "完整解锁流程目前仅适用于 Vivo")
            return
        try:
            profile = self._get_profile(str(self.model_combo.currentData() or ""))
            if profile is None:
                raise VerificationError("请选择有效 Vivo 机型")
        except Exception as exc:
            QMessageBox.warning(self, "NEBUMTK", f"一键流程前置检查失败: {exc}")
            return
        preload = None
        context = self._capture_device_action_context()
        if context is None:
            return
        # Root installation is not part of this build: the operator already has
        # root, and the one-click workflow never writes a SakiSU init_boot.
        install_sakisu_root = False
        sakisu_kmi = ""
        sakisu_init_boot = None
        self._confirm_and_start_flow(
            DestructiveFlowRequest(
                "vivo_complete_unlock",
                None,
                ("inactive LK", "patched LK", "misc"),
                "unlock and return to the verified original slot; user data will be cleared",
                "cancellable before finalization; LK/misc/reboot finalization is atomic",
                context,
                preload_override=preload,
                install_sakisu_root=install_sakisu_root,
                sakisu_init_boot=sakisu_init_boot,
                sakisu_kmi=sakisu_kmi,
            )
        )

    def _start_brand_flow(self, brand: str, flow_kind: str) -> None:
        if self._operation_active():
            return
        if not self._require_mode("fastboot"):
            return
        selected = self._selected_brand()
        if selected != brand and not MainWindow._test_mode_enabled(self):
            QMessageBox.warning(self, "NEBUMTK", f"当前机型不是 {brand.upper()}，禁止执行该步骤")
            return
        context = self._capture_device_action_context()
        if context is None:
            return
        self._confirm_and_start_flow(
            DestructiveFlowRequest(
                flow_kind,
                None,
                ("bootloader unlock state",),
                "no slot change; bootloader unlock state may change",
                "cancellable before the unlock command is issued",
                context,
            )
        )

    def _xingc_ready(self) -> bool:
        if self._operation_active():
            return False
        if not self._require_mode("fastboot"):
            return False
        if self._selected_brand() != "vivo" and not MainWindow._test_mode_enabled(self):
            QMessageBox.warning(self, "NEBUMTK", "当前机型不是 VIVO，禁止执行 Xingc 操作")
            return False
        return True

    def _clear_xingc_context(self) -> None:
        payload = getattr(self, "_xingc_payload", None)
        clear = getattr(payload, "clear", None)
        if callable(clear):
            clear()
        self._xingc_token = ""
        self._xingc_payload = b""
        self._xingc_serial = ""

    def _xingc_read_token(self) -> None:
        if not self._xingc_ready():
            return
        self._start_utility("xingc_token", {"serial": self._current_serial})

    def _xingc_sign_token(self) -> None:
        if not self._xingc_ready():
            return
        if not self._xingc_token or self._xingc_serial != self._current_serial:
            QMessageBox.warning(self, "NEBUMTK", "请先为当前设备读取新的 Xingc Token")
            return
        self._start_utility(
            "xingc_sign",
            {
                "serial": self._current_serial,
                "token": self._xingc_token,
                "model_id": str(self.model_combo.currentData() or ""),
            },
        )

    def _xingc_authorize(self) -> None:
        if not self._xingc_ready():
            return
        if not self._xingc_payload or self._xingc_serial != self._current_serial:
            QMessageBox.warning(self, "NEBUMTK", "请先读取并签名当前设备的 Xingc Token")
            return
        self._start_utility(
            "xingc_auth",
            {
                "serial": self._current_serial,
                "payload": self._xingc_payload,
            },
        )

    def _xingc_lock(self) -> None:
        if self._operation_active():
            return
        if not self._require_mode("fastboot"):
            return
        if self._selected_brand() != "vivo" and not MainWindow._test_mode_enabled(self):
            QMessageBox.warning(self, "NEBUMTK", "当前机型不支持 Xingc 回锁")
            return
        context = self._capture_device_action_context()
        if context is None:
            return
        self._confirm_and_start_flow(
            DestructiveFlowRequest(
                "vivo_xingc_lock",
                None,
                ("设备锁状态",),
                "槽位不变；设备锁状态将改变",
                "命令发出前可取消",
                context,
            )
        )

    def _start_vivo_xingc_unlock(self) -> None:
        if self._operation_active():
            return
        if not self._require_mode("fastboot"):
            return
        if self._selected_brand() != "vivo" and not MainWindow._test_mode_enabled(self):
            QMessageBox.warning(self, "NEBUMTK", "当前机型不是 VIVO，禁止执行 Xingc 解锁")
            return
        context = self._capture_device_action_context()
        if context is None:
            return
        self._confirm_and_start_flow(
            DestructiveFlowRequest(
                "vivo_xingc_unlock",
                None,
                ("设备锁状态",),
                "槽位不变；设备锁状态将改变",
                "命令发出前可取消",
                context,
            )
        )

    def _start_dual_slot_operation(self, *, reboot_after: bool) -> None:
        if self._operation_active():
            return
        if not self._require_mode("fastboot"):
            return
        profile = self._get_profile(str(self.model_combo.currentData() or ""))
        if (
            profile is None
            or not ({"xingc", "generic_lk"} & set(profile.capabilities))
        ) and not MainWindow._test_mode_enabled(self):
            QMessageBox.warning(self, "NEBUMTK", "当前 patched LK 双槽同步仅支持 Vivo")
            return
        patched_lk = self._resolve_patched_lk()
        if patched_lk is None:
            return
        if isinstance(patched_lk, str):
            context = self._capture_device_action_context()
            if context is None:
                return
            self._confirm_and_start_flow(
                DestructiveFlowRequest(
                    "dual_slot_sync" if reboot_after else "dual_slot_apply",
                    None,
                    ("设备启动组件",),
                    (
                        "执行完整解锁并重启"
                        if reboot_after
                        else "执行完整解锁步骤；完成后可单独重启"
                    ),
                    "开始设备写入前可取消",
                    context,
                    artifact_ref=patched_lk,
                )
            )
            return
        self._prepare_file_snapshot(
            patched_lk,
            lambda snapshot, context: self._confirm_and_start_flow(
                DestructiveFlowRequest(
                    "dual_slot_sync" if reboot_after else "dual_slot_apply",
                    snapshot,
                    ("lk_a", "lk_b"),
                    (
                        "写入所选镜像并重启"
                        if reboot_after
                        else "写入所选镜像；完成后可单独重启"
                    ),
                    "每次设备写入开始前可取消",
                    context,
                )
            ),
        )

    def _start_dual_slot_sync(self) -> None:
        self._start_dual_slot_operation(reboot_after=True)

    def _start_dual_slot_apply(self) -> None:
        self._start_dual_slot_operation(reboot_after=False)

    def _start_restore_original_lk(self) -> None:
        if self._operation_active() or not self._require_mode("fastboot"):
            return
        source = Path(self.original_lk_edit.text().strip())
        if not source.is_file() or source.stat().st_size <= 0:
            QMessageBox.warning(self, "NEBUMTK", "请选择非空的原机 LK")
            return
        self._prepare_file_snapshot(
            source,
            lambda snapshot, context: self._confirm_and_start_flow(
                DestructiveFlowRequest(
                    "restore_original_lk",
                    snapshot,
                    ("lk_a", "lk_b"),
                    "把所选原机 LK 写回双槽，切回原槽后重启",
                    "每次写入开始前可取消；最后一次写入后不可取消",
                    context,
                )
            ),
        )

    def _start_honor_complete(self) -> None:
        if self._operation_active() or not self._require_mode("fastboot"):
            return
        patched_lk = self._resolve_patched_lk()
        if patched_lk is None:
            return
        context = self._capture_device_action_context()
        if context is None:
            return
        self._confirm_and_start_flow(
            DestructiveFlowRequest(
                "honor_complete",
                None,
                ("设备启动组件", "misc"),
                "完成解锁并返回原槽",
                "设备写入开始前可取消",
                context,
                artifact_ref=str(patched_lk),
            )
        )

    def _on_worker_confirmation_requested(self, message: str) -> None:
        worker = self.worker
        if worker is None or not hasattr(worker, "_confirmation_wait"):
            return
        if self._closing:
            worker._confirmation_result = False
            worker._confirmation_wait.set()
            return
        reply = QMessageBox.question(self, "确认破坏性操作", message)
        worker._confirmation_result = MainWindow._is_yes_reply(reply)
        worker._confirmation_wait.set()

    def cancel_flow(self) -> None:
        if not self._operation_active():
            return
        self.cancel_button.setEnabled(False)
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.step_label.setText("正在取消…")
        elif self.utility_worker and self.utility_worker.isRunning():
            self.utility_worker.cancel()
            self.activity_stage_label.setText("正在取消工具操作…")
        elif self.terminal_worker and self.terminal_worker.isRunning():
            self.terminal_worker.cancel()
            self.activity_stage_label.setText("正在取消诊断命令…")
        elif self.reboot_worker and self.reboot_worker.isRunning():
            self.reboot_worker.cancel()
            self.activity_stage_label.setText("正在停止等待…")

        elif self.generic_worker and self.generic_worker.isRunning():
            self.generic_worker.cancel()
            self.activity_panel.apply_progress(
                ProgressSnapshot.waiting("Stopping partition operation…", cancellable=False)
            )
        elif self.dyn_worker and self.dyn_worker.isRunning():
            self.dyn_worker.cancel()
            self.activity_panel.apply_progress(
                ProgressSnapshot.waiting("Stopping Vivo dyn operation…", cancellable=False)
            )
        elif self.sakisu_worker and self.sakisu_worker.isRunning():
            self.sakisu_worker.cancel()
            self.activity_panel.apply_progress(
                ProgressSnapshot.waiting("Stopping SakiSU patch…", cancellable=False)
            )
        elif self.su_probe_worker and self.su_probe_worker.isRunning():
            self.su_probe_worker.cancel()
        elif self.file_snapshot_worker and self.file_snapshot_worker.isRunning():
            self.file_snapshot_worker.cancel()
            self.activity_panel.apply_progress(
                ProgressSnapshot.waiting("Stopping local file verification…", cancellable=False)
            )

    def _start_flow(
        self,
        flow_kind: str,
        *,
        patched_lk: Path | VerifiedFileSnapshot | str | None = None,
        source_lk: Path | VerifiedFileSnapshot | None = None,
        preload_override: Path | str | None = None,
        confirm: bool = False,
        confirmed_active_slot: str = "",
        backup_directory: Path | None = None,
        local_lk_profile: str = "",
        install_sakisu_root: bool = False,
        sakisu_init_boot: Path | None = None,
        sakisu_kmi: str = "",
    ) -> None:
        if self._operation_active():
            return
        from ..flows.factory import FASTBOOT_FLOWS, LOCAL_FLOWS

        is_local = flow_kind in LOCAL_FLOWS
        target_serial = "" if is_local else self._current_serial
        if not is_local and not target_serial:
            QMessageBox.warning(self, "NEBUMTK", "未检测到设备")
            return
        if confirm:
            raise VerificationError("generic flow confirmation is prohibited")

        if flow_kind == "vivo_complete_unlock":
            need_adb = True
            need_fastboot = True
        else:
            need_adb = flow_kind not in FASTBOOT_FLOWS and not is_local
            need_fastboot = flow_kind in FASTBOOT_FLOWS
        if not is_local:
            try:
                EnvChecker(self.settings).validate(need_adb=need_adb, need_fastboot=need_fastboot)
            except EnvError as exc:
                QMessageBox.critical(self, "NEBUMTK", str(exc))
                return

        model_key = self.model_combo.currentData() or ""
        if not model_key and not is_local:
            QMessageBox.warning(self, "NEBUMTK", "请先选择机型")
            return
        skip_profile_identity = (
            MainWindow._test_mode_enabled(self) if not is_local else False
        )
        runtime_model_key = (
            str(self.runtime_combo.currentData() or "")
            if skip_profile_identity
            else (str(model_key) if not is_local else "")
        )
        if skip_profile_identity and not runtime_model_key:
            QMessageBox.warning(self, "NEBUMTK", "测试模式需要选择运行配置")
            return
        if (
            skip_profile_identity
            and not is_local
            and not MainWindow._confirm_test_mode_operation(self, flow_kind)
        ):
            return
        # 完整诊断仅写入日志文件；右侧仅显示简短活动阶段。
        session_log = SessionLog(self.workspace.logs_dir(), lambda _message: None)
        if session_log.path is not None and hasattr(self, "log_file_label"):
            self.log_file_label.setText("日志: 已启用")
        if skip_profile_identity:
            session_log("测试模式：跳过机型身份匹配")

        flow_label = FLOW_DISPLAY_NAMES.get(flow_kind, "设备操作")
        if flow_kind in {"dual_slot_sync", "dual_slot_apply"}:
            profile = self._get_profile(str(self.model_combo.currentData() or ""))
            flow_label = (
                "完整解锁 BL"
                if profile is not None
                and profile.has_capability("generic_lk")
                and not profile.has_capability("xingc")
                else "Fastboot 双槽写入"
            )
        self._active_flow_label = flow_label
        self._flow_session_log = session_log
        session_log(f"{flow_label} 开始")
        self._flow_active = True
        self._flow_cancelled = False
        self._append_activity(f"{flow_label} 开始", key=("flow-start", flow_label))
        self._set_device_presentation(
            "busy",
            serial=target_serial,
            mode=self._current_mode,
            detail="流程执行中，设备扫描已暂停",
        )
        self._pause_device_scanning()
        self._set_busy(True)
        self.worker = FlowWorker(
            self.project_root,
            self.workspace_root,
            self.settings,
            target_serial,
            model_key,
            flow_kind,
            patched_lk=patched_lk,
            source_lk=source_lk,
            preload_override=preload_override,
            backup_directory=backup_directory,
            skip_profile_identity=skip_profile_identity,
            runtime_model_key=runtime_model_key,
            confirmed_active_slot=confirmed_active_slot,
            local_lk_profile=local_lk_profile,
            install_sakisu_root=install_sakisu_root,
            sakisu_init_boot=sakisu_init_boot,
            sakisu_kmi=sakisu_kmi,
            parent=self,
        )
        self.worker.progress_changed.connect(self._on_progress)
        self.worker.activity_event.connect(self._on_activity_event)
        if hasattr(self.worker, "log_message"):
            self.worker.log_message.connect(self._on_worker_log)
        if hasattr(self.worker, "confirmation_requested"):
            self.worker.confirmation_requested.connect(self._on_worker_confirmation_requested)
        self.worker.finished_ok.connect(self._on_success)
        self.worker.finished_error.connect(self._on_error)
        self.worker.cancelled_signal.connect(self._on_cancelled)
        self.worker.finished.connect(self._on_flow_finished)
        self.worker.start()

    def _set_busy(self, busy: bool) -> None:
        for button in MainWindow._all_flow_buttons(self):
            button.setEnabled(not busy)
        self.model_combo.setEnabled(not busy)
        runtime_combo = getattr(self, "runtime_combo", None)
        if runtime_combo is not None:
            runtime_combo.setEnabled(
                not busy and MainWindow._test_mode_enabled(self)
            )
        self.preload_edit.setEnabled(not busy)
        self.patched_lk_edit.setEnabled(not busy)
        self.fastboot_patched_lk_edit.setEnabled(not busy)
        self.original_lk_edit.setEnabled(not busy)
        self.source_lk_edit.setEnabled(not busy)
        self.output_lk_edit.setEnabled(not busy)
        self.local_lk_profile_combo.setEnabled(not busy)
        self.advanced_page.su_provider_combo.setEnabled(not busy)
        self.advanced_page.custom_su_edit.setEnabled(not busy)
        self.cmd_edit.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        if not busy:
            self._refresh_action_guidance()

    def _set_indeterminate_progress(self, label: str) -> None:
        for bar in (self.progress_bar, self.activity_progress):
            bar.setRange(0, 0)
        self.step_label.setText(label)
        self.activity_panel.apply_progress(
            ProgressSnapshot.waiting(
                label,
                cancellable=self._operation_active(),
            )
        )

    def _reset_progress(self) -> None:
        for bar in (self.progress_bar, self.activity_progress):
            bar.setRange(0, 100)
            bar.setValue(0)
        self.step_label.setText("Ready")
        self.activity_panel.apply_progress(ProgressSnapshot.idle())
        self._last_progress_value = None

    def _on_progress(self, value: int, label: str) -> None:
        public_label = self._active_flow_label or "设备操作"
        snapshot = ProgressSnapshot.determinate(
            value,
            public_label,
            cancellable=self._operation_active(),
        )
        for bar in (self.progress_bar, self.activity_progress):
            bar.setRange(0, 100)
            bar.setValue(value)
        self.step_label.setText(public_label)
        self.activity_panel.apply_progress(snapshot)
        self._last_progress_value = value

    def _safe_activity_text(self, event: ActivityEvent) -> str:
        title = str(event.title or "").strip()
        detail = str(event.detail or "").strip()
        for sensitive in (
            event.serial,
            self._current_serial,
            self._context_serial,
            self._utility_serial,
        ):
            value = str(sensitive or "").strip()
            if value:
                title = title.replace(value, "<设备>")
                detail = detail.replace(value, "<设备>")
        title = compact_log_message(title)
        detail = compact_log_message(detail)
        if detail and detail != title:
            return compact_log_message(f"{title} · {detail}")
        return title or detail or "设备操作"

    def _on_activity_event(self, event: object) -> None:
        if not isinstance(event, ActivityEvent):
            return
        if self._active_flow_label:
            self.activity_stage_label.setText(self._active_flow_label)
            return
        text = self._safe_activity_text(event)
        self.activity_stage_label.setText(text)
        self._append_activity(
            text,
            key=("activity", str(event.kind), text),
            error=event.kind in {ActivityKind.ERROR, ActivityKind.TIMEOUT},
        )

    def _on_cancelled(self) -> None:
        self._flow_cancelled = True
        self._reset_progress()
        self.step_label.setText("已取消")
        self.activity_stage_label.setText("已取消")
        self._append_activity("流程已取消", key=("flow-cancelled",))
        if self._flow_session_log is not None:
            self._flow_session_log(f"{self._active_flow_label or '设备操作'} 取消")

    def _on_error(self, message: str) -> None:
        if self._flow_cancelled:
            return
        self._reset_progress()
        self.step_label.setText("失败")
        self.activity_stage_label.setText("失败")
        short = _safe_diagnostic_error_message(message)
        self._append_activity("流程失败", key=("flow-error", short), error=True)
        if self._flow_session_log is not None:
            self._flow_session_log(f"{self._active_flow_label or '设备操作'} 失败")
        QMessageBox.critical(self, "NEBUMTK", short)

    def _on_success(self) -> None:
        if self.worker is not None and self.worker.flow_kind == "patch_lk":
            output = self.worker.patched_lk
            if output is not None and output.is_file():
                self._set_selected_patched_lk(output)
                self._set_image_status("patched LK 已生成并选中", "ready")
        self._reset_progress()
        self.step_label.setText("完成")
        self.activity_stage_label.setText("完成")
        label = self._active_flow_label or "设备操作"
        self._append_activity(f"{label} 完成", key=("flow-success", label))
        if self._flow_session_log is not None:
            self._flow_session_log(f"{label} 成功")

    def _on_flow_finished(self) -> None:
        finished_worker = self.sender()
        if finished_worker is not self.worker:
            return
        self._flow_active = False
        self._active_flow_label = ""
        self._flow_session_log = None
        finished_worker.deleteLater()
        self.worker = None
        self._reset_progress()
        self._set_busy(False)
        self._set_device_presentation(
            "scanning",
            serial=self._current_serial,
            mode=self._current_mode,
            detail="正在重新确认设备状态",
        )
        if self._closing:
            self._resume_pending_close()
            return
        self._resume_device_scanning()

    def _append_activity(
        self,
        message: str,
        *,
        key: tuple[object, ...],
        error: bool = False,
    ) -> None:
        if key == self._last_activity_key:
            return
        self._last_activity_key = key
        self._append_log(message, error=error)

    def _on_worker_log(self, message: str, *, session_log: "SessionLog | None" = None) -> None:
        """Route a worker's detailed log line to the UI and the redacted disk sink.

        The UI view compacts through ``_append_log``; the persistent sink keeps a
        redacted, bounded diagnostic trail (operation stage, error class, failure
        layer, stable code) without command/payload/token/path content.
        """

        self._append_log(message)
        sink = session_log if session_log is not None else self._flow_session_log
        if sink is not None:
            try:
                sink(self._redact_persistent(message, self._current_serial))
            except Exception:
                pass

    def _append_log(self, message: str, *, error: bool = False) -> None:
        text = compact_log_message(message)
        if not text:
            return
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if error:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor("#c0392b"))
            cursor.insertText(text + "\n", fmt)
        else:
            cursor.insertText(text + "\n")
        self.log_view.setTextCursor(cursor)
        bar = self.log_view.verticalScrollBar()
        if bar is not None:
            bar.setValue(bar.maximum())

    def _operation_workers(self) -> tuple[object, ...]:
        return tuple(
            worker
            for worker in (
                self.worker,
                self.utility_worker,
                self.terminal_worker,
                self.reboot_worker,
                self.generic_worker,
                self.dyn_worker,
                self.sakisu_worker,
                self.su_probe_worker,
                self.file_snapshot_worker,
            )
            if worker is not None and worker.isRunning()
        )

    def _resume_pending_close(self) -> None:
        scan_running = bool(self.scan_worker is not None and self.scan_worker.isRunning())
        if self._closing and not self._operation_workers() and not scan_running:
            QTimer.singleShot(0, self.close)

    def nativeEvent(self, event_type, message):  # type: ignore[no-untyped-def]  # noqa: N802
        hit = native_windows_resize_hit(self, message)
        if hit is not None:
            return True, hit
        return super().nativeEvent(event_type, message)

    def closeEvent(self, event) -> None:
        active_workers = self._operation_workers()
        if active_workers:
            reply = QMessageBox.question(
                self, "NEBUMTK",
                "操作运行中，确认退出？（将先安全取消，操作结束后再关闭）",
            )
            if reply != QMessageBox.Yes:
                self._closing = False
                event.ignore()
                return
        self._closing = True
        self.device_scan_timer.stop()
        self._refresh_pending = False
        if active_workers:
            for worker in active_workers:
                if (
                    worker is self.utility_worker
                    and worker.operation.startswith("xingc_")
                ):
                    self._utility_clear_xingc_on_finish = True
                worker.cancel()
            event.ignore()
            return
        self._clear_xingc_context()
        if self.scan_worker and self.scan_worker.isRunning():
            event.ignore()
            return
        super().closeEvent(event)
# provenance: NBMTK-canary-7f3a91c4e8b2d05f
