# SPDX-FileCopyrightText: 2026 雾月星辰 & MLXC <github@XingChenRS>
# SPDX-License-Identifier: AGPL-3.0-only
"""Capability-driven temporary root and unlock tasks."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..theme import UiRole, apply_role
from ..widgets import TaskSection, action_button, path_row
from .common import page_layout


# What 「准备解锁」 actually does, per brand. It writes the operator's selected
# patched LK into the currently inactive slot together with that brand's misc
# image and reboots into fastboot. It never issues `fastboot flashing unlock` —
# the unlock command lives on the row below it.
PREPARE_UNLOCK_NOTES = {
    "vivo": (
        "把所选 patched LK 写入对槽 lk，并写入本机 misc（misc 让设备下次启动到对槽），"
        "然后重启进入 fastboot。所选 LK 必须是本机对应、已打好补丁的那一份；"
        "这一步只写对槽，双槽同步发生在完整解锁收尾。"
    ),
    "honor": (
        "把所选 patched LK 写入对槽并写入 misc（misc 让设备下次启动到对槽），"
        "然后重启进入 fastboot。Honor 靠设备启动到对槽的 patched LK 完成解锁；"
        "之后再执行「完整解锁 BL」，把双槽同步为 patched 并切回原槽。"
    ),
    "oppo": (
        "把所选 patched LK 写入对槽并写入 misc（misc 让设备下次启动到对槽），"
        "然后重启进入 fastboot，为下一步解锁做准备。"
        "解锁本身由下面的「完整解锁 BL」发出 flashing unlock；"
        "解锁完成后必须用「还原双槽原 LK」把原机 LK 写回双槽。"
    ),
    "": (
        "把所选 patched LK 写入对槽，并写入当前机型对应的 misc"
        "（misc 让设备下次启动到对槽），然后重启进入 fastboot。"
    ),
}


def _action_row(
    title: str,
    context: str,
    state: str,
    button: QPushButton,
    note: str = "",
) -> QWidget:
    """One independently runnable action, plus the line that explains it.

    The note is built into the row's own frame on purpose.  A note appended to
    the section separately keeps rendering after its row is hidden, so it
    silently reattaches to whichever row is still on screen and describes the
    wrong action.
    """

    row = QFrame()
    row.setObjectName("manualAction")
    outer = QVBoxLayout(row)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(2)

    body = QHBoxLayout()
    body.setContentsMargins(0, 8, 0, 8)
    body.setSpacing(10)
    copy = QVBoxLayout()
    copy.setSpacing(1)
    title_label = QLabel(title)
    title_label.setObjectName("actionTitle")
    context_label = QLabel(context)
    context_label.setObjectName("actionContext")
    copy.addWidget(title_label)
    copy.addWidget(context_label)
    state_label = QLabel(state)
    state_label.setObjectName("actionState")
    body.addLayout(copy, 1)
    body.addWidget(state_label)
    body.addWidget(button)
    outer.addLayout(body)

    note_label = None
    if note:
        note_label = QLabel(note)
        note_label.setObjectName("mutedText")
        note_label.setWordWrap(True)
        outer.addWidget(note_label)

    row.title_label = title_label
    row.context_label = context_label
    row.state_label = state_label
    row.note_label = note_label
    return row


class RootUnlockPage(QWidget):
    """Expose shortcuts and every independently retryable applicable stage."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        page, layout = page_layout("Root 与解锁", "只显示当前设备匹配的功能")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(page)

        self.capability_reason = QLabel("当前设备没有可用的 Root 或解锁操作。")
        self.capability_reason.setObjectName("inlineStatus")
        self.capability_reason.setProperty("state", "warning")
        layout.addWidget(self.capability_reason)

        self.empty_state = TaskSection("当前设备暂无可用操作")
        self.empty_state_status = QLabel("重新扫描设备后，可用操作会自动显示。")
        self.empty_state_status.setObjectName("inlineStatus")
        self.empty_state.content_layout.addWidget(self.empty_state_status)
        layout.addWidget(self.empty_state)

        self.recommended_card = QFrame()
        self.recommended_card.setObjectName("recommendedCard")
        recommended = QVBoxLayout(self.recommended_card)
        self.recommended_layout = recommended
        recommended.setContentsMargins(15, 13, 15, 14)
        recommended.setSpacing(10)
        recommended_copy = QVBoxLayout()
        recommended_copy.setSpacing(2)
        self.recommended_badge = QLabel("当前设备可用")
        self.recommended_badge.setObjectName("eyebrow")
        self.recommended_title = QLabel("Vivo 完整解锁")
        self.recommended_title.setObjectName("recommendedTitle")
        self.recommended_summary = QLabel("执行适配当前设备的一键流程")
        self.recommended_summary.setObjectName("sectionDescription")
        recommended_copy.addWidget(self.recommended_badge)
        recommended_copy.addWidget(self.recommended_title)
        recommended_copy.addWidget(self.recommended_summary)
        recommended.addLayout(recommended_copy)

        impact_row = QHBoxLayout()
        impact_row.setSpacing(10)
        self.recommended_impact = QLabel("会清除设备数据")
        self.recommended_impact.setObjectName("impactSummary")
        self.recommended_reboot = QLabel("完成后自动重启")
        self.recommended_reboot.setObjectName("impactSummary")
        impact_row.addWidget(self.recommended_impact)
        impact_row.addWidget(self.recommended_reboot)
        recommended.addLayout(impact_row)

        self.auto_flow_button = action_button(
            "开始 Vivo 完整解锁", "vivoRecipeAction", role=UiRole.DANGER
        )
        self.honor_auto_button = action_button(
            "开始 Honor 完整解锁", "honorRecipeAction", role=UiRole.DANGER
        )
        for button in (
            self.auto_flow_button,
            self.honor_auto_button,
        ):
            recommended.addWidget(button)
        layout.addWidget(self.recommended_card)
        self.recommended_card.setVisible(False)

        self.privilege_section = TaskSection(
            "临时 Root", "仅用于本次连接，不安装 Root 管理器"
        )
        privilege_row = QHBoxLayout()
        privilege_row.setContentsMargins(0, 0, 0, 0)
        self.privilege_status = QLabel("等待当前设备能力确认。")
        self.privilege_status.setObjectName("actionState")
        self.root_button = action_button(
            "获取临时权限", "authorizedPrivilegeAction", role=UiRole.PRIMARY
        )
        privilege_row.addWidget(self.privilege_status, 1)
        privilege_row.addWidget(self.root_button)
        self.privilege_section.content_layout.addLayout(privilege_row)
        layout.addWidget(self.privilege_section)

        self.lk_tools_section = TaskSection(
            "LK 文件",
            "「准备解锁」和双槽同步都从这里选 LK；选择本身不连接设备",
        )
        self.patched_lk_edit = QLineEdit()
        self.patched_lk_edit.setReadOnly(True)
        self.patched_lk_edit.setPlaceholderText("选择 patched LK")
        self.patched_lk_browse = QPushButton("选择 patched LK")
        self.fastboot_patched_lk_edit = self.patched_lk_edit
        self.fastboot_patched_lk_browse = self.patched_lk_browse
        self.patched_lk_row = path_row(self.patched_lk_edit, self.patched_lk_browse)
        self.lk_tools_section.content_layout.addWidget(self.patched_lk_row)

        self.dual_slot_sync_button = action_button(
            "同步双槽", "syncPatchedLkAction", role=UiRole.SECONDARY
        )
        self.dual_action_row = _action_row(
            "同步 patched LK",
            "Fastboot",
            "选择文件后可执行",
            self.dual_slot_sync_button,
            "用于解锁后必须保留 patched LK 的机型（Vivo / Honor）："
            "把所选 LK 同时写入两个槽，切回原槽后重启。"
            "请确认所选 LK 是本次设备对应、已打好补丁的那一份。",
        )
        self.dual_action_title = self.dual_action_row.title_label
        self.lk_tools_section.content_layout.addWidget(self.dual_action_row)

        self.original_lk_edit = QLineEdit()
        self.original_lk_edit.setReadOnly(True)
        self.original_lk_edit.setPlaceholderText("选择原机 LK")
        self.original_lk_browse = QPushButton("选择原机 LK")
        self.original_lk_row = path_row(self.original_lk_edit, self.original_lk_browse)
        self.lk_tools_section.content_layout.addWidget(self.original_lk_row)
        self.original_lk_sync_button = action_button(
            "写入双槽", "restoreOriginalLkAction", role=UiRole.SECONDARY
        )
        self.original_dual_action_row = _action_row(
            "还原双槽原 LK",
            "Fastboot",
            "选择文件后可执行",
            self.original_lk_sync_button,
            "用于解锁后需要还原本机 LK 的机型（例如 OPPO）："
            "把所选 LK 同时写入两个槽，切回原槽后重启。"
            "请确认所选 LK 是本机原厂、未打过补丁的那一份。",
        )
        self.lk_tools_section.content_layout.addWidget(self.original_dual_action_row)
        self.manual_section = TaskSection(
            "分步操作", "每项独立执行，失败后可以单独重试"
        )
        self.manual_section.setObjectName("manualSection")
        self.recipe_section = self.manual_section

        self.backup_button = action_button(
            "执行", "authorizedBackupAction", role=UiRole.SECONDARY
        )
        self.backup_action_row = _action_row(
            "保存恢复点", "本机备份", "可执行", self.backup_button
        )
        self.manual_section.content_layout.addWidget(self.backup_action_row)

        self.write_button = action_button(
            "执行", "specializedLkWriteAction", role=UiRole.SECONDARY
        )
        self.write_action_row = _action_row(
            "准备解锁",
            "ADB",
            "可执行",
            self.write_button,
            PREPARE_UNLOCK_NOTES[""],
        )
        self.write_action_title = self.write_action_row.title_label
        self.write_action_context = self.write_action_row.context_label
        self.write_action_note = self.write_action_row.note_label
        self.manual_section.content_layout.addWidget(self.write_action_row)

        self.oppo_unlock_button = action_button(
            "执行", "oppoUnlockAction", role=UiRole.DANGER
        )
        self.oppo_action_row = _action_row(
            "完整解锁 BL",
            "独立操作",
            "可执行",
            self.oppo_unlock_button,
            "只发出 fastboot flashing unlock；解锁完成后如需把原机 LK 写回双槽，"
            "用上面的「还原双槽原 LK」。",
        )
        self.manual_section.content_layout.addWidget(self.oppo_action_row)

        self.sakisu_root_checkbox = QCheckBox(
            "完整解锁收尾时写入原槽 init_boot（可选，默认不写）"
        )
        self.sakisu_root_checkbox.setToolTip(
            "只影响 Vivo 完整解锁的收尾：把「镜像与恢复」页修补好的 init_boot 写入原槽。"
            "不写对槽；不安装或管理 Manager。若镜像页没有已修补的产物，"
            "请在镜像页选定 KMI，让本次流程用备份出来的原槽 init_boot 现打。"
        )
        self.sakisu_root_checkbox.setVisible(False)
        self.manual_section.content_layout.addWidget(self.sakisu_root_checkbox)

        self.honor_unlock_button = action_button(
            "完整解锁 BL", "honorUnlockAction", role=UiRole.DANGER
        )
        self.honor_action_row = _action_row(
            "完整解锁 BL", "独立操作", "可执行", self.honor_unlock_button
        )
        self.manual_section.content_layout.addWidget(self.honor_action_row)

        self.xingc_heading = QLabel("Xingc")
        self.xingc_heading.setObjectName("manualGroupHeading")
        self.manual_section.content_layout.addWidget(self.xingc_heading)
        self.vivo_xingc_button = action_button(
            "Xingc 授权 / 解锁", "authorizedXingcAction", role=UiRole.PRIMARY
        )
        self.xingc_unlock_row = _action_row(
            "Xingc 授权 / 解锁", "独立操作", "可执行", self.vivo_xingc_button
        )
        self.manual_section.content_layout.addWidget(self.xingc_unlock_row)
        self.xingc_lock_button = action_button(
            "回锁 (relock)", "authorizedXingcLockAction", role=UiRole.DANGER
        )
        self.xingc_lock_row = _action_row(
            "回锁 (relock)", "独立操作", "可执行", self.xingc_lock_button
        )
        self.manual_section.content_layout.addWidget(self.xingc_lock_row)
        self.xingc_status = QLabel("等待同一设备上下文。")
        self.xingc_status.setObjectName("inlineStatus")
        self.manual_section.content_layout.addWidget(self.xingc_status)

        self.reboot_action_button = action_button(
            "执行", "unlockRebootAction", role=UiRole.SECONDARY
        )
        self.reboot_action_row = _action_row(
            "重启设备", "独立操作", "可执行", self.reboot_action_button
        )
        self.manual_section.content_layout.addWidget(self.reboot_action_row)
        layout.addWidget(self.manual_section)
        layout.addWidget(self.lk_tools_section)

        # Compatibility controls remain available to orchestration without
        # appearing in the commercial surface.
        self.preload_edit = QLineEdit()
        self.preload_browse = QPushButton("受保护资源")
        self.xingc_token_button = QPushButton("读取")
        self.xingc_sign_button = QPushButton("授权")
        self.xingc_auth_button = QPushButton("提交")
        self.manual_xingc_panel = QWidget()
        self.vivo_preflight_label = QLabel("等待当前设备和能力检查。")
        self.vivo_preflight_label.setObjectName("inlineStatus")
        self.honor_unavailable = QLabel("")
        self.xingc_section = self.manual_section
        for widget in (
            self.preload_edit,
            self.preload_browse,
            self.xingc_token_button,
            self.xingc_sign_button,
            self.xingc_auth_button,
            self.manual_xingc_panel,
            self.vivo_preflight_label,
            self.honor_unavailable,
        ):
            widget.setVisible(False)
            layout.addWidget(widget)
        layout.addStretch(1)
        self.apply_capabilities(frozenset())

    def apply_capabilities(
        self,
        capabilities: frozenset[str],
        *,
        test_mode: bool = False,
        mode: str | None = None,
        brand: str = "",
    ) -> None:
        preload = "preload" in capabilities
        xingc = "xingc" in capabilities
        generic_lk = "generic_lk" in capabilities
        flashing_unlock = "flashing_unlock" in capabilities
        has_any = preload or xingc or generic_lk or flashing_unlock
        is_vivo = xingc
        is_honor = generic_lk and not xingc
        is_oppo = flashing_unlock and not xingc and not generic_lk

        # Test mode relaxes human-imposed brand-guess gating so operators can
        # reach protected/Xingc/LK flows even without an auto-matched profile.
        # Real device boundaries are enforced elsewhere. When test_mode is
        # False the behavior is identical to the capability-driven defaults.
        effective_any = has_any or test_mode
        forward_actions = is_vivo or is_honor or is_oppo or test_mode
        xingc_visible = is_vivo or test_mode
        xingc_enabled = xingc or test_mode
        normalized_mode = str(mode or "").strip().casefold()
        adb_context = not normalized_mode or normalized_mode == "adb"
        fastboot_context = not normalized_mode or normalized_mode == "fastboot"

        self.empty_state.setVisible(not effective_any)
        self.capability_reason.setVisible(not effective_any)
        if test_mode:
            self.recommended_title.setText("手动流程（测试模式）")
            self.recommended_summary.setText("测试模式下已放开手动与 Xingc 操作")
            self.recommended_impact.setVisible(False)
            self.recommended_reboot.setText("按设备提示完成")
            self.recommended_card.setVisible(True)
        else:
            self.recommended_card.setVisible(False)
        # The pickers themselves are never gated on the transport: 「准备解锁」
        # is an ADB action whose input is a patched LK, so hiding this section in
        # ADB left that action impossible to satisfy — and for a brand with no
        # LK patch of its own there was then no way to supply one at all.
        self.lk_tools_section.setVisible(effective_any)
        self.privilege_section.setVisible(False)
        self.root_button.setEnabled(False)
        self.manual_section.setVisible(effective_any)
        self.backup_action_row.setVisible(adb_context)

        self.auto_flow_button.setVisible(is_vivo and adb_context)
        self.honor_auto_button.setVisible(is_honor and adb_context)

        if is_vivo:
            self.recommended_title.setText("Vivo 完整解锁")
            self.recommended_summary.setText("执行适配当前设备的一键流程")
            self.recommended_impact.setText("会清除设备数据")
            self.recommended_impact.setVisible(True)
            self.recommended_reboot.setText("完成后自动重启")
        elif is_oppo:
            self.recommended_title.setText("OPPO 完整解锁 BL")
            self.recommended_summary.setText("也可以在下方逐步执行并单独重试")
            self.recommended_impact.setText("会清除设备数据")
            self.recommended_impact.setVisible(True)
            self.recommended_reboot.setText("按设备提示完成")
        elif is_honor:
            self.recommended_title.setText("Honor 完整解锁 BL")
            self.recommended_summary.setText("也可以在下方逐步执行并单独重试")
            self.recommended_impact.setVisible(False)
            self.recommended_reboot.setText("完成后自动重启")

        self.patched_lk_row.setVisible(True)
        self.original_lk_row.setVisible(True)
        # Dual-slot sync is fastboot-only, and exactly one direction applies per
        # brand.  Never offer the other one merely greyed out: a button that can
        # never be enabled only invites the operator to work out why.
        self.dual_action_row.setVisible(fastboot_context and (xingc or generic_lk))
        self.original_dual_action_row.setVisible(fastboot_context and flashing_unlock)
        self.write_action_row.setVisible(forward_actions and adb_context)
        self.write_action_context.setText("ADB")
        # The prepare-unlock row writes the inactive slot for every brand that
        # has an LK write path, but what happens next differs per brand, so the
        # line under it is chosen by the device rather than shared.
        self.write_action_note.setText(
            PREPARE_UNLOCK_NOTES.get(
                str(brand).strip().casefold(), PREPARE_UNLOCK_NOTES[""]
            )
        )
        self.oppo_action_row.setVisible(is_oppo and fastboot_context)
        self.sakisu_root_checkbox.setVisible(False)
        self.sakisu_root_checkbox.setEnabled(False)
        self.sakisu_root_checkbox.setChecked(False)
        self.honor_action_row.setVisible(is_honor and fastboot_context)
        self.xingc_heading.setVisible(xingc_visible and fastboot_context)
        self.xingc_unlock_row.setVisible(xingc_visible and fastboot_context)
        self.xingc_lock_row.setVisible(xingc_visible and fastboot_context)
        self.xingc_status.setVisible(xingc_visible and fastboot_context)
        self.reboot_action_row.setVisible(forward_actions and bool(normalized_mode))

        self.write_button.setVisible(forward_actions and adb_context)
        self.dual_slot_sync_button.setText("同步双槽")
        apply_role(self.dual_slot_sync_button, UiRole.SECONDARY)
        self.oppo_unlock_button.setText("完整解锁 BL" if is_oppo else "执行")
        for button in (
            self.vivo_xingc_button,
            self.xingc_lock_button,
            self.xingc_token_button,
            self.xingc_sign_button,
            self.xingc_auth_button,
        ):
            button.setEnabled(xingc_enabled)
# provenance: NBMTK-canary-7f3a91c4e8b2d05f
